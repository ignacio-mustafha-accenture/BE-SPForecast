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
    port=int(os.getenv('SUPABASE_DB_PORT', os.getenv('DB_PORT', 5432))),
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

ALREADY_EXISTS = ('42710', '42P07', '42P16')


def quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def map_type(col):
    if col['character_maximum_length']:
        return f"VARCHAR({col['character_maximum_length']})"
    if col['data_type'] == 'numeric' and col['numeric_precision']:
        return f"NUMERIC({col['numeric_precision']},{col['numeric_scale'] or 0})"
    return PG_TYPE_MAP.get(col['data_type'], col['data_type'].upper())


def resolve_serial(col):
    default = col['column_default']
    if default and 'nextval' in default:
        if col['data_type'] == 'integer':
            return 'SERIAL', None
        if col['data_type'] == 'bigint':
            return 'BIGSERIAL', None
    return map_type(col), default


async def extract_tables(src):
    log.info('Extrayendo tablas de Supabase...')
    statements = []

    for table in TABLES:
        cols = await src.fetch("""
            SELECT column_name, data_type, character_maximum_length,
                   numeric_precision, numeric_scale,
                   is_nullable, column_default
            FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = $1
            ORDER BY ordinal_position
        """, table)

        if not cols:
            log.warning(f'  Tabla {table!r} no encontrada en Supabase, saltando.')
            continue

        pks = await src.fetch("""
            SELECT a.attname AS column_name
            FROM pg_constraint c
            JOIN pg_class rel ON rel.oid = c.conrelid
            JOIN pg_namespace n ON n.oid = rel.relnamespace
            JOIN unnest(c.conkey) WITH ORDINALITY AS k(attnum, ord) ON TRUE
            JOIN pg_attribute a ON a.attrelid = rel.oid AND a.attnum = k.attnum
            WHERE n.nspname = 'public' AND rel.relname = $1 AND c.contype = 'p'
            ORDER BY k.ord
        """, table)
        pk_cols = [r['column_name'] for r in pks]

        col_defs = []
        for c in cols:
            dt, default = resolve_serial(c)
            nullable    = '' if c['is_nullable'] == 'YES' else ' NOT NULL'
            default_str = f' DEFAULT {default}' if default else ''
            col_defs.append(f"  {quote(c['column_name'])} {dt}{default_str}{nullable}")

        if pk_cols:
            cols_sql = ', '.join(quote(c) for c in pk_cols)
            col_defs.append(f"  PRIMARY KEY ({cols_sql})")

        statements.append((
            table,
            f"CREATE TABLE IF NOT EXISTS {quote(table)} (\n"
            + ',\n'.join(col_defs)
            + '\n)'
        ))
        log.info(f'  OK {table} ({len(cols)} columnas)')

    return statements


async def extract_constraints(src):
    log.info('Extrayendo constraints (FK, UNIQUE, CHECK)...')
    rows = await src.fetch("""
        SELECT rel.relname     AS table_name,
               c.conname       AS name,
               c.contype::text AS kind,
               pg_get_constraintdef(c.oid) AS def
        FROM pg_constraint c
        JOIN pg_class rel     ON rel.oid = c.conrelid
        JOIN pg_namespace n   ON n.oid = rel.relnamespace
        WHERE n.nspname = 'public'
          AND c.contype IN ('f', 'u', 'c')
          AND rel.relname = ANY($1::text[])
        ORDER BY c.contype, rel.relname, c.conname
    """, TABLES)

    statements = []
    for r in rows:
        definition = r['def']
        if r['kind'] == 'f' and 'DEFERRABLE' not in definition.upper():
            definition += ' DEFERRABLE INITIALLY IMMEDIATE'
        statements.append((
            f"{r['table_name']}.{r['name']}",
            f"ALTER TABLE {quote(r['table_name'])} "
            f"ADD CONSTRAINT {quote(r['name'])} {definition}"
        ))

    kinds = {'f': 0, 'u': 0, 'c': 0}
    for r in rows:
        kinds[r['kind']] = kinds.get(r['kind'], 0) + 1
    log.info(f"  {kinds.get('f', 0)} foreign keys, "
             f"{kinds.get('u', 0)} unique, {kinds.get('c', 0)} check")
    return statements


async def extract_indexes(src):
    log.info('Extrayendo indices...')
    rows = await src.fetch("""
        SELECT t.relname AS table_name,
               ic.relname AS name,
               pg_get_indexdef(i.indexrelid) AS def
        FROM pg_index i
        JOIN pg_class t       ON t.oid = i.indrelid
        JOIN pg_class ic      ON ic.oid = i.indexrelid
        JOIN pg_namespace n   ON n.oid = t.relnamespace
        WHERE n.nspname = 'public'
          AND t.relname = ANY($1::text[])
          AND NOT i.indisprimary
          AND NOT EXISTS (
              SELECT 1 FROM pg_constraint c WHERE c.conindid = i.indexrelid
          )
        ORDER BY t.relname, ic.relname
    """, TABLES)

    statements = []
    for r in rows:
        ddl = r['def']
        for prefix in ('CREATE UNIQUE INDEX ', 'CREATE INDEX '):
            if ddl.startswith(prefix):
                ddl = ddl.replace(prefix, prefix + 'IF NOT EXISTS ', 1)
                break
        statements.append((f"{r['table_name']}.{r['name']}", ddl))

    log.info(f'  {len(statements)} indices')
    return statements


async def extract_functions(src):
    log.info('Extrayendo funciones y procedures...')
    rows = await src.fetch("""
        SELECT p.proname AS name, pg_get_functiondef(p.oid) AS def
        FROM pg_proc p
        JOIN pg_namespace n ON n.oid = p.pronamespace
        WHERE n.nspname = 'public'
          AND p.prokind IN ('f', 'p')
          AND NOT EXISTS (
              SELECT 1 FROM pg_depend d
              WHERE d.objid = p.oid AND d.deptype = 'e'
          )
        ORDER BY p.proname
    """)
    statements = [(r['name'], r['def']) for r in rows]
    log.info(f'  {len(statements)} rutinas')
    return statements


async def apply_statements(dst, statements, label):
    ok = skipped = failed = 0
    for name, sql in statements:
        try:
            await dst.execute(sql)
            ok += 1
        except asyncpg.PostgresError as e:
            if getattr(e, 'sqlstate', None) in ALREADY_EXISTS:
                skipped += 1
            else:
                failed += 1
                log.warning(f'  FALLO {name}: {e}')
    log.info(f'  {label}: {ok} aplicados, {skipped} ya existian, {failed} fallaron')
    return failed


async def run_migration():
    if not all([SUPABASE['host'], AZURE['host']]):
        log.error('Variables de entorno incompletas. Verificar .env')
        sys.exit(1)

    if SUPABASE['host'] == AZURE['host'] and SUPABASE['database'] == AZURE['database']:
        log.error('Origen y destino son la misma base. Abortando.')
        sys.exit(1)

    start = datetime.now()
    log.info('=== Migracion de schema Supabase -> Azure ===')
    log.info(f'  destino: {AZURE["host"]}/{AZURE["database"]}')

    src = await asyncpg.connect(**SUPABASE)
    dst = await asyncpg.connect(**AZURE)

    total_failed = 0

    try:
        log.info('Aplicando archivos SQL versionados...')
        for filepath in EXTRA_SQL_FILES:
            if not os.path.exists(filepath):
                log.warning(f'  {os.path.basename(filepath)} no encontrado, saltando.')
                continue
            with open(filepath, encoding='utf-8') as fh:
                sql = fh.read()
            try:
                await dst.execute(sql)
                log.info(f'  OK {os.path.basename(filepath)}')
            except asyncpg.PostgresError as e:
                total_failed += 1
                log.warning(f'  FALLO {os.path.basename(filepath)}: {e}')

        tables = await extract_tables(src)
        total_failed += await apply_statements(dst, tables, 'tablas')

        constraints = await extract_constraints(src)
        total_failed += await apply_statements(dst, constraints, 'constraints')

        indexes = await extract_indexes(src)
        total_failed += await apply_statements(dst, indexes, 'indices')

        functions = await extract_functions(src)
        total_failed += await apply_statements(dst, functions, 'rutinas')

        present = await dst.fetch("""
            SELECT table_name FROM information_schema.tables
            WHERE table_schema = 'public' AND table_type = 'BASE TABLE'
            ORDER BY table_name
        """)
        names = [r['table_name'] for r in present]
        log.info(f'Tablas en destino ({len(names)}): {names}')

        missing = [t for t in TABLES if t not in names]
        if missing:
            log.warning(f'Tablas faltantes: {missing}')
        else:
            log.info('Todas las tablas esperadas existen.')

        fk_count = await dst.fetchval("""
            SELECT count(*) FROM pg_constraint c
            JOIN pg_class rel ON rel.oid = c.conrelid
            JOIN pg_namespace n ON n.oid = rel.relnamespace
            WHERE n.nspname = 'public' AND c.contype = 'f'
        """)
        log.info(f'Foreign keys en destino: {fk_count}')

    finally:
        await src.close()
        await dst.close()

    elapsed = (datetime.now() - start).total_seconds()
    log.info(f'=== Schema migrado en {elapsed:.1f}s ===')

    if total_failed:
        log.error(f'{total_failed} sentencias fallaron. Revisar antes de migrar datos.')
        sys.exit(1)


if __name__ == '__main__':
    try:
        asyncio.run(run_migration())
    except Exception as e:
        log.error(f'Error fatal: {e}')
        sys.exit(1)
