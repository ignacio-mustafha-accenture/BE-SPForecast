"""
fix_multiproject_rolloff.py
---------------------------
Corrección puntual para empleados SAP cuya migración capturó solo el PRIMER
bloque de proyecto (format-A con ";"), ignorando el bloque actual.

Empleados afectados (detectados por análisis de migration_simulation.html):
  - jesus.d.martinez:  "Vicuña|..|30/09/2026 ; AES|01/10/2026|"
    → DB tiene roll_off=Sep-30, debería ser AES desde Oct-1 (sin fin → 2099-12-31)
  - mariano.degano:    "YPF|..|30/09/2026 ; Ferrero|05/10/2026|31/07/2027"
    → DB tiene roll_off=Sep-30, debería ser Ferrero Oct-5 → Jul-31-2027

Resultado esperado para Oct-P1 SAP:
  - jesus.d.martinez CHG: 0 → ~72h (10 días laborables menos feriado Oct-12)
  - mariano.degano CHG:   0 → ~64h (Oct 5-15 menos feriado Oct-12)
  - App SAP CHG: 1448 → ~1584h
  - Diferencia restante con HTML (1736h): ~152h = feriado Oct-12 × ~19 empleados (esperado)

Uso:
    python scripts/fix_multiproject_rolloff.py [--dry-run]   (default: dry-run)
    python scripts/fix_multiproject_rolloff.py --apply
"""

import asyncio
import argparse
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import asyncpg
from app.config import settings

FIXES = [
    {
        "eid": "jesus.d.martinez",
        "client": "AES",
        "roll_on": date(2026, 10, 1),
        "roll_off": date(2099, 12, 31),   # open-ended project
        "reason": "2nd block: 100|AES|01/10/2026| — first block Vicuña ended Sep-30",
    },
    {
        "eid": "mariano.degano",
        "client": "Ferrero",
        "roll_on": date(2026, 10, 5),
        "roll_off": date(2027, 7, 31),
        "reason": "2nd block: 100|Ferrero|05/10/2026|31/07/2027 — first block YPF ended Sep-30",
    },
]

# Periods from Oct-P1 onward that need recalculation
RECALC_PERIODS_QUERY = """
    SELECT period_name FROM periods
    WHERE start_date >= '2026-10-01'
    ORDER BY start_date
"""


async def main(apply: bool):
    conn = await asyncpg.connect(
        host=settings.DB_HOST,
        port=settings.DB_PORT,
        user=settings.DB_USER,
        password=settings.DB_PASSWORD,
        database=settings.DB_NAME,
        ssl="require",
    )
    try:
        periods = [r["period_name"] for r in await conn.fetch(RECALC_PERIODS_QUERY)]
        print(f"Períodos a recalcular: {periods}\n")

        for fix in FIXES:
            eid = fix["eid"]
            print(f"{'[DRY-RUN] ' if not apply else ''}Aplicando fix para {eid}")
            print(f"  Cliente: {fix['client']}")
            print(f"  roll_on:  {fix['roll_on']}")
            print(f"  roll_off: {fix['roll_off']}")
            print(f"  Razón:    {fix['reason']}")

            if apply:
                async with conn.transaction():
                    await conn.execute("""
                        UPDATE forecast_update
                        SET client   = $1,
                            roll_on  = $2,
                            roll_off = $3,
                            updated_at = NOW()
                        WHERE eid = $4
                    """, fix["client"], fix["roll_on"], fix["roll_off"], eid)

                    for pname in periods:
                        print(f"  recalculate_forecast_period({eid}, {pname})...")
                        await conn.execute(
                            "SELECT recalculate_forecast_period($1, $2)",
                            eid, pname,
                        )

                print(f"  ✓ {eid} actualizado y recalculado\n")
            else:
                print()

        if not apply:
            print("Modo DRY-RUN: sin cambios en DB. Usa --apply para ejecutar.")
        else:
            print("Todos los fixes aplicados. Recargá /all para ver los nuevos totales SAP.")
    finally:
        await conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    asyncio.run(main(apply=args.apply))
