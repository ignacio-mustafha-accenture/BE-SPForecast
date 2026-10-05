import asyncio, sys, csv, re
from datetime import datetime
sys.path.insert(0, '.')
from app.config import settings
import asyncpg

def parse_level(level_str):
    m = re.match(r'(\d+)', level_str.strip())
    return int(m.group(1)) if m else None

def parse_date(d):
    if not d or not d.strip(): return None
    for fmt in ('%d/%m/%Y', '%Y-%m-%d'):
        try: return datetime.strptime(d.strip(), fmt).date()
        except: pass
    return None

def parse_comentarios(c):
    if not c or not c.strip(): return None, None, None, None
    if '|' in c:
        parts = c.split('|')
        pct = float(parts[0]) if parts[0].replace('.','').isdigit() else None
        client = parts[1].strip() if len(parts) > 1 else None
        roll_on = parse_date(parts[2]) if len(parts) > 2 else None
        roll_off = parse_date(parts[3]) if len(parts) > 3 else None
        return pct, client, roll_on, roll_off
    if c.startswith('Client:'):
        return None, c.replace('Client:', '').strip(), None, None
    return None, None, None, None

STATUS_MAP = {
    'Active':     ('AR', True, False),
    'LATAM':      ('AR', True, False),
    'NJ':         ('AR', True, True),
    'LOA':        ('AR', True, False),
    'NA':         ('AR', True, False),
    'EMEA':       ('AR', True, False),
    'Internal':   ('AR', True, False),
    'Unassigned': ('AR', True, False),
}

async def main():
    conn = await asyncpg.connect(
        host=settings.DB_HOST, port=settings.DB_PORT,
        user=settings.DB_USER, password=settings.DB_PASSWORD,
        database=settings.DB_NAME, ssl='require'
    )
    with open('data.csv', encoding='utf-8-sig') as f:
        reader = csv.reader(f)
        rows = list(reader)
    headers = rows[4]
    data = [dict(zip(headers, r)) for r in rows[5:] if any(r) and r[11].strip()]
    inserted = updated = fu_upserted = 0
    async with conn.transaction():
        for row in data:
            eid = row['Enterprise ID'].strip()
            status = row['Status'].strip()
            country, active, new_joiner = STATUS_MAP.get(status, ('AR', True, False))
            cl = parse_level(row['Level Name'])
            offering = row['Offerings'].strip() or None
            career_track = row['Sub-Offerings'].strip() or None
            org_unit = row['Previous group'].strip() or None
            fad = parse_date(row['First Availability Date'])
            pct, client, roll_on, roll_off = parse_comentarios(row['Comentarios'])
            exists = await conn.fetchval("SELECT eid FROM employees WHERE eid=$1", eid)
            if exists:
                await conn.execute("""
                    UPDATE employees SET
                        country=COALESCE($2, country), cl=$3, active=$4, new_joiner=$5,
                        offering=$6, career_track=$7, org_unit_level_5=$8, charge=TRUE
                    WHERE eid=$1
                """, eid, country, cl, active, new_joiner, offering, career_track, org_unit)
                updated += 1
            else:
                await conn.execute("""
                    INSERT INTO employees (eid, name, country, cl, active, new_joiner, offering, career_track, org_unit_level_5, charge)
                    VALUES ($1, $1, $2, $3, $4, $5, $6, $7, $8, TRUE)
                """, eid, country, cl, active, new_joiner, offering, career_track, org_unit)
                inserted += 1
            if client or roll_on or roll_off or pct is not None or fad:
                await conn.execute("""
                    INSERT INTO forecast_update (eid, client, roll_on, roll_off, chargeability_pct, first_available, updated_at)
                    VALUES ($1,$2,$3,$4,$5,$6,NOW())
                    ON CONFLICT (eid) DO UPDATE SET
                        client=COALESCE($2, forecast_update.client),
                        roll_on=COALESCE($3, forecast_update.roll_on),
                        roll_off=COALESCE($4, forecast_update.roll_off),
                        chargeability_pct=COALESCE($5, forecast_update.chargeability_pct),
                        first_available=COALESCE($6, forecast_update.first_available),
                        updated_at=NOW()
                """, eid, client, roll_on, roll_off, pct, fad)
                fu_upserted += 1
    print(f"Insertados:  {inserted}")
    print(f"Actualizados: {updated}")
    print(f"Forecast update: {fu_upserted}")
    await conn.close()

asyncio.run(main())
