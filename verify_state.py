import asyncio, sys, json
sys.path.insert(0, '.')
import app.db as db
from app.services.state_service import get_state

async def main():
    await db.create_pool()
    state = await get_state(0)

    p0 = state["periods"][0]
    print(f"Periodo: {p0['period_name']}")
    print(f"SAH por pais: {p0.get('sah_by_country')}")

    emp = next((e for e in state["employees"] if e["EID"] == "cecilia.arato"), None)
    if emp:
        print(f"\ncecilia.arato:")
        print(f"  chg_neto: {emp.get('chg_neto')}")
        print(f"  DaysToAvailable: {emp.get('DaysToAvailable')}")

    con_dias = [e for e in state["employees"] if e.get("DaysToAvailable") is not None]
    print(f"\nEmpleados con DaysToAvailable: {len(con_dias)} de {len(state['employees'])}")
    for e in con_dias[:5]:
        print(f"  {e['EID']}: {e['DaysToAvailable']} dias (FAD {e.get('FAD')})")

    await db.pool.close()

asyncio.run(main())
