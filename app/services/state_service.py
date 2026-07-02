import time
from datetime import date
from loguru import logger
import app.db as db
from app.config import settings


async def _timed_fetch(conn, query: str, *args, request_id: str = "-"):
    start = time.monotonic()
    result = await conn.fetch(query, *args)
    ms = int((time.monotonic() - start) * 1000)
    if ms > settings.SLOW_QUERY_THRESHOLD_MS:
        logger.bind(request_id=request_id, duration_ms=ms).warning(
            "Slow query detected", query=query[:120]
        )
    return result


def _fallback_periods(window_offset: int) -> list:
    MN = ["Ene","Feb","Mar","Abr","May","Jun","Jul","Ago","Sep","Oct","Nov","Dic"]
    today = date.today()
    y, m = today.year, today.month - 1  # 0-indexed month
    h = 0 if today.day <= 15 else 1

    def add(y, m, h, n):
        t = y * 24 + m * 2 + h + n
        ny = t // 24
        t -= ny * 24
        return ny, t // 2, t % 2

    result = []
    for i in range(6):
        py, pm, ph = add(y, m, h, window_offset + i)
        s = 1 if ph == 0 else 16
        import calendar
        e = 15 if ph == 0 else calendar.monthrange(py, pm + 1)[1]
        pn = f"{MN[pm]}-P{ph+1}"
        result.append({
            "id": f"P{i+1}",
            "period_name": pn,
            "label": pn,
            "sah": 80,
            "isCurrent": window_offset == 0 and i == 0,
            "start_date": date(py, pm + 1, s).isoformat(),
            "end_date": date(py, pm + 1, e).isoformat(),
        })
    return result


async def get_state(window_offset: int = 0) -> dict:
    async with db.pool.acquire() as conn:
        # --- Periods ---
        try:
            period_rows = await conn.fetch("""
                SELECT p.period_name, p.start_date, p.end_date,
                       SUM(CASE WHEN c.is_working_day AND c.country='Argentina' THEN 8 ELSE 0 END) AS sah
                FROM periods p
                LEFT JOIN calendar c ON c.period_name = p.period_name
                  AND EXTRACT(YEAR FROM c.date) IN (2025, 2026)
                GROUP BY p.period_name, p.start_date, p.end_date
                ORDER BY p.start_date
            """)
            today = date.today()
            rows_list = list(period_rows)
            cur = next(
                (i for i, r in enumerate(rows_list)
                 if r["start_date"] <= today <= r["end_date"]),
                0,
            )
            slice_start = max(0, cur + window_offset)
            sliced = rows_list[slice_start: slice_start + 6]
            periods = [
                {
                    "id": f"P{i+1}",
                    "period_name": r["period_name"],
                    "label": r["period_name"],
                    "sah": float(r["sah"] or 80),
                    "isCurrent": window_offset == 0 and i == 0,
                    "start_date": r["start_date"].isoformat(),
                    "end_date": r["end_date"].isoformat(),
                }
                for i, r in enumerate(sliced)
            ] or _fallback_periods(window_offset)
        except Exception:
            logger.exception("Failed to fetch periods, using fallback")
            periods = _fallback_periods(window_offset)

        period_names = [p["period_name"] for p in periods]

        # --- Employees ---
        emp_rows = await conn.fetch("""
            WITH latest_fu AS (
                SELECT DISTINCT ON (eid) * FROM forecast_update ORDER BY eid, updated_at DESC NULLS LAST
            )
            SELECT
                e.eid AS "EID",
                e.name AS "Name",
                COALESCE(e.country, e.location) AS "Country",
                CASE WHEN e.cl IS NOT NULL
                     THEN CAST(CAST(e.cl AS NUMERIC) AS INTEGER)::text
                     ELSE NULL END AS "CL",
                COALESCE(e.fte, 1.0) AS "FTE",
                TO_CHAR(e.hire_date,'DD/MM/YY') AS "HireDate",
                COALESCE(pl.name, e.people_lead::text) AS "Manager",
                COALESCE(te.name, fu.te_approver::text) AS "TEApprover",
                fu.offering AS "ProjectType",
                fu.client AS "Client",
                COALESCE(am.name, fu.account_manager::text) AS "AccountManager",
                fu.office AS "Office",
                TO_CHAR(fu.roll_on,'DD/MM/YY') AS "RollOn",
                TO_CHAR(fu.roll_off,'DD/MM/YY') AS "RollOff",
                TO_CHAR(fu.first_available,'DD/MM/YY') AS "FAD",
                fu.days_available AS "DaysToAvailable",
                fu.chargeability_pct AS "ChargeabilityPct",
                TO_CHAR(fu.next_pto,'DD/MM/YY') AS "NextPTO",
                TO_CHAR(fu.next_pto_end,'DD/MM/YY') AS "NextPTOEnd",
                fu.next_pto_hours AS "NextPTOHours",
                fu.next_client AS "NextClientPTO",
                e.new_joiner AS "NewJoiner",
                TO_CHAR(e.termination_date,'DD/MM/YY') AS "TerminationDate",
                COALESCE(e.charge, TRUE) AS "Charge"
            FROM employees e
            LEFT JOIN latest_fu fu ON e.eid = fu.eid
            LEFT JOIN employees pl ON e.people_lead = pl.eid
            LEFT JOIN employees am ON fu.account_manager = am.eid
            LEFT JOIN employees te ON fu.te_approver = te.eid
            WHERE e.active = TRUE
            ORDER BY COALESCE(e.country, e.location), e.name
        """)

        # --- Forecast map ---
        fp_rows = await conn.fetch(
            "SELECT eid, period_name, chg, sah FROM forecast_periods WHERE period_name = ANY($1)",
            period_names,
        )
        forecast_map: dict = {}
        for fp in fp_rows:
            if fp["eid"] not in forecast_map:
                forecast_map[fp["eid"]] = {}
            forecast_map[fp["eid"]][fp["period_name"]] = {
                "chg": float(fp["chg"] or 0),
                "sah": float(fp["sah"] or 0),
            }

        employees = []
        for e in emp_rows:
            row = dict(e)
            fp = forecast_map.get(row["EID"], {})
            chg_arr = [float(fp.get(pn, {}).get("chg", 0)) for pn in period_names]
            sah_arr = [float(fp.get(pn, {}).get("sah", 0)) for pn in period_names]
            cp_arr = [
                round(chg_arr[i] / sah_arr[i] * 100) if sah_arr[i] > 0 else 0
                for i in range(len(period_names))
            ]
            row.update({
                "chg": chg_arr,
                "sah": sah_arr,
                "cp": cp_arr,
                "sickDays": [0] * len(period_names),
                "ppaAdj": [0] * len(period_names),
                "NJFormat": (
                    f"{row['Name']} | {row['HireDate']} | CL{row['CL']} | {row['Country']}"
                    if row.get("NewJoiner") else None
                ),
                "FTE": float(row.get("FTE") or 1),
                "ChargeabilityPct": float(row.get("ChargeabilityPct") or 0),
                "DaysToAvailable": float(row.get("DaysToAvailable") or 0),
                "NextPTOHours": float(row.get("NextPTOHours") or 0),
                "Charge": row.get("Charge") is not False,
            })
            employees.append(row)

        # --- Targets ---
        target_rows = await conn.fetch(
            "SELECT country, target_pct FROM targets WHERE fiscal_year='FY26' AND (valid_to IS NULL OR valid_to>=CURRENT_DATE)"
        )
        targets = {"general": 87}
        for t in target_rows:
            targets[t["country"]] = float(t["target_pct"])

        # --- Tickets ---
        ticket_rows = await conn.fetch("""
            SELECT t.id::text AS id, t.type, t.eid, t.detail, t.status,
                   TO_CHAR(t.date,'DD/MM/YY') AS date,
                   COALESCE(e.name, t.created_by::text) AS "by",
                   t.nj_name, t.cl, t.location, t.people_lead,
                   t.client_name, t.offering_type, t.chargeability_pct,
                   t.hours_to_move, t.from_period, t.to_period, t.comments
            FROM tickets t LEFT JOIN employees e ON t.created_by=e.eid
            ORDER BY t.id DESC
        """)
        tickets = [dict(r) for r in ticket_rows]

        # --- PPA log ---
        ppa_rows = await conn.fetch("""
            SELECT p.id::text AS id, p.eid, e.name,
                   p.from_period AS "from", p.to_period AS "to",
                   p.hours AS hs, p.reason,
                   TO_CHAR(p.created_at,'DD/MM/YY') AS date
            FROM ppa_log p LEFT JOIN employees e ON p.eid=e.eid
            ORDER BY p.created_at DESC
        """)
        ppa_log = [dict(r) for r in ppa_rows]

    return {
        "periods": periods,
        "employees": employees,
        "targets": targets,
        "tickets": tickets,
        "ppa_log": ppa_log,
    }
