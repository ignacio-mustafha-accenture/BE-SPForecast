import time
import unicodedata
import asyncpg
from loguru import logger
import app.db as db
from app.config import settings
from app.errors import AppError, ForecastException
from app.models.tickets import TicketCreate, TicketUpdate, VALID_TICKET_TYPES

REQUIRED_FIELDS: dict = {
    "newproj": ["eid", "client_name", "offering_type", "chargeability_pct", "start_date", "end_date"],
    "ongoing": ["eid", "end_date"],
    "pto":     ["eid", "start_date", "end_date"],
    "sick":    ["eid", "start_date", "end_date"],
    "nj":      ["nj_name", "cl", "location", "people_lead", "start_date", "te_approver"],
    "baja":    ["eid", "end_date"],
}


def _normalize_nj_eid(name: str) -> str:
    normalized = unicodedata.normalize('NFD', name)
    stripped = ''.join(c for c in normalized if unicodedata.category(c) != 'Mn')
    return "NJ_" + stripped.lower().replace(" ", ".")


def _compute_period_name(date_val) -> str:
    from datetime import date as _date
    MN = ["Ene", "Feb", "Mar", "Abr", "May", "Jun", "Jul", "Ago", "Sep", "Oct", "Nov", "Dic"]
    d = _date.fromisoformat(date_val) if isinstance(date_val, str) else date_val
    return f"{MN[d.month - 1]}-P{1 if d.day <= 15 else 2}"


async def _get_period_for_date(conn, date_val) -> str:
    date_str = date_val.isoformat() if hasattr(date_val, "isoformat") else date_val
    try:
        row = await conn.fetchrow(
            "SELECT period_name FROM periods WHERE start_date <= $1::text::date AND end_date >= $1::text::date LIMIT 1",
            date_str,
        )
        if row:
            return row["period_name"]
    except Exception:
        pass
    return _compute_period_name(date_val)


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
                   COALESCE(u.full_name, t.created_by) AS "by",
                   t.nj_name, t.cl, t.location, t.people_lead,
                   t.client_name, t.offering_type, t.chargeability_pct,
                   t.hours_to_move, t.from_period, t.to_period, t.comments,
                   t.start_date::text AS start_date,
                   t.end_date::text AS end_date,
                   t.rejection_reason,
                   COALESCE(t.scenario_type, 'assumption') AS scenario_type,
                   t.effectivization_date::text AS effectivization_date,
                   COALESCE(emp.name, t.nj_name) AS eid_name,
                   COALESCE(emp.country, emp.location) AS eid_country,
                   COUNT(*) OVER () AS _total
            FROM tickets t
            LEFT JOIN users u ON u.email = t.created_by
            LEFT JOIN employees emp ON t.eid = emp.eid
            {where}
            ORDER BY t.id DESC
            LIMIT ${limit_idx} OFFSET ${offset_idx}
        """, *params)

    total = int(rows[0]["_total"]) if rows else 0
    pages = -(-total // page_size) if page_size > 0 else 0
    items = [{k: v for k, v in dict(r).items() if k != "_total"} for r in rows]
    return {"items": items, "total": total, "page": page, "page_size": page_size, "pages": pages}


async def _fetch_full_ticket(conn, ticket_id: str) -> dict:
    row = await conn.fetchrow("""
        SELECT t.id::text AS id, t.type, t.eid, t.detail, t.status,
               TO_CHAR(t.date,'DD/MM/YY') AS date,
               COALESCE(e.name, u.full_name, t.created_by::text) AS "by",
               t.nj_name, t.cl, t.location, t.people_lead,
               t.client_name, t.offering_type, t.chargeability_pct,
               t.hours_to_move, t.from_period, t.to_period, t.comments,
               t.start_date::text AS start_date,
               t.end_date::text AS end_date,
               t.rejection_reason,
               COALESCE(t.scenario_type, 'assumption') AS scenario_type,
               t.effectivization_date::text AS effectivization_date,
               COALESCE(emp.name, t.nj_name) AS eid_name,
               COALESCE(emp.country, emp.location) AS eid_country
        FROM tickets t
        LEFT JOIN employees e   ON t.created_by = e.eid
        LEFT JOIN users u       ON u.email = t.created_by
        LEFT JOIN employees emp ON t.eid = emp.eid
        WHERE t.id = $1
    """, int(ticket_id))
    return dict(row)


async def get_ticket(ticket_id: int) -> dict:
    async with db.pool.acquire() as conn:
        row = await conn.fetchrow("""
            SELECT t.id::text AS id, t.type, t.eid, t.detail, t.status,
                   TO_CHAR(t.date,'DD/MM/YY') AS date,
                   COALESCE(e.name, u.full_name, t.created_by::text) AS "by",
                   t.nj_name, t.cl, t.location, t.people_lead,
                   t.client_name, t.offering_type, t.chargeability_pct,
                   t.hours_to_move, t.from_period, t.to_period, t.comments,
                   t.start_date::text AS start_date,
                   t.end_date::text AS end_date,
                   t.rejection_reason,
                   COALESCE(t.scenario_type, 'assumption') AS scenario_type,
                   t.effectivization_date::text AS effectivization_date,
                   COALESCE(emp.name, t.nj_name) AS eid_name,
                   COALESCE(emp.country, emp.location) AS eid_country
            FROM tickets t
            LEFT JOIN employees e   ON t.created_by = e.eid
            LEFT JOIN users u       ON u.email = t.created_by
            LEFT JOIN employees emp ON t.eid = emp.eid
            WHERE t.id = $1
        """, ticket_id)
        if not row:
            raise ForecastException(AppError.TICKET_NOT_FOUND)
        return dict(row)


async def create(body: TicketCreate, created_by: str, request_id: str) -> dict:
    if body.type not in VALID_TICKET_TYPES:
        raise ForecastException(AppError.TICKET_INVALID_TYPE)

    required = REQUIRED_FIELDS.get(body.type, [])
    for field in required:
        if not getattr(body, field, None):
            raise ForecastException(AppError.TICKET_MISSING_FIELDS)

    if body.type in ("newproj", "ongoing") and body.scenario_type == "assumption" and not body.effectivization_date:
        raise ForecastException(AppError.VALIDATION_ERROR, "effectivization_date is required for assumption tickets")

    effective_end_date = body.end_date or body.new_end_date or body.start_date

    if body.effectivization_date and effective_end_date:
        from datetime import date as _date
        try:
            eff_date = _date.fromisoformat(body.effectivization_date)
            end_date = _date.fromisoformat(effective_end_date)
            if eff_date > end_date:
                raise ForecastException(AppError.VALIDATION_ERROR, "effectivization_date must be <= end_date")
        except ValueError:
            raise ForecastException(AppError.VALIDATION_ERROR, "Invalid effectivization_date format (use YYYY-MM-DD)")

    logger.bind(action="tickets:create", request_id=request_id).info(
        "Creating ticket", type=body.type, eid=body.eid
    )
    start = time.monotonic()

    try:
        async with db.pool.acquire() as conn:
            async with conn.transaction():
                try:
                    if body.type == "newproj" and body.eid and body.client_name:
                        existing = await conn.fetchrow(
                            "SELECT id FROM tickets WHERE type='newproj' AND eid=$1 AND client_name=$2 AND status='Approved' LIMIT 1",
                            body.eid, body.client_name,
                        )
                        if existing:
                            raise ForecastException(
                                AppError.VALIDATION_ERROR,
                                "Ya existe una asignación activa para este empleado y cliente. Usá un ticket 'En Curso'.",
                            )

                    if body.eid and body.type not in ("nj",):
                        emp = await conn.fetchrow("SELECT eid FROM employees WHERE eid=$1", body.eid)
                        if not emp:
                            logger.bind(request_id=request_id).warning("Employee not found", eid=body.eid)
                            raise ForecastException(AppError.EMPLOYEE_NOT_FOUND)

                    ticket_row = await conn.fetchrow(
                        """
                        INSERT INTO tickets (
                            type, eid, detail, status, date, created_by,
                            nj_name, start_date, end_date, cl, location, people_lead,
                            client_name, offering_type, chargeability_pct,
                            hours_to_move, from_period, to_period, comments,
                            scenario_type, effectivization_date
                        ) VALUES ($1,$2,$3,$4,CURRENT_DATE,$5,$6,$7::text::date,$8::text::date,
                                  $9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19,$20::text::date)
                        RETURNING id::text
                        """,
                        body.type, body.eid or None, body.detail, body.status, created_by or None,
                        body.nj_name or None, body.start_date or None, effective_end_date or None,
                        body.cl, body.location or None, body.people_lead or None,
                        body.client_name or None, body.offering_type or None,
                        body.chargeability_pct, body.hours_to_move, body.from_period or None,
                        body.to_period or None, body.comments or None,
                        body.scenario_type or "assumption",
                        body.effectivization_date or None,
                    )

                    await _apply_side_effects(conn, body, effective_end_date, created_by, request_id)
                    ticket = await _fetch_full_ticket(conn, ticket_row["id"])

                    duration = int((time.monotonic() - start) * 1000)
                    logger.bind(action="tickets:create", request_id=request_id, duration_ms=duration).info(
                        "Ticket created", ticket_id=ticket["id"]
                    )
                    return ticket

                except ForecastException:
                    raise
                except asyncpg.UniqueViolationError:
                    logger.bind(request_id=request_id).warning("EID conflict", eid=body.eid)
                    raise ForecastException(AppError.EMPLOYEE_EID_TAKEN)
                except asyncpg.ForeignKeyViolationError as e:
                    logger.bind(request_id=request_id).warning("FK violation creating ticket", detail=str(e))
                    raise ForecastException(AppError.VALIDATION_ERROR, "Un EID referenciado no existe. Verificá people_lead y te_approver.")
                except Exception as e:
                    logger.bind(request_id=request_id).exception("Unexpected error creating ticket")
                    raise ForecastException(AppError.INTERNAL_ERROR, str(e))
    except ForecastException:
        raise
    except Exception as e:
        logger.bind(request_id=request_id).exception("Pool/transaction error creating ticket")
        raise ForecastException(AppError.INTERNAL_ERROR, str(e))


async def _apply_side_effects(conn, body: TicketCreate, effective_end_date, created_by, request_id: str):
    if body.type == "nj" and body.nj_name:
        nj_eid = body.eid_accenture or _normalize_nj_eid(body.nj_name)
        exists = await conn.fetchrow("SELECT eid FROM employees WHERE eid=$1", nj_eid)
        if not exists:
            pl_eid = None
            if body.people_lead:
                pl_row = await conn.fetchrow("SELECT eid FROM employees WHERE eid=$1", body.people_lead)
                pl_eid = body.people_lead if pl_row else None
            await conn.execute(
                """
                INSERT INTO employees (eid, name, country, location, cl, hire_date, new_joiner, active, people_lead)
                VALUES ($1,$2,$3,$3,$4,$5::text::date,TRUE,TRUE,$6)
                """,
                nj_eid, body.nj_name, body.location or None,
                body.cl, body.start_date or None, pl_eid,
            )
        if body.te_approver:
            te_row = await conn.fetchrow("SELECT eid FROM employees WHERE eid=$1", body.te_approver)
            if not te_row:
                raise ForecastException(
                    AppError.VALIDATION_ERROR,
                    f"TE Approver EID '{body.te_approver}' no existe. Ingresá un EID válido (ej. garcia.sofia).",
                )
            await conn.execute(
                """
                INSERT INTO forecast_update (eid, te_approver, updated_at)
                VALUES ($1, $2, NOW())
                ON CONFLICT (eid) DO UPDATE SET te_approver=EXCLUDED.te_approver, updated_at=NOW()
                """,
                nj_eid, body.te_approver,
            )


async def _apply_approval_side_effects(conn, ticket: dict, request_id: str):
    from datetime import date as _d

    t_type = ticket.get("type")
    eid = ticket.get("eid")
    if not eid:
        return

    if t_type in ("sick", "pto"):
        start_date = ticket.get("start_date")
        end_date   = ticket.get("end_date") or start_date
        if not start_date:
            return
        if isinstance(start_date, str):
            start_date = _d.fromisoformat(start_date)
        if isinstance(end_date, str):
            end_date = _d.fromisoformat(end_date)

        emp = await conn.fetchrow(
            "SELECT COALESCE(country, location) AS country FROM employees WHERE eid=$1", eid
        )
        country = emp["country"] if emp else None

        absence_type = "SICK" if t_type == "sick" else "PTO"

        if country:
            days_count = await conn.fetchval(
                """
                SELECT COUNT(*) FROM calendar
                WHERE country=$1 AND date BETWEEN $2 AND $3 AND is_working_day=TRUE
                """,
                country, start_date, end_date,
            )
        else:
            days_count = (end_date - start_date).days + 1

        await conn.execute(
            "DELETE FROM absences WHERE eid=$1 AND start_date=$2 AND end_date=$3 AND type=$4",
            eid, start_date, end_date, absence_type,
        )
        await conn.execute(
            "INSERT INTO absences (eid, type, start_date, end_date, hours) VALUES ($1,$2,$3,$4,$5)",
            eid, absence_type, start_date, end_date, days_count * 8,
        )

        logger.bind(request_id=request_id).info(
            "Absence inserted on ticket approval",
            eid=eid, type=absence_type, start=str(start_date), end=str(end_date), hours=days_count * 8,
        )

        if absence_type == "PTO":
            today = _d.today()
            next_abs = await conn.fetchrow(
                """
                SELECT start_date, end_date, hours FROM absences
                WHERE eid=$1 AND type='PTO' AND end_date >= $2
                ORDER BY start_date ASC
                LIMIT 1
                """,
                eid, today,
            )
            if next_abs:
                await conn.execute(
                    """
                    UPDATE forecast_update
                    SET next_pto=$2, next_pto_end=$3, next_pto_hours=$4, updated_at=NOW()
                    WHERE eid=$1
                    """,
                    eid,
                    next_abs["start_date"],
                    next_abs["end_date"],
                    next_abs["hours"] or 0,
                )

    elif t_type == "newproj":
        await conn.execute(
            """
            INSERT INTO forecast_update (eid, client, offering, roll_on, roll_off, chargeability_pct, updated_at)
            VALUES ($1,$2,$3,$4::text::date,$5::text::date,$6,NOW())
            ON CONFLICT (eid) DO UPDATE SET
                client=COALESCE($2,forecast_update.client),
                offering=COALESCE($3,forecast_update.offering),
                roll_on=COALESCE($4::text::date,forecast_update.roll_on),
                roll_off=COALESCE($5::text::date,forecast_update.roll_off),
                chargeability_pct=COALESCE($6,forecast_update.chargeability_pct),
                updated_at=NOW()
            """,
            eid,
            ticket.get("client_name"), ticket.get("offering_type"),
            ticket.get("start_date"), ticket.get("end_date"),
            ticket.get("chargeability_pct"),
        )
        logger.bind(request_id=request_id).info("newproj applied to forecast_update", eid=eid)

    elif t_type == "ongoing":
        await conn.execute(
            """
            UPDATE forecast_update SET
                roll_off=COALESCE($2::text::date, roll_off),
                chargeability_pct=COALESCE($3, chargeability_pct),
                updated_at=NOW()
            WHERE eid=$1
            """,
            eid, ticket.get("end_date"), ticket.get("chargeability_pct"),
        )
        logger.bind(request_id=request_id).info("ongoing applied to forecast_update", eid=eid)

    elif t_type == "baja":
        end_date = ticket.get("end_date")
        if end_date:
            await conn.execute(
                "UPDATE employees SET termination_date=$2::text::date, active=FALSE WHERE eid=$1", eid, end_date
            )
        logger.bind(request_id=request_id).info("baja applied to employees", eid=eid)


async def _recalculate_all_periods_for_eid(conn, eid: str, request_id: str):
    try:
        periods = await conn.fetch("SELECT period_name FROM periods ORDER BY start_date")
    except Exception as e:
        logger.bind(request_id=request_id).warning("Periods table unavailable, skipping recalculate", error=str(e))
        return
    logger.bind(request_id=request_id).debug(
        "Recalculating all periods for employee", eid=eid, count=len(periods)
    )
    for p in periods:
        try:
            await conn.execute("SELECT recalculate_forecast_period($1,$2)", eid, p["period_name"])
        except Exception as e:
            logger.bind(request_id=request_id).warning(f"Period recalculate failed | period={p['period_name']} | error={e}")


async def update(ticket_id: int, body: TicketUpdate, request_id: str) -> dict:
    updates = {k: v for k, v in body.model_dump(exclude_none=True).items() if v not in (None, "")}
    if not updates:
        raise ForecastException(AppError.VALIDATION_ERROR, "No valid fields provided")

    cols = list(updates.keys())
    vals = list(updates.values())
    set_clause = ", ".join(f"{c}=${i+1}" for i, c in enumerate(cols))
    vals.append(ticket_id)

    async with db.pool.acquire() as conn:
        try:
            row = await conn.fetchrow(
                f"UPDATE tickets SET {set_clause} WHERE id=${len(vals)} RETURNING id::text",
                *vals,
            )
            if not row:
                raise ForecastException(AppError.TICKET_NOT_FOUND)
            return await _fetch_full_ticket(conn, row["id"])
        except ForecastException:
            raise
        except Exception as e:
            logger.bind(request_id=request_id).exception("Unexpected error updating ticket")
            raise ForecastException(AppError.INTERNAL_ERROR, str(e))


async def approve_ticket(ticket_id: int, request_id: str) -> dict:
    async with db.pool.acquire() as conn:
        try:
            current = await conn.fetchrow("SELECT status FROM tickets WHERE id=$1", ticket_id)
            if not current:
                raise ForecastException(AppError.TICKET_NOT_FOUND)
            if current["status"] != "Open":
                raise ForecastException(AppError.TICKET_INVALID_STATUS)
            row = await conn.fetchrow(
                "UPDATE tickets SET status='Approved' WHERE id=$1 RETURNING id::text, eid",
                ticket_id,
            )
            if not row:
                raise ForecastException(AppError.TICKET_NOT_FOUND)
            ticket = await _fetch_full_ticket(conn, row["id"])
            await _apply_approval_side_effects(conn, ticket, request_id)
            if row["eid"]:
                await _recalculate_all_periods_for_eid(conn, row["eid"], request_id)
            return ticket
        except ForecastException:
            raise
        except Exception as e:
            logger.bind(request_id=request_id).exception("Unexpected error approving ticket")
            raise ForecastException(AppError.INTERNAL_ERROR, str(e))


async def reject_ticket(ticket_id: int, reason: str, request_id: str) -> dict:
    if not reason or not reason.strip():
        raise ForecastException(AppError.VALIDATION_ERROR, "Rejection reason is required")
    async with db.pool.acquire() as conn:
        try:
            current = await conn.fetchrow("SELECT status FROM tickets WHERE id=$1", ticket_id)
            if not current:
                raise ForecastException(AppError.TICKET_NOT_FOUND)
            if current["status"] != "Open":
                raise ForecastException(AppError.TICKET_INVALID_STATUS)
            row = await conn.fetchrow(
                "UPDATE tickets SET status='Rejected', rejection_reason=$2 WHERE id=$1 RETURNING id::text",
                ticket_id, reason,
            )
            if not row:
                raise ForecastException(AppError.TICKET_NOT_FOUND)
            return await _fetch_full_ticket(conn, row["id"])
        except ForecastException:
            raise
        except Exception as e:
            logger.bind(request_id=request_id).exception("Unexpected error rejecting ticket")
            raise ForecastException(AppError.INTERNAL_ERROR, str(e))


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
                    "UPDATE tickets SET status='Approved', detail=COALESCE(detail,'')||' · EID: '||$1"
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
