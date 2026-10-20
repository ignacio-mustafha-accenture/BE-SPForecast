"""
Compara Oct-P1: CSV (lógica del HTML) vs DB.
"""
import asyncio, csv, re, sys
sys.path.insert(0, ".")
import asyncpg
from app.config import settings

ENCODING = "latin-1"
COL_EID = 11
MONTH_MAP = {
    "JUL":"Jul","AUG":"Ago","SEP":"Sep","OCT":"Oct",
    "NOV":"Nov","DEC":"Dic","JAN":"Ene","FEB":"Feb",
    "MAR":"Mar","APR":"Abr","MAY":"May","JUN":"Jun",
}

def clean(v):
    s = str(v).strip() if v is not None else ""
    return s if s else None

def to_float(v):
    try:
        return float(str(v).strip().replace("%","") or "0")
    except:
        return 0.0

def period_name(month, half, year):
    mon = MONTH_MAP.get(month.upper())
    if not mon: return None
    suffix = f"-{str(year)[2:]}" if int(year) != 2026 else ""
    return f"{mon}-{half}{suffix}"

def detect_period_cols(header):
    cols = {}
    chg_re = re.compile(r"^CHG (HL|SL) (\w+) (P[12]) (\d{4})$")
    sah_re = re.compile(r"^SAH P[12] (\w+) (P[12]) (\d{4})$")
    for i, h in enumerate(header):
        h = (h or "").strip().upper()
        m = chg_re.match(h)
        if m:
            hl_sl, month, half, year = m.groups()
            pname = period_name(month, half, year)
            if pname:
                cols[i] = (pname, "chg_hl" if hl_sl == "HL" else "chg_sl")
            continue
        m = sah_re.match(h)
        if m:
            month, half, year = m.groups()
            pname = period_name(month, half, year)
            if pname:
                cols[i] = (pname, "sah")
    return cols

CSV_PATH = r"C:\Users\ignacio.mustafha\Downloads\01 - FORECAST CONTROL S&P(S&P) (1).csv"
TARGET_PERIOD = "Oct-P1"

async def main():
    # ── CSV side (HTML logic) ──────────────────────────────────────────────
    csv_by_offering = {}   # {offering: {hc, chg_hl, chg_sl, sah}}
    with open(CSV_PATH, encoding=ENCODING, newline="") as f:
        reader = csv.reader(f)
        header = next(reader)
        pcols = detect_period_cols(header)
        for row in reader:
            eid = clean(row[COL_EID]) if len(row) > COL_EID else None
            if not eid or "." not in eid:
                continue
            main_skill = clean(row[4]) if len(row) > 4 else None
            off = main_skill or "?"
            if off not in csv_by_offering:
                csv_by_offering[off] = {"hc": 0, "chg_hl": 0.0, "chg_sl": 0.0, "sah": 0.0}
            csv_by_offering[off]["hc"] += 1
            for i, (pname, col_type) in pcols.items():
                if pname == TARGET_PERIOD and i < len(row):
                    csv_by_offering[off][col_type] = csv_by_offering[off].get(col_type, 0.0) + to_float(row[i])

    # ── DB side ───────────────────────────────────────────────────────────
    conn = await asyncpg.connect(
        host=settings.DB_HOST, port=settings.DB_PORT,
        user=settings.DB_USER, password=settings.DB_PASSWORD,
        database=settings.DB_NAME, ssl="require",
    )
    db_rows = await conn.fetch("""
        SELECT e.offering,
               COUNT(*)              as hc,
               SUM(fp.chg_hl)        as chg_hl,
               SUM(fp.chg_sl)        as chg_sl,
               SUM(fp.chg)           as chg,
               SUM(fp.sah)           as sah,
               SUM(fp.absence_hours) as abs_h
        FROM forecast_periods fp
        JOIN employees e ON e.eid = fp.eid
        WHERE fp.period_name = $1
        GROUP BY e.offering ORDER BY e.offering
    """, TARGET_PERIOD)
    await conn.close()

    db_by_off = {}
    for r in db_rows:
        db_by_off[r["offering"] or "?"] = {
            "hc": r["hc"], "chg_hl": float(r["chg_hl"] or 0),
            "chg_sl": float(r["chg_sl"] or 0), "chg": float(r["chg"] or 0),
            "sah": float(r["sah"] or 0), "abs_h": float(r["abs_h"] or 0),
        }

    # ── Print comparison ──────────────────────────────────────────────────
    print(f"=== Comparación {TARGET_PERIOD}: CSV (HTML) vs DB ===\n")
    offerings = sorted(set(list(csv_by_offering) + list(db_by_off)))

    for off in offerings:
        csv_d = csv_by_offering.get(off, {})
        db_d  = db_by_off.get(off, {})

        csv_chg = csv_d.get("chg_hl", 0) + csv_d.get("chg_sl", 0)
        csv_sah = csv_d.get("sah", 0)
        csv_pct = round(csv_chg / csv_sah * 100, 1) if csv_sah else 0

        db_chg  = db_d.get("chg", 0)
        db_sah  = db_d.get("sah", 0)
        db_abs  = db_d.get("abs_h", 0)
        db_net  = db_sah - db_abs
        db_pct_gross = round(db_chg / db_sah * 100, 1) if db_sah else 0
        db_pct_net   = round(db_chg / db_net  * 100, 1) if db_net else 0

        print(f"  {off}")
        print(f"    CSV (HTML):  HC={csv_d.get('hc',0):3d}  CHG={csv_chg:7.0f}  SAH={csv_sah:7.0f}  CHG%={csv_pct:.1f}%")
        print(f"    DB (bruto):  HC={db_d.get('hc',0):3d}  CHG={db_chg:7.0f}  SAH={db_sah:7.0f}  CHG%={db_pct_gross:.1f}%  (abs={db_abs:.0f}h)")
        print(f"    DB (neto):                                         CHG%={db_pct_net:.1f}%  (SAH-abs={db_net:.0f}h)")
        print()

asyncio.run(main())
