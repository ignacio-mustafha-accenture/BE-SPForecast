"""
fix_oct_p1_sah.py
-----------------
Fixes SAH and CHG for Oct-P1 (and updates the stored proc for future runs).

Root cause: recalculate_forecast_period computed SAH with NOT EXISTS on absences
table, but migration inserted absences with period-level date ranges (start=period_start,
end=period_end). NOT EXISTS then excluded ALL calendar days → SAH=0 for those employees.

Also: SAH didn't respect roll_on / roll_off boundaries, so new-joiners who started
mid-period got the full 80h instead of just their available days.

Fix:
  1. ALTER the stored proc: compute gross SAH within [max(roll_on,start), min(roll_off,end)]
     then subtract absence_hours (sum of a.hours, not date-range exclusion).
  2. Run the fixed logic for Oct-P1 directly in SQL (surgical UPDATE, no other periods touched).
  3. Print before/after comparison against SAP CSV.
"""
import asyncio, sys, os, pandas as pd
sys.path.insert(0, ".")
import asyncpg
from app.config import settings

CSV_PATH = r"C:\Users\ignacio.mustafha\Downloads\01 - FORECAST CONTROL S&P(S&P) (1).csv"
PERIOD   = "Oct-P1"

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
  v_eff_end        DATE;
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
  -- Employee country + roll dates
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

  -- Effective window clamped by roll_on / roll_off
  v_eff_start := GREATEST(COALESCE(v_roll_on, v_period_start), v_period_start);
  v_eff_end   := LEAST(COALESCE(v_roll_off,   v_period_end),   v_period_end);

  -- Absence hours (computed first — drives both SAH and CHG deduction)
  SELECT COALESCE(SUM(COALESCE(a.hours, 0)), 0)
  INTO v_absence_hours
  FROM absences a
  WHERE a.eid = p_eid
    AND a.start_date <= v_period_end
    AND a.end_date   >= v_period_start;

  -- Gross SAH = working hours in effective window (no NOT EXISTS needed)
  SELECT COALESCE(SUM(CASE WHEN c.is_working_day THEN 8 ELSE 0 END), 0)
  INTO v_gross_sah
  FROM calendar c
  WHERE c.country = v_country
    AND c.date BETWEEN v_eff_start AND v_eff_end;

  -- Net SAH = gross minus absence hours (floor at 0)
  v_sah := GREATEST(v_gross_sah - v_absence_hours, 0);

  -- CHG from chargeability_blocks (gross hours in block ∩ effective window, then prorate absences)
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
           AND c.date BETWEEN GREATEST(cb.start_date, v_eff_start) AND LEAST(cb.end_date, v_eff_end)
        ) * cb.chargeability_pct / 100.0 AS blk_chg
      FROM chargeability_blocks cb
      WHERE cb.eid = p_eid
        AND cb.start_date <= v_period_end
        AND cb.end_date   >= v_period_start
    ) sub;

    -- Prorate absence deduction across hl/sl proportionally
    v_total_gross := COALESCE(v_gross_hl, 0) + COALESCE(v_gross_sl, 0);
    IF v_total_gross > 0 THEN
      v_chg_hl := GREATEST(v_gross_hl - v_absence_hours * v_gross_hl / v_total_gross, 0);
      v_chg_sl := GREATEST(v_gross_sl - v_absence_hours * v_gross_sl / v_total_gross, 0);
    ELSE
      v_chg_hl := 0;
      v_chg_sl := 0;
    END IF;
  ELSE
    -- No blocks: use effective window directly
    IF v_eff_start <= v_eff_end THEN
      SELECT COALESCE(SUM(CASE WHEN c.is_working_day THEN 8 ELSE 0 END), 0)
      INTO v_chg_hl
      FROM calendar c
      WHERE c.country = v_country
        AND c.date BETWEEN v_eff_start AND v_eff_end;
      v_chg_hl := GREATEST(v_chg_hl - v_absence_hours, 0);
    ELSE
      v_chg_hl := 0;
    END IF;
    v_chg_sl := 0;
  END IF;

  -- PPA adjustment
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

    # ── Load CSV reference ──────────────────────────────────────────────────
    df = pd.read_csv(CSV_PATH, encoding="latin1", low_memory=False)
    active = df[df["Status"] == "Active"].copy()
    active["eid_clean"] = active["Enterprise ID"].str.strip().str.lower()
    csv_sah = dict(zip(active["eid_clean"],
                       pd.to_numeric(active["SAH P1 OCT P1 2026"], errors="coerce").fillna(0)))
    csv_total = sum(v for v in csv_sah.values() if v > 0)

    # ── Snapshot BEFORE ─────────────────────────────────────────────────────
    before = await conn.fetch(
        "SELECT eid, sah FROM forecast_periods WHERE period_name=$1 ORDER BY eid",
        PERIOD
    )
    total_before = sum(float(r["sah"]) for r in before)
    print(f"ANTES:  {total_before:.0f}h  (CSV target: {csv_total:.0f}h)")

    # ── Step 1: Update the stored proc ──────────────────────────────────────
    print("\n[1/3] Actualizando stored proc recalculate_forecast_period...")
    await conn.execute(NEW_PROC)
    print("      OK")

    # ── Step 2: Run fixed proc for every active employee on Oct-P1 ──────────
    print(f"\n[2/3] Recalculando {PERIOD} para todos los empleados activos...")
    employees = await conn.fetch(
        "SELECT eid FROM employees WHERE active=TRUE ORDER BY eid"
    )
    updated = 0
    for e in employees:
        try:
            await conn.execute(
                "SELECT recalculate_forecast_period($1,$2)",
                e["eid"], PERIOD
            )
            updated += 1
        except Exception as ex:
            print(f"      WARN {e['eid']}: {ex}")

    # Apply chargeability_blocks Python override (mirrors recalculate_employee)
    for e in employees:
        await conn.execute(
            """
            WITH totals AS (
                SELECT
                    COALESCE(SUM(chargeability_pct) FILTER (WHERE scenario_type = 'effective'),  0) AS hl_pct,
                    COALESCE(SUM(chargeability_pct) FILTER (WHERE scenario_type = 'assumption'), 0) AS sl_pct
                FROM chargeability_blocks
                WHERE eid = $1 AND period_name = $2
            )
            UPDATE forecast_periods fp
            SET chg_pct_hl = t.hl_pct,
                chg_pct_sl = t.sl_pct,
                chg_hl     = ROUND(fp.sah * t.hl_pct / 100.0),
                chg_sl     = ROUND(fp.sah * t.sl_pct / 100.0),
                chg        = ROUND(fp.sah * (t.hl_pct + t.sl_pct) / 100.0)
            FROM totals t
            WHERE fp.eid = $1 AND fp.period_name = $2
              AND (t.hl_pct + t.sl_pct) > 0
            """,
            e["eid"], PERIOD,
        )

    # Re-apply SAH PPA adjustments
    await conn.execute(
        """
        UPDATE forecast_periods fp
        SET sah        = fp.sah + COALESCE(fp.sah_ppa_adj, 0),
            chg_pct    = CASE WHEN fp.sah + COALESCE(fp.sah_ppa_adj, 0) > 0
                              THEN ROUND(fp.chg / (fp.sah + COALESCE(fp.sah_ppa_adj, 0)) * 100, 2)
                              ELSE 0 END
        WHERE fp.period_name = $1 AND COALESCE(fp.sah_ppa_adj, 0) != 0
        """,
        PERIOD
    )

    print(f"      {updated} empleados recalculados")

    # ── Step 3: Snapshot AFTER + diff vs CSV ───────────────────────────────
    print(f"\n[3/3] Resultado {PERIOD}:")
    after = await conn.fetch(
        "SELECT eid, sah, chg FROM forecast_periods WHERE period_name=$1 ORDER BY eid",
        PERIOD
    )
    total_after = sum(float(r["sah"]) for r in after)
    total_chg   = sum(float(r["chg"]) for r in after)

    print(f"      SAH total: {total_after:.0f}h  (CSV: {csv_total:.0f}h, diff: {total_after - csv_total:+.0f}h)")
    print(f"      CHG total: {total_chg:.0f}h")
    print(f"      Delta vs antes: {total_after - total_before:+.0f}h")

    # Employees with remaining diff vs CSV (only those in CSV with positive SAH)
    after_map = {r["eid"]: float(r["sah"]) for r in after}
    print()
    print(f"{'EID':<32} {'antes':>7} {'despues':>8} {'CSV':>7} {'diff':>7}")
    print("-" * 75)
    before_map = {r["eid"]: float(r["sah"]) for r in before}
    for eid in sorted(set(after_map) | set(csv_sah)):
        b = before_map.get(eid, 0)
        a = after_map.get(eid, 0)
        c = csv_sah.get(eid, None)
        if abs(a - b) > 0.1 or (c is not None and abs(a - c) > 0.1):
            c_str = f"{c:.0f}" if c is not None else "N/A"
            diff_str = f"{a - c:+.0f}" if c is not None else "N/A"
            print(f"  {eid:<30} {b:>7.0f} {a:>8.0f} {c_str:>7} {diff_str:>7}")

    await conn.close()
    print("\nDone.")


asyncio.run(main())
