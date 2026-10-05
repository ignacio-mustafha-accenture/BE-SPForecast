import asyncio, sys
sys.path.insert(0, '.')
from app.config import settings
import asyncpg

async def main():
    conn = await asyncpg.connect(
        host=settings.DB_HOST, port=settings.DB_PORT,
        user=settings.DB_USER, password=settings.DB_PASSWORD,
        database=settings.DB_NAME, ssl='require'
    )
    async with conn.transaction():
        await conn.execute("""
            ALTER TABLE forecast_periods
              ADD COLUMN IF NOT EXISTS chg_cascadeadas_hl NUMERIC DEFAULT 0,
              ADD COLUMN IF NOT EXISTS chg_cascadeadas_sl NUMERIC DEFAULT 0
        """)
        print("OK — columnas chg_cascadeadas_hl y chg_cascadeadas_sl agregadas a forecast_periods")

        updated = await conn.fetchval("""
            WITH upd AS (
                UPDATE forecast_periods
                SET chg_cascadeadas_hl = COALESCE(chg_cascadeadas, 0),
                    chg_cascadeadas_sl = 0
                WHERE COALESCE(chg_cascadeadas, 0) <> 0
                RETURNING 1
            )
            SELECT COUNT(*) FROM upd
        """)
        print(f"OK — backfill histórico: {updated} filas con chg_cascadeadas_hl = chg_cascadeadas")

        await conn.execute("""
            ALTER TABLE ppa_log
              ADD COLUMN IF NOT EXISTS reversed_at  TIMESTAMP,
              ADD COLUMN IF NOT EXISTS reversed_by  VARCHAR(255)
        """)
        print("OK — columnas reversed_at y reversed_by agregadas a ppa_log")

        await conn.execute("""
            ALTER TABLE ppa_log DROP CONSTRAINT IF EXISTS ppa_log_status_check
        """)
        await conn.execute("""
            ALTER TABLE ppa_log ADD CONSTRAINT ppa_log_status_check
              CHECK (status IN ('pending', 'approved', 'rejected', 'reversed'))
        """)
        print("OK — constraint status expandida para admitir 'reversed'")

    await conn.close()
    print("Migration completada.")

asyncio.run(main())
