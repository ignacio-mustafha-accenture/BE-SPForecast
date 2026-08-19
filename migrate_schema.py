import asyncio
import asyncpg
import logging
import sys
import os
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

TABLES = [
    'employees', 'periods', 'calendar', 'forecast_periods', 'forecast_update',
    'absences', 'chargeability_blocks', 'client_catalog', 'holidays',
    'targets', 'te_approvers', 'tickets', 'ppa_log',
    'users', 'permissions', 'role_permissions', 'user_permissions',
    'password_reset_tokens', 'audit_log',
]

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
EXTRA_SQL_FILES = [
    os.path.join(SCRIPT_DIR, 'schema_auth.sql'),
    os.path.join(SCRIPT_DIR, 'schema_clients.sql'),
    os.path.join(SCRIPT_DIR, 'schema_holidays.sql'),
]

PG_TYPE_MAP = {
    'integer':                      'INTEGER',
    'bigint':                       'BIGINT',
    'smallint':                     'SMALLINT',
    'boolean':                      'BOOLEAN',
    'text':                         'TEXT',
    'numeric':                      'NUMERIC',
    'real':                         'REAL',
    'double precision':             'DOUBLE PRECISION',
    'timestamp without time zone':  'TIMESTAMP',
    'timestamp with time zone':     'TIMESTAMPTZ',
    'date':                         'DATE',
    'time without time zone':       'TIME',
    'jsonb':                        'JSONB',
    'json':                         'JSON',
    'uuid':                         'UUID',
    'bytea':                        'BYTEA',
    'inet':                         'INET',
}


def map_type(col):
    if col['character_maximum_length']:
        return f"VARCHAR({col['character_maximum_length']})"
    return PG_TYPE_MAP.get(col['data_type'], col['data_type'].upper())


def resolve_serial(col):
    """Convierte nextval() en SERIAL/BIGSERIAL y devuelve (tipo, default)."""
    default = col['column_default']
    if default and 'nextval' in default:
        if col['data_type'] == 'integer':
            return 'SERIAL', None
        if col['data_type'] == 'bigint':
            return 'BIGSERIAL', None
    return map_type(col), default


async def extract_schema(src):
    log.info('Extrayendo schema de Supabase...')
    ddl_parts = []

    for table in TABLES:
        cols = await src.fetch("""
            SELECT column_name, data_type, character_maximum_length,
                   is_nullable, column_default
            FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = $1
            ORDER BY ordinal_position
        """, table)

        if not cols:
            log.warning(f'  Tabla {table!r} no encontrada en Supabase, saltando.')
            continue

        pks = await src.fetch("""
            SELECT kcu.column_name
            FROM information_schema.table_constraints tc
            JOIN information_schema.key_column_usage kcu
              ON tc.constraint_name = kcu.constraint_name
             AND tc.table_schema    = kcu.table_schema
            WHERE tc.constraint_type = 'PRIMARY KEY'
              AND tc.table_schema    = 'public'
              AND tc.table_name      = $1
            ORDER BY kcu.ordinal_position
        """, table)
        pk_cols = [r['column_name'] for r in pks]

        col_defs = []
        for c in cols:
            dt, default = resolve_serial(c)
            nullable    = '' if c['is_nullable'] == 'YES' else ' NOT NULL'
            default_str = f' DEFAULT {default}' if default else ''
            col_defs.append(f"  {c['column_name']} {dt}{default_str}{nullable}")

        if pk_cols:
            col_defs.append(f"  PRIMARY KEY ({', '.join(pk_cols)})")

        ddl = (
            f"-- {table}\n"
            f"CREATE TABLE IF NOT EXISTS {table} (\n"
            + ',\n'.join(col_defs)
            + '\n);\n'
        )
        ddl_parts.append(ddl)
        log.info(f'  OK {table} ({len(cols)} columnas)')

    return '\n'.join(ddl_parts)


async def apply_sql(dst, sql, label):
    try:
        async with dst.transaction():
            await dst.execute(sql)
        log.info(f'  OK {label}')
        return True
    except asyncpg.PostgresError as e:
        log.warning(f'  WARN {label}: {e}')
        return False


async def run_migration():
    if not all([SUPABASE['host'], AZURE['host']]):
        log.error('Variables de entorno incompletas. Verificar .env')
        sys.exit(1)

    start = datetime.now()
    log.info('=== Iniciando migracion Supabase -> Azure ===')

    src = await asyncpg.connect(**SUPABASE)
    dst = await asyncpg.connect(**AZURE)

    try:
        schema_sql = await extract_schema(src)

        log.info('Aplicando schema principal en Azure...')
        await apply_sql(dst, schema_sql, 'schema principal')

        log.info('Aplicando schemas adicionales...')
        for filepath in EXTRA_SQL_FILES:
            if not os.path.exists(filepath):
                log.warning(f'  {os.path.basename(filepath)} no encontrado, saltando.')
                continue
            sql = open(filepath, encoding='utf-8').read()
            await apply_sql(dst, sql, os.path.basename(filepath))

        tables_in_azure = await dst.fetch("""
            SELECT table_name FROM information_schema.tables
            WHERE table_schema = 'public' AND table_type = 'BASE TABLE'
            ORDER BY table_name
        """)
        names = [r['table_name'] for r in tables_in_azure]
        log.info(f'Tablas en Azure ({len(names)}): {names}')

        missing = [t for t in TABLES if t not in names]
        if missing:
            log.warning(f'Tablas faltantes: {missing}')
        else:
            log.info('Todas las tablas creadas correctamente.')

    finally:
        await src.close()
        await dst.close()

    elapsed = (datetime.now() - start).total_seconds()
    log.info(f'=== Migracion completa en {elapsed:.1f}s ===')


if __name__ == '__main__':
    try:
        asyncio.run(run_migration())
    except Exception as e:
        log.error(f'Error fatal: {e}')
        sys.exit(1)