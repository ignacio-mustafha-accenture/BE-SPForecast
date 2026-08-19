"""
fix_sequences.py
Resetea las secuencias de todas las tablas para evitar conflictos de PK
despues de una migracion de datos.
Uso: python fix_sequences.py
"""
import asyncio
import asyncpg
import os
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), '.env'))

AZURE = dict(
    host=os.getenv('DB_HOST'),
    port=int(os.getenv('DB_PORT', 5432)),
    user=os.getenv('DB_USER'),
    password=os.getenv('DB_PASSWORD'),
    database=os.getenv('DB_NAME'),
    ssl='require',
)

TABLES = [
    'users', 'audit_log', 'tickets', 'absences',
    'chargeability_blocks', 'ppa_log', 'periods',
    'employees', 'forecast_periods', 'holidays',
    'employee_daily_hours', 'permissions', 'role_permissions',
    'user_permissions', 'password_reset_tokens', 'calendar',
    'forecast_update', 'client_catalog', 'targets', 'te_approvers',
]


async def main():
    conn = await asyncpg.connect(**AZURE)
    try:
        for table in TABLES:
            try:
                seq = await conn.fetchval(
                    "SELECT pg_get_serial_sequence($1, 'id')", table
                )
                if not seq:
                    print(f'  SKIP {table}: sin secuencia')
                    continue
                await conn.execute(f"""
                    SELECT setval('{seq}', COALESCE((SELECT MAX(id) FROM {table}), 1))
                """)
                max_id = await conn.fetchval(f'SELECT MAX(id) FROM {table}')
                print(f'  OK {table}: secuencia reseteada a {max_id or 1}')
            except Exception as e:
                print(f'  WARN {table}: {e}')
    finally:
        await conn.close()
    print('Secuencias reseteadas.')


asyncio.run(main())