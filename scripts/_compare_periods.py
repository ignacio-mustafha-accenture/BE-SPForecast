"""
Compara CHG / SAH / CHG% entre CSV (lógica HTML) y DB para períodos dados.
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
    # ── CSV parse ──────────────────────────────────────────────────────────
    # {period: {chg_hl, chg_sl, sah, hc}}
    csv_totals = {p: {"chg_hl": 0.0, "chg_sl": 0.0, "sah": 0.0, "hc": 0} for p in TARGET_PERIODS}
    csv_off    = {p: {} for p in TARGET_PERIODS}  # {period: {offering: {chg_hl,chg_sl,sah,hc}}}

    with open(CSV_PATH, encoding=ENCODING, newline="") as f:
        reader = csv.reader(f)
        header = next(reader)
        pcols = detect_period_cols(header)
        for row in reader:
            eid = clean(row[COL_EID]) if len(row) > COL_EID else None
            if not eid or "." not in eid:
                continue
            off = clean(row[4]) if len(row) > 4 else "?"
            off = off or "?"
            for pname in TARGET_PERIODS:
                csv_totals[pname]["hc"] += 1
                if off not in csv_off[pname]:
                    csv_off[pname][off] = {"chg_hl": 0.0, "chg_sl": 0.0, "sah": 0.0, "hc": 0}
                csv_off[pname][off]["hc"] += 1
            for i, (pname, col_type) in pcols.items():
                if pname in TARGET_PERIODS and i < len(row):
                    v = to_float(row[i])
                    csv_totals[pname][col_type] = csv_totals[pname].get(col_type, 0.0) + v
                    csv_off[pname][off][col_type] = csv_off[pname][off].get(col_type, 0.0) + v

    # ── DB query ───────────────────────────────────────────────────────────
    conn = await asyncpg.connect(
        host=settings.DB_HOST, port=settings.DB_PORT,
        user=settings.DB_USER, password=settings.DB_PASSWORD,
        database=settings.DB_NAME, ssl="require",
    )

    db_rows = await conn.fetch("""
        SELECT fp.period_name, e.offering,
               COUNT(*)              as hc,
               SUM(fp.chg_hl)        as chg_hl,
               SUM(fp.chg_sl)        as chg_sl,
               SUM(fp.chg)           as chg,
               SUM(fp.sah)           as sah,
               SUM(fp.absence_hours) as abs_h
        FROM forecast_periods fp
        JOIN employees e ON e.eid = fp.eid
        WHERE fp.period_name = ANY($1)
        GROUP BY fp.period_name, e.offering
        ORDER BY fp.period_name, e.offering
    """, TARGET_PERIODS)
    await conn.close()

    # group by period
    db_by_period = {p: {} for p in TARGET_PERIODS}
    for r in db_rows:
        p = r["period_name"]
        off = r["offering"] or "?"
        db_by_period[p][off] = {
            "hc": r["hc"],
            "chg_hl": float(r["chg_hl"] or 0),
            "chg_sl": float(r["chg_sl"] or 0),
            "chg": float(r["chg"] or 0),
            "sah": float(r["sah"] or 0),
            "abs_h": float(r["abs_h"] or 0),
        }

    # ── Print ──────────────────────────────────────────────────────────────
    for pname in TARGET_PERIODS:
        print(f"\n{'='*70}")
        print(f"  {pname}")
        print(f"{'='*70}")
        print(f"  {'Offering':<10} │ {'CSV':^36} │ {'DB bruto':^36} │ {'DB neto':^10}")
        print(f"  {'':10} │ {'HC':>4} {'CHG':>8} {'SAH':>8} {'CHG%':>7} │ {'HC':>4} {'CHG':>8} {'SAH':>8} {'CHG%':>7} │ {'CHG%':>10}")
        print(f"  {'-'*10}-+-{'-'*36}-+-{'-'*36}-+-{'-'*10}")

        offerings = sorted(set(list(csv_off[pname]) + list(db_by_period[pname])))
        csv_tot_chg = 0.0; csv_tot_sah = 0.0; csv_tot_hc = 0
        db_tot_chg  = 0.0; db_tot_sah  = 0.0; db_tot_abs = 0.0; db_tot_hc = 0

        for off in offerings:
            c = csv_off[pname].get(off, {})
            d = db_by_period[pname].get(off, {})

            c_chg = c.get("chg_hl", 0) + c.get("chg_sl", 0)
            c_sah = c.get("sah", 0)
            c_pct = round(c_chg / c_sah * 100, 1) if c_sah else 0
            c_hc  = c.get("hc", 0)

            d_chg  = d.get("chg", 0)
            d_sah  = d.get("sah", 0)
            d_abs  = d.get("abs_h", 0)
            d_net  = d_sah - d_abs
            d_hc   = d.get("hc", 0)
            d_pct_g = round(d_chg / d_sah * 100, 1) if d_sah else 0
            d_pct_n = round(d_chg / d_net  * 100, 1) if d_net else 0

            csv_tot_chg += c_chg; csv_tot_sah += c_sah; csv_tot_hc += c_hc
            db_tot_chg  += d_chg; db_tot_sah  += d_sah; db_tot_abs += d_abs; db_tot_hc += d_hc

            print(f"  {off:<10} │ {c_hc:>4} {c_chg:>8.0f} {c_sah:>8.0f} {c_pct:>6.1f}% │ {d_hc:>4} {d_chg:>8.0f} {d_sah:>8.0f} {d_pct_g:>6.1f}% │ {d_pct_n:>9.1f}%")

        print(f"  {'-'*10}-+-{'-'*36}-+-{'-'*36}-+-{'-'*10}")
        csv_tot_pct = round(csv_tot_chg / csv_tot_sah * 100, 1) if csv_tot_sah else 0
        db_tot_pct_g = round(db_tot_chg / db_tot_sah  * 100, 1) if db_tot_sah  else 0
        db_tot_net   = db_tot_sah - db_tot_abs
        db_tot_pct_n = round(db_tot_chg / db_tot_net  * 100, 1) if db_tot_net  else 0
        print(f"  {'TOTAL':<10} │ {csv_tot_hc:>4} {csv_tot_chg:>8.0f} {csv_tot_sah:>8.0f} {csv_tot_pct:>6.1f}% │ {db_tot_hc:>4} {db_tot_chg:>8.0f} {db_tot_sah:>8.0f} {db_tot_pct_g:>6.1f}% │ {db_tot_pct_n:>9.1f}%")

asyncio.run(main())
