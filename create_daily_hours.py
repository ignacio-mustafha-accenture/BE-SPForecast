import asyncio
import asyncpg
import logging
import os
import sys
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), '.env'))

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%H:%M:%S',
)
log = logging.getLogger(__name__)

AZURE = dict(
    host=os.getenv('DB_HOST'),
    port=int(os.getenv('DB_PORT', 5432)),
    user=os.getenv('DB_USER'),
    password=os.getenv('DB_PASSWORD'),
    database=os.getenv('DB_NAME'),
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

    holidays_raw = await conn.fetch("""
        SELECT country, date FROM holidays
        WHERE date BETWEEN $1 AND $2
    """, horizon_start, horizon_end)

    holidays_by_country: dict = {}
    for h in holidays_raw:
        holidays_by_country.setdefault(h['country'], set()).add(h['date'])

    absences_raw = await conn.fetch("""
        SELECT eid, start_date, end_date
        FROM absences
        WHERE start_date <= $2 AND end_date >= $1
    """, horizon_start, horizon_end)

    absent_days: dict = {}
    for a in absences_raw:
        for d in date_range(a['start_date'], a['end_date']):
            absent_days.setdefault(a['eid'], set()).add(d)

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

                if is_weekend or is_holiday or is_absent:
                    sah    = Decimal('0')
                    chg_hl = Decimal('0')
                    chg_sl = Decimal('0')
                else:
                    sah = Decimal('8')

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
    if not all([AZURE['host'], AZURE['user'], AZURE['password'], AZURE['database']]):
        log.error('Variables de entorno de DB no encontradas. Verificar .env')
        sys.exit(1)

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