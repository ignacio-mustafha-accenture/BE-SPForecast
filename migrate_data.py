import asyncio
import asyncpg
import logging
import os
import sys
from datetime import datetime
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), '.env'))

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%H:%M:%S',
)
log = logging.getLogger(__name__)

SUPABASE = dict(
    host=os.getenv('SUPABASE_DB_HOST'),
    port=int(os.getenv('DB_PORT', 5432)),
    user=os.getenv('SUPABASE_DB_USER'),
    password=os.getenv('SUPABASE_DB_PASSWORD'),
    database=os.getenv('SUPABASE_DB_NAME'),
    ssl='require',
)

AZURE = dict(
    host=os.getenv('DB_HOST'),
    port=int(os.getenv('DB_PORT', 5432)),
    user=os.getenv('DB_USER'),
    password=os.getenv('DB_PASSWORD'),
    database=os.getenv('DB_NAME'),
    ssl='require',
)

# Orden respeta dependencias de FK
TABLES = [
    'employees',
    'periods',
    'calendar',
    'forecast_periods',
    'forecast_update',
    'absences',
    'chargeability_blocks',
    'client_catalog',
    'holidays',
    'targets',
    'te_approvers',
    'tickets',
    'ppa_log',
    'users',
    'permissions',
    'role_permissions',
    'user_permissions',
    'password_reset_tokens',
    'audit_log',
]

BATCH_SIZE = 500


async def migrate_table(src, dst, table: str) -> int:
    cols = await src.fetch("""
        SELECT column_name FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = $1
        ORDER BY ordinal_position
    """, table)
    col_names = [c['column_name'] for c in cols]

    if not col_names:
        log.warning(f'  {table}: sin columnas, saltando.')
        return 0

    await dst.execute(f'TRUNCATE TABLE {table} CASCADE')

    rows = await src.fetch(f'SELECT * FROM {table}')
    if not rows:
        log.info(f'  {table}: vacia, saltando.')
        return 0

    placeholders = ', '.join(f'${i+1}' for i in range(len(col_names)))
    col_list     = ', '.join(col_names)
    insert_sql   = f'INSERT INTO {table} ({col_list}) VALUES ({placeholders})'

    inserted = 0
    data = [tuple(r[c] for c in col_names) for r in rows]

    for i in range(0, len(data), BATCH_SIZE):
        batch = data[i:i + BATCH_SIZE]
        await dst.executemany(insert_sql, batch)
        inserted += len(batch)

    return inserted


async def run():
    if not all([SUPABASE['host'], AZURE['host']]):
        log.error('Variables de entorno incompletas. Verificar .env')
        sys.exit(1)

    start = datetime.now()
    log.info('=== Migrando datos Supabase -> Azure ===')

    src = await asyncpg.connect(**SUPABASE)
    dst = await asyncpg.connect(**AZURE)

    total = 0
    errors = []

    try:
        for table in TABLES:
            try:
                n = await migrate_table(src, dst, table)
                if n > 0:
                    log.info(f'  OK {table}: {n} filas')
                total += n
            except Exception as e:
                log.warning(f'  WARN {table}: {e}')
                errors.append((table, str(e)))
    finally:
        await src.close()
        await dst.close()

    elapsed = (datetime.now() - start).total_seconds()
    log.info(f'=== {total} filas migradas en {elapsed:.1f}s ===')

    if errors:
        log.warning(f'Tablas con errores: {[e[0] for e in errors]}')
    else:
        log.info('Sin errores.')


if __name__ == '__main__':
    try:
        asyncio.run(run())
    except Exception as e:
        log.error(f'Error fatal: {e}')
        sys.exit(1)