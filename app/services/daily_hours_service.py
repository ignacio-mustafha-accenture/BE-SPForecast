"""
Recalcula employee_daily_hours para UN empleado puntual. Es la misma logica
de create_daily_hours.py, acotada a un eid, pensada para llamarse dentro de
la transaccion de aprobacion de un ticket.

Por que hace falta: hasta ahora employee_daily_hours solo se actualizaba
corriendo create_daily_hours.py a mano. Cada aprobacion de ticket actualizaba
chargeability_blocks y forecast_periods correctamente, pero la vista Diario
seguia mostrando los valores de la ultima vez que alguien se acordo de correr
el script completo. Encontrado el 2026-09-08 con un ticket de Jesica Scotta:
el bloque de cargabilidad estaba bien, pero el diario seguia con datos de
antes del fix.

Correr esto por empleado es rapido (una persona, no 116) y se puede llamar en
cada aprobacion sin agregar latencia perceptible ni infraestructura de
scheduling.
"""

from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP

import asyncpg
from loguru import logger

from app.country import to_iso

TWO = Decimal('0.01')


def _date_range(start: date, end: date):
    cur = start
    while cur <= end:
        yield cur
        cur += timedelta(days=1)


def _is_weekday(d: date) -> bool:
    return d.weekday() < 5


def _workdays(start: date, end: date, holidays: set) -> list:
    return [d for d in _date_range(start, end) if _is_weekday(d) and d not in holidays]


def _distribute(total: Decimal, days: int) -> list:
    if days <= 0:
        return []
    cents = int((Decimal(total) * 100).to_integral_value(ROUND_HALF_UP))
    base, remainder = divmod(cents, days)
    return [
        Decimal(base + (1 if i < remainder else 0)) / 100
        for i in range(days)
    ]


async def recalculate_daily_hours_for_eid(conn: asyncpg.Connection, eid: str, request_id: str = ""):
    """Recalcula employee_daily_hours solo para eid, sobre todo el horizonte
    de periods. Misma logica que create_daily_hours.py: SAH en 0 fines de
    semana/feriados/ausencias, CHG HL solo si el dia cae dentro del rango real
    del bloque de cargabilidad (start_date/end_date del bloque, no del
    periodo), y PPA repartido dia a dia sobre los workdays del periodo origen.
    """
    log = logger.bind(request_id=request_id)

    emp = await conn.fetchrow(
        """
        SELECT e.eid, e.country, e.location
        FROM employees e
        WHERE e.eid = $1
          AND (e.termination_date IS NULL OR e.termination_date > CURRENT_DATE)
        """,
        eid,
    )
    if not emp:
        log.info(f"recalculate_daily_hours_for_eid: {eid} inactivo o inexistente, se omite")
        return 0

    periods = await conn.fetch(
        "SELECT period_name, start_date, end_date FROM periods ORDER BY start_date"
    )
    if not periods:
        log.warning("recalculate_daily_hours_for_eid: sin periodos en la DB")
        return 0

    horizon_start = periods[0]["start_date"]
    horizon_end = periods[-1]["end_date"]

    country = to_iso(emp["country"], emp["location"])
    holidays_raw = await conn.fetch(
        "SELECT date FROM holidays WHERE country = $1 AND date BETWEEN $2 AND $3",
        country, horizon_start, horizon_end,
    )
    h_set = {h["date"] for h in holidays_raw}

    absences_raw = await conn.fetch(
        "SELECT start_date, end_date FROM absences WHERE eid = $1 AND start_date <= $2 AND end_date >= $3",
        eid, horizon_end, horizon_start,
    )
    abs_set = set()
    for a in absences_raw:
        for d in _date_range(a["start_date"], a["end_date"]):
            abs_set.add(d)

    blocks_raw = await conn.fetch(
        """
        SELECT period_name, chargeability_pct, scenario_type, start_date, end_date
        FROM chargeability_blocks WHERE eid = $1
        """,
        eid,
    )
    pct_map: dict = {}
    for b in blocks_raw:
        key = b["period_name"]
        if key not in pct_map:
            pct_map[key] = {"hl": Decimal("0"), "sl": Decimal("0"), "hl_start": None, "hl_end": None}
        if b["scenario_type"] == "effective":
            pct_map[key]["hl"] = Decimal(str(b["chargeability_pct"]))
            pct_map[key]["hl_start"] = b["start_date"]
            pct_map[key]["hl_end"] = b["end_date"]
        else:
            pct_map[key]["sl"] = Decimal(str(b["chargeability_pct"]))

    ppa_raw = await conn.fetch(
        "SELECT from_period, to_period, hours FROM ppa_log WHERE eid = $1",
        eid,
    )
    period_map = {p["period_name"]: p for p in periods}
    ppa_by_date: dict = {}
    for p in ppa_raw:
        for pname, sign in ((p["to_period"], 1), (p["from_period"], -1)):
            period = period_map.get(pname)
            if not period:
                continue
            wdays = _workdays(period["start_date"], period["end_date"], h_set)
            if not wdays:
                continue
            amounts = _distribute(Decimal(p["hours"]), len(wdays))
            for d, amount in zip(wdays, amounts):
                ppa_by_date[d] = ppa_by_date.get(d, Decimal("0")) + amount * sign

    rows = []
    for period in periods:
        pname = period["period_name"]
        pcts = pct_map.get(pname, {"hl": Decimal("0"), "sl": Decimal("0"), "hl_start": None, "hl_end": None})
        pct_hl, pct_sl = pcts["hl"], pcts["sl"]
        hl_start, hl_end = pcts["hl_start"], pcts["hl_end"]

        for d in _date_range(period["start_date"], period["end_date"]):
            is_holiday = d in h_set
            is_weekend = not _is_weekday(d)
            is_absent = d in abs_set

            if is_weekend or is_holiday or is_absent:
                sah = chg_hl = chg_sl = Decimal("0")
            else:
                sah = Decimal("8")
                in_block = True
                if hl_start and d < hl_start:
                    in_block = False
                if hl_end and d > hl_end:
                    in_block = False
                chg_hl = (sah * pct_hl / 100).quantize(TWO, ROUND_HALF_UP) if in_block else Decimal("0")
                chg_sl = (sah * pct_sl / 100).quantize(TWO, ROUND_HALF_UP)

            chg_ppa = ppa_by_date.get(d, Decimal("0")).quantize(TWO, ROUND_HALF_UP)
            rows.append((eid, d, float(sah), float(chg_hl), float(chg_sl), float(chg_ppa)))

    if rows:
        await conn.executemany(
            """
            INSERT INTO employee_daily_hours (eid, date, sah, chg_hl, chg_sl, chg_ppa, updated_at)
            VALUES ($1, $2, $3, $4, $5, $6, NOW())
            ON CONFLICT (eid, date) DO UPDATE SET
                sah        = EXCLUDED.sah,
                chg_hl     = EXCLUDED.chg_hl,
                chg_sl     = EXCLUDED.chg_sl,
                chg_ppa    = EXCLUDED.chg_ppa,
                updated_at = NOW()
            """,
            rows,
        )

    log.info(f"recalculate_daily_hours_for_eid: {eid} -> {len(rows)} filas actualizadas")
    return len(rows)