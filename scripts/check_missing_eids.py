import asyncio, sys
sys.path.insert(0, '.')
from app.config import settings
import asyncpg

FALTAN = ['d.hernandez.cortes','iliana.espino','sylvia.g.villanueva','patrick.robert',
          's.morales.espinosa','d.mr.medina','valentina.araudo','ana.l.quintero',
          'i.fernandez.penarol','tomas.perez','valentina.m.tondi']

async def main():
    conn = await asyncpg.connect(host=settings.DB_HOST, port=settings.DB_PORT,
        user=settings.DB_USER, password=settings.DB_PASSWORD,
        database=settings.DB_NAME, ssl='require')

    print('--- Existen en employees? ---')
    rows = await conn.fetch("SELECT eid, name, active, country FROM employees WHERE eid = ANY($1)", FALTAN)
    for r in rows:
        print(f"  {r['eid']:26} activo={r['active']} pais={r['country']}")
    if not rows:
        print('  ninguno')

    print()
    print('--- Parecidos por apellido ---')
    for f in FALTAN:
        ap = f.split('.')[-1]
        sim = await conn.fetch(
            "SELECT eid FROM employees WHERE eid ILIKE $1 OR name ILIKE $1", f'%{ap}%')
        if sim:
            print(f"  {f}  ->  {[s['eid'] for s in sim]}")

    print()
    print('--- Jose Alvarado / Chara Millenaar ---')
    for n in ['alvarado','millenaar','millenar','chara']:
        sim = await conn.fetch("SELECT eid, name FROM employees WHERE name ILIKE $1", f'%{n}%')
        for s in sim:
            print(f"  {n}: {s['eid']} = {s['name']}")

    await conn.close()

asyncio.run(main())
