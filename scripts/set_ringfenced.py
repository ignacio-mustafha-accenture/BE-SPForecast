"""
set_ringfenced.py
-----------------
Marca como ISG Ringfenced a las personas del listado que paso el equipo.

Los Ringfenced tienen una assumption distinta al resto (Assumption 2 del Excel:
post roll-off van al 0% el primer periodo, 75% el segundo y 100% despues,
mientras que los no-ISG se quedan en 0% dos periodos).

Escribe dos campos porque el frontend evalua los dos juntos:
    employees.ringfenced        -> booleano
    forecast_update.isg_aligned -> 'Yes' / NULL

Es idempotente: se puede correr las veces que haga falta. Y limpia a quien ya
no este en la lista, asi el listado del equipo es la fuente de verdad y no se
acumulan marcas viejas.

Uso:
    python scripts/set_ringfenced.py            # dry-run
    python scripts/set_ringfenced.py --apply    # escribe
"""

import asyncio
import sys

sys.path.insert(0, ".")

import asyncpg

from app.config import settings

# Listado pasado por el equipo (27/08/2026).
# Son los que figuran en celeste en el Excel de forecast.
RINGFENCED = [
    "nicolas.zappacosta",
    "florencia.salvucci",
    "c.ruiz",
    "mariano.tanus",
    "lucila.b.scarpa",
    "estefania.castaneda",
    "cecilia.arato",
    "agustin.teglia",
    "guillermo.mishima",
    "nicolas.della.rocca",
    "giuliana.ruffini",
    "pedro.coppola",
    "anastasia.gualdoni",
    "matias.n.ales",
    "maria.lucia.sanchez",
    "candela.wettstein",
    "camila.slemenson",
    "b.martinez.angello",
]


async def main(apply: bool):
    conn = await asyncpg.connect(
        host=settings.DB_HOST,
        port=settings.DB_PORT,
        user=settings.DB_USER,
        password=settings.DB_PASSWORD,
        database=settings.DB_NAME,
        ssl="require",
    )

    print(f"Listado recibido: {len(RINGFENCED)} EIDs\n")

    existentes = {
        r["eid"] for r in await conn.fetch(
            "SELECT eid FROM employees WHERE eid = ANY($1)", RINGFENCED
        )
    }
    faltantes = [e for e in RINGFENCED if e not in existentes]

    print(f"Encontrados en la base: {len(existentes)}")
    if faltantes:
        print(f"NO estan en la base ({len(faltantes)}):")
        for f in faltantes:
            print(f"   - {f}")
    print()

    marcados = {
        r["eid"] for r in await conn.fetch(
            "SELECT eid FROM employees WHERE ringfenced IS TRUE"
        )
    }

    a_marcar = existentes - marcados
    a_desmarcar = marcados - set(RINGFENCED)

    print(f"Se marcarian:    {len(a_marcar)}")
    for e in sorted(a_marcar):
        print(f"   + {e}")
    if a_desmarcar:
        print(f"\nSe desmarcarian: {len(a_desmarcar)}  (estaban marcados y no vienen en el listado)")
        for e in sorted(a_desmarcar):
            print(f"   - {e}")
    print()

    if not apply:
        print("[DRY RUN] No se escribio nada. Volve a correr con --apply.")
        await conn.close()
        return

    async with conn.transaction():
        # El listado del equipo es la fuente de verdad: se limpia y se vuelve a marcar
        await conn.execute("UPDATE employees SET ringfenced = FALSE WHERE ringfenced IS TRUE")
        await conn.execute(
            "UPDATE employees SET ringfenced = TRUE WHERE eid = ANY($1)", RINGFENCED
        )

        await conn.execute("UPDATE forecast_update SET isg_aligned = NULL")
        await conn.execute(
            "UPDATE forecast_update SET isg_aligned = 'Yes' WHERE eid = ANY($1)", RINGFENCED
        )

    total = await conn.fetchval(
        "SELECT COUNT(*) FROM employees WHERE ringfenced IS TRUE AND active IS TRUE"
    )
    print(f"OK. {total} empleados activos marcados como ISG Ringfenced.")

    print("\nVerificacion:")
    rows = await conn.fetch("""
        WITH latest AS (
            SELECT DISTINCT ON (eid) * FROM forecast_update
            ORDER BY eid, updated_at DESC NULLS LAST
        )
        SELECT e.eid, e.ringfenced, l.isg_aligned, l.client
        FROM employees e LEFT JOIN latest l ON l.eid = e.eid
        WHERE e.ringfenced IS TRUE
        ORDER BY e.eid
    """)
    for r in rows:
        print(f"   {r['eid']:24} ringfenced={r['ringfenced']} isg={r['isg_aligned']!r} "
              f"cliente={str(r['client'])[:24]}")

    await conn.close()


if __name__ == "__main__":
    asyncio.run(main("--apply" in sys.argv))