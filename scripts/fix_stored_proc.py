"""
fix_stored_proc.py
------------------
Solo actualiza recalculate_forecast_period en la DB.
Cambia el cálculo de SAH: de NOT EXISTS (se rompe con ausencias de rango-periodo)
a gross_sah(con clamp roll_on) - absence_hours.
"""
import asyncio, sys
sys.path.insert(0, ".")
import asyncpg
from app.config import settings

NEW_PROC = r"""
CREATE OR REPLACE FUNCTION public.recalculate_forecast_period(
  p_eid character varying,
  p_period_name character varying
)
RETURNS void
LANGUAGE plpgsql
AS $$
DECLARE
  v_roll_on        DATE;
  v_roll_off       DATE;
  v_country        VARCHAR;
  v_period_start   DATE;
  v_period_end     DATE;
  v_eff_start      DATE;
  v_sah            NUMERIC;
  v_gross_sah      NUMERIC;
  v_chg            NUMERIC;
  v_chg_hl         NUMERIC := 0;
  v_chg_sl         NUMERIC := 0;
  v_gross_hl       NUMERIC := 0;
  v_gross_sl       NUMERIC := 0;
  v_ppa_adj        NUMERIC := 0;
  v_absence_hours  NUMERIC := 0;
  v_block_count    INTEGER := 0;
  v_total_gross    NUMERIC;
BEGIN
  SELECT fu.roll_on, fu.roll_off, COALESCE(e.country, e.location)
  INTO v_roll_on, v_roll_off, v_country
  FROM forecast_update fu
  JOIN employees e ON fu.eid = e.eid
  WHERE fu.eid = p_eid
  ORDER BY fu.updated_at DESC NULLS LAST LIMIT 1;

  IF v_country IS NULL THEN
    SELECT COALESCE(e.country, e.location) INTO v_country
    FROM employees e WHERE e.eid = p_eid;
  END IF;

  v_country := CASE UPPER(TRIM(v_country))
    WHEN 'AR'         THEN 'Argentina'
    WHEN 'ARGENTINA'  THEN 'Argentina'
    WHEN 'MX'         THEN 'Mexico'
    WHEN 'MEXICO'     THEN 'Mexico'
    WHEN 'CR'         THEN 'Costa Rica'
    WHEN 'COSTA RICA' THEN 'Costa Rica'
    ELSE v_country
  END;

  SELECT start_date, end_date INTO v_period_start, v_period_end
  FROM periods WHERE period_name = p_period_name;

  -- SAH: clamp only by roll_on (new joiners), NOT by roll_off
  -- (roll_off = end of project, employee is still available on bench)
  v_eff_start := GREATEST(COALESCE(v_roll_on, v_period_start), v_period_start);

  -- Absence hours (computed first, used for both SAH and CHG deduction)
  SELECT COALESCE(SUM(COALESCE(a.hours, 0)), 0)
  INTO v_absence_hours
  FROM absences a
  WHERE a.eid = p_eid
    AND a.start_date <= v_period_end
    AND a.end_date   >= v_period_start;

  -- Gross SAH = working hours from eff_start to period_end
  SELECT COALESCE(SUM(CASE WHEN c.is_working_day THEN 8 ELSE 0 END), 0)
  INTO v_gross_sah
  FROM calendar c
  WHERE c.country = v_country
    AND c.date BETWEEN v_eff_start AND v_period_end;

  -- Net SAH = gross minus absence hours (floor at 0)
  v_sah := GREATEST(v_gross_sah - v_absence_hours, 0);

  -- CHG: from chargeability_blocks (gross hours in block window, then prorate absences)
  SELECT COUNT(*) INTO v_block_count
  FROM chargeability_blocks
  WHERE eid = p_eid AND start_date <= v_period_end AND end_date >= v_period_start;

  IF v_block_count > 0 THEN
    SELECT
      COALESCE(SUM(CASE WHEN sub.scenario_type = 'effective'  THEN blk_chg ELSE 0 END), 0),
      COALESCE(SUM(CASE WHEN sub.scenario_type = 'assumption' THEN blk_chg ELSE 0 END), 0)
    INTO v_gross_hl, v_gross_sl
    FROM (
      SELECT cb.scenario_type,
        (SELECT COALESCE(SUM(CASE WHEN c.is_working_day THEN 8 ELSE 0 END), 0)
         FROM calendar c
         WHERE c.country = v_country
           AND c.date BETWEEN GREATEST(cb.start_date, v_eff_start) AND LEAST(cb.end_date, v_period_end)
        ) * cb.chargeability_pct / 100.0 AS blk_chg
      FROM chargeability_blocks cb
      WHERE cb.eid = p_eid
        AND cb.start_date <= v_period_end
        AND cb.end_date   >= v_period_start
    ) sub;

    v_total_gross := COALESCE(v_gross_hl, 0) + COALESCE(v_gross_sl, 0);
    IF v_total_gross > 0 THEN
      v_chg_hl := GREATEST(v_gross_hl - v_absence_hours * v_gross_hl / v_total_gross, 0);
      v_chg_sl := GREATEST(v_gross_sl - v_absence_hours * v_gross_sl / v_total_gross, 0);
    ELSE
      v_chg_hl := 0;
      v_chg_sl := 0;
    END IF;
  ELSE
    IF v_roll_on IS NOT NULL AND v_roll_off IS NOT NULL THEN
      SELECT COALESCE(SUM(CASE WHEN c.is_working_day THEN 8 ELSE 0 END), 0)
      INTO v_chg_hl
      FROM calendar c
      WHERE c.country = v_country
        AND c.date BETWEEN GREATEST(v_roll_on, v_period_start) AND LEAST(v_roll_off, v_period_end);
      v_chg_hl := GREATEST(v_chg_hl - v_absence_hours, 0);
    ELSE
      v_chg_hl := 0;
    END IF;
    v_chg_sl := 0;
  END IF;

  SELECT COALESCE(SUM(
    CASE WHEN to_period   = p_period_name THEN  hours
         WHEN from_period = p_period_name THEN -hours
         ELSE 0 END
  ), 0)
  INTO v_ppa_adj
  FROM ppa_log
  WHERE eid = p_eid
    AND (to_period = p_period_name OR from_period = p_period_name);

  v_chg := COALESCE(v_chg_hl, 0) + COALESCE(v_chg_sl, 0) + COALESCE(v_ppa_adj, 0);

  INSERT INTO forecast_periods (
    eid, period_name, chg, sah, chg_pct,
    chg_hl, chg_sl, absence_hours,
    chg_pct_hl, chg_pct_sl
  )
  VALUES (
    p_eid, p_period_name,
    v_chg,
    COALESCE(v_sah, 0),
    CASE WHEN COALESCE(v_sah, 0) > 0 THEN ROUND(v_chg    / v_sah * 100, 2) ELSE 0 END,
    COALESCE(v_chg_hl, 0),
    COALESCE(v_chg_sl, 0),
    COALESCE(v_absence_hours, 0),
    CASE WHEN COALESCE(v_sah, 0) > 0 THEN ROUND(v_chg_hl / v_sah * 100, 2) ELSE 0 END,
    CASE WHEN COALESCE(v_sah, 0) > 0 THEN ROUND(v_chg_sl / v_sah * 100, 2) ELSE 0 END
  )
  ON CONFLICT (eid, period_name) DO UPDATE SET
    chg           = EXCLUDED.chg,
    sah           = EXCLUDED.sah,
    chg_pct       = EXCLUDED.chg_pct,
    chg_hl        = EXCLUDED.chg_hl,
    chg_sl        = EXCLUDED.chg_sl,
    absence_hours = EXCLUDED.absence_hours,
    chg_pct_hl    = EXCLUDED.chg_pct_hl,
    chg_pct_sl    = EXCLUDED.chg_pct_sl;
END;
$$;
"""

async def main():
    conn = await asyncpg.connect(
        host=settings.DB_HOST, port=settings.DB_PORT,
        user=settings.DB_USER, password=settings.DB_PASSWORD,
        database=settings.DB_NAME, ssl="require",
    )
    await conn.execute(NEW_PROC)
    await conn.close()
    print("stored proc actualizado.")

asyncio.run(main())
