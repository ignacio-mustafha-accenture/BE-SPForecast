import asyncio, sys
sys.path.insert(0, '.')
from app.config import settings
import asyncpg
from collections import Counter

VALIDOS = {'SO','PR','Tools','S4','Ariba','Oracle'}

async def main():
    conn = await asyncpg.connect(host=settings.DB_HOST, port=settings.DB_PORT,
        user=settings.DB_USER, password=settings.DB_PASSWORD,
        database=settings.DB_NAME, ssl='require')

    rows = await conn.fetch("""
        WITH latest AS (
            SELECT DISTINCT ON (eid) * FROM forecast_update
            ORDER BY eid, updated_at DESC NULLS LAST
        )
        SELECT e.eid, e.name, l.client, l.offering, l.first_available
        FROM employees e LEFT JOIN latest l ON l.eid = e.eid
        WHERE e.active = TRUE
    """)

    print(f'Empleados activos: {len(rows)}')
    print()

    print('--- Offering ---')
    for k,v in Counter(r['offering'] for r in rows).most_common():
        flag = '' if (k in VALIDOS or k is None) else '  <-- FUERA DE TAXONOMIA'
        print(f'  {str(k):16} {v}{flag}')

    print()
    print('--- Clientes que todavia parecen fechas ---')
    malos = [r for r in rows if r['client'] and (' to ' in r['client'] or r['client'][:2].isdigit())]
    if malos:
        for r in malos: print(f"  {r['eid']:26} {r['client']!r}")
    else:
        print('  ninguno')

    print()
    print('--- first_available con año anterior a 2025 ---')
    viejas = [r for r in rows if r['first_available'] and r['first_available'].year < 2025]
    if viejas:
        for r in viejas[:10]: print(f"  {r['eid']:26} {r['first_available']}")
        print(f'  total: {len(viejas)}')
    else:
        print('  ninguna')

    print()
    print('--- Sin cliente ---')
    sin = [r['eid'] for r in rows if not r['client']]
    print(f'  {len(sin)}: {sin[:10]}')

    print()
    print('--- Muestra de 8 ---')
    for r in rows[:8]:
        print(f"  {r['eid']:26} {str(r['offering'] or '-'):8} {str(r['client'] or '-')[:26]:26} {r['first_available']}")

    await conn.close()

asyncio.run(main())
