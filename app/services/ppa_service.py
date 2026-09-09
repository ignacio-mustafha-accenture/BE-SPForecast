import time
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP
from loguru import logger
import app.db as db
from app.country import to_iso
from app.errors import AppError, ForecastException
from app.models.ppa import PPACreate


def _date_range(start: date, end: date):
    cur = start
    while cur <= end:
        yield cur
        cur += timedelta(days=1)


def _is_weekday(d: date) -> bool:
    return d.weekday() < 5


def _distribute(total: Decimal, days: int) -> list[Decimal]:
    cents = int((Decimal(total) * 100).to_integral_value(ROUND_HALF_UP))
    base, remainder = divmod(cents, days)
    return [
        Decimal(base + (1 if i < remainder else 0)) / 100
        for i in range(days)
    ]


async def _get_workdays(conn, period_name: str, country: str) -> list[date]:
    period = await conn.fetchrow(
        "SELECT start_date, end_date FROM periods WHERE period_name=$1",
        period_name,
    )
    if not period:
        return []
    holidays = await conn.fetch(
        "SELECT date FROM holidays WHERE country=$1 AND date BETWEEN $2 AND $3",
        country, period["start_date"], period["end_date"],
    )
    holiday_set = {h["date"] for h in holidays}
    return [
        d for d in _date_range(period["start_date"], period["end_date"])
        if _is_weekday(d) and d not in holiday_set
    ]


async def _apply_ppa_to_daily_hours(conn, eid, from_period, to_period, hours, country):
    """Reparte el PPA dia a dia en employee_daily_hours.

    Esta tabla ya NO alimenta la vista global (state_service y totals_service leen
    forecast_periods). Se mantiene porque la granularidad diaria es lo que necesita la
    vista Diaria; el impacto en los totales lo aplica _apply_ppa_to_forecast_periods.
    """
    for period_name, sign in [(from_period, -1), (to_period, 1)]:
        workdays = await _get_workdays(conn, period_name, country)
        if not workdays:
            logger.warning(f"No workdays found for period {period_name}, skipping PPA distribution")
            continue
        amounts = _distribute(Decimal(hours), len(workdays))
        await conn.executemany(
            """
            INSERT INTO employee_daily_hours (eid, date, sah, chg_hl, chg_sl, chg_ppa, updated_at)
            VALUES ($1, $2, 0, 0, 0, $3, NOW())
            ON CONFLICT (eid, date) DO UPDATE SET
                chg_ppa    = employee_daily_hours.chg_ppa + $3,
                updated_at = NOW()
            """,
            [(eid, d, amount * sign) for d, amount in zip(workdays, amounts)],
        )
    logger.info("PPA applied to daily hours", eid=eid, from_period=from_period, to_period=to_period, hours=hours)


# Acumula las horas del PPA en forecast_periods.chg_cascadeadas y rederiva las columnas
# que dependen de ella, con las mismas formulas que usa la vista global:
#   chg        = chg_hl + chg_sl + chg_cascadeadas
#   chg_pct    = chg / sah * 100
#   chg_pct_hl = (chg_hl + chg_cascadeadas) / sah * 100
# chg_sl y chg_pct_sl no se tocan: el PPA no es soft lock.
# El $3 es el delta con signo, y se suma dentro del mismo UPDATE para que el
# read-modify-write quede serializado por el lock de fila de Postgres.
_UPSERT_PPA_FP = """
    INSERT INTO forecast_periods (
        eid, period_name, chg, sah, chg_pct,
        chg_hl, chg_sl, chg_cascadeadas, absence_hours, chg_pct_hl, chg_pct_sl
    )
    VALUES ($1, $2, $3, 0, 0, 0, 0, $3, 0, 0, 0)
    ON CONFLICT (eid, period_name) DO UPDATE SET
        chg_cascadeadas = COALESCE(forecast_periods.chg_cascadeadas, 0) + $3,
        chg             = COALESCE(forecast_periods.chg_hl, 0)
                        + COALESCE(forecast_periods.chg_sl, 0)
                        + COALESCE(forecast_periods.chg_cascadeadas, 0) + $3,
        chg_pct         = CASE WHEN COALESCE(forecast_periods.sah, 0) > 0
                               THEN ROUND((COALESCE(forecast_periods.chg_hl, 0)
                                         + COALESCE(forecast_periods.chg_sl, 0)
                                         + COALESCE(forecast_periods.chg_cascadeadas, 0) + $3)
                                          / forecast_periods.sah * 100, 2)
                               ELSE 0 END,
        chg_pct_hl      = CASE WHEN COALESCE(forecast_periods.sah, 0) > 0
                               THEN ROUND((COALESCE(forecast_periods.chg_hl, 0)
                                         + COALESCE(forecast_periods.chg_cascadeadas, 0) + $3)
                                          / forecast_periods.sah * 100, 2)
                               ELSE 0 END
"""


async def _apply_ppa_to_forecast_periods(conn, eid, from_period, to_period, hours):
    """Impacta el PPA en forecast_periods, que es lo que lee la vista global.

    Decision sobre from_period: se DESCUENTA del origen y se SUMA al destino. No es una
    eleccion nueva, es la semantica que ya tenian los dos caminos que existian:
    _apply_ppa_to_daily_hours reparte con signo -1 en from_period y +1 en to_period, y el
    stored proc recalculate_forecast_period calcula su ajuste como
    SUM(CASE WHEN to_period = p THEN hours WHEN from_period = p THEN -hours END).
    Se replica tal cual para no cambiar la semantica de negocio: el PPA mueve horas, no
    las crea, asi que el neto sobre el total del empleado es cero.

    Ojo: si from_period y to_period son el mismo periodo los dos deltas se cancelan, que
    es el resultado correcto. En ppa_log hay filas asi (por ejemplo Feb-P1 -> Feb-P1).
    """
    for period_name, sign in [(from_period, -1), (to_period, 1)]:
        delta = Decimal(hours) * sign
        result = await conn.execute(_UPSERT_PPA_FP, eid, period_name, delta)
        # Si no se escribio ninguna fila el total del periodo quedaria sin el PPA y la
        # aprobacion mentiria. Se corta la transaccion en vez de aprobar a medias.
        if result and result.split()[-1] == "0":
            raise ForecastException(
                AppError.DB_ERROR,
                f"No se pudo impactar el PPA en el periodo {period_name}",
            )
    logger.info(
        "PPA applied to forecast_periods",
        eid=eid, from_period=from_period, to_period=to_period, hours=hours,
    )


async def list_ppa(eid=None, from_period=None, status=None, page=1, page_size=25):
    conditions, params = [], []
    if eid:
        params.append(f"%{eid}%")
        conditions.append(f"p.eid ILIKE ${len(params)}")
    if from_period:
        params.append(from_period)
        conditions.append(f"p.from_period = ${len(params)}")
    if status:
        params.append(status)
        conditions.append(f"p.status = ${len(params)}")
    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    offset = (page - 1) * page_size
    params.append(page_size)
    limit_idx = len(params)
    params.append(offset)
    offset_idx = len(params)
    async with db.pool.acquire() as conn:
        rows = await conn.fetch(f"""
            SELECT p.id::text AS id, p.eid, e.name,
                   p.from_period AS "from", p.to_period AS "to",
                   p.hours AS hs, p.reason, p.status, p.rejection_reason,
                   TO_CHAR(p.created_at,'DD/MM/YY') AS date,
                   COALESCE(e.country, e.location) AS country,
                   COUNT(*) OVER () AS _total
            FROM ppa_log p LEFT JOIN employees e ON p.eid=e.eid
            {where}
            ORDER BY p.created_at DESC
            LIMIT ${limit_idx} OFFSET ${offset_idx}
        """, *params)
    total = int(rows[0]["_total"]) if rows else 0
    pages = -(-total // page_size) if page_size > 0 else 0
    items = [{k: v for k, v in dict(r).items() if k != "_total"} for r in rows]
    return {"items": items, "total": total, "page": page, "page_size": page_size, "pages": pages}


async def create(body: PPACreate, created_by: str, request_id: str) -> dict:
    logger.bind(action="ppa:create", request_id=request_id).info(
        "Creating PPA (pending)", eid=body.eid, from_period=body.from_period, to_period=body.to_period,
    )
    async with db.pool.acquire() as conn:
        async with conn.transaction():
            emp = await conn.fetchrow("SELECT eid, country, location FROM employees WHERE eid=$1", body.eid)
            if not emp:
                raise ForecastException(AppError.EMPLOYEE_NOT_FOUND)
            for period_name in (body.from_period, body.to_period):
                period = await conn.fetchrow("SELECT period_name FROM periods WHERE period_name=$1", period_name)
                if not period:
                    raise ForecastException(AppError.PERIOD_NOT_FOUND, f"Periodo {period_name} no encontrado")
            total_hours = (body.hours_chargeable or 0) + (body.hours_standard or 0)
            row = await conn.fetchrow(
                """
                INSERT INTO ppa_log (eid, from_period, to_period, hours, hours_chargeable, hours_standard, reason, created_at, created_by, status)
                VALUES ($1, $2, $3, $4, $5, $6, $7, NOW(), $8, 'pending')
                RETURNING id::text
                """,
                body.eid, body.from_period, body.to_period,
                total_hours, body.hours_chargeable, body.hours_standard,
                body.reason or None, created_by or None,
            )
    return {"ok": True, "id": row["id"]}


async def approve(ppa_id: str, approved_by: str, request_id: str) -> dict:
    logger.bind(action="ppa:approve", request_id=request_id).info("Approving PPA", ppa_id=ppa_id)
    start = time.monotonic()
    async with db.pool.acquire() as conn:
        # Una sola transaccion para el impacto en forecast_periods, el reparto diario y el
        # cambio de status: o se ve el PPA en los totales o el PPA sigue pendiente.
        async with conn.transaction():
            # FOR UPDATE OF p toma el lock de la fila de ppa_log antes de leer el status,
            # asi dos aprobaciones simultaneas del mismo PPA no lo aplican dos veces.
            ppa = await conn.fetchrow(
                """
                SELECT p.id, p.eid, p.from_period, p.to_period, p.hours, p.status,
                       COALESCE(e.country, e.location) AS country
                FROM ppa_log p LEFT JOIN employees e ON p.eid = e.eid
                WHERE p.id = $1
                FOR UPDATE OF p
                """,
                int(ppa_id),
            )
            if not ppa:
                raise ForecastException(AppError.NOT_FOUND, "PPA no encontrado")
            if ppa["status"] != "pending":
                raise ForecastException(AppError.VALIDATION_ERROR, "El PPA no esta pendiente")
            country = to_iso(ppa["country"], ppa["country"])
            # Primero forecast_periods, que es lo que alimenta la vista global
            await _apply_ppa_to_forecast_periods(
                conn, eid=ppa["eid"], from_period=ppa["from_period"],
                to_period=ppa["to_period"], hours=ppa["hours"],
            )
            # Y despues el detalle diario, que solo consume la vista Diaria
            await _apply_ppa_to_daily_hours(
                conn, eid=ppa["eid"], from_period=ppa["from_period"],
                to_period=ppa["to_period"], hours=ppa["hours"], country=country,
            )
            await conn.execute(
                "UPDATE ppa_log SET status='approved', resolved_at=NOW(), resolved_by=$1 WHERE id=$2",
                approved_by, int(ppa_id),
            )
    duration = int((time.monotonic() - start) * 1000)
    logger.bind(action="ppa:approve", request_id=request_id, duration_ms=duration).info("PPA approved", ppa_id=ppa_id)
    return {"ok": True}


async def reject(ppa_id: str, reason: str, rejected_by: str, request_id: str) -> dict:
    logger.bind(action="ppa:reject", request_id=request_id).info("Rejecting PPA", ppa_id=ppa_id)
    async with db.pool.acquire() as conn:
        ppa = await conn.fetchrow("SELECT id, status FROM ppa_log WHERE id=$1", int(ppa_id))
        if not ppa:
            raise ForecastException(AppError.NOT_FOUND, "PPA no encontrado")
        if ppa["status"] != "pending":
            raise ForecastException(AppError.VALIDATION_ERROR, "El PPA no esta pendiente")
        await conn.execute(
            "UPDATE ppa_log SET status= 'rejected', rejection_reason=$1, resolved_at=NOW(), resolved_by=$2 WHERE id=$3",
            reason, rejected_by, int(ppa_id),
        )
    return {"ok": True}
