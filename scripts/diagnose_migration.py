"""
diagnose_migration.py
---------------------
Diagnóstico pre-cleanup: compara el estado de la DB contra el CSV fuente
y reporta discrepancias.

Uso:
    python scripts/diagnose_migration.py "ruta/al/CSV"
"""

import argparse
import asyncio
import csv
import os
import re
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, ".")

import asyncpg

from app.config import settings

ENCODING = "latin-1"
COL_EID = 11

KNOWN_OFFERINGS = {"SO", "PR", "Tools", "S4", "Ariba", "Oracle"}

MONTH_MAP = {
    "JUL": "Jul", "AUG": "Ago", "SEP": "Sep", "OCT": "Oct",
    "NOV": "Nov", "DEC": "Dic", "JAN": "Ene", "FEB": "Feb",
    "MAR": "Mar", "APR": "Abr", "MAY": "May", "JUN": "Jun",
}


def clean(v):
    if v is None:
        return None
    s = str(v).strip()
    return s if s else None


def to_float(v):
    try:
        return float(str(v).strip().replace("%", "") or "0")
    except (TypeError, ValueError):
        return 0.0


def period_name_from_csv(month_str, half_str, year_str):
    mon = MONTH_MAP.get(month_str.upper())
    if not mon:
        return None
    year = int(year_str)
    suffix = f"-{str(year)[2:]}" if year != 2026 else ""
    return f"{mon}-{half_str}{suffix}"


def detect_period_columns(header):
    period_cols = {}
    period_order = []
    seen = set()
    chg_re = re.compile(r"^CHG (HL|SL) (\w+) (P[12]) (\d{4})$")
    sah_re = re.compile(r"^SAH P[12] (\w+) (P[12]) (\d{4})$")
    for i, h in enumerate(header):
        h = (h or "").strip().upper()
        m = chg_re.match(h)
        if m:
            hl_sl, month, half, year = m.groups()
            pname = period_name_from_csv(month, half, year)
            if pname:
                col_type = "chg_hl" if hl_sl == "HL" else "chg_sl"
                period_cols[i] = (pname, col_type)
                if pname not in seen:
                    seen.add(pname)
                    period_order.append(pname)
            continue
        m = sah_re.match(h)
        if m:
            month, half, year = m.groups()
            pname = period_name_from_csv(month, half, year)
            if pname:
                period_cols[i] = (pname, "sah")
                if pname not in seen:
                    seen.add(pname)
                    period_order.append(pname)
    return period_cols, period_order


def read_csv_eids(path):
    eids = set()
    offerings_by_eid = {}
    with open(path, encoding=ENCODING, newline="") as f:
        reader = csv.reader(f)
        header = next(reader)
        period_cols, period_order = detect_period_columns(header)
        for row in reader:
            eid = clean(row[COL_EID]) if len(row) > COL_EID else None
            if not eid or "." not in eid:
                continue
            eids.add(eid)
            main_skill = clean(row[4]) if len(row) > 4 else None
            status = clean(row[0]) or "Active"
            # Collect CHG% per offering per period for cross-check
            periods = {}
            for i, (pname, col_type) in period_cols.items():
                if pname not in periods:
                    periods[pname] = {"chg_hl": 0.0, "chg_sl": 0.0, "sah": 0.0}
                if col_type in periods[pname] and i < len(row):
                    periods[pname][col_type] = to_float(row[i])
            offerings_by_eid[eid] = {
                "main_skill": main_skill or "",
                "status": status,
                "periods": {k: v for k, v in periods.items()
                            if v["sah"] > 0 or v["chg_hl"] > 0 or v["chg_sl"] > 0},
            }
    return eids, offerings_by_eid, period_order


async def run_diagnosis(csv_path):
    lines = []

    def log(msg=""):
        print(msg)
        lines.append(msg)

    log("=" * 70)
    log("DIAGNÓSTICO DE MIGRACIÓN")
    log(f"CSV:  {csv_path}")
    log(f"Fecha: {date.today()}")
    log("=" * 70)

    # ── Parse CSV ──────────────────────────────────────────────────────────
    csv_eids, csv_data, period_order = read_csv_eids(csv_path)

    active_csv  = sum(1 for d in csv_data.values() if d["status"] == "Active")
    out_csv     = sum(1 for d in csv_data.values() if d["status"] == "OUT")
    nj_csv      = sum(1 for d in csv_data.values()
                      if d["status"] not in ("Active", "OUT"))
    new_off_csv = sorted({
        d["main_skill"] for d in csv_data.values()
        if d["main_skill"] and d["main_skill"] not in KNOWN_OFFERINGS
    })

    log(f"\n── CSV ──────────────────────────────────────────────────────────────")
    log(f"  Empleados válidos: {len(csv_eids)}")
    log(f"  Active: {active_csv}  OUT: {out_csv}  Otros (NJ/licencia): {nj_csv}")
    log(f"  Períodos detectados: {len(period_order)}")
    log(f"  Períodos: {period_order}")
    log(f"  Offerings nuevos (no en KNOWN_OFFERINGS): {new_off_csv or 'ninguno'}")

    # ── Connect DB ──────────────────────────────────────────────────────────
    conn = await asyncpg.connect(
        host=settings.DB_HOST,
        port=settings.DB_PORT,
        user=settings.DB_USER,
        password=settings.DB_PASSWORD,
        database=settings.DB_NAME,
        ssl="require",
    )

    try:
        # ── Employees in DB ────────────────────────────────────────────────
        db_employees = await conn.fetch(
            "SELECT eid, active, offering FROM employees ORDER BY eid"
        )
        db_eids = {r["eid"] for r in db_employees}
        db_active   = sum(1 for r in db_employees if r["active"])
        db_inactive = sum(1 for r in db_employees if not r["active"])

        log(f"\n── DB — employees ───────────────────────────────────────────────────")
        log(f"  Total empleados: {len(db_eids)}")
        log(f"  Active: {db_active}  Inactive: {db_inactive}")

        # ── EID diff ───────────────────────────────────────────────────────
        in_db_not_csv = sorted(db_eids - csv_eids)
        in_csv_not_db = sorted(csv_eids - db_eids)

        log(f"\n── Diferencias de EIDs ──────────────────────────────────────────────")
        log(f"  En DB pero NO en CSV ({len(in_db_not_csv)}) → candidatos a eliminar:")
        for e in in_db_not_csv:
            log(f"    - {e}")
        if not in_db_not_csv:
            log("    (ninguno)")

        log(f"\n  En CSV pero NO en DB ({len(in_csv_not_db)}) → no migrados / NJ pendientes:")
        for e in in_csv_not_db:
            log(f"    - {e}")
        if not in_csv_not_db:
            log("    (ninguno)")

        # ── Tickets ────────────────────────────────────────────────────────
        ticket_rows = await conn.fetch(
            "SELECT type, status, COUNT(*) as n FROM tickets GROUP BY 1, 2 ORDER BY 1, 2"
        )
        total_tickets = sum(r["n"] for r in ticket_rows)
        log(f"\n── Tickets ──────────────────────────────────────────────────────────")
        log(f"  Total tickets: {total_tickets}")
        if ticket_rows:
            for r in ticket_rows:
                log(f"    type={r['type']:12s}  status={r['status']:12s}  n={r['n']}")
        else:
            log("    (sin tickets)")

        # ── Forecast periods per employee ──────────────────────────────────
        fp_rows = await conn.fetch("""
            SELECT eid, COUNT(*) as n_periods
            FROM forecast_periods
            GROUP BY eid
        """)
        fp_by_eid = {r["eid"]: r["n_periods"] for r in fp_rows}

        expected_periods = len(period_order)
        discrepancies = []
        for eid in csv_eids & db_eids:
            db_n = fp_by_eid.get(eid, 0)
            csv_n = len(csv_data[eid]["periods"])
            if db_n != csv_n:
                discrepancies.append((eid, csv_n, db_n))

        log(f"\n── Forecast periods por empleado ────────────────────────────────────")
        log(f"  Períodos esperados del CSV: {expected_periods}")
        log(f"  Empleados con discrepancia en n° de períodos: {len(discrepancies)}")
        if discrepancies:
            log(f"  {'EID':40s}  {'CSV_n':6s}  {'DB_n':6s}")
            for eid, csv_n, db_n in sorted(discrepancies)[:30]:
                log(f"  {eid:40s}  {csv_n:6d}  {db_n:6d}")
            if len(discrepancies) > 30:
                log(f"  ... y {len(discrepancies) - 30} más")

        # ── CHG% por offering ──────────────────────────────────────────────
        log(f"\n── CHG% promedio por offering (DB) ──────────────────────────────────")
        offerings_db = await conn.fetch("""
            SELECT e.offering, AVG(fp.chg_pct) as avg_chg
            FROM forecast_periods fp
            JOIN employees e ON e.eid = fp.eid
            WHERE e.offering IS NOT NULL
            GROUP BY e.offering
            ORDER BY e.offering
        """)
        log(f"  {'Offering':20s}  {'Avg CHG% DB':12s}")
        for r in offerings_db:
            log(f"  {r['offering']:20s}  {r['avg_chg']:.1f}%")

        # HC per offering from CSV
        hc_csv = {}
        for eid, d in csv_data.items():
            skill = d["main_skill"] or "?"
            hc_csv[skill] = hc_csv.get(skill, 0) + 1

        log(f"\n  HC por main_skill (CSV):")
        for skill, n in sorted(hc_csv.items()):
            log(f"    {skill:20s}  {n} HC")

        # HC per offering from DB
        hc_db = {}
        for r in db_employees:
            off = r["offering"] or "?"
            hc_db[off] = hc_db.get(off, 0) + 1

        log(f"\n  HC por offering (DB):")
        for off, n in sorted(hc_db.items()):
            log(f"    {off:20s}  {n} HC")

        # ── Absences ───────────────────────────────────────────────────────
        abs_count = await conn.fetchval("SELECT COUNT(*) FROM absences WHERE type='pto'")
        log(f"\n── Absencias PTO en DB: {abs_count}")

        log(f"\n{'=' * 70}")
        log("Diagnóstico completo.")

    finally:
        await conn.close()

    out_path = Path("migration_diagnosis.txt")
    out_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"\nReporte guardado en: {out_path}")


def parse_args():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("csv_path", help="Ruta al CSV de control S&P")
    return p.parse_args()


if __name__ == "__main__":
    args = parse_args()
    if not os.path.exists(args.csv_path):
        print(f"ERROR: No existe el archivo: {args.csv_path}")
        sys.exit(1)
    try:
        asyncio.run(run_diagnosis(args.csv_path))
    except KeyboardInterrupt:
        print("\nInterrumpido.")
        sys.exit(1)
    except Exception as e:
        print(f"\nERROR: {e}")
        raise
