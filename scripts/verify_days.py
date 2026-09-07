import asyncio, sys
sys.path.insert(0, '.')
import app.db as db
from app.services.state_service import get_state

async def main():
    await db.create_pool()
    st = await get_state(0)
    con = [e for e in st['employees'] if e.get('DaysToAvailable') is not None]
    print(f"Con DaysToAvailable: {len(con)} de {len(st['employees'])}")
    print()
    for e in sorted(con, key=lambda x: x['DaysToAvailable'])[:12]:
        print(f"  {e['EID']:26} {e['DaysToAvailable']:>6.0f} dias   roll_off={e.get('RollOff')}")
    await db.pool.close()

asyncio.run(main())
