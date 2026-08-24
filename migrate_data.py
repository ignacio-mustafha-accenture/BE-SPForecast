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


def quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


async def columns_of(conn, table: str) -> list:
    rows = await conn.fetch("""
        SELECT column_name FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = $1
        ORDER BY ordinal_position
    """, table)
    return [r['column_name'] for r in rows]


async def copy_table(src, dst, table: str, src_cols: list, dst_cols: list) -> int:
    shared = [c for c in src_cols if c in dst_cols]
    if not shared:
        log.warning(f'  {table}: sin columnas en comun, saltando.')
        return 0

    dropped = [c for c in src_cols if c not in dst_cols]
    if dropped:
        log.warning(f'  {table}: columnas ausentes en destino, se omiten: {dropped}')

    col_list     = ', '.join(quote(c) for c in shared)
    placeholders = ', '.join(f'${i + 1}' for i in range(len(shared)))
    insert_sql   = (f'INSERT INTO {quote(table)} ({col_list}) '
                    f'VALUES ({placeholders})')

    inserted = 0
    async with src.transaction():
        cursor = await src.cursor(f'SELECT {col_list} FROM {quote(table)}')
        while True:
            rows = await cursor.fetch(BATCH_SIZE)
            if not rows:
                break
            await dst.executemany(insert_sql, [tuple(r) for r in rows])
            inserted += len(rows)

    return inserted


async def reset_sequences(dst, tables: list) -> int:
    rows = await dst.fetch("""
        SELECT table_name, column_name
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND column_default LIKE 'nextval%'
          AND table_name = ANY($1::text[])
    """, tables)

    fixed = 0
    for r in rows:
        table, column = r['table_name'], r['column_name']
        seq = await dst.fetchval(
            'SELECT pg_get_serial_sequence($1, $2)', f'public.{table}', column
        )
        if not seq:
            continue
        await dst.execute(
            f'SELECT setval($1::regclass, COALESCE((SELECT MAX({quote(column)}) '
            f'FROM {quote(table)}), 0) + 1, false)', seq
        )
        fixed += 1
    return fixed


async def run(apply: bool):
    if not all([SUPABASE['host'], AZURE['host']]):
        log.error('Variables de entorno incompletas. Verificar .env')
        sys.exit(1)

    if SUPABASE['host'] == AZURE['host'] and SUPABASE['database'] == AZURE['database']:
        log.error('Origen y destino son la misma base. Abortando.')
        sys.exit(1)

    start = datetime.now()
    mode = 'APLICANDO' if apply else 'DRY-RUN (no escribe nada)'
    log.info(f'=== Migracion de datos Supabase -> Azure [{mode}] ===')
    log.info(f'  origen:  {SUPABASE["host"]}/{SUPABASE["database"]}')
    log.info(f'  destino: {AZURE["host"]}/{AZURE["database"]}')

    src = await asyncpg.connect(**SUPABASE)
    dst = await asyncpg.connect(**AZURE)

    try:
        plan = []
        for table in TABLES:
            src_cols = await columns_of(src, table)
            dst_cols = await columns_of(dst, table)
            if not src_cols:
                log.warning(f'  {table}: no existe en origen, saltando.')
                continue
            if not dst_cols:
                log.warning(f'  {table}: no existe en destino, saltando.')
                continue
            count = await src.fetchval(f'SELECT count(*) FROM {quote(table)}')
            plan.append((table, src_cols, dst_cols, count))
            log.info(f'  {table:<24} {count:>9,} filas')

        total_src = sum(p[3] for p in plan)
        log.info(f'  {"TOTAL":<24} {total_src:>9,} filas en {len(plan)} tablas')

        if not apply:
            log.info('Dry-run terminado. Volver a correr con --yes para aplicar.')
            return

        targets = [p[0] for p in plan]
        truncate_list = ', '.join(quote(t) for t in reversed(targets))

        total = 0
        async with dst.transaction():
            await dst.execute('SET CONSTRAINTS ALL DEFERRED')

            log.info('Vaciando tablas de destino...')
            await dst.execute(f'TRUNCATE TABLE {truncate_list} CASCADE')

            for table, src_cols, dst_cols, _ in plan:
                n = await copy_table(src, dst, table, src_cols, dst_cols)
                total += n
                log.info(f'  OK {table}: {n:,} filas')

            fixed = await reset_sequences(dst, targets)
            log.info(f'Secuencias reseteadas: {fixed}')

        log.info('Verificando destino...')
        mismatches = []
        for table, _, _, expected in plan:
            got = await dst.fetchval(f'SELECT count(*) FROM {quote(table)}')
            if got != expected:
                mismatches.append((table, expected, got))
                log.warning(f'  {table}: origen {expected:,} vs destino {got:,}')

        if mismatches:
            log.error(f'{len(mismatches)} tablas con diferencias de conteo.')
        else:
            log.info('Conteos coinciden en todas las tablas.')

    finally:
        await src.close()
        await dst.close()

    elapsed = (datetime.now() - start).total_seconds()
    log.info(f'=== {total_src if not apply else total:,} filas en {elapsed:.1f}s ===')


def apply_requested() -> bool:
    if '--yes' in sys.argv:
        return True
    return os.getenv('MIGRATE_APPLY', '').strip().lower() in ('1', 'true', 'yes')


if __name__ == '__main__':
    try:
        asyncio.run(run(apply=apply_requested()))
    except Exception as e:
        log.error(f'Error fatal: {e}')
        sys.exit(1)
