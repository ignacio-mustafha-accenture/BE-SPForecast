"""Verifica que el SAH/CHG de la base coincida con el Excel. Solo lectura."""
import asyncio
import asyncpg
from app.config import settings

ESPERADO = {  # del Excel, 2026 8 2 - Forecast S&P.xlsx
    'agostina.romero':   {'Sep-P1': (88, 88), 'Sep-P2': (96, 96), 'Oct-P1': (80, 80),
                          'Oct-P2': (88, 88), 'Nov-P1': (80, 80)},
    'andres.buzzurro':   {'Sep-P1': (44, 88), 'Sep-P2': (48, 96), 'Oct-P1': (40, 80),
                          'Oct-P2': (44, 88), 'Nov-P1': (40, 80)},
    'antonio.j.guardo':  {'Sep-P1': (100, 88), 'Sep-P2': (96, 96), 'Oct-P1': (80, 80),
                          'Oct-P2': (88, 88), 'Nov-P1': (0, 80)},
}


async def main():
    conn = await asyncpg.connect(
        host=settings.DB_HOST, port=settings.DB_PORT,
        user=settings.DB_USER, password=settings.DB_PASSWORD,
        database=settings.DB_NAME, ssl='require')
    try:
        fallos = 0
        for eid, periodos in ESPERADO.items():
            print(f'=== {eid} ===')
            print(f'  {"periodo":<9} {"esperado CHG/SAH":>18} {"fp":>14} {"daily(SUM edh)":>16}')
            for pn, (echg, esah) in periodos.items():
                fp = await conn.fetchrow(
                    'SELECT chg, sah FROM forecast_periods WHERE eid=$1 AND period_name=$2',
                    eid, pn)
                edh = await conn.fetchrow(
                    """SELECT COALESCE(SUM(edh.sah),0) AS sah,
                              COALESCE(SUM(edh.chg_hl + edh.chg_sl + edh.chg_ppa),0) AS chg
                       FROM employee_daily_hours edh
                       JOIN periods p ON p.period_name=$2
                       WHERE edh.eid=$1 AND edh.date BETWEEN p.start_date AND p.end_date""",
                    eid, pn)
                fp_txt = f'{float(fp["chg"]):g}/{float(fp["sah"]):g}' if fp else 'sin fila'
                e_txt = f'{float(edh["chg"]):g}/{float(edh["sah"]):g}' if edh else '-'
                ok = fp and round(float(fp['sah'])) == esah and round(float(fp['chg'])) == echg
                if not ok:
                    fallos += 1
                print(f'  {pn:<9} {f"{echg}/{esah}":>18} {fp_txt:>14} {e_txt:>16}'
                      f'  {"ok" if ok else "<<< NO COINCIDE"}')
            print()

        nov2 = await conn.fetchval(
            "SELECT COUNT(*) FROM chargeability_blocks WHERE period_name='Nov-P2'")
        print(f'Bloques en Nov-P2 (el Excel no lo trae aun): {nov2}')
        print(f'\n{"TODO OK" if fallos == 0 else f"{fallos} desvios respecto del Excel"}')
    finally:
        await conn.close()


asyncio.run(main())
