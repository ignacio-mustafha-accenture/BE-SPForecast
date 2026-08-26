"""
populate_daily_hours.py
-----------------------
Puebla employee_daily_hours desde forecast_periods, distribuyendo
horas proporcionalmente entre los días hábiles de cada período.

El join usa el rango start_date/end_date de la tabla periods (no
calendar.period_name, que no es único por año).

Uso:
    python populate_daily_hours.py            # solo inserta EIDs sin datos
    python populate_daily_hours.py --force    # reprocesa todos
    python populate_daily_hours.py --dry-run  # muestra qué haría sin escribir
"""

import asyncio
import sys
import argparse
from decimal import Decimal

sys.path.insert(0, ".")
from app.config import settings
import asyncpg


async def main(force: bool = False, dry_run: bool = False):
    conn = await asyncpg.connect(
        host=settings.DB_HOST,
        port=settings.DB_PORT,
        user=settings.DB_USER,
        password=settings.DB_PASSWORD,
        database=settings.DB_NAME,
        ssl="require",
    )

    # 1. Períodos con su rango real de fechas
    print("Cargando períodos...")
    period_rows = await conn.fetch("""
        SELECT period_name, start_date, end_date
        FROM periods
        ORDER BY start_date
    """)

    # Deduplicar por start_date (mismo problema que en state_service:
    # puede haber dos registros con igual start_date de años distintos)
    seen: dict = {}
    for r in sorted(period_rows, key=lambda r: (r["start_date"], r["period_name"])):
        if r["start_date"] not in seen:
            seen[r["start_date"]] = r
    periods = list(seen.values())
    print(f"  {len(periods)} períodos únicos")

    # 2. Días hábiles por período usando el rango de fechas real
    print("Calculando días hábiles por período (join por fecha, no por nombre)...")
    period_days: dict[str, list] = {}
    for p in periods:
        rows = await conn.fetch("""
            SELECT date FROM calendar
            WHERE country = 'Argentina'
              AND is_working_day = TRUE
              AND date BETWEEN $1 AND $2
            ORDER BY date
        """, p["start_date"], p["end_date"])
        days = [r["date"] for r in rows]
        period_days[p["period_name"]] = days
        print(f"  {p['period_name']} ({p['start_date']} -> {p['end_date']}): {len(days)} dias habiles")

    # 3. Forecast periods con datos reales
    print("\nCargando forecast_periods...")
    fp_rows = await conn.fetch("""
        SELECT fp.eid, fp.period_name, fp.chg_hl, fp.chg_sl, fp.chg_cascadeadas
        FROM forecast_periods fp
        JOIN employees e ON fp.eid = e.eid
        WHERE fp.chg_hl != 0 OR fp.chg_sl != 0 OR fp.sah != 0
    """)
    print(f"  {len(fp_rows)} registros con datos")

    # 4. EIDs a saltear si no es --force
    skip_eids: set[str] = set()
    if not force:
        existing = await conn.fetch("SELECT DISTINCT eid FROM employee_daily_hours")
        skip_eids = {r["eid"] for r in existing}
        print(f"  {len(skip_eids)} EIDs ya existentes (--force para reprocesar)")

    # 5. Construir filas
    rows_to_insert: list[tuple] = []
    skipped_existing = 0
    skipped_no_calendar = 0

    for fp in fp_rows:
        eid = fp["eid"]
        pn  = fp["period_name"]

        if eid in skip_eids:
            skipped_existing += 1
            continue

        days = period_days.get(pn)
        if not days:
            print(f"  WARN: sin dias habiles para periodo '{pn}' -- skip {eid}")
            skipped_no_calendar += 1
            continue

        n = Decimal(len(days))
        chg_hl_day  = (fp["chg_hl"]          or Decimal(0)) / n
        chg_sl_day  = (fp["chg_sl"]          or Decimal(0)) / n
        chg_ppa_day = (fp["chg_cascadeadas"] or Decimal(0)) / n

        for day in days:
            rows_to_insert.append((
                eid,
                day,
                Decimal("8.00"),
                chg_hl_day,
                chg_sl_day,
                chg_ppa_day,
            ))

    print(f"\nResumen:")
    print(f"  Filas a insertar:         {len(rows_to_insert)}")
    print(f"  Skipped (ya existian):    {skipped_existing}")
    print(f"  Skipped (sin calendario): {skipped_no_calendar}")

    if dry_run:
        print("\n[DRY RUN] No se escribio nada.")
        await conn.close()
        return

    if not rows_to_insert:
        print("\nNada que insertar.")
        await conn.close()
        return

    # 6. Limpiar datos previos de los EIDs que vamos a reprocesar
    eids_to_insert = list({r[0] for r in rows_to_insert})
    print(f"\nLimpiando {len(eids_to_insert)} EIDs en employee_daily_hours...")
    async with conn.transaction():
        await conn.execute(
            "DELETE FROM employee_daily_hours WHERE eid = ANY($1)",
            eids_to_insert,
        )
        await conn.executemany("""
            INSERT INTO employee_daily_hours (eid, date, sah, chg_hl, chg_sl, chg_ppa)
            VALUES ($1, $2, $3, $4, $5, $6)
        """, rows_to_insert)

    total = await conn.fetchval("SELECT COUNT(*) FROM employee_daily_hours")
    print(f"OK. employee_daily_hours ahora tiene {total} filas.")
    await conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--force", action="store_true",
                        help="Reprocesar aunque el EID ya tenga datos")
    parser.add_argument("--dry-run", action="store_true",
                        help="Mostrar que haria sin escribir nada")
    args = parser.parse_args()
    asyncio.run(main(force=args.force, dry_run=args.dry_run))