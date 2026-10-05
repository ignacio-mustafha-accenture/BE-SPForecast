import asyncio, sys, csv
from datetime import date
sys.path.insert(0, '.')
from app.config import settings
import asyncpg

MONTH_MAP = {
    'JUL': 'Jul', 'AUG': 'Ago', 'SEP': 'Sep', 'OCT': 'Oct',
    'NOV': 'Nov', 'DEC': 'Dic', 'JAN': 'Ene', 'FEB': 'Feb',
    'MAR': 'Mar', 'APR': 'Abr',
}

PERIODS_2027 = [
    ('Ene-P1-27', date(2027,1,1), date(2027,1,15)),
    ('Ene-P2-27', date(2027,1,16), date(2027,1,31)),
    ('Feb-P1-27', date(2027,2,1), date(2027,2,15)),
    ('Feb-P2-27', date(2027,2,16), date(2027,2,28)),
    ('Mar-P1-27', date(2027,3,1), date(2027,3,15)),
    ('Mar-P2-27', date(2027,3,16), date(2027,3,31)),
    ('Abr-P1-27', date(2027,4,1), date(2027,4,15)),
]

def sf(v):
    try: return float(str(v).replace('%','').strip())
    except: return 0.0

def csv_to_period(col_suffix):
    parts = col_suffix.strip().split()
    month = MONTH_MAP.get(parts[0])
    p = parts[1]
    year = parts[2] if len(parts) > 2 else '2026'
    if not month: return None
    return f"{month}-{p}" if year == '2026' else f"{month}-{p}-27"

async def main():
    conn = await asyncpg.connect(
        host=settings.DB_HOST, port=settings.DB_PORT,
        user=settings.DB_USER, password=settings.DB_PASSWORD,
        database=settings.DB_NAME, ssl='require'
    )

    for name, start, end in PERIODS_2027:
        exists = await conn.fetchval("SELECT period_name FROM periods WHERE period_name=$1", name)
        if not exists:
            await conn.execute("INSERT INTO periods (period_name, start_date, end_date) VALUES ($1, $2, $3)", name, start, end)
            print(f"Created period: {name}")

    valid_eids = {r['eid'] for r in await conn.fetch("SELECT eid FROM employees")}

    with open('data.csv', encoding='utf-8-sig') as f:
        all_rows = list(csv.reader(f))
    
    headers = all_rows[4]
    data = [dict(zip(headers, r)) for r in all_rows[5:] if any(r) and r[11].strip()]

    period_cols = {}
    for h in headers:
        if h.startswith('CHG HL '):
            suffix = h.replace('CHG HL ', '')
            period = csv_to_period(suffix)
            if not period: continue
            sah_matches = [h2 for h2 in headers if h2.startswith('SAH') and suffix.split()[0] in h2 and suffix.split()[1] in h2]
            period_cols[period] = {
                'chg_hl': f'CHG HL {suffix}',
                'chg_sl': f'CHG SL {suffix}',
                'chg': f'CHG NET {suffix}',
                'sah': sah_matches[0] if sah_matches else None,
                'chg_pct': f'CHG% {suffix}',
            }

    print(f"Periods: {list(period_cols.keys())}")
    
    count = skipped = 0
    async with conn.transaction():
        for row in data:
            eid = row['Enterprise ID'].strip()
            if eid not in valid_eids:
                skipped += 1
                continue
            for pname, cols in period_cols.items():
                chg_hl = sf(row.get(cols['chg_hl'], 0))
                chg_sl = sf(row.get(cols['chg_sl'], 0))
                chg = sf(row.get(cols['chg'], 0))
                sah = sf(row.get(cols['sah'], 0)) if cols['sah'] else 0
                chg_pct = sf(row.get(cols['chg_pct'], 0))
                chg_pct_hl = (chg_hl / sah * 100) if sah > 0 else 0
                chg_pct_sl = (chg_sl / sah * 100) if sah > 0 else 0

                await conn.execute("""
                    INSERT INTO forecast_periods (eid, period_name, chg, sah, chg_pct, chg_hl, chg_sl, chg_cascadeadas, absence_hours, chg_pct_sl, chg_pct_hl)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, 0, 0, $8, $9)
                    ON CONFLICT (eid, period_name) DO UPDATE SET
                        chg=$3, sah=$4, chg_pct=$5, chg_hl=$6, chg_sl=$7, chg_pct_sl=$8, chg_pct_hl=$9
                """, eid, pname, chg, sah, chg_pct, chg_hl, chg_sl, chg_pct_sl, chg_pct_hl)
                count += 1

    print(f"OK: {count} cargados, {skipped} skipped")
    await conn.close()

asyncio.run(main())
