import time
import asyncpg
from loguru import logger
import app.db as db
from app.config import settings
from app.errors import AppError, ForecastException
from app.models.tickets import TicketCreate, TicketUpdate, VALID_TICKET_TYPES

REQUIRED_FIELDS: dict = {
    "newproj": [],
    "ongoing": [],
    "pto":     ["eid", "start_date"],
    "sick":    ["eid", "start_date"],
    "nj":      ["nj_name"],
    "baja":    ["eid"],
}


async def list_tickets(
    status: str | None = None,
    type_: str | None = None,
    q: str | None = None,
    page: int = 1,
    page_size: int = 25,
) -> dict:
    conditions: list[str] = []
    params: list = []

    if status:
        params.append(status)
        conditions.append(f"t.status = ${len(params)}")

    if type_:
        params.append(type_)
        conditions.append(f"t.type = ${len(params)}")

    if q:
        params.append(f"%{q}%")
        conditions.append(f"(COALESCE(emp.name, t.nj_name, t.eid) ILIKE ${len(params)} OR t.eid ILIKE ${len(params)})")

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    offset = (page - 1) * page_size
    params.append(page_size)
    limit_idx = len(params)
    params.append(offset)
    offset_idx = len(params)

    async with db.pool.acquire() as conn:
        rows = await conn.fetch(f"""
            SELECT t.id::text AS id, t.type, t.eid, t.detail, t.status,
                   TO_CHAR(t.date,'DD/MM/YY') AS date,
                   COALESCE(e.name, t.created_by::text) AS "by",
                   t.nj_name, t.cl, t.location, t.people_lead,
                   t.client_name, t.offering_type, t.chargeability_pct,
                   t.hours_to_move, t.from_period, t.to_period, t.comments,
                   TO_CHAR(t.start_date,'DD/MM/YY') AS start_date,
                   TO_CHAR(t.end_date,'DD/MM/YY') AS end_date,
                   COALESCE(emp.name, t.nj_name) AS eid_name,
                   COALESCE(emp.country, emp.location) AS eid_country,
                   COUNT(*) OVER () AS _total
            FROM tickets t
            LEFT JOIN employees e ON t.created_by = e.eid
            LEFT JOIN employees emp ON t.eid = emp.eid
            {where}
            ORDER BY t.id DESC
            LIMIT ${limit_idx} OFFSET ${offset_idx}
        """, *params)

    total = int(rows[0]["_total"]) if rows else 0
    pages = -(-total // page_size) if page_size > 0 else 0
    items = [{k: v for k, v in dict(r).items() if k != "_total"} for r in rows]
    return {"items": items, "total": total, "page": page, "page_size": page_size, "pages": pages}


async def create(body: TicketCreate, created_by: str, request_id: str) -> dict:
    if body.type not in VALID_TICKET_TYPES:
        raise ForecastException(AppError.TICKET_INVALID_TYPE)

    required = REQUIRED_FIELDS.get(body.type, [])
    for field in required:
        if not getattr(body, field, None):
            raise ForecastException(AppError.TICKET_MISSING_FIELDS)

    effective_end_date = body.end_date or body.new_end_date

    logger.bind(action="tickets:create", request_id=request_id).info(
        "Creating ticket", type=body.type, eid=body.eid
    )
    start = time.monotonic()

    async with db.pool.acquire() as conn:
        async with conn.transaction():
            try:
                if body.eid and body.type not in ("nj",):
                    emp = await conn.fetchrow("SELECT eid FROM employees WHERE eid=$1", body.eid)
                    if not emp:
                        logger.bind(request_id=request_id).warning("Employee not found", eid=body.eid)
                        raise ForecastException(AppError.EMPLOYEE_NOT_FOUND)

                ticket = await conn.fetchrow(
                    """
                    INSERT INTO tickets (
                        type, eid, detail, status, date, created_by,
                        nj_name, start_date, end_date, cl, location, people_lead,
                        client_name, offering_type, chargeability_pct,
                        hours_to_move, from_period, to_period, comments
                    ) VALUES ($1,$2,$3,$4,CURRENT_DATE,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18)
                    RETURNING id::text, type, eid, detail, status, TO_CHAR(date,'DD/MM/YY') AS date
                    """,
                    body.type, body.eid or None, body.detail, body.status, created_by or None,
                    body.nj_name or None, body.start_date or None, effective_end_date or None,
                    body.cl, body.location or None, body.people_lead or None,
                    body.client_name or None, body.offering_type or None,
                    body.chargeability_pct, body.hours_to_move, body.from_period or None,
                    body.to_period or None, body.comments or None,
                )

                await _apply_side_effects(conn, body, effective_end_date, created_by, request_id)

                duration = int((time.monotonic() - start) * 1000)
                logger.bind(action="tickets:create", request_id=request_id, duration_ms=duration).info(
                    "Ticket created", ticket_id=ticket["id"]
                )
                return dict(ticket)

            except ForecastException:
                raise
            except asyncpg.UniqueViolationError:
                logger.bind(request_id=request_id).warning("EID conflict", eid=body.eid)
                raise ForecastException(AppError.EMPLOYEE_EID_TAKEN)
            except Exception:
                logger.bind(request_id=request_id).exception("Unexpected error creating ticket")
                raise ForecastException(AppError.INTERNAL_ERROR)


async def _apply_side_effects(conn, body: TicketCreate, effective_end_date, created_by, request_id: str):
    if body.type == "newproj" and body.eid:
        await conn.execute(
            """
            UPDATE forecast_update SET
                client            = COALESCE($1, client),
                offering          = COALESCE($2, offering),
                roll_on           = COALESCE($3::date, roll_on),
                roll_off          = COALESCE($4::date, roll_off),
                chargeability_pct = COALESCE($5, chargeability_pct),
                updated_at        = NOW()
            WHERE eid = $6
            """,
            body.client_name or None, body.offering_type or None,
            body.start_date or None, effective_end_date or None,
            body.chargeability_pct, body.eid,
        )
        await _recalculate_all_periods_for_eid(conn, body.eid, request_id)

    elif body.type == "ongoing" and body.eid:
        updates, vals = [], []
        if effective_end_date:
            updates.append(f"roll_off = ${len(vals)+1}::date")
            vals.append(effective_end_date)
        if body.chargeability_pct is not None:
            updates.append(f"chargeability_pct = ${len(vals)+1}")
            vals.append(body.chargeability_pct)
        if updates:
            updates.append("updated_at = NOW()")
            vals.append(body.eid)
            await conn.execute(
                f"UPDATE forecast_update SET {', '.join(updates)} WHERE eid = ${len(vals)}",
                *vals,
            )
            await _recalculate_all_periods_for_eid(conn, body.eid, request_id)

    elif body.type == "pto" and body.eid and body.start_date and effective_end_date:
        await conn.execute(
            """
            INSERT INTO absences (eid, period_name, type, start_date, end_date, created_at, created_by)
            SELECT $1,
                   (SELECT period_name FROM periods
                    WHERE start_date <= $2::date AND end_date >= $2::date LIMIT 1),
                   'PTO', $2::date, $3::date, NOW(), $4
            """,
            body.eid, body.start_date, effective_end_date, created_by or None,
        )

    elif body.type == "sick" and body.eid and body.start_date and effective_end_date:
        await conn.execute(
            """
            INSERT INTO absences (eid, period_name, type, start_date, end_date, created_at, created_by)
            SELECT $1,
                   (SELECT period_name FROM periods
                    WHERE start_date <= $2::date AND end_date >= $2::date LIMIT 1),
                   'SICK', $2::date, $3::date, NOW(), $4
            """,
            body.eid, body.start_date, effective_end_date, created_by or None,
        )
        await _recalculate_all_periods_for_eid(conn, body.eid, request_id)

    elif body.type == "nj" and body.nj_name:
        nj_eid = "NJ_" + body.nj_name.lower().replace(" ", ".")
        exists = await conn.fetchrow("SELECT eid FROM employees WHERE eid=$1", nj_eid)
        if not exists:
            await conn.execute(
                """
                INSERT INTO employees (eid, name, location, cl, hire_date, new_joiner, active)
                VALUES ($1,$2,$3,$4,$5::date,TRUE,TRUE)
                """,
                nj_eid, body.nj_name, body.location or None,
                body.cl, body.start_date or None,
            )

    elif body.type == "baja" and body.eid and effective_end_date:
        await conn.execute(
            "UPDATE employees SET termination_date=$1 WHERE eid=$2",
            effective_end_date, body.eid,
        )


async def _recalculate_all_periods_for_eid(conn, eid: str, request_id: str):
    periods = await conn.fetch("SELECT period_name FROM periods ORDER BY start_date")
    logger.bind(request_id=request_id).debug(
        "Recalculating all periods for employee", eid=eid, count=len(periods)
    )
    for p in periods:
        await conn.execute("SELECT recalculate_forecast_period($1,$2)", eid, p["period_name"])


async def update(ticket_id: int, body: TicketUpdate, request_id: str) -> dict:
    updates = {k: v for k, v in body.model_dump(exclude_none=True).items() if v is not None}
    if not updates:
        raise ForecastException(AppError.VALIDATION_ERROR, "No valid fields provided")

    cols = list(updates.keys())
    vals = list(updates.values())
    set_clause = ", ".join(f"{c}=${i+1}" for i, c in enumerate(cols))
    vals.append(ticket_id)

    async with db.pool.acquire() as conn:
        row = await conn.fetchrow(
            f"UPDATE tickets SET {set_clause} WHERE id=${len(vals)}"
            " RETURNING id::text, type, eid, detail, status, TO_CHAR(date,'DD/MM/YY') AS date",
            *vals,
        )
    if not row:
        raise ForecastException(AppError.TICKET_NOT_FOUND)
    return dict(row)


async def assign_eid(ticket_id: int, new_eid: str, new_name, request_id: str) -> dict:
    async with db.pool.acquire() as conn:
        async with conn.transaction():
            try:
                existing_eid_row = await conn.fetchrow(
                    "SELECT eid FROM employees WHERE eid=$1", new_eid
                )
                if existing_eid_row:
                    existing_nj = await conn.fetchrow(
                        "SELECT new_joiner FROM employees WHERE eid=$1", new_eid
                    )
                    if not (existing_nj and existing_nj["new_joiner"]):
                        raise ForecastException(AppError.EMPLOYEE_EID_TAKEN)

                tkt = await conn.fetchrow(
                    "UPDATE tickets SET status='Cerrado', detail=COALESCE(detail,'')||' · EID: '||$1"
                    " WHERE id=$2 RETURNING eid AS old_eid",
                    new_eid, ticket_id,
                )
                if not tkt:
                    raise ForecastException(AppError.TICKET_NOT_FOUND)

                old_eid = tkt["old_eid"]
                if old_eid:
                    await conn.execute(
                        "UPDATE employees SET eid=$1, name=COALESCE($2,name), new_joiner=FALSE WHERE eid=$3",
                        new_eid, new_name or None, old_eid,
                    )
                    await conn.execute("UPDATE forecast_update SET eid=$1 WHERE eid=$2", new_eid, old_eid)
                    await conn.execute("UPDATE forecast_periods SET eid=$1 WHERE eid=$2", new_eid, old_eid)

                return {"ok": True, "new_eid": new_eid}

            except ForecastException:
                raise
            except Exception:
                logger.bind(request_id=request_id).exception("Unexpected error assigning EID")
                raise ForecastException(AppError.INTERNAL_ERROR)
