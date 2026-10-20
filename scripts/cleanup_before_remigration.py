"""
cleanup_before_remigration.py
------------------------------
Limpia la DB antes de re-correr la migración:
  1. Borra TODOS los tickets (requerimiento explícito).
  2. Elimina empleados que están en DB pero NO en el CSV (con cascade manual).
  3. Borra absencias de tipo 'pto' de los empleados que SÍ quedan,
     para permitir reinserción limpia desde la migración.

Flags:
    --dry-run   (default) Solo imprime lo que haría, sin tocar la DB.
    --apply     Ejecuta los DELETEs en una sola transacción.

Uso:
    python scripts/cleanup_before_remigration.py "ruta/al/CSV"            # dry-run
    python scripts/cleanup_before_remigration.py "ruta/al/CSV" --apply    # ejecuta
"""

import argparse
import asyncio
import csv
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, ".")

import asyncpg

from app.config import settings

ENCODING = "latin-1"
COL_EID = 11


def clean(v):
    if v is None:
        return None
    s = str(v).strip()
    return s if s else None


def read_csv_eids(path):
    eids = set()
    with open(path, encoding=ENCODING, newline="") as f:
        reader = csv.reader(f)
        next(reader)  # skip header
        for row in reader:
            eid = clean(row[COL_EID]) if len(row) > COL_EID else None
            if not eid or "." not in eid:
                continue
            eids.add(eid)
    return eids


async def run_cleanup(csv_path, apply):
    mode = "APLICANDO" if apply else "DRY-RUN"
    print(f"\n=== cleanup_before_remigration.py [{mode}] ===")
    print(f"CSV: {csv_path}\n")

    csv_eids = read_csv_eids(csv_path)
    print(f"EIDs en CSV: {len(csv_eids)}")

    conn = await asyncpg.connect(
        host=settings.DB_HOST,
        port=settings.DB_PORT,
        user=settings.DB_USER,
        password=settings.DB_PASSWORD,
        database=settings.DB_NAME,
        ssl="require",
    )

    try:
        # ── Fetch current DB state ─────────────────────────────────────────
        db_rows = await conn.fetch("SELECT eid FROM employees")
        db_eids = {r["eid"] for r in db_rows}

        eids_to_delete = sorted(db_eids - csv_eids)
        remaining_eids = sorted(db_eids & csv_eids)

        ticket_count = await conn.fetchval("SELECT COUNT(*) FROM tickets")
        abs_count    = await conn.fetchval(
            "SELECT COUNT(*) FROM absences WHERE type='pto' AND eid = ANY($1)",
            list(remaining_eids),
        )
        fp_count     = await conn.fetchval(
            "SELECT COUNT(*) FROM forecast_periods WHERE eid = ANY($1)",
            list(remaining_eids),
        )

        print(f"Empleados en DB:     {len(db_eids)}")
        print(f"Empleados a eliminar (en DB, no en CSV): {len(eids_to_delete)}")
        if eids_to_delete:
            for e in eids_to_delete:
                print(f"   - {e}")
        print(f"Empleados que quedan: {len(remaining_eids)}")
        print(f"\nTickets a borrar:  {ticket_count}")
        print(f"Forecast periods a borrar (empleados que quedan): {fp_count}")
        print(f"Absencias PTO a borrar (empleados que quedan): {abs_count}")

        # ── Dependency counts for employees to delete ──────────────────────
        if eids_to_delete:
            delete_list = list(eids_to_delete)
            counts = {}
            for table, col in [
                ("employee_daily_hours", "eid"),
                ("chargeability_blocks",  "eid"),
                ("absences",              "eid"),
                ("forecast_periods",      "eid"),
                ("forecast_update",       "eid"),
            ]:
                try:
                    n = await conn.fetchval(
                        f"SELECT COUNT(*) FROM {table} WHERE {col} = ANY($1)",
                        delete_list,
                    )
                    counts[table] = n
                except asyncpg.UndefinedTableError:
                    counts[table] = "tabla no existe"

            # ppa_log has two eid references
            for col in ("eid", "resolved_by"):
                try:
                    n = await conn.fetchval(
                        f"SELECT COUNT(*) FROM ppa_log WHERE {col} = ANY($1)",
                        delete_list,
                    )
                    counts[f"ppa_log.{col}"] = n
                except asyncpg.UndefinedTableError:
                    counts[f"ppa_log.{col}"] = "tabla no existe"

            try:
                n = await conn.fetchval(
                    "SELECT COUNT(*) FROM tickets WHERE eid = ANY($1)",
                    delete_list,
                )
                counts["tickets (de empleados a borrar)"] = n
            except asyncpg.UndefinedTableError:
                counts["tickets (de empleados a borrar)"] = "tabla no existe"

            print(f"\nRegistros relacionados a empleados que se eliminarían:")
            for tbl, n in counts.items():
                print(f"   {tbl:45s} {n}")

        if not apply:
            print(f"\nDry-run terminado. Volver a correr con --apply para ejecutar.")
            return

        # ── APPLY ──────────────────────────────────────────────────────────
        print("\nEjecutando cleanup en transacción única...")

        async with conn.transaction():

            # 1. Borrar TODOS los tickets
            deleted_tickets = await conn.fetchval(
                "SELECT COUNT(*) FROM tickets"
            )
            await conn.execute("DELETE FROM tickets")
            print(f"  [tickets]            Borrados: {deleted_tickets}")

            # 2. Cascade delete de empleados que no están en el CSV
            if eids_to_delete:
                delete_list = list(eids_to_delete)

                # employee_daily_hours
                try:
                    r = await conn.execute(
                        "DELETE FROM employee_daily_hours WHERE eid = ANY($1)", delete_list
                    )
                    print(f"  [employee_daily_hours] {r}")
                except asyncpg.UndefinedTableError:
                    print(f"  [employee_daily_hours] tabla no existe, omitiendo")

                # chargeability_blocks
                try:
                    r = await conn.execute(
                        "DELETE FROM chargeability_blocks WHERE eid = ANY($1)", delete_list
                    )
                    print(f"  [chargeability_blocks] {r}")
                except asyncpg.UndefinedTableError:
                    print(f"  [chargeability_blocks] tabla no existe, omitiendo")

                # absences
                r = await conn.execute(
                    "DELETE FROM absences WHERE eid = ANY($1)", delete_list
                )
                print(f"  [absences (a borrar)] {r}")

                # ppa_log
                try:
                    r = await conn.execute(
                        "DELETE FROM ppa_log WHERE eid = ANY($1) OR resolved_by = ANY($1)",
                        delete_list,
                    )
                    print(f"  [ppa_log]              {r}")
                except asyncpg.UndefinedTableError:
                    print(f"  [ppa_log]              tabla no existe, omitiendo")

                # forecast_periods
                r = await conn.execute(
                    "DELETE FROM forecast_periods WHERE eid = ANY($1)", delete_list
                )
                print(f"  [forecast_periods]     {r}")

                # forecast_update
                r = await conn.execute(
                    "DELETE FROM forecast_update WHERE eid = ANY($1)", delete_list
                )
                print(f"  [forecast_update]      {r}")

                # employees
                r = await conn.execute(
                    "DELETE FROM employees WHERE eid = ANY($1)", delete_list
                )
                print(f"  [employees]            {r}")

            else:
                print("  [empleados] Ningún empleado a eliminar.")

            # 3. Borrar ppa_log de los empleados que quedan
            #    (recalculate_forecast_period aplica PPAs viejas e infla CHG)
            try:
                r = await conn.execute(
                    "DELETE FROM ppa_log WHERE eid = ANY($1) OR resolved_by = ANY($1)",
                    list(remaining_eids),
                )
                print(f"  [ppa_log (quedan)]         {r}")
            except asyncpg.UndefinedTableError:
                print(f"  [ppa_log] tabla no existe, omitiendo")

            # 4. Borrar forecast_periods de los empleados que quedan
            #    (eliminan períodos de migraciones anteriores que ON CONFLICT UPDATE no toca)
            if remaining_eids:
                r = await conn.execute(
                    "DELETE FROM forecast_periods WHERE eid = ANY($1)",
                    list(remaining_eids),
                )
                print(f"  [forecast_periods (quedan)] {r}")

            # 4. Borrar absencias PTO de los empleados que quedan
            if remaining_eids:
                r = await conn.execute(
                    "DELETE FROM absences WHERE type='pto' AND eid = ANY($1)",
                    list(remaining_eids),
                )
                print(f"  [absences PTO (quedan)] {r}")

        print("\nCleanup completado exitosamente.")
        print("Próximo paso: re-correr migrate_sp_control.py con --apply --yes-ptos")

    finally:
        await conn.close()


def parse_args():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("csv_path", help="Ruta al CSV de control S&P")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--dry-run", dest="apply", action="store_false",
                   help="Solo mostrar qué se haría (default)")
    g.add_argument("--apply", dest="apply", action="store_true",
                   help="Ejecutar los DELETEs en la DB")
    p.set_defaults(apply=False)
    return p.parse_args()


if __name__ == "__main__":
    args = parse_args()
    if not os.path.exists(args.csv_path):
        print(f"ERROR: No existe el archivo: {args.csv_path}")
        sys.exit(1)
    try:
        asyncio.run(run_cleanup(args.csv_path, args.apply))
    except KeyboardInterrupt:
        print("\nInterrumpido.")
        sys.exit(1)
    except Exception as e:
        print(f"\nERROR: {e}")
        raise
