"""
verificar_forecast.py
---------------------
Compara lo que quedó en la base contra el bloque de totales de los Excel, y
revisa las inconsistencias que ya nos rompieron los números antes.

Reemplaza los scripts diag_* y check_* sueltos: en vez de mirar un síntoma por
vez, corre todos los chequeos y muestra sólo lo que está mal.

Es de sólo lectura. Se corre después de import_forecast.py y
create_daily_hours.py.

Uso:
    python scripts\\verificar_forecast.py
    python scripts\\verificar_forecast.py --periodo Sep-P2
"""

import argparse
import asyncio
import os
import sys
from collections import defaultdict

import asyncpg
from dotenv import load_dotenv

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(RAIZ, '.env'))

AZURE = dict(
    host=os.getenv('DB_HOST'),
    port=int(os.getenv('DB_PORT', 5432)),
    user=os.getenv('DB_USER'),
    password=os.getenv('DB_PASSWORD'),
    database=os.getenv('DB_NAME'),
    ssl='require',
)

ORDEN_OFFERING = ['SO', 'PR', 'Tools', 'S4', 'Ariba', 'Oracle']
PAISES = [('AR', 'Total S&P Arg'), ('MX', 'Total S&P Mexico'), ('CR', 'Total S&P Costa Rica')]

# Valores del bloque de totales, para no depender de tener los Excel a mano.
# Actualizar cuando cambie el archivo del mes.
ESPERADO = {
    'Sep-P1': {
        ('AR', 'SO'):     (34, 2191, 2456),
        ('AR', 'PR'):     (15,  590,  944),
        ('AR', 'Tools'):  (18, 1127, 1152),
        ('AR', 'S4'):     (26, 1840, 1912),
        ('AR', 'Ariba'):  (14,  866, 1080),
        ('AR', 'Oracle'): ( 1,    0,   72),
        ('MX', 'SO'):     ( 6,  330,  528),
        ('MX', 'Tools'):  ( 1,   72,   88),
        ('CR', 'SO'):     ( 1,   88,   88),
        ('CR', 'PR'):     ( 1,   88,   88),
    },
}

TOLERANCIA_HORAS = 2   # el Excel trae decimales y la base redondea por empleado

OK = '  OK  '
MAL = ' MAL  '


def linea(label, hc, chg, sah, indent='', ref=None):
    pct = chg / sah * 100 if sah else 0
    txt = (f"  {indent}{label:22} {hc:4} {chg:8.0f} {sah:8.0f} {pct:7.1f}%")
    if ref is None:
        print(txt)
        return True
    hc_e, chg_e, sah_e = ref
    ok = (hc == hc_e
          and abs(chg - chg_e) <= TOLERANCIA_HORAS
          and abs(sah - sah_e) <= TOLERANCIA_HORAS)
    if ok:
        print(f'{txt}   {OK}')
    else:
        print(f'{txt}   {MAL}  excel: hc={hc_e} chg={chg_e} sah={sah_e}')
    return ok


async def totales(conn, periodo):
    """Reproduce el agrupado de totals_service: pais desde employees.country,
    offering desde forecast_update (CTE latest_fu), no desde employees."""
    rows = await conn.fetch(
        """
        WITH latest_fu AS (
            SELECT DISTINCT ON (eid) eid, offering FROM forecast_update
            ORDER BY eid, updated_at DESC NULLS LAST
        )
        SELECT COALESCE(NULLIF(TRIM(e.country), ''), 'SIN PAIS') AS pais,
               COALESCE(NULLIF(TRIM(fu.offering), ''), 'SIN OFFERING') AS offering,
               COUNT(*)                                    AS hc,
               COALESCE(SUM(fp.chg_hl), 0)                 AS hl,
               COALESCE(SUM(fp.chg_sl), 0)                 AS sl,
               COALESCE(SUM(fp.sah), 0)                    AS sah
        FROM employees e
        LEFT JOIN latest_fu fu ON fu.eid = e.eid
        LEFT JOIN forecast_periods fp
               ON fp.eid = e.eid AND fp.period_name = $1
        WHERE e.active
        GROUP BY 1, 2
        """, periodo)

    agg = {}
    for r in rows:
        agg[(r['pais'], r['offering'])] = {
            'hc': r['hc'], 'hl': float(r['hl']),
            'sl': float(r['sl']), 'sah': float(r['sah']),
        }
    return agg


async def main(periodo):
    conn = await asyncpg.connect(**AZURE)
    problemas = []
    try:
        agg = await totales(conn, periodo)
        ref = ESPERADO.get(periodo)

        print()
        print(f'  === {periodo} · totales de la base ===')
        if ref:
            print('  (comparado contra el bloque de totales del Excel)')
        else:
            print(f'  (sin valores de referencia cargados para {periodo}, '
                  'ver ESPERADO en este script)')
        print(f"  {'grupo':22} {'HC':>4} {'CHG':>8} {'SAH':>8} {'CHG%':>8}")
        print('  ' + '-' * 56)

        total = {'hc': 0, 'hl': 0.0, 'sl': 0.0, 'sah': 0.0}
        for pais, nombre in PAISES:
            offs = [(o, a) for (p, o), a in agg.items() if p == pais]
            if not offs:
                continue
            sub = {'hc': 0, 'hl': 0.0, 'sl': 0.0, 'sah': 0.0}
            for _o, a in offs:
                for k in sub:
                    sub[k] += a[k]
            linea(nombre, sub['hc'], sub['hl'] + sub['sl'], sub['sah'])
            for o in ORDEN_OFFERING:
                for oo, a in offs:
                    if oo != o:
                        continue
                    esperado = ref.get((pais, o)) if ref else None
                    ok = linea(o, a['hc'], a['hl'] + a['sl'], a['sah'],
                               indent='  ', ref=esperado)
                    if not ok:
                        problemas.append(f'{pais}/{o} no coincide con el Excel')
            for k in total:
                total[k] += sub[k]

        # Los que no caen en ningun pais o offering conocido: no entran a ningun
        # grupo de la app y su SAH queda fuera del denominador.
        huerfanos = {k: v for k, v in agg.items()
                     if k[0] not in dict(PAISES) or k[1] == 'SIN OFFERING'}
        print('  ' + '-' * 56)
        linea('TOTAL', total['hc'], total['hl'] + total['sl'], total['sah'])
        print()

        if huerfanos:
            problemas.append(f'{len(huerfanos)} grupos sin pais u offering valido')
            print('  --- sin pais u offering valido (no entran a ningun total) ---')
            for (p, o), a in sorted(huerfanos.items()):
                print(f"    {p:14} {o:16} hc={a['hc']}")
            print()

        # --- offering desalineado entre las dos tablas
        desal = await conn.fetch(
            """
            SELECT e.eid, e.offering AS emp_off, fu.offering AS fu_off
            FROM employees e
            LEFT JOIN (
                SELECT DISTINCT ON (eid) eid, offering FROM forecast_update
                ORDER BY eid, updated_at DESC NULLS LAST
            ) fu ON fu.eid = e.eid
            WHERE e.active AND COALESCE(e.offering,'') <> COALESCE(fu.offering,'')
            ORDER BY e.eid
            """)
        if desal:
            problemas.append(f'{len(desal)} empleados con offering desalineado')
            print(f'  --- offering distinto entre employees y forecast_update '
                  f'({len(desal)}) ---')
            for r in desal[:15]:
                print(f"    {r['eid']:26} employees={r['emp_off']} "
                      f"forecast_update={r['fu_off']}")
            print()

        # --- country con formato largo
        pais_raro = await conn.fetch(
            """
            SELECT country, COUNT(*) n FROM employees
            WHERE active AND (country IS NULL OR country NOT IN ('AR','MX','CR'))
            GROUP BY 1 ORDER BY 2 DESC
            """)
        if pais_raro:
            problemas.append('hay country con formato distinto de AR/MX/CR')
            print('  --- country que el filtro de Pais no reconoce ---')
            for r in pais_raro:
                print(f"    {str(r['country']):16} {r['n']} empleados")
            print()

        # --- SAH sin bloques y bloques sin SAH
        inconsistentes = await conn.fetchrow(
            """
            SELECT
              COUNT(*) FILTER (
                WHERE fp.sah > 0 AND NOT EXISTS (
                  SELECT 1 FROM chargeability_blocks cb
                  WHERE cb.eid = fp.eid AND cb.period_name = fp.period_name)
              ) AS sah_sin_bloques,
              COUNT(*) FILTER (
                WHERE COALESCE(fp.sah,0) = 0 AND EXISTS (
                  SELECT 1 FROM chargeability_blocks cb
                  WHERE cb.eid = fp.eid AND cb.period_name = fp.period_name)
              ) AS bloques_sin_sah
            FROM forecast_periods fp
            JOIN employees e ON e.eid = fp.eid AND e.active
            WHERE fp.period_name = $1
            """, periodo)
        if inconsistentes['bloques_sin_sah']:
            problemas.append('hay bloques con SAH en cero')
            print(f"  --- {inconsistentes['bloques_sin_sah']} empleados con bloques "
                  'pero SAH en 0: el CHG queda en cero ---')
            print()

        # --- CHG por encima del SAH
        sobre = await conn.fetch(
            """
            SELECT fp.eid, fp.sah, fp.chg_hl + fp.chg_sl AS chg,
                   ROUND((fp.chg_hl + fp.chg_sl) / NULLIF(fp.sah,0) * 100, 1) AS pct
            FROM forecast_periods fp
            JOIN employees e ON e.eid = fp.eid AND e.active
            WHERE fp.period_name = $1 AND fp.sah > 0
              AND (fp.chg_hl + fp.chg_sl) > fp.sah * 1.05
            ORDER BY 4 DESC
            """, periodo)
        if sobre:
            print(f'  --- {len(sobre)} empleados con CHG por encima del SAH ---')
            print('  (viene del Excel, no es un error de carga, pero conviene '
                  'tenerlo a mano si preguntan)')
            for r in sobre[:10]:
                print(f"    {r['eid']:26} chg={float(r['chg']):6.0f} "
                      f"sah={float(r['sah']):5.0f}  {float(r['pct']):6.1f}%")
            print()

        # --- horas diarias al dia
        edh = await conn.fetchrow(
            """
            SELECT COUNT(*) AS filas, MAX(created_at) AS ultima
            FROM employee_daily_hours
            """)
        fp_upd = await conn.fetchval(
            'SELECT MAX(created_at) FROM chargeability_blocks')
        print(f"  employee_daily_hours: {edh['filas']} filas")
        if edh['ultima'] and fp_upd and edh['ultima'] < fp_upd:
            problemas.append('employee_daily_hours esta desactualizada')
            print(f"    ultima escritura {edh['ultima']} es anterior a la de "
                  f'chargeability_blocks ({fp_upd})')
            print('    -> correr create_daily_hours.py')
        print()

        print('  ' + '=' * 56)
        if problemas:
            print(f'  {len(problemas)} cosas para revisar:')
            for p in problemas:
                print(f'    - {p}')
        else:
            print('  Todo consistente.')
        print()
        return 1 if problemas else 0
    finally:
        await conn.close()


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--periodo', default='Sep-P1',
                    help='periodo a verificar (default Sep-P1)')
    args = ap.parse_args()

    if not all([AZURE['host'], AZURE['user'], AZURE['password'], AZURE['database']]):
        raise SystemExit('Faltan variables de DB en .env')

    try:
        sys.exit(asyncio.run(main(args.periodo)))
    except KeyboardInterrupt:
        sys.exit(130)