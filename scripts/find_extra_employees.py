"""
find_extra_employees.py
-----------------------
Sincroniza active=TRUE/FALSE en la DB contra el listado del CSV (HTML).

  - Empleados activos AR en DB pero NO en CSV → deactivate
  - Empleados en CSV pero inactivos en DB → reactivate

Uso:
    python scripts/find_extra_employees.py          # solo lista
    python scripts/find_extra_employees.py --apply  # aplica cambios
"""

import asyncio
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import asyncpg
from app.config import settings

HTML_EIDS_FILE = Path(r"C:\Users\IGNACI~1.MUS\AppData\Local\Temp\html_eids.txt")


async def main(apply: bool):
    html_eids = set(HTML_EIDS_FILE.read_text(encoding="utf-8-sig").strip().splitlines())
    print(f"EIDs en HTML/CSV: {len(html_eids)}\n")

    conn = await asyncpg.connect(
        host=settings.DB_HOST, port=settings.DB_PORT,
        user=settings.DB_USER, password=settings.DB_PASSWORD,
        database=settings.DB_NAME, ssl="require",
    )
    try:
        all_ar = await conn.fetch("""
            SELECT eid, name, COALESCE(offering, '') AS offering,
                   COALESCE(country, location, '') AS country, active
            FROM employees
            WHERE LOWER(COALESCE(country, location)) IN ('ar', 'arg', 'argentina')
        """)

        active_ar_eids = {r["eid"] for r in all_ar if r["active"]}
        to_deactivate = [r for r in all_ar if r["active"] and r["eid"] not in html_eids]
        to_reactivate = [r for r in all_ar if not r["active"] and r["eid"] in html_eids]

        print(f"Activos AR en DB:  {len(active_ar_eids)}")
        print(f">> DESACTIVAR (en DB, no en CSV): {len(to_deactivate)}")
        for r in to_deactivate:
            print(f"   {r['eid']:<35} {r['offering']:<12}")

        print()
        print(f">> REACTIVAR (en CSV, inactivos en DB): {len(to_reactivate)}")
        for r in to_reactivate:
            print(f"   {r['eid']:<35} {r['offering']:<12}")

        if apply:
            print()
            async with conn.transaction():
                if to_deactivate:
                    await conn.execute(
                        "UPDATE employees SET active = FALSE WHERE eid = ANY($1)",
                        [r["eid"] for r in to_deactivate],
                    )
                    print(f"✓ Desactivados: {len(to_deactivate)}")
                if to_reactivate:
                    await conn.execute(
                        "UPDATE employees SET active = TRUE WHERE eid = ANY($1)",
                        [r["eid"] for r in to_reactivate],
                    )
                    print(f"✓ Reactivados:  {len(to_reactivate)}")
            print("\nRecargá /all para ver los totales actualizados.")
        else:
            print("\nModo lectura — usá --apply para aplicar.")
    finally:
        await conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    asyncio.run(main(apply=args.apply))
