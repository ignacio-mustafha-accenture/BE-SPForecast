"""
create_daily_hours.py

Crea y puebla la tabla employee_daily_hours en Azure PostgreSQL.

Logica oficial:
  SAH     : 8hs en dias habiles (L-V, sin feriados). 0 en vacaciones y enfermedad.
  CHG HL  : SAH * pct_hl/100  (solo dias dentro del bloque efectivo)
  CHG SL  : SAH * pct_sl/100
  CHG PPA : horas cascadeadas distribuidas por dia habil del periodo.
            Positivo en periodo destino, negativo en periodo origen.

Formulas:
  CHG% HL = (CHG_HL + CHG_PPA) / SAH * 100
  CHG% SL = (CHG_HL + CHG_SL + CHG_PPA) / SAH * 100

Uso: python create_daily_hours.py
"""

import asyncio
import asyncpg
import logging
import sys
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%H:%M:%S',
)
log = logging.getLogger(__name__)

AZURE = dict(
    host='forecast-db-dev.postgres.database.azure.com',
    port=5432,
    database='forecast',
    user='forecastadmin',
    password='Forecast2026@Secure!',
    ssl='require',
)

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS employee_daily_hours (
    id          SERIAL        PRIMARY KEY,
    eid         VARCHAR(50)   NOT NULL,
    date        DATE          NOT NULL,
    sah         NUMERIC(6,2)  NOT NULL DEFAULT 0,
    chg_hl      NUMERIC(6,2)  NOT NULL DEFAULT 0,
    chg_sl      NUMERIC(6,2)  NOT NULL DEFAULT 0,
    chg_ppa     NUMERIC(6,2)  NOT NULL DEFAULT 0,
    updated_at  TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    UNIQUE (eid, date)
);
CREATE INDEX IF NOT EXISTS idx_edh_eid      ON employee_daily_hours (eid);
CREATE INDEX IF NOT EXISTS idx_edh_date     ON employee_daily_hours (date);
CREATE INDEX IF NOT EXISTS idx_edh_eid_date ON employee_daily_hours (eid, date);
"""

TWO = Decimal('0.01')


def date_range(start: date, end: date):
    cur = start
    while cur <= end:
        yield cur
        cur += timedelta(days=1)


def is_weekday(d: date) -> bool:
    return d.weekday() < 5


def workdays(start: date, end: date, holidays: set) -> list:
    return [d for d in date_range(start, end) if is_weekday(d) and d not in holidays]


async def build_rows(conn: asyncpg.Connection) -> list:
    # ── empleados activos con roll_on/roll_off desde chargeability_blocks ──
    employees = await conn.fetch("""
        SELECT e.eid,
               e.location AS country,
               MIN(cb.start_date) AS roll_on,
               MAX(cb.end_date)   AS roll_off
        FROM employees e
        LEFT JOIN chargeability_blocks cb
               ON cb.eid = e.eid AND cb.scenario_type = 'effective'
        WHERE e.termination_date IS NULL OR e.termination_date > CURRENT_DATE
        GROUP BY e.eid, e.location
    """)
    log.info(f'  {len(employees)} empleados activos')
    if not employees:
        return []

    # ── periodos ───────────────────────────────────────────────────────────
    periods = await conn.fetch("""
        SELECT period_name, start_date, end_date
        FROM periods ORDER BY start_date
    """)
    if not periods:
        log.warning('Sin periodos en la DB.')
        return []

    horizon_start = periods[0]['start_date']
    horizon_end   = periods[-1]['end_date']
    period_map    = {p['period_name']: p for p in periods}
    log.info(f'  Horizonte: {horizon_start} -> {horizon_end}')

    # ── feriados por pais ──────────────────────────────────────────────────
    holidays_raw = await conn.fetch("""
        SELECT country, date FROM holidays
        WHERE date BETWEEN $1 AND $2
    """, horizon_start, horizon_end)

    holidays_by_country: dict = {}
    for h in holidays_raw:
        holidays_by_country.setdefault(h['country'], set()).add(h['date'])

    # ── ausencias aprobadas (PTO + SICK) → SAH, CHG_HL y CHG_SL a 0 ──────
    absences_raw = await conn.fetch("""
        SELECT eid, start_date, end_date
        FROM absences
        WHERE start_date <= $2 AND end_date >= $1
    """, horizon_start, horizon_end)

    absent_days: dict = {}
    for a in absences_raw:
        for d in date_range(a['start_date'], a['end_date']):
            absent_days.setdefault(a['eid'], set()).add(d)

    # ── bloques de cargabilidad por (eid, period_name) ────────────────────
    blocks_raw = await conn.fetch("""
        SELECT eid, period_name, chargeability_pct, scenario_type,
               start_date, end_date
        FROM chargeability_blocks
    """)

    pct_map: dict = {}
    for b in blocks_raw:
        key = (b['eid'], b['period_name'])
        if key not in pct_map:
            pct_map[key] = {'hl': Decimal('0'), 'sl': Decimal('0'),
                            'hl_start': None, 'hl_end': None}
        if b['scenario_type'] == 'effective':
            pct_map[key]['hl']       = Decimal(str(b['chargeability_pct']))
            pct_map[key]['hl_start'] = b['start_date']
            pct_map[key]['hl_end']   = b['end_date']
        elif b['scenario_type'] == 'assumption':
            pct_map[key]['sl'] = Decimal(str(b['chargeability_pct']))

    # ── PPA: distribuir horas por dia habil ───────────────────────────────
    # Destino: +horas/dia | Origen: -horas/dia
    ppas = await conn.fetch("SELECT eid, from_period, to_period, hours FROM ppa_log")

    ppa_by_eid_date: dict = {}
    for ppa in ppas:
        hours = Decimal(str(ppa['hours']))
        eid   = ppa['eid']
        emp   = next((e for e in employees if e['eid'] == eid), None)
        country = emp['country'] if emp else 'AR'
        h_set = holidays_by_country.get(country, set())

        for period_name, sign in [(ppa['to_period'], 1), (ppa['from_period'], -1)]:
            period = period_map.get(period_name)
            if not period:
                continue
            wdays = workdays(period['start_date'], period['end_date'], h_set)
            if not wdays:
                continue
            daily = (hours / len(wdays) * sign).quantize(TWO, ROUND_HALF_UP)
            for d in wdays:
                key = (eid, d)
                ppa_by_eid_date[key] = ppa_by_eid_date.get(key, Decimal('0')) + daily

    # ── construir filas ────────────────────────────────────────────────────
    log.info('Calculando horas diarias...')
    rows = []

    for emp in employees:
        eid     = emp['eid']
        country = emp['country'] or 'AR'
        h_set   = holidays_by_country.get(country, set())
        abs_set = absent_days.get(eid, set())

        for period in periods:
            pname = period['period_name']
            pcts  = pct_map.get((eid, pname), {
                'hl': Decimal('0'), 'sl': Decimal('0'),
                'hl_start': None,   'hl_end': None,
            })
            pct_hl   = pcts['hl']
            pct_sl   = pcts['sl']
            hl_start = pcts['hl_start']
            hl_end   = pcts['hl_end']

            for d in date_range(period['start_date'], period['end_date']):
                is_holiday = d in h_set
                is_weekend = not is_weekday(d)
                is_absent  = d in abs_set

                # SAH y CHG a 0 en fin de semana, feriado o ausencia
                if is_weekend or is_holiday or is_absent:
                    sah    = Decimal('0')
                    chg_hl = Decimal('0')
                    chg_sl = Decimal('0')
                else:
                    sah = Decimal('8')

                    # CHG HL: solo dentro del rango del bloque efectivo
                    in_block = True
                    if hl_start and d < hl_start: in_block = False
                    if hl_end   and d > hl_end:   in_block = False

                    chg_hl = (sah * pct_hl / 100).quantize(TWO, ROUND_HALF_UP) if in_block else Decimal('0')
                    chg_sl = (sah * pct_sl / 100).quantize(TWO, ROUND_HALF_UP)

                chg_ppa = ppa_by_eid_date.get((eid, d), Decimal('0')).quantize(TWO, ROUND_HALF_UP)

                rows.append((
                    eid,
                    d,
                    float(sah),
                    float(chg_hl),
                    float(chg_sl),
                    float(chg_ppa),
                ))

    log.info(f'  {len(rows)} filas calculadas')
    return rows


async def run():
    log.info('=== Generando employee_daily_hours en Azure ===')
    conn = await asyncpg.connect(**AZURE)

    try:
        log.info('Creando tabla e indices...')
        await conn.execute(CREATE_TABLE_SQL)
        log.info('  OK')

        rows = await build_rows(conn)
        if not rows:
            log.warning('Sin filas. Verificar que haya empleados y periodos.')
            return

        log.info('Insertando (upsert por lotes de 1000)...')
        batch_size = 1000
        inserted   = 0

        for i in range(0, len(rows), batch_size):
            batch = rows[i:i + batch_size]
            await conn.executemany("""
                INSERT INTO employee_daily_hours
                    (eid, date, sah, chg_hl, chg_sl, chg_ppa, updated_at)
                VALUES ($1, $2, $3, $4, $5, $6, NOW())
                ON CONFLICT (eid, date) DO UPDATE SET
                    sah        = EXCLUDED.sah,
                    chg_hl     = EXCLUDED.chg_hl,
                    chg_sl     = EXCLUDED.chg_sl,
                    chg_ppa    = EXCLUDED.chg_ppa,
                    updated_at = NOW()
            """, batch)
            inserted += len(batch)
            log.info(f'  {inserted}/{len(rows)}')

        log.info('=== Completado ===')

    finally:
        await conn.close()


if __name__ == '__main__':
    try:
        asyncio.run(run())
    except Exception as e:
        log.error(f'Error fatal: {e}')
        sys.exit(1)