import asyncio
import asyncpg
import logging
import os
import sys
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), '.env'))

logging.basicConfig(level=logging.INFO, format='%(message)s')
log = logging.getLogger(__name__)

AZURE = dict(
    host=os.getenv('DB_HOST'),
    port=int(os.getenv('DB_PORT', 5432)),
    user=os.getenv('DB_USER'),
    password=os.getenv('DB_PASSWORD'),
    database=os.getenv('DB_NAME'),
    ssl='require',
)


def date_range(start: date, end: date):
    cur = start
    while cur <= end:
        yield cur
        cur += timedelta(days=1)


def workdays(start: date, end: date, holidays: set) -> list:
    return [d for d in date_range(start, end) if d.weekday() < 5 and d not in holidays]


def distribute(total: Decimal, days: int) -> list:
    cents = int((Decimal(total) * 100).to_integral_value(ROUND_HALF_UP))
    base, remainder = divmod(cents, days)
    return [Decimal(base + (1 if i < remainder else 0)) / 100 for i in range(days)]


async def run():
    conn = await asyncpg.connect(**AZURE)
    try:
        log.info('=== 1. employees: country vs location ===')
        rows = await conn.fetch("""
            SELECT country, location, COUNT(*) AS n
            FROM employees GROUP BY country, location ORDER BY n DESC
        """)
        for r in rows:
            log.info(f"  country={r['country']!r:<20} location={r['location']!r:<10} n={r['n']}")

        log.info('=== 2. holidays: valores de country ===')
        rows = await conn.fetch("""
            SELECT country, COUNT(*) AS n FROM holidays GROUP BY country ORDER BY country
        """)
        for r in rows:
            log.info(f"  country={r['country']!r:<20} feriados={r['n']}")

        log.info('=== 3. cuantos empleados matchean feriados con cada estrategia ===')
        r = await conn.fetchrow("""
            SELECT
              COUNT(*) FILTER (WHERE EXISTS (
                SELECT 1 FROM holidays h WHERE h.country = COALESCE(e.country, e.location)
              )) AS live_match,
              COUNT(*) FILTER (WHERE EXISTS (
                SELECT 1 FROM holidays h WHERE h.country = e.location
              )) AS batch_match,
              COUNT(*) AS total
            FROM employees e
        """)
        log.info(f"  servicio en vivo COALESCE(country,location): {r['live_match']}/{r['total']}")
        log.info(f"  batch location:                              {r['batch_match']}/{r['total']}")

        log.info('=== 4. ppa_log ===')
        ppas = await conn.fetch("""
            SELECT p.id, p.eid, p.from_period, p.to_period, p.hours,
                   e.country, e.location
            FROM ppa_log p LEFT JOIN employees e ON e.eid = p.eid
            ORDER BY p.id
        """)
        log.info(f"  {len(ppas)} registros")

        periods = {
            p['period_name']: p
            for p in await conn.fetch('SELECT period_name, start_date, end_date FROM periods')
        }

        for p in ppas:
            live_country = p['country'] or p['location'] or 'AR'
            batch_country = p['location'] or 'AR'
            log.info(f"  --- ppa id={p['id']} eid={p['eid']} {p['from_period']} -> {p['to_period']} "
                     f"hours={p['hours']}")
            log.info(f"      pais segun servicio={live_country!r}  segun batch={batch_country!r}")

            for label, ctry in (('live', live_country), ('batch', batch_country)):
                hs = {h['date'] for h in await conn.fetch(
                    'SELECT date FROM holidays WHERE country=$1', ctry
                )}
                for pname in (p['from_period'], p['to_period']):
                    per = periods.get(pname)
                    if not per:
                        log.info(f"      [{label}] periodo {pname} NO EXISTE")
                        continue
                    wd = workdays(per['start_date'], per['end_date'], hs)
                    log.info(f"      [{label}] {pname}: {len(wd)} dias habiles")

            for pname, sign in ((p['from_period'], -1), (p['to_period'], 1)):
                per = periods.get(pname)
                if not per:
                    continue
                stored = await conn.fetchrow("""
                    SELECT COALESCE(SUM(chg_ppa), 0) AS total
                    FROM employee_daily_hours
                    WHERE eid = $1 AND date BETWEEN $2 AND $3
                """, p['eid'], per['start_date'], per['end_date'])
                esperado = Decimal(str(p['hours'])) * sign
                log.info(f"      GUARDADO en {pname}: {stored['total']}  (esperado {esperado})")

        log.info('=== 5. chg_ppa distinto de cero en la tabla ===')
        r = await conn.fetchrow("""
            SELECT COUNT(*) AS filas, COALESCE(SUM(chg_ppa), 0) AS suma
            FROM employee_daily_hours WHERE chg_ppa <> 0
        """)
        log.info(f"  filas={r['filas']} suma_global={r['suma']} (deberia ser 0 si origen y destino cierran)")

        log.info('=== 6. totales de la tabla ===')
        r = await conn.fetchrow('SELECT COUNT(*) AS n, MIN(date) AS d1, MAX(date) AS d2 FROM employee_daily_hours')
        log.info(f"  filas={r['n']} rango={r['d1']} -> {r['d2']}")
    finally:
        await conn.close()


if __name__ == '__main__':
    try:
        asyncio.run(run())
    except Exception as e:
        log.error(f'Error fatal: {type(e).__name__}: {e}')
        sys.exit(1)
