"""
sync_forecast.py — Sincronización completa Excel → DB

Fases:
  1. Leer y parsear Excel/CSV
  2. Resolver EIDs contra employees
  3. Leer estado actual de la DB
  4. Diff + detección de conflictos con tickets aprobados
  5. Actualizar forecast_update y employees
  6. Recrear chargeability_blocks (presente + futuro)
  7. Recalcular forecast_periods
  8. Output / dry-run

Uso:
  python sync_forecast.py forecast.xlsx           # dry-run
  python sync_forecast.py forecast.xlsx --apply   # aplica en una transacción atómica
"""

import argparse
import asyncio
import csv
import datetime
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, ".")
import asyncpg
from app.config import settings

# ─── Constantes (igual a import_forecast.py) ────────────────────────────────

COLUMNAS = {
    "eid": ["EID"],
    "location": ["Location"],
    "offering": ["Offering Label"],
    "cl": ["CL"],
    "cliente": ["Cliente"],
    "chargeability_pct": ["Chargeability %", "Chargeability", "Chg %"],
    "status": ["Status"],
    "hire_date": ["Hire Date"],
    "office": ["Office"],
    "te_approver": ["T&E approver (level 7)", "T&E approver"],
    "roll_on": ["Roll-on"],
    "roll_off": ["Roll-off"],
    "first_available": ["First"],
    "next_client": ["Next Client"],
}
OBLIGATORIAS = ["eid", "offering", "cliente"]
VALID_OFFERINGS = {"SO", "PR", "Tools", "S4", "Ariba", "Oracle"}
JUNK = ("assumptions", "cascadeo", "lista de", "feriados", "total", "hc ")
LOCATION_TO_COUNTRY = {"ARG": "AR", "MX": "MX", "CR": "CR"}
FORMATOS_FECHA = ("%d-%b-%y", "%d-%b-%Y", "%d/%m/%Y", "%Y-%m-%d", "%d/%m/%y")

TODAY = datetime.date.today()

# ─── Helpers de parseo ───────────────────────────────────────────────────────

def norm(s):
    return " ".join(str(s or "").split()).strip().lower()


def leer_filas(path):
    if path.suffix.lower() in (".xlsx", ".xlsm"):
        import openpyxl
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
        hoja = next((h for h in wb.sheetnames if norm(h) == "forecast update"), None)
        if hoja is None:
            raise SystemExit("No encuentro la hoja 'Forecast Update'. Hay: %s" % wb.sheetnames)
        filas = [["" if c is None else c for c in f] for f in wb[hoja].iter_rows(values_only=True)]
        wb.close()
        return filas
    with open(path, encoding="utf-8-sig", errors="replace") as f:
        return list(csv.reader(f))


def ubicar_encabezado(filas):
    for i, fila in enumerate(filas[:30]):
        celdas = [norm(c) for c in fila]
        if "eid" not in celdas:
            continue
        mapa = {}
        for interno, cands in COLUMNAS.items():
            for cand in cands:
                if norm(cand) in celdas:
                    mapa[interno] = celdas.index(norm(cand))
                    break
        if [c for c in OBLIGATORIAS if c not in mapa]:
            continue
        return i, mapa
    raise SystemExit("No encontré la fila de encabezado con EID, Offering Label y Cliente.")


def celda(fila, mapa, clave):
    i = mapa.get(clave)
    if i is None or i >= len(fila):
        return None
    v = fila[i]
    if isinstance(v, (datetime.datetime, datetime.date)):
        return v.date() if isinstance(v, datetime.datetime) else v
    s = str(v).strip()
    return s or None


def a_fecha(v):
    if v is None:
        return None
    if isinstance(v, datetime.date):
        return v
    s = str(v).strip()
    if not s or s.startswith("00/"):
        return None
    for fmt in FORMATOS_FECHA:
        try:
            d = datetime.datetime.strptime(s, fmt).date()
            return d if d.year >= 2000 else None
        except ValueError:
            continue
    return None


def a_entero(v):
    try:
        return int(float(str(v).strip()))
    except (TypeError, ValueError):
        return None


def a_float(v):
    try:
        return float(str(v).strip())
    except (TypeError, ValueError):
        return None


# ─── Fase 1: Parsear archivo ─────────────────────────────────────────────────

def parsear(path):
    filas = leer_filas(path)
    h, mapa = ubicar_encabezado(filas)
    print("Encabezado en la fila %d" % (h + 1))
    print("Columnas mapeadas:")
    for k, v in sorted(mapa.items(), key=lambda x: x[1]):
        print("   %-20s -> col %d" % (k, v))
    falt = [k for k in COLUMNAS if k not in mapa]
    if falt:
        print("No están en el archivo (se ignoran): %s" % ", ".join(falt))
    print()
    out = []
    for fila in filas[h + 1:]:
        eid = celda(fila, mapa, "eid")
        if not eid:
            continue
        eid = str(eid).strip()
        if any(eid.lower().startswith(j) for j in JUNK):
            continue
        if not (celda(fila, mapa, "offering") or celda(fila, mapa, "cliente")):
            continue
        out.append({
            "eid": eid,
            "location": celda(fila, mapa, "location"),
            "offering": celda(fila, mapa, "offering"),
            "cl": a_entero(celda(fila, mapa, "cl")),
            "cliente": celda(fila, mapa, "cliente"),
            "chargeability_pct": a_float(celda(fila, mapa, "chargeability_pct")),
            "status": celda(fila, mapa, "status"),
            "hire_date": a_fecha(celda(fila, mapa, "hire_date")),
            "office": celda(fila, mapa, "office"),
            "te_approver": celda(fila, mapa, "te_approver"),
            "roll_on": a_fecha(celda(fila, mapa, "roll_on")),
            "roll_off": a_fecha(celda(fila, mapa, "roll_off")),
            "first_available": a_fecha(celda(fila, mapa, "first_available")),
            "next_client": celda(fila, mapa, "next_client"),
        })
    return out


# ─── Fase 2: Resolver EIDs ───────────────────────────────────────────────────

async def resolver_eids(conn, filas):
    db = await conn.fetch("SELECT eid, name FROM employees")
    por_eid = {r["eid"] for r in db}
    por_nombre = {(r["name"] or "").strip().lower(): r["eid"] for r in db}
    alias, sin_match = {}, []
    for f in filas:
        e = f["eid"]
        if e in por_eid:
            alias[e] = e
        elif e.lower() in por_nombre:
            alias[e] = por_nombre[e.lower()]
        else:
            sin_match.append(e)
    return alias, sin_match


# ─── Fase 3: Leer estado actual de la DB ─────────────────────────────────────

async def leer_estado_actual(conn, eids):
    if not eids:
        return {}
    rows = await conn.fetch("""
        WITH latest AS (
            SELECT DISTINCT ON (eid) *
            FROM forecast_update
            ORDER BY eid, updated_at DESC NULLS LAST
        )
        SELECT eid, client, offering, roll_on, roll_off, chargeability_pct,
               te_approver, office, first_available, next_client, status
        FROM latest
        WHERE eid = ANY($1::text[])
    """, list(eids))
    return {r["eid"]: dict(r) for r in rows}


# ─── Fase 4: Diff + conflictos con tickets ───────────────────────────────────

CAMPOS_DIFF = [
    ("offering",         "offering"),
    ("cliente",          "client"),
    ("roll_on",          "roll_on"),
    ("roll_off",         "roll_off"),
    ("chargeability_pct","chargeability_pct"),
    ("first_available",  "first_available"),
    ("next_client",      "next_client"),
    ("status",           "status"),
    ("te_approver",      "te_approver"),
    ("office",           "office"),
]

CAMPOS_TICKET = {
    "client":            "client_name",
    "roll_on":           "start_date",
    "roll_off":          "end_date",
    "chargeability_pct": "chargeability_pct",
}


async def detectar_conflictos_ticket(conn, eid, cambios_db):
    """
    Busca tickets Approved que puedan haber fijado el estado actual.
    Devuelve lista de (ticket_id, log_lines) para appendear a comments.
    """
    tickets = await conn.fetch("""
        SELECT id, client_name, start_date, end_date, chargeability_pct, comments
        FROM tickets
        WHERE eid = $1 AND status = 'Approved' AND type IN ('newproj', 'ongoing')
        ORDER BY id DESC LIMIT 5
    """, eid)

    conflictos = []
    today_str = TODAY.isoformat()

    for ticket in tickets:
        log_lines = []
        for db_col, ticket_col in CAMPOS_TICKET.items():
            if db_col not in cambios_db:
                continue
            viejo, nuevo = cambios_db[db_col]
            ticket_val = ticket[ticket_col]
            # Si el valor viejo coincide con lo que el ticket fijó → fue pisado
            if ticket_val is not None and str(ticket_val) == str(viejo):
                log_lines.append(
                    "[Sync Excel %s] %s: %r → %r" % (today_str, db_col, viejo, nuevo)
                )
        if log_lines:
            conflictos.append((ticket["id"], log_lines))

    return conflictos


# ─── Fase 5–7: Operaciones sobre la DB (dentro de la transacción) ─────────────

async def pct_for_period(num: int, p_num: int) -> float:
    if num == 4:
        return 90.0
    if num in (1, 3):
        return 0.0 if p_num <= 2 else 50.0
    return {1: 0.0, 2: 75.0}.get(p_num, 100.0)


async def upsert_projection_blocks_post_rolloff(conn, eid, roll_off):
    """Crea bloques de assumption para períodos posteriores al roll_off."""
    periods = await conn.fetch(
        "SELECT period_name, start_date, end_date FROM periods WHERE start_date > $1 ORDER BY start_date LIMIT 6",
        roll_off,
    )
    if not periods:
        return 0

    row = await conn.fetchrow("SELECT ringfenced, new_joiner FROM employees WHERE eid=$1", eid)
    is_nj = bool(row and row["new_joiner"])
    is_ringfenced = bool(row and row.get("ringfenced"))

    if is_nj:
        num = 3
    elif is_ringfenced:
        num = 2
    else:
        num = 1

    for i, period in enumerate(periods):
        p_num = i + 1
        pct = await pct_for_period(num, p_num)
        period_name = period["period_name"]
        end_date = period["end_date"]

        await conn.execute(
            "DELETE FROM chargeability_blocks WHERE eid=$1 AND period_name=$2 AND scenario_type='assumption'",
            eid, period_name,
        )
        await conn.execute(
            """
            INSERT INTO chargeability_blocks
                (eid, period_name, chargeability_pct, scenario_type,
                 start_date, end_date, created_by)
            VALUES ($1, $2, $3, 'assumption', $4, $5, 'sync_excel')
            """,
            eid, period_name, pct, period["start_date"], end_date,
        )
    return len(periods)


async def recrear_blocks(conn, eid, roll_on, roll_off, chargeability_pct):
    """
    Fase 5: Elimina bloques presentes/futuros y los recrea desde el Excel.
    """
    # 5a. Borrar bloques futuros
    await conn.execute(
        "DELETE FROM chargeability_blocks WHERE eid=$1 AND end_date >= $2",
        eid, TODAY,
    )

    if roll_on is None or roll_off is None:
        # Sin fechas no recreamos bloques de effective
        return

    pct = chargeability_pct if chargeability_pct is not None else 100.0

    # 5b. Insertar bloques effective para cada período que overlap con [roll_on, roll_off]
    periods = await conn.fetch(
        "SELECT period_name, start_date, end_date FROM periods ORDER BY start_date",
    )

    for period in periods:
        p_start = period["start_date"]
        p_end = period["end_date"]

        # Chequear overlap con [roll_on, roll_off]
        block_start = max(roll_on, p_start)
        block_end = min(roll_off, p_end)
        if block_start > block_end:
            continue
        # Solo bloques que no sean completamente históricos
        if block_end < TODAY:
            continue

        await conn.execute(
            """
            INSERT INTO chargeability_blocks
                (eid, period_name, chargeability_pct, scenario_type,
                 start_date, end_date, created_by)
            VALUES ($1, $2, $3, 'effective', $4, $5, 'sync_excel')
            """,
            eid, period["period_name"], pct, block_start, block_end,
        )

    # 5c. Bloques de assumption post roll_off
    await upsert_projection_blocks_post_rolloff(conn, eid, roll_off)


async def recalcular_employee(conn, eid):
    """
    Fase 6: Recalcula todos los períodos para el empleado (sin pool, con conn directa).
    """
    periods = await conn.fetch("SELECT period_name FROM periods ORDER BY start_date")
    for p in periods:
        pname = p["period_name"]
        try:
            await conn.execute("SELECT recalculate_forecast_period($1,$2)", eid, pname)
        except Exception as e:
            print("   WARN recalculate stored proc fallo eid=%s period=%s: %s" % (eid, pname, e))

        await conn.execute(
            """
            WITH totals AS (
                SELECT
                    COALESCE(SUM(chargeability_pct) FILTER (WHERE scenario_type = 'effective'),  0) AS hl_pct,
                    COALESCE(SUM(chargeability_pct) FILTER (WHERE scenario_type = 'assumption'), 0) AS sl_pct
                FROM chargeability_blocks
                WHERE eid = $1 AND period_name = $2
            )
            UPDATE forecast_periods fp
            SET chg_pct_hl = t.hl_pct,
                chg_pct_sl = t.sl_pct,
                chg_hl     = ROUND(fp.sah * t.hl_pct / 100.0),
                chg_sl     = ROUND(fp.sah * t.sl_pct / 100.0),
                chg        = ROUND(fp.sah * (t.hl_pct + t.sl_pct) / 100.0)
            FROM totals t
            WHERE fp.eid = $1 AND fp.period_name = $2
            """,
            eid, pname,
        )

        await conn.execute(
            """
            UPDATE forecast_periods fp
            SET sah        = fp.sah + COALESCE(fp.sah_ppa_adj, 0),
                chg_pct    = CASE WHEN fp.sah + COALESCE(fp.sah_ppa_adj, 0) > 0
                                  THEN ROUND(fp.chg / (fp.sah + COALESCE(fp.sah_ppa_adj, 0)) * 100, 2)
                                  ELSE 0 END,
                chg_pct_hl = CASE WHEN fp.sah + COALESCE(fp.sah_ppa_adj, 0) > 0
                                  THEN ROUND((fp.chg_hl + COALESCE(fp.chg_cascadeadas_hl, 0))
                                             / (fp.sah + COALESCE(fp.sah_ppa_adj, 0)) * 100, 2)
                                  ELSE 0 END,
                chg_pct_sl = CASE WHEN fp.sah + COALESCE(fp.sah_ppa_adj, 0) > 0
                                  THEN ROUND(fp.chg_sl / (fp.sah + COALESCE(fp.sah_ppa_adj, 0)) * 100, 2)
                                  ELSE 0 END
            WHERE fp.eid = $1 AND fp.period_name = $2 AND COALESCE(fp.sah_ppa_adj, 0) != 0
            """,
            eid, pname,
        )


# ─── Main ─────────────────────────────────────────────────────────────────────

async def main(path, apply):
    # Fase 1
    filas = parsear(path)
    print("%d filas de empleado leídas" % len(filas))
    print()
    print("Offerings en el archivo:")
    for k, v in Counter(f["offering"] for f in filas).most_common():
        marca = "" if k in VALID_OFFERINGS else "   <-- fuera de la taxonomía"
        print("   %-12s %d%s" % (k, v, marca))
    print()

    conn = await asyncpg.connect(
        host=settings.DB_HOST, port=settings.DB_PORT,
        user=settings.DB_USER, password=settings.DB_PASSWORD,
        database=settings.DB_NAME, ssl="require",
    )

    # Fase 2
    alias, sin_match = await resolver_eids(conn, filas)
    print("EIDs que matchean: %d" % len(alias))
    if sin_match:
        print("EIDs sin match en la base (%d):" % len(sin_match))
        for m in sin_match:
            print("   - %s" % m)
    print()

    eids_resueltos = set(alias.values())

    # Fase 3
    dict_actual = await leer_estado_actual(conn, eids_resueltos)

    # Fase 4: Diff + conflictos
    resumen_cambios = Counter()
    muestras_cambios = []
    todos_conflictos = []  # list of (eid, ticket_id, log_lines)
    warnings_sin_roll = []

    for f in filas:
        eid = alias.get(f["eid"])
        if not eid:
            continue

        cur = dict_actual.get(eid, {})
        cambios_db = {}

        for campo_excel, col_db in CAMPOS_DIFF:
            nuevo = f.get(campo_excel)
            if nuevo is None:
                continue
            viejo = cur.get(col_db)
            if str(nuevo) != str(viejo if viejo is not None else ""):
                cambios_db[col_db] = (viejo, nuevo)
                resumen_cambios[campo_excel] += 1

        if cambios_db and len(muestras_cambios) < 10:
            claves_mostrar = [k for k in ("client", "roll_on", "roll_off", "chargeability_pct") if k in cambios_db]
            if claves_mostrar:
                muestras_cambios.append((eid, {k: cambios_db[k] for k in claves_mostrar}))

        if cambios_db:
            conflictos = await detectar_conflictos_ticket(conn, eid, cambios_db)
            for ticket_id, log_lines in conflictos:
                todos_conflictos.append((eid, ticket_id, log_lines))

        if f["roll_on"] is None or f["roll_off"] is None:
            warnings_sin_roll.append(eid)

    # Report dry-run
    print("Cambios que se aplicarían:")
    for k, v in resumen_cambios.most_common():
        print("   %-22s %d empleados" % (k, v))
    print()

    if muestras_cambios:
        print("Ejemplos de cambios:")
        for eid, diff in muestras_cambios:
            print("   %s" % eid)
            for col, (viejo, nuevo) in diff.items():
                print("      %s: %r → %r" % (col, viejo, nuevo))
        print()

    if todos_conflictos:
        print("Tickets aprobados que serían pisados (%d):" % len(todos_conflictos))
        for eid, ticket_id, log_lines in todos_conflictos:
            print("   ticket #%d (eid=%s)" % (ticket_id, eid))
            for line in log_lines:
                print("      %s" % line)
        print()

    if warnings_sin_roll:
        print("WARN: %d empleados sin roll_on/roll_off (no se recrearán bloques):" % len(warnings_sin_roll))
        for eid in warnings_sin_roll[:10]:
            print("   - %s" % eid)
        print()

    if not apply:
        print("[DRY RUN] No se escribió nada. Volvé a correr con --apply.")
        await conn.close()
        return

    # ── Aplicar en una sola transacción ──────────────────────────────────────
    n_procesados = 0
    n_con_cambios = 0
    n_tickets_log = 0

    async with conn.transaction():
        await conn.execute(
            "ALTER TABLE forecast_update ADD COLUMN IF NOT EXISTS status VARCHAR(40)"
        )

        for f in filas:
            eid = alias.get(f["eid"])
            if not eid:
                continue

            n_procesados += 1
            cur = dict_actual.get(eid, {})

            # Fase 4 (apply): Appendear a tickets.comments
            cambios_db = {}
            for campo_excel, col_db in CAMPOS_DIFF:
                nuevo = f.get(campo_excel)
                if nuevo is None:
                    continue
                viejo = cur.get(col_db)
                if str(nuevo) != str(viejo if viejo is not None else ""):
                    cambios_db[col_db] = (viejo, nuevo)

            if cambios_db:
                n_con_cambios += 1
                conflictos = await detectar_conflictos_ticket(conn, eid, cambios_db)
                for ticket_id, log_lines in conflictos:
                    append_text = "\n".join(log_lines)
                    await conn.execute(
                        "UPDATE tickets SET comments = COALESCE(comments,'') || $1 WHERE id = $2",
                        "\n" + append_text, ticket_id,
                    )
                    n_tickets_log += 1

            # Fase 4 (apply): Actualizar forecast_update
            sets, params = [], []

            def add(col, val):
                if val is None:
                    return
                params.append(val)
                sets.append("%s = $%d" % (col, len(params)))

            add("offering",         f["offering"])
            add("client",           f["cliente"])
            add("chargeability_pct",f["chargeability_pct"])
            add("first_available",  f["first_available"])
            add("roll_on",          f["roll_on"])
            add("roll_off",         f["roll_off"])
            add("te_approver",      f["te_approver"])
            add("office",           f["office"])
            add("next_client",      f["next_client"])
            add("status",           f["status"])

            if sets:
                params.append(eid)
                res = await conn.execute(
                    "UPDATE forecast_update SET %s, updated_at = NOW(), updated_by = 'sync_excel' WHERE eid = $%d"
                    % (", ".join(sets), len(params)),
                    *params,
                )
                if res.endswith(" 0"):
                    await conn.execute(
                        """INSERT INTO forecast_update
                            (eid, offering, client, chargeability_pct, first_available,
                             roll_on, roll_off, te_approver, office, next_client,
                             status, updated_at, updated_by, scenario_type)
                           VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,NOW(),'sync_excel','effective')""",
                        eid, f["offering"], f["cliente"], f["chargeability_pct"],
                        f["first_available"], f["roll_on"], f["roll_off"],
                        f["te_approver"], f["office"], f["next_client"], f["status"],
                    )

            # Fase 4 (apply): Actualizar employees
            es, ep = [], []

            def add_emp(col, val):
                if val is None:
                    return
                ep.append(val)
                es.append("%s = $%d" % (col, len(ep)))

            add_emp("offering", f["offering"])
            add_emp("cl",       f["cl"])
            add_emp("hire_date",f["hire_date"])
            if f["location"]:
                add_emp("country", LOCATION_TO_COUNTRY.get(f["location"], f["location"]))

            if es:
                ep.append(eid)
                await conn.execute(
                    "UPDATE employees SET %s WHERE eid = $%d" % (", ".join(es), len(ep)),
                    *ep,
                )

            # Fase 5: Recrear chargeability_blocks
            await recrear_blocks(
                conn, eid,
                f["roll_on"], f["roll_off"], f["chargeability_pct"],
            )

            # Fase 6: Recalcular forecast_periods
            await recalcular_employee(conn, eid)

    # Report final
    print()
    print("=" * 50)
    print("SYNC COMPLETADO")
    print("  Empleados procesados : %d" % n_procesados)
    print("  Con cambios          : %d" % n_con_cambios)
    print("  Skipped (sin match)  : %d" % len(sin_match))
    print("  Tickets logueados    : %d" % n_tickets_log)
    if warnings_sin_roll:
        print("  Sin roll dates       : %d (sin bloques recreados)" % len(warnings_sin_roll))
    print("=" * 50)

    await conn.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Sincronización completa Excel → DB")
    ap.add_argument("archivo", help="Ruta al Excel o CSV con Forecast Update")
    ap.add_argument("--apply", action="store_true", help="Aplicar cambios (default: dry-run)")
    a = ap.parse_args()
    asyncio.run(main(Path(a.archivo), a.apply))
