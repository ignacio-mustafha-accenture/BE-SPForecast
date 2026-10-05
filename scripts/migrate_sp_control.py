"""
migrate_sp_control.py
---------------------
Migra 104 empleados S&P Argentina desde "01 - FORECAST CONTROL S&P(S&P).csv"
hacia Azure PostgreSQL (ForecastOS).

Lo que hace:
  1. Lee y parsea el CSV (mismo parseo que generate_simulation.py).
  2. Dry-run por defecto: imprime todo lo que haría, sin escribir nada.
  3. Con --apply: ejecuta en una sola transacción:
       - employees: active/charge/cl/country
       - offerings: crea el offering si Main Skill no existe en la DB
       - client_catalog: crea clientes nuevos
       - forecast_update: upsert por eid
       - forecast_periods: upsert por (eid, period_name)
       - absences: insert PTOs
       - tickets: insert PTOs (con confirmación interactiva por consola para
                  PTOs sin deducción)
  4. Genera migration_report.csv y unparsed_pto.csv.

Uso:
    python scripts/migrate_sp_control.py "ruta/al/CSV"           # dry-run
    python scripts/migrate_sp_control.py "ruta/al/CSV" --apply   # ejecuta
"""

import argparse
import asyncio
import csv
import os
import re
import sys
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, ".")

import asyncpg

from app.config import settings

# ─── constantes (mismas que generate_simulation.py) ──────────────────────────

ENCODING = "latin-1"
TODAY = date.today()

COL_STATUS      = 0
COL_OFFERING    = 1
COL_SUB_OFF     = 2
COL_COMENTARIOS = 8
COL_FIRST_AVAIL = 9
COL_LEVEL       = 10
COL_EID         = 11

MONTH_MAP = {
    "JUL": "Jul", "AUG": "Ago", "SEP": "Sep", "OCT": "Oct",
    "NOV": "Nov", "DEC": "Dic", "JAN": "Ene", "FEB": "Feb",
    "MAR": "Mar", "APR": "Abr", "MAY": "May", "JUN": "Jun",
}

SPECIAL_CLIENTS = {"", "out", "licencia", "nj", "sl", "loa"}

KNOWN_OFFERINGS = {"SO", "PR", "Tools", "S4", "Ariba", "Oracle"}

INFORMAL_PTO_KEYWORDS = re.compile(
    r"\b(ago|ene|feb|abr|jul|ago|sept|nov|dic|enero|febrero|marzo|abril|mayo|junio|"
    r"julio|agosto|septiembre|octubre|noviembre|diciembre|loa|loar|acn days|ver notas|"
    r"d[íi]a ec|turno|vacacion)\b",
    re.IGNORECASE,
)


# ─── helpers ─────────────────────────────────────────────────────────────────

def clean(v):
    if v is None:
        return None
    s = str(v).strip()
    return s if s else None


def to_float(v):
    try:
        return float(str(v).strip().replace("%", "") or "0")
    except (TypeError, ValueError):
        return 0.0


def to_int_cl(level_name):
    m = re.match(r"^(\d+)", str(level_name or "").strip())
    return int(m.group(1)) if m else None


def parse_date_str(s, year_hint=2026):
    if not s:
        return None
    s = s.strip()
    for fmt in ("%d/%m/%Y", "%d/%m/%y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            pass
    m = re.match(r"^(\d{1,2})/(\d{1,2})$", s)
    if m:
        try:
            return date(year_hint, int(m.group(2)), int(m.group(1)))
        except ValueError:
            pass
    m = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{4})$", s)
    if m:
        try:
            return date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
        except ValueError:
            pass
    return None


def excel_serial_to_date(n):
    try:
        v = int(float(str(n).strip()))
        if v > 40000:
            return datetime(1899, 12, 30) + timedelta(days=v)
        return None
    except (TypeError, ValueError):
        return None


def period_name_from_csv(month_str, half_str, year_str):
    mon = MONTH_MAP.get(month_str.upper())
    if not mon:
        return None
    year = int(year_str)
    suffix = f"-{str(year)[2:]}" if year != 2026 else ""
    return f"{mon}-{half_str}{suffix}"


# ─── CSV parsing (replicated from generate_simulation.py) ────────────────────

def detect_period_columns(header):
    period_cols = {}
    period_order = []
    seen = set()

    chg_re = re.compile(r"^CHG (HL|SL) (\w+) (P[12]) (\d{4})$")
    sah_re = re.compile(r"^SAH P[12] (\w+) (P[12]) (\d{4})$")

    for i, h in enumerate(header):
        h = (h or "").strip().upper()
        m = chg_re.match(h)
        if m:
            hl_sl, month, half, year = m.groups()
            pname = period_name_from_csv(month, half, year)
            if pname:
                col_type = "chg_hl" if hl_sl == "HL" else "chg_sl"
                period_cols[i] = (pname, col_type)
                if pname not in seen:
                    seen.add(pname)
                    period_order.append(pname)
            continue
        m = sah_re.match(h)
        if m:
            month, half, year = m.groups()
            pname = period_name_from_csv(month, half, year)
            if pname:
                period_cols[i] = (pname, "sah")
                if pname not in seen:
                    seen.add(pname)
                    period_order.append(pname)

    return period_cols, period_order


def _parse_comma_or_dash_list(s, no_ticket, ptos, unparsed, year_hint=2026):
    parts = re.split(r"[,;]\s*", s.strip())
    parsed_any = False
    for part in parts:
        part = part.strip()
        if not part:
            continue
        part = re.sub(r"\s*-\s*(?=\d)", "-", part)
        part = re.sub(r"(?<=\d)-\s+(?=\d)", "-", part)

        # M/D-M/D
        m = re.match(r"^(\d{1,2})/(\d{1,2})-(\d{1,2})/(\d{1,2})$", part)
        if m:
            try:
                sd = date(year_hint, int(m.group(1)), int(m.group(2)))
                ed = date(year_hint, int(m.group(3)), int(m.group(4)))
                ptos.append({"start_date": sd.isoformat(), "end_date": ed.isoformat(),
                             "no_ticket": no_ticket})
                parsed_any = True
                continue
            except ValueError:
                pass

        # M/D-M-D
        m = re.match(r"^(\d{1,2})/(\d{1,2})-(\d{1,2})-(\d{1,2})$", part)
        if m:
            try:
                sd = date(year_hint, int(m.group(1)), int(m.group(2)))
                ed = date(year_hint, int(m.group(3)), int(m.group(4)))
                ptos.append({"start_date": sd.isoformat(), "end_date": ed.isoformat(),
                             "no_ticket": no_ticket})
                parsed_any = True
                continue
            except ValueError:
                pass

        # M/D-M/D as M/D1-D2 consecutive days
        m = re.match(r"^(\d{1,2})/(\d{1,2})-(\d{1,2})$", part)
        if m:
            try:
                sd = date(year_hint, int(m.group(1)), int(m.group(2)))
                ed = date(year_hint, int(m.group(1)), int(m.group(3)))
                ptos.append({"start_date": sd.isoformat(), "end_date": ed.isoformat(),
                             "no_ticket": no_ticket})
                parsed_any = True
                continue
            except ValueError:
                pass

        # M/D single date
        m = re.match(r"^(\d{1,2})/(\d{1,2})$", part)
        if m:
            try:
                d_ = date(year_hint, int(m.group(1)), int(m.group(2)))
                ptos.append({"start_date": d_.isoformat(), "end_date": d_.isoformat(),
                             "no_ticket": no_ticket})
                parsed_any = True
                continue
            except ValueError:
                pass

        # Full date DD/MM/YYYY
        d_ = parse_date_str(part, year_hint)
        if d_:
            ptos.append({"start_date": d_.isoformat(), "end_date": d_.isoformat(),
                         "no_ticket": no_ticket})
            parsed_any = True
            continue

        # Excel serial
        d_ = excel_serial_to_date(part)
        if d_:
            ptos.append({"start_date": d_.date().isoformat(),
                         "end_date": d_.date().isoformat(),
                         "no_ticket": no_ticket})
            parsed_any = True
            continue

        if part:
            unparsed.append(part)

    return parsed_any


def parse_pto_b(pto_raw, eid, ptos, unparsed, year_hint=2026):
    raw = pto_raw.strip()
    no_ticket = "sin ticket" in raw.lower()

    # Strip prefix
    for prefix in ("Next PTO ", "PTO ", "next pto ", "pto "):
        if raw.lower().startswith(prefix.lower()):
            raw = raw[len(prefix):].strip()
            break

    if not raw or raw == "-":
        return

    if INFORMAL_PTO_KEYWORDS.search(raw):
        unparsed.append(pto_raw)
        return

    # Excel serial
    d_ = excel_serial_to_date(raw)
    if d_:
        ptos.append({"start_date": d_.date().isoformat(),
                     "end_date": d_.date().isoformat(),
                     "no_ticket": no_ticket})
        return

    # "a" or "al" range — split by . , or " - "
    parts_a = re.split(r"[.,]|\s+-\s*", raw)
    for part in parts_a:
        part = part.strip()
        m = re.match(r"^(.+?)\s+a(?:l)?\s+(.+)$", part, re.IGNORECASE)
        if m:
            sd = parse_date_str(m.group(1).strip(), year_hint)
            ed = parse_date_str(m.group(2).strip(), year_hint)
            if sd and ed:
                ptos.append({"start_date": sd.isoformat(), "end_date": ed.isoformat(),
                             "no_ticket": no_ticket})
                continue
        if part:
            normalized = re.sub(r"\s*-\s*(?=\d)", "-", part)
            normalized = re.sub(r"(?<=\d)-\s+(?=\d)", "-", normalized)
            unp_before = len(unparsed)
            parsed_any = _parse_comma_or_dash_list(normalized, no_ticket, ptos, unparsed, year_hint)
            if not parsed_any and len(unparsed) == unp_before and normalized.strip():
                unparsed.append(pto_raw)


def parse_format_a(comment, eid):
    segs = comment.split(";")
    client = None
    roll_on = roll_off = None
    new_joiner = licencia = False
    ptos = []
    unparsed = []

    for seg in segs:
        seg = seg.strip()
        if not seg:
            continue

        # PTO
        m = re.match(r"^(\d+)\s+PTO\s+(\w+)\s+(P[12])$", seg, re.IGNORECASE)
        if m:
            n_days, month, half = m.groups()
            pname = period_name_from_csv(month, half, "2026")
            if not pname:
                pname = period_name_from_csv(month, half, "2027")
            ptos.append({"n_days": int(n_days), "period": pname,
                         "start_date": None, "end_date": None, "no_ticket": False})
            continue

        # HE — skip
        if re.match(r"^\d+\s+HE\s+\w+\s+P[12]$", seg, re.IGNORECASE):
            continue

        # Project block: n|client|roll_on|roll_off
        m = re.match(r"^(\d+)\|(.*)$", seg)
        if m:
            parts = seg.split("|")
            if len(parts) >= 3:
                c = clean(parts[1]) or ""
                ro_str = clean(parts[2]) if len(parts) > 2 else None
                rf_str = clean(parts[3]) if len(parts) > 3 else None
                cl = c.lower()
                if cl == "nj":
                    new_joiner = True
                if cl in ("licencia", "loa"):
                    licencia = True
                if cl not in SPECIAL_CLIENTS:
                    if client is None:
                        client = c
                        roll_on = parse_date_str(ro_str)
                        roll_off = parse_date_str(rf_str)
            continue

        unparsed.append(seg)

    return {"client": client, "roll_on": roll_on, "roll_off": roll_off,
            "new_joiner": new_joiner, "licencia": licencia,
            "ptos": ptos, "unparsed_segments": unparsed}


def parse_format_b(comment, eid):
    parts = [p.strip() for p in comment.split("|")]
    client = None
    roll_on = roll_off = None
    new_joiner = licencia = False
    ptos = []
    unparsed = []

    if len(parts) >= 2:
        c = clean(parts[1])
        if c:
            cl = c.lower()
            if cl == "nj":
                new_joiner = True
            elif cl in ("licencia", "loa"):
                licencia = True
            elif cl.lower() not in SPECIAL_CLIENTS:
                client = c

    if len(parts) >= 3:
        date_field = parts[2].strip()
        if " to " in date_field.lower():
            halves = re.split(r"\s+to\s+", date_field, flags=re.IGNORECASE)
            if len(halves) == 2:
                roll_on = parse_date_str(halves[0].strip())
                roll_off = parse_date_str(halves[1].strip())
        elif date_field not in ("-", ""):
            roll_on = parse_date_str(date_field)

    if len(parts) >= 4:
        pto_field = parts[3].strip()
        if pto_field and pto_field != "-":
            # Detect if field 3 was single date and field 4 is roll_off
            if roll_off is None and re.match(r"^\d{2}/\d{2}/\d{4}$", pto_field):
                roll_off = parse_date_str(pto_field)
            else:
                parse_pto_b(pto_field, eid, ptos, unparsed)

    return {"client": client, "roll_on": roll_on, "roll_off": roll_off,
            "new_joiner": new_joiner, "licencia": licencia,
            "ptos": ptos, "unparsed_segments": unparsed}


def parse_employee(row, period_cols):
    def g(i):
        return clean(row[i]) if i < len(row) else None

    eid        = g(COL_EID)
    status     = g(COL_STATUS) or "Active"
    off        = g(COL_OFFERING)
    sub        = g(COL_SUB_OFF)
    main_skill = g(4)
    level      = g(COL_LEVEL)
    cl         = to_int_cl(level)
    fa_str     = g(COL_FIRST_AVAIL)
    first_available = parse_date_str(fa_str)
    comment    = g(COL_COMENTARIOS) or ""

    fmt = "A" if ";" in comment else "B"
    if fmt == "A":
        parsed = parse_format_a(comment, eid)
    else:
        parsed = parse_format_b(comment, eid)

    roll_off = parsed["roll_off"]
    if roll_off is None and first_available:
        roll_off = first_available

    days_to_avail = None
    if roll_off:
        days_to_avail = max(0, (roll_off - TODAY).days)

    periods = {}
    for i, (pname, col_type) in period_cols.items():
        if pname not in periods:
            periods[pname] = {"chg_hl": 0.0, "chg_sl": 0.0, "sah": 0.0}
        if col_type in periods[pname]:
            periods[pname][col_type] = to_float(g(i))

    offering_is_new = bool(main_skill and main_skill not in KNOWN_OFFERINGS)
    ptos_need_confirm = [
        p for p in parsed["ptos"]
        if p.get("start_date") and not p.get("no_ticket")
    ]

    return {
        "eid":              eid,
        "status":           status,
        "cl":               cl,
        "offering":         off or "",
        "sub_offering":     sub or "",
        "main_skill":       main_skill or "",
        "offering_is_new":  offering_is_new,
        "client":           parsed["client"],
        "roll_on":          parsed["roll_on"],
        "roll_off":         roll_off,
        "first_available":  first_available,
        "days_to_avail":    days_to_avail,
        "format":           fmt,
        "new_joiner":       parsed["new_joiner"],
        "licencia":         parsed["licencia"],
        "periods":          {k: v for k, v in periods.items()
                             if v["sah"] > 0 or v["chg_hl"] > 0 or v["chg_sl"] > 0},
        "ptos":             parsed["ptos"],
        "ptos_need_confirm": ptos_need_confirm,
        "unparsed_segments": parsed["unparsed_segments"],
        "raw_comment":      comment,
    }


def read_csv(path):
    with open(path, encoding=ENCODING, newline="") as f:
        reader = csv.reader(f)
        header = next(reader)
        period_cols, period_order = detect_period_columns(header)

        employees = []
        issues = {"pto_unparsed": []}
        for row in reader:
            eid = clean(row[COL_EID]) if len(row) > COL_EID else None
            if not eid or "." not in eid:
                continue
            emp = parse_employee(row, period_cols)
            if not emp["eid"]:
                continue

            for seg in emp["unparsed_segments"]:
                issues["pto_unparsed"].append({"eid": emp["eid"], "raw_text": seg})

            employees.append(emp)

    return employees, period_order, issues


# ─── DB helpers ──────────────────────────────────────────────────────────────

async def resolve_eids(conn, employees):
    db_rows = await conn.fetch("SELECT eid, name FROM employees")
    by_eid  = {r["eid"] for r in db_rows}
    by_name = {(r["name"] or "").strip().lower(): r["eid"] for r in db_rows}

    resolved, missing = {}, []
    for emp in employees:
        e = emp["eid"]
        if e in by_eid:
            resolved[e] = e
        elif (hit := by_name.get(e.lower())):
            resolved[e] = hit
        else:
            missing.append(e)
    return resolved, missing


async def ensure_offering(conn, name, apply):
    """Returns offering id for name, creating it if needed. Returns (id, created)."""
    row = await conn.fetchrow(
        "SELECT id FROM offerings WHERE LOWER(name) = LOWER($1)", name
    )
    if row:
        return row["id"], False
    if apply:
        new_id = await conn.fetchval(
            "INSERT INTO offerings (name) VALUES ($1) RETURNING id", name
        )
        return new_id, True
    return None, True  # dry-run: would create


async def ensure_client(conn, name, apply):
    """Returns client id for name, creating it if needed. Returns (id, created)."""
    if not name:
        return None, False
    row = await conn.fetchrow(
        "SELECT id FROM client_catalog WHERE LOWER(name) = LOWER($1)", name
    )
    if row:
        return row["id"], False
    if apply:
        new_id = await conn.fetchval(
            "INSERT INTO client_catalog (name) VALUES ($1) RETURNING id", name
        )
        return new_id, True
    return None, True  # dry-run: would create


async def get_period_dates(conn, period_name):
    row = await conn.fetchrow(
        "SELECT start_date, end_date FROM periods WHERE period_name = $1", period_name
    )
    return (row["start_date"], row["end_date"]) if row else (None, None)


# ─── PTO confirmation ─────────────────────────────────────────────────────────

def confirm_pto(eid, pto, apply):
    """
    Asks the user on the console whether to create+approve a PTO ticket.
    Returns True if the ticket should be created.
    In dry-run (apply=False) always returns True (just for the preview count).
    """
    if not apply:
        return True  # dry-run: show as if confirmed

    sd = pto.get("start_date", "?")
    ed = pto.get("end_date", "?")
    print()
    print(f"  PTO sin deducción — {eid}")
    print(f"    Fechas: {sd} -> {ed}")
    while True:
        resp = input("  ¿Crear ticket y aprobarlo? [s/n]: ").strip().lower()
        if resp in ("s", "si", "sí", "y", "yes"):
            return True
        if resp in ("n", "no"):
            return False
        print("  Responder 's' o 'n'.")


# ─── main migration logic ─────────────────────────────────────────────────────

async def run_migration(csv_path, apply):
    print(f"\nCSV: {csv_path}")
    employees, period_order, issues = read_csv(csv_path)
    print(f"Empleados encontrados: {len(employees)}")
    print(f"Periodos detectados:   {len(period_order)}")

    # Stats
    active  = sum(1 for e in employees if e["status"] == "Active")
    out     = sum(1 for e in employees if e["status"] == "OUT")
    nj      = sum(1 for e in employees if e["new_joiner"])
    new_off = [e["main_skill"] for e in employees if e["offering_is_new"] and e["main_skill"]]
    new_off_unique = sorted(set(new_off))
    all_clients = [e["client"] for e in employees if e["client"]]

    print(f"\n  Active: {active}  OUT: {out}  NJ: {nj}")
    print(f"  Offerings nuevos (a crear): {new_off_unique if new_off_unique else 'ninguno'}")
    print(f"  Clientes únicos en CSV: {len(set(all_clients))}")

    conn = await asyncpg.connect(
        host=settings.DB_HOST,
        port=settings.DB_PORT,
        user=settings.DB_USER,
        password=settings.DB_PASSWORD,
        database=settings.DB_NAME,
        ssl="require",
    )

    try:
        resolved, missing = await resolve_eids(conn, employees)
        print(f"\nEIDs resueltos: {len(resolved)}  |  Sin match: {len(missing)}")
        for m in missing:
            print(f"   - {m}")

        # Preview of clients not yet in DB
        clients_in_db = {
            r["name"].lower()
            for r in await conn.fetch("SELECT name FROM client_catalog")
        }
        new_clients = sorted({
            e["client"] for e in employees
            if e["client"] and e["client"].lower() not in clients_in_db
        })
        print(f"\nClientes nuevos (a crear en client_catalog): {new_clients if new_clients else 'ninguno'}")

        # ─── dry-run preview ──────────────────────────────────────────────────
        emp_updates = len(resolved)
        fu_upserts  = len(resolved)
        fp_upserts  = sum(len(e["periods"]) for e in employees if e["eid"] in resolved)
        pto_total   = sum(len(e["ptos"]) for e in employees if e["eid"] in resolved)
        pto_confirm = sum(len(e["ptos_need_confirm"])
                          for e in employees if e["eid"] in resolved)
        pto_unparsed = len(issues["pto_unparsed"])

        print(f"""
Cambios que se aplicarían:
  employees update:          {emp_updates}
  forecast_update upserts:   {fu_upserts}
  forecast_periods upserts:  {fp_upserts}
  PTOs totales:              {pto_total}  (de los cuales {pto_confirm} requieren confirmación)
  PTOs no parseados:         {pto_unparsed}
  Offerings nuevos:          {len(new_off_unique)}
  Clientes nuevos:           {len(new_clients)}
""")

        if not apply:
            print("Dry-run terminado. Volver a correr con --apply para ejecutar.\n")
            _write_reports(employees, resolved, issues)
            return

        # ─── apply ────────────────────────────────────────────────────────────
        print("Aplicando...\n")

        # Ensure offerings exist (outside the per-employee transaction)
        offering_map = {}  # main_skill -> db offering name/id (best-effort)
        for emp in employees:
            ms = emp["main_skill"]
            if ms and ms not in offering_map:
                _, created = await ensure_offering(conn, ms, apply)
                offering_map[ms] = ms
                if created:
                    print(f"  [offering] Creado: '{ms}'")

        tickets_created = 0
        tickets_skipped = 0
        fp_count = 0
        abs_count = 0

        async with conn.transaction():
            for emp in employees:
                eid = emp["eid"]
                if eid not in resolved:
                    continue
                db_eid = resolved[eid]

                # 1. employees
                active_flag = emp["status"] != "OUT"
                update_fields = [
                    ("active", active_flag),
                    ("charge", active_flag),
                    ("country", "AR"),
                ]
                if emp["cl"] is not None:
                    update_fields.append(("cl", float(emp["cl"])))
                if emp["new_joiner"]:
                    update_fields.append(("new_joiner", True))
                if emp["main_skill"]:
                    update_fields.append(("offering", emp["main_skill"]))

                set_clause = ", ".join(
                    f"{col} = ${i+2}" for i, (col, _) in enumerate(update_fields)
                )
                await conn.execute(
                    f"UPDATE employees SET {set_clause} WHERE eid = $1",
                    db_eid, *[v for _, v in update_fields],
                )

                # 2. client in catalog
                client_name = emp["client"]
                if client_name:
                    _, created = await ensure_client(conn, client_name, apply)
                    if created:
                        print(f"  [client_catalog] Creado: '{client_name}'")

                # 3. forecast_update
                roll_on_v  = emp["roll_on"]
                roll_off_v = emp["roll_off"]
                fa_v       = emp["first_available"]
                days_v     = emp["days_to_avail"]
                has_chg    = any(
                    v["chg_hl"] > 0 for v in emp["periods"].values()
                )
                scenario   = "effective" if has_chg else "assumption"

                await conn.execute("""
                    INSERT INTO forecast_update
                        (eid, client, roll_on, roll_off, first_available,
                         days_available, scenario_type, updated_at)
                    VALUES ($1,$2,$3,$4,$5,$6,$7,NOW())
                    ON CONFLICT (eid) DO UPDATE SET
                        client          = COALESCE($2, forecast_update.client),
                        roll_on         = COALESCE($3, forecast_update.roll_on),
                        roll_off        = COALESCE($4, forecast_update.roll_off),
                        first_available = COALESCE($5, forecast_update.first_available),
                        days_available  = COALESCE($6, forecast_update.days_available),
                        scenario_type   = $7,
                        updated_at      = NOW()
                """, db_eid, client_name,
                     roll_on_v, roll_off_v, fa_v, days_v, scenario)

                # 4. forecast_periods
                for pname, pdata in emp["periods"].items():
                    chg_hl  = pdata["chg_hl"]
                    chg_sl  = pdata["chg_sl"]
                    sah     = pdata["sah"]
                    chg     = chg_hl + chg_sl
                    pct_hl  = round(chg_hl / sah * 100, 2) if sah > 0 else 0.0
                    pct_sl  = round(chg_sl / sah * 100, 2) if sah > 0 else 0.0
                    pct     = round(chg    / sah * 100, 2) if sah > 0 else 0.0

                    await conn.execute("""
                        INSERT INTO forecast_periods
                            (eid, period_name, chg_hl, chg_sl, sah,
                             chg, chg_neto, chg_cascadeadas,
                             chg_cascadeadas_hl, chg_cascadeadas_sl,
                             chg_pct_hl, chg_pct_sl, chg_pct, absence_hours)
                        VALUES ($1,$2,$3,$4,$5,$6,$7,0,0,0,$8,$9,$10,0)
                        ON CONFLICT (eid, period_name) DO UPDATE SET
                            chg_hl             = $3,
                            chg_sl             = $4,
                            sah                = $5,
                            chg                = $6,
                            chg_neto           = $7,
                            chg_cascadeadas    = 0,
                            chg_cascadeadas_hl = 0,
                            chg_cascadeadas_sl = 0,
                            chg_pct_hl         = $8,
                            chg_pct_sl         = $9,
                            chg_pct            = $10
                    """, db_eid, pname, chg_hl, chg_sl, sah,
                         chg, chg, pct_hl, pct_sl, pct)
                    fp_count += 1

                # 5. absences + tickets (PTOs)
                for pto in emp["ptos"]:
                    sd_str = pto.get("start_date")
                    ed_str = pto.get("end_date")
                    no_ticket = pto.get("no_ticket", False)
                    n_days = pto.get("n_days")
                    pname_ref = pto.get("period")

                    # Resolve dates for period-only PTOs (Formato A)
                    if not sd_str and pname_ref:
                        pd_start, pd_end = await get_period_dates(conn, pname_ref)
                        if pd_start:
                            sd_str = pd_start.isoformat()
                            ed_str = pd_end.isoformat() if pd_end else sd_str

                    if not sd_str:
                        continue

                    sd = date.fromisoformat(sd_str)
                    ed = date.fromisoformat(ed_str) if ed_str else sd
                    hours = (n_days * 8) if n_days else max(1, (ed - sd).days + 1) * 8

                    # Insert absence
                    await conn.execute("""
                        INSERT INTO absences (eid, type, start_date, end_date, hours)
                        VALUES ($1,'pto',$2,$3,$4)
                        ON CONFLICT DO NOTHING
                    """, db_eid, sd, ed, hours)
                    abs_count += 1

                    # Ticket
                    if no_ticket:
                        continue

                    # PTOs with specific dates need interactive confirmation
                    if sd_str and not no_ticket:
                        create_ticket = confirm_pto(db_eid, pto, apply)
                    else:
                        create_ticket = True

                    if create_ticket:
                        await conn.execute("""
                            INSERT INTO tickets
                                (type, eid, status, start_date, end_date, created_by, created_at)
                            VALUES ('pto',$1,'Aprobado',$2,$3,'migrate_sp_control',NOW())
                        """, db_eid, sd, ed)
                        tickets_created += 1
                    else:
                        tickets_skipped += 1

        print(f"\nResultado:")
        print(f"  employees actualizados:    {len(resolved)}")
        print(f"  forecast_update upserts:   {len(resolved)}")
        print(f"  forecast_periods upserts:  {fp_count}")
        print(f"  absences insertadas:       {abs_count}")
        print(f"  tickets PTO creados:       {tickets_created}")
        print(f"  tickets PTO omitidos:      {tickets_skipped}")

    finally:
        await conn.close()

    _write_reports(employees, resolved, issues)


def _write_reports(employees, resolved, issues):
    report_path = Path("migration_report.csv")
    unparsed_path = Path("unparsed_pto.csv")

    with open(report_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["eid", "status", "resolved", "main_skill", "offering_is_new",
                    "client", "periods_with_data", "ptos_parsed",
                    "ptos_need_confirm", "unparsed_segs"])
        for emp in employees:
            w.writerow([
                emp["eid"],
                emp["status"],
                "si" if emp["eid"] in resolved else "NO",
                emp["main_skill"],
                "si" if emp["offering_is_new"] else "",
                emp["client"] or "",
                len(emp["periods"]),
                len(emp["ptos"]),
                len(emp["ptos_need_confirm"]),
                len(emp["unparsed_segments"]),
            ])

    with open(unparsed_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["eid", "raw_text"])
        for item in issues["pto_unparsed"]:
            w.writerow([item["eid"], item["raw_text"]])

    print(f"\nReportes generados:")
    print(f"  {report_path}")
    print(f"  {unparsed_path}")


# ─── entrypoint ──────────────────────────────────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("csv_path", help="Ruta al CSV de control S&P")
    p.add_argument("--apply", action="store_true",
                   help="Ejecutar contra la DB. Sin este flag: dry-run.")
    return p.parse_args()


if __name__ == "__main__":
    args = parse_args()
    if not os.path.exists(args.csv_path):
        print(f"ERROR: No existe el archivo: {args.csv_path}")
        sys.exit(1)

    mode = "APLICANDO" if args.apply else "DRY-RUN (no escribe nada)"
    print(f"\n=== migrate_sp_control.py [{mode}] ===")
    if args.apply:
        print("ADVERTENCIA: PTOs sin deducción requerirán confirmación por consola.")

    try:
        asyncio.run(run_migration(args.csv_path, args.apply))
    except KeyboardInterrupt:
        print("\nInterrumpido por el usuario.")
        sys.exit(1)
    except Exception as e:
        print(f"\nERROR: {e}")
        raise
