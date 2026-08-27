import time
from loguru import logger
import app.db as db
from app.config import settings
from app.errors import AppError, ForecastException
from app.models.employees import EmployeeUpdate
import app.services.recalculate_service as recalculate_service


async def list_employees(
    country: str | None,
    q: str | None,
    status: str | None,
    page: int,
    page_size: int,
    offering: str | None = None,
    te_approver: str | None = None,
    chg_bucket: str | None = None,
) -> dict:
    conditions = ["e.active = TRUE"]
    params: list = []

    if country:
        COUNTRY_VARIANTS: dict[str, list[str]] = {
            'ar': ['ar', 'argentina'],
            'mx': ['mx', 'mexico', 'méxico'],
            'cr': ['cr', 'costa rica'],
        }
        expanded: list[str] = []
        for c in country.split(','):
            key = c.strip().lower()
            expanded.extend(COUNTRY_VARIANTS.get(key, [key]))
        params.append(expanded)
        conditions.append(f"LOWER(COALESCE(e.country, e.location)) = ANY(${len(params)})")

    if q:
        params.append(f"%{q}%")
        conditions.append(f"(e.name ILIKE ${len(params)} OR e.eid ILIKE ${len(params)})")

    if status == "unassigned":
        conditions.append("e.charge IS FALSE")
    elif status == "green":
        conditions.append("fu.chargeability_pct >= 80")
    elif status == "yellow":
        conditions.append("fu.chargeability_pct >= 50 AND fu.chargeability_pct < 80")
    elif status == "red":
        conditions.append("fu.chargeability_pct < 50 AND COALESCE(e.charge, TRUE) = TRUE")

    if offering:
        params.append(offering)
        conditions.append(f"fu.offering = ${len(params)}")

    if te_approver:
        params.append(f"%{te_approver}%")
        conditions.append(f"fu.te_approver ILIKE ${len(params)}")

    if chg_bucket == "over":
        conditions.append("fp_cur.chg_pct_hl > 100")
    elif chg_bucket == "full":
        conditions.append("fp_cur.chg_pct_hl = 100")
    elif chg_bucket == "under":
        conditions.append("fp_cur.chg_pct_hl < 100")

    where = " AND ".join(conditions)
    offset = (page - 1) * page_size
    params.append(page_size)
    limit_idx = len(params)
    params.append(offset)
    offset_idx = len(params)

    async with db.pool.acquire() as conn:
        rows = await conn.fetch(f"""
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
                fu.te_approver AS "TEApprover",
                fu.offering AS "ProjectType",
                e.offering AS "EmployeeOffering",
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
                fu.notes AS "Notes",
                e.new_joiner AS "NewJoiner",
                TO_CHAR(e.termination_date,'DD/MM/YY') AS "TerminationDate",
                COALESCE(e.charge, TRUE) AS "Charge",
                COALESCE(e.ringfenced, FALSE) AS "Ringfenced",
                COUNT(*) OVER () AS _total
            FROM employees e
            LEFT JOIN latest_fu fu ON e.eid = fu.eid
            LEFT JOIN employees pl ON e.people_lead = pl.eid
            LEFT JOIN employees am ON fu.account_manager = am.eid
            LEFT JOIN (
                SELECT DISTINCT ON (fp.eid) fp.eid, fp.chg_pct_hl
                FROM forecast_periods fp
                JOIN periods p ON fp.period_name = p.period_name
                WHERE p.start_date <= CURRENT_DATE AND p.end_date >= CURRENT_DATE
                ORDER BY fp.eid
            ) fp_cur ON fp_cur.eid = e.eid
            WHERE {where}
            ORDER BY COALESCE(e.country, e.location), e.name
            LIMIT ${limit_idx} OFFSET ${offset_idx}
        """, *params)

        total = int(rows[0]["_total"]) if rows else 0

        period_row = await conn.fetchrow(
            "SELECT period_name FROM periods WHERE start_date <= CURRENT_DATE AND end_date >= CURRENT_DATE LIMIT 1"
        )
        current_period = period_row["period_name"] if period_row else None

        eids = [r["EID"] for r in rows]
        fp_map: dict = {}
        scenario_map: dict = {}
        assumption_eids: set = set()
        if eids and current_period:
            fp_rows = await conn.fetch(
                """SELECT eid, chg, sah, chg_hl, chg_sl, chg_cascadeadas,
                          absence_hours, chg_pct_sl, chg_pct_hl
                   FROM forecast_periods WHERE eid = ANY($1) AND period_name = $2""",
                eids, current_period,
            )
            fp_map = {r["eid"]: r for r in fp_rows}

            block_rows = await conn.fetch(
                """SELECT DISTINCT ON (eid) eid, scenario_type
                   FROM chargeability_blocks
                   WHERE eid = ANY($1) AND period_name = $2
                   ORDER BY eid, (scenario_type = 'assumption') DESC""",
                eids, current_period,
            )
            scenario_map = {r["eid"]: r["scenario_type"] for r in block_rows}

            assumption_rows = await conn.fetch(
                """SELECT DISTINCT cb.eid
                   FROM chargeability_blocks cb
                   JOIN periods p ON cb.period_name = p.period_name
                   WHERE cb.eid = ANY($1) AND cb.scenario_type = 'assumption'
                     AND p.end_date >= CURRENT_DATE""",
                eids,
            )
            assumption_eids = {r["eid"] for r in assumption_rows}

    employees = []
    for r in rows:
        row = dict(r)
        row.pop("_total", None)
        fp = fp_map.get(row["EID"])
        chg_val              = float(fp["chg"] or 0)             if fp else 0.0
        sah_val              = float(fp["sah"] or 0)             if fp else 0.0
        chg_hl_val           = float(fp["chg_hl"] or 0)          if fp else 0.0
        chg_sl_val           = float(fp["chg_sl"] or 0)          if fp else 0.0
        chg_cascadeadas_val  = float(fp["chg_cascadeadas"] or 0) if fp else 0.0
        absence_hours_val    = float(fp["absence_hours"] or 0)   if fp else 0.0
        chg_pct_sl_val       = float(fp["chg_pct_sl"] or 0)      if fp else 0.0
        chg_pct_hl_val       = float(fp["chg_pct_hl"] or 0)      if fp else 0.0
        row.update({
            "chg":              [chg_val],
            "sah":              [sah_val],
            "cp":               [chg_pct_hl_val],
            "chg_hl":           [chg_hl_val],
            "chg_sl":           [chg_sl_val],
            "chg_cascadeadas":  [chg_cascadeadas_val],
            "absence_hours":    [absence_hours_val],
            "chg_pct_sl":       [chg_pct_sl_val],
            "chg_pct_hl":       [chg_pct_hl_val],
            "ScenarioType":         scenario_map.get(row["EID"], "effective"),
            "HasAssumptionBlocks":  row["EID"] in assumption_eids,
            "NJFormat": (
                f"{row['Name']} | {row['HireDate']} | CL{row['CL']} | {row['Country']}"
                if row.get("NewJoiner") else None
            ),
            "FTE":              float(row.get("FTE") or 1),
            "ChargeabilityPct": float(row.get("ChargeabilityPct") or 0),
            "DaysToAvailable":  float(row.get("DaysToAvailable") or 0),
            "NextPTOHours":     float(row.get("NextPTOHours") or 0),
            "Charge":           row.get("Charge") is not False,
            "Ringfenced":       bool(row.get("Ringfenced") or False),
            "EmployeeOffering": row.get("EmployeeOffering"),
        })
        employees.append(row)

    pages = -(-total // page_size) if page_size > 0 else 0
    return {"items": employees, "total": total, "page": page, "page_size": page_size, "pages": pages}


async def get_employees_on_pto() -> list:
    async with db.pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT
                e.eid AS "EID",
                e.name AS "Name",
                COALESCE(e.country, e.location) AS "Country",
                a.start_date::text AS "PTOStart",
                a.end_date::text AS "PTOEnd",
                a.hours AS "PTOHours"
            FROM absences a
            JOIN employees e ON a.eid = e.eid
            WHERE a.type = 'PTO'
              AND a.start_date <= CURRENT_DATE
              AND a.end_date >= CURRENT_DATE
            ORDER BY e.name
        """)
        return [dict(r) for r in rows]


async def update(eid: str, body: EmployeeUpdate, request_id: str) -> dict:
    logger.bind(action="employees:update", request_id=request_id).info(
        "Updating employee", eid=eid
    )
    start = time.monotonic()

    async with db.pool.acquire() as conn:
        async with conn.transaction():
            emp = await conn.fetchrow("SELECT eid FROM employees WHERE eid=$1", eid)
            if not emp:
                raise ForecastException(AppError.EMPLOYEE_NOT_FOUND)

            effective_eid = body.new_eid or eid

            if body.new_eid and body.new_eid != eid:
                taken = await conn.fetchrow("SELECT eid FROM employees WHERE eid=$1", body.new_eid)
                if taken:
                    raise ForecastException(AppError.EMPLOYEE_EID_TAKEN)

            if body.new_eid or body.name or body.cl is not None or body.ringfenced is not None or body.employee_offering is not None:
                await conn.execute(
                    """
                    UPDATE employees SET
                        eid        = COALESCE($1, eid),
                        name       = COALESCE($2, name),
                        cl         = COALESCE($3, cl),
                        ringfenced = COALESCE($5, ringfenced),
                        offering   = COALESCE($6, offering)
                    WHERE eid = $4
                    """,
                    body.new_eid or None, body.name or None, body.cl, eid,
                    body.ringfenced, body.employee_offering or None,
                )
                if body.new_eid and body.new_eid != eid:
                    await conn.execute("UPDATE forecast_update SET eid=$1 WHERE eid=$2", body.new_eid, eid)
                    await conn.execute("UPDATE forecast_periods SET eid=$1 WHERE eid=$2", body.new_eid, eid)
                    await conn.execute("UPDATE tickets SET eid=$1 WHERE eid=$2", body.new_eid, eid)

            await conn.execute(
                """
                INSERT INTO forecast_update (
                    eid, client, offering, roll_on, roll_off,
                    account_manager, notes, next_client, chargeability_pct, updated_at
                ) VALUES ($8,$1,$2,$3::date,$4::date,$5,$6,$7,$9,NOW())
                ON CONFLICT (eid) DO UPDATE SET
                    client            = COALESCE($1, forecast_update.client),
                    offering          = COALESCE($2, forecast_update.offering),
                    roll_on           = COALESCE($3::date, forecast_update.roll_on),
                    roll_off          = COALESCE($4::date, forecast_update.roll_off),
                    account_manager   = COALESCE($5, forecast_update.account_manager),
                    notes             = COALESCE($6, forecast_update.notes),
                    next_client       = COALESCE($7, forecast_update.next_client),
                    chargeability_pct = COALESCE($9, forecast_update.chargeability_pct),
                    updated_at        = NOW()
                """,
                body.client or None,
                body.offering or None,
                body.roll_on or None,
                body.roll_off or None,
                body.account_manager or None,
                body.notes or None,
                body.next_client or None,
                effective_eid,
                body.chargeability_pct,
            )

            if body.roll_on or body.roll_off or body.chargeability_pct is not None:
                periods = await conn.fetch("SELECT period_name FROM periods ORDER BY start_date")
                logger.bind(request_id=request_id).debug(
                    "Recalculating periods after employee update", eid=effective_eid
                )
                for p in periods:
                    await conn.execute(
                        "SELECT recalculate_forecast_period($1,$2)", effective_eid, p["period_name"]
                    )

    duration = int((time.monotonic() - start) * 1000)
    logger.bind(action="employees:update", request_id=request_id, duration_ms=duration).info(
        "Employee updated", eid=effective_eid
    )
    return {"ok": True}


async def get_employee(eid: str) -> dict:
    async with db.pool.acquire() as conn:
        row = await conn.fetchrow("""
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
                fu.te_approver AS "TEApprover",
                fu.offering AS "ProjectType",
                e.offering AS "EmployeeOffering",
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
                fu.notes AS "Notes",
                e.new_joiner AS "NewJoiner",
                TO_CHAR(e.termination_date,'DD/MM/YY') AS "TerminationDate",
                COALESCE(e.charge, TRUE) AS "Charge",
                COALESCE(e.ringfenced, FALSE) AS "Ringfenced"
            FROM employees e
            LEFT JOIN latest_fu fu ON e.eid = fu.eid
            LEFT JOIN employees pl ON e.people_lead = pl.eid
            LEFT JOIN employees am ON fu.account_manager = am.eid
            WHERE e.eid = $1
        """, eid)

        if not row:
            from app.errors import AppError, ForecastException
            raise ForecastException(AppError.EMPLOYEE_NOT_FOUND)

        period_row = await conn.fetchrow(
            "SELECT period_name FROM periods WHERE start_date <= CURRENT_DATE AND end_date >= CURRENT_DATE LIMIT 1"
        )
        current_period = period_row["period_name"] if period_row else None

        fp_map: dict = {}
        if current_period:
            fp_rows = await conn.fetch(
                """SELECT eid, chg, sah, chg_hl, chg_sl, chg_cascadeadas,
                          absence_hours, chg_pct_sl, chg_pct_hl
                   FROM forecast_periods WHERE eid = $1 AND period_name = $2""",
                eid, current_period,
            )
            fp_map = {r["eid"]: r for r in fp_rows}

    result = dict(row)
    fp = fp_map.get(eid)
    chg_val              = float(fp["chg"] or 0)             if fp else 0.0
    sah_val              = float(fp["sah"] or 0)             if fp else 0.0
    chg_hl_val           = float(fp["chg_hl"] or 0)          if fp else 0.0
    chg_sl_val           = float(fp["chg_sl"] or 0)          if fp else 0.0
    chg_cascadeadas_val  = float(fp["chg_cascadeadas"] or 0) if fp else 0.0
    absence_hours_val    = float(fp["absence_hours"] or 0)   if fp else 0.0
    chg_pct_sl_val       = float(fp["chg_pct_sl"] or 0)      if fp else 0.0
    chg_pct_hl_val       = float(fp["chg_pct_hl"] or 0)      if fp else 0.0
    result.update({
        "chg":              [chg_val],
        "sah":              [sah_val],
        "cp":               [chg_pct_hl_val],
        "chg_hl":           [chg_hl_val],
        "chg_sl":           [chg_sl_val],
        "chg_cascadeadas":  [chg_cascadeadas_val],
        "absence_hours":    [absence_hours_val],
        "chg_pct_sl":       [chg_pct_sl_val],
        "chg_pct_hl":       [chg_pct_hl_val],
        "NJFormat": (
            f"{result['Name']} | {result['HireDate']} | CL{result['CL']} | {result['Country']}"
            if result.get("NewJoiner") else None
        ),
        "FTE":              float(result.get("FTE") or 1),
        "ChargeabilityPct": float(result.get("ChargeabilityPct") or 0),
        "DaysToAvailable":  float(result.get("DaysToAvailable") or 0),
        "NextPTOHours":     float(result.get("NextPTOHours") or 0),
        "Charge":           result.get("Charge") is not False,
        "Ringfenced":       bool(result.get("Ringfenced") or False),
        "EmployeeOffering": result.get("EmployeeOffering"),
    })
    return result


async def assign_real_eid(old_eid: str, new_eid: str, new_name: str | None, request_id: str) -> dict:
    async with db.pool.acquire() as conn:
        async with conn.transaction():
            try:
                emp = await conn.fetchrow("SELECT new_joiner FROM employees WHERE eid=$1", old_eid)
                if not emp:
                    raise ForecastException(AppError.EMPLOYEE_NOT_FOUND)
                if not emp["new_joiner"]:
                    raise ForecastException(AppError.VALIDATION_ERROR, "Employee is not a new joiner")

                existing = await conn.fetchrow("SELECT new_joiner FROM employees WHERE eid=$1", new_eid)
                if existing and not existing["new_joiner"]:
                    raise ForecastException(AppError.EMPLOYEE_EID_TAKEN)

                await conn.execute(
                    """
                    INSERT INTO employees (eid, name, country, location, cl, fte, active,
                        hire_date, termination_date, people_lead, new_joiner, charge, ringfenced, offering)
                    SELECT $1, COALESCE($2, name), country, location, cl, fte, active,
                        hire_date, termination_date, people_lead, FALSE, charge, ringfenced, offering
                    FROM employees WHERE eid=$3
                    """,
                    new_eid, new_name or None, old_eid,
                )
                await conn.execute("UPDATE forecast_update SET eid=$1 WHERE eid=$2", new_eid, old_eid)
                await conn.execute("UPDATE forecast_periods SET eid=$1 WHERE eid=$2", new_eid, old_eid)
                await conn.execute("UPDATE chargeability_blocks SET eid=$1 WHERE eid=$2", new_eid, old_eid)
                await conn.execute("UPDATE absences SET eid=$1 WHERE eid=$2", new_eid, old_eid)
                await conn.execute("UPDATE tickets SET eid=$1 WHERE eid=$2", new_eid, old_eid)
                await conn.execute("UPDATE ppa_log SET eid=$1 WHERE eid=$2", new_eid, old_eid)
                await conn.execute("UPDATE users SET eid=$1 WHERE eid=$2", new_eid, old_eid)
                await conn.execute("DELETE FROM employees WHERE eid=$1", old_eid)

                logger.bind(request_id=request_id).info("NJ EID assigned", old_eid=old_eid, new_eid=new_eid)

            except ForecastException:
                raise
            except Exception:
                logger.bind(request_id=request_id).exception("Unexpected error assigning real EID", old_eid=old_eid, new_eid=new_eid)
                raise ForecastException(AppError.INTERNAL_ERROR)

    await recalculate_service.recalculate_employee(new_eid, request_id)
    return {"ok": True, "new_eid": new_eid}
