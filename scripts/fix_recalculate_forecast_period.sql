CREATE OR REPLACE FUNCTION public.recalculate_forecast_period(p_eid character varying, p_period_name character varying)
 RETURNS void
 LANGUAGE plpgsql
AS $function$
DECLARE
  v_roll_on        DATE;
  v_roll_off       DATE;
  v_country        VARCHAR;
  v_period_start   DATE;
  v_period_end     DATE;
  v_sah            NUMERIC;
  v_chg            NUMERIC;
  v_chg_hl         NUMERIC := 0;
  v_chg_sl         NUMERIC := 0;
  v_ppa_adj        NUMERIC := 0;
  v_absence_hours  NUMERIC := 0;
  v_block_count    INTEGER := 0;
BEGIN
  SELECT fu.roll_on, fu.roll_off,
         COALESCE(e.country, e.location)
  INTO v_roll_on, v_roll_off, v_country
  FROM forecast_update fu
  JOIN employees e ON fu.eid = e.eid
  WHERE fu.eid = p_eid
  ORDER BY fu.updated_at DESC NULLS LAST LIMIT 1;

  IF v_country IS NULL THEN
    SELECT COALESCE(e.country, e.location)
    INTO v_country
    FROM employees e WHERE e.eid = p_eid;
  END IF;

  -- normaliza codigo corto/nombre largo antes de comparar contra calendar.country
  v_country := CASE UPPER(TRIM(v_country))
    WHEN 'AR' THEN 'Argentina'
    WHEN 'ARGENTINA' THEN 'Argentina'
    WHEN 'MX' THEN 'Mexico'
    WHEN 'MEXICO' THEN 'Mexico'
    WHEN 'CR' THEN 'Costa Rica'
    WHEN 'COSTA RICA' THEN 'Costa Rica'
    ELSE v_country
  END;

  SELECT start_date, end_date
  INTO v_period_start, v_period_end
  FROM periods WHERE period_name = p_period_name;

  -- SAH: working days in period range, excluding absence days for this employee
  SELECT COALESCE(SUM(CASE WHEN c.is_working_day THEN 8 ELSE 0 END), 0)
  INTO v_sah
  FROM calendar c
  WHERE c.country = v_country
    AND c.date BETWEEN v_period_start AND v_period_end
    AND NOT EXISTS (
      SELECT 1 FROM absences a
      WHERE a.eid = p_eid
        AND c.date BETWEEN a.start_date AND a.end_date
    );

  -- CHG from chargeability_blocks
  SELECT COUNT(*)
  INTO v_block_count
  FROM chargeability_blocks
  WHERE eid = p_eid
    AND start_date <= v_period_end
    AND end_date   >= v_period_start;

  IF v_block_count > 0 THEN
    SELECT
      COALESCE(SUM(CASE WHEN sub.scenario_type = 'effective'  THEN blk_chg ELSE 0 END), 0),
      COALESCE(SUM(CASE WHEN sub.scenario_type = 'assumption' THEN blk_chg ELSE 0 END), 0)
    INTO v_chg_hl, v_chg_sl
    FROM (
      SELECT
        cb.scenario_type,
        -- Exclude absence days from CHG calculation
        (SELECT COALESCE(SUM(CASE WHEN c.is_working_day THEN 8 ELSE 0 END), 0)
         FROM calendar c
         WHERE c.country = v_country
           AND c.date BETWEEN GREATEST(cb.start_date, v_period_start) AND LEAST(cb.end_date, v_period_end)
           AND NOT EXISTS (
             SELECT 1 FROM absences a
             WHERE a.eid = p_eid
               AND c.date BETWEEN a.start_date AND a.end_date
           )
        ) * cb.chargeability_pct / 100.0 AS blk_chg
      FROM chargeability_blocks cb
      WHERE cb.eid = p_eid
        AND cb.start_date <= v_period_end
        AND cb.end_date   >= v_period_start
    ) sub;
  ELSE
    IF v_roll_on IS NOT NULL AND v_roll_off IS NOT NULL THEN
      SELECT COALESCE(SUM(CASE WHEN c.is_working_day THEN 8 ELSE 0 END), 0)
      INTO v_chg_hl
      FROM calendar c
      WHERE c.country = v_country
        AND c.date BETWEEN GREATEST(v_roll_on, v_period_start) AND LEAST(v_roll_off, v_period_end)
        AND NOT EXISTS (
          SELECT 1 FROM absences a
          WHERE a.eid = p_eid
            AND c.date BETWEEN a.start_date AND a.end_date
        );
    ELSE
      v_chg_hl := 0;
    END IF;
    v_chg_sl := 0;
  END IF;

  -- PPA adjustment
  SELECT COALESCE(SUM(
    CASE WHEN to_period   = p_period_name THEN  hours
         WHEN from_period = p_period_name THEN -hours
         ELSE 0
    END
  ), 0)
  INTO v_ppa_adj
  FROM ppa_log
  WHERE eid = p_eid
    AND (to_period = p_period_name OR from_period = p_period_name);

  -- Absence hours: approved absences overlapping this period
  SELECT COALESCE(SUM(COALESCE(a.hours, 0)), 0)
  INTO v_absence_hours
  FROM absences a
  WHERE a.eid = p_eid
    AND a.start_date <= v_period_end
    AND a.end_date   >= v_period_start;

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
$function$