"""
Identifica empleados que tienen CHG/SAH en el CSV para un período
pero NO tienen fila en forecast_periods en la DB.
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
TARGET_PERIODS = ["Oct-P1", "Oct-P2", "Nov-P1"]
CSV_PATH = r"C:\Users\ignacio.mustafha\Downloads\01 - FORECAST CONTROL S&P(S&P) (1).csv"

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

async def main():
    # Parse CSV
    # {eid: {period: {chg_hl, chg_sl, sah, offering}}}
    csv_data = {}
    with open(CSV_PATH, encoding=ENCODING, newline="") as f:
        reader = csv.reader(f)
        header = next(reader)
        pcols = detect_period_cols(header)
        for row in reader:
            eid = clean(row[COL_EID]) if len(row) > COL_EID else None
            if not eid or "." not in eid:
                continue
            off = (clean(row[4]) if len(row) > 4 else None) or "?"
            status = clean(row[0]) or "Active"
            csv_data[eid] = {"offering": off, "status": status, "periods": {}}
            for i, (pname, col_type) in pcols.items():
                if pname in TARGET_PERIODS and i < len(row):
                    if pname not in csv_data[eid]["periods"]:
                        csv_data[eid]["periods"][pname] = {"chg_hl": 0.0, "chg_sl": 0.0, "sah": 0.0}
                    csv_data[eid]["periods"][pname][col_type] += to_float(row[i])

    # Query DB
    conn = await asyncpg.connect(
        host=settings.DB_HOST, port=settings.DB_PORT,
        user=settings.DB_USER, password=settings.DB_PASSWORD,
        database=settings.DB_NAME, ssl="require",
    )
    db_rows = await conn.fetch("""
        SELECT eid, period_name, chg_hl, chg_sl, chg, sah, absence_hours
        FROM forecast_periods
        WHERE period_name = ANY($1)
    """, TARGET_PERIODS)
    await conn.close()

    db_fps = {}  # {(eid, period): row}
    for r in db_rows:
        db_fps[(r["eid"], r["period_name"])] = r

    # Compare
    for pname in TARGET_PERIODS:
        print(f"\n{'='*65}")
        print(f"  {pname} — empleados en CSV pero SIN fila en DB (o con SAH diferente)")
        print(f"{'='*65}")
        missing = []
        sah_diff = []

        for eid, d in csv_data.items():
            pd = d["periods"].get(pname, {})
            csv_chg = pd.get("chg_hl", 0) + pd.get("chg_sl", 0)
            csv_sah = pd.get("sah", 0)

            key = (eid, pname)
            if key not in db_fps:
                missing.append((eid, d["offering"], d["status"], csv_chg, csv_sah))
            else:
                db_r = db_fps[key]
                db_sah = float(db_r["sah"] or 0)
                if abs(db_sah - csv_sah) > 0.5:
                    sah_diff.append((eid, d["offering"], csv_chg, csv_sah, float(db_r["chg"] or 0), db_sah, float(db_r["absence_hours"] or 0)))

        if missing:
            print(f"\n  Sin fila en DB ({len(missing)}):")
            print(f"  {'EID':35s} {'Offering':8s} {'Status':8s} {'CSV CHG':>8} {'CSV SAH':>8}")
            for eid, off, st, chg, sah in sorted(missing):
                print(f"  {eid:35s} {off:8s} {st:8s} {chg:>8.0f} {sah:>8.0f}")
        else:
            print("  Todos los empleados con datos tienen fila en DB.")

        if sah_diff:
            print(f"\n  Con SAH diferente entre CSV y DB ({len(sah_diff)}):")
            print(f"  {'EID':35s} {'Off':6s} {'CSV_CHG':>8} {'CSV_SAH':>8} {'DB_CHG':>8} {'DB_SAH':>8} {'DB_abs':>7}")
            for row in sorted(sah_diff):
                eid, off, c_chg, c_sah, d_chg, d_sah, d_abs = row
                print(f"  {eid:35s} {off:6s} {c_chg:>8.0f} {c_sah:>8.0f} {d_chg:>8.0f} {d_sah:>8.0f} {d_abs:>7.0f}")

asyncio.run(main())
