"""
migrate_sp_control.py
---------------------
Migra empleados S&P Argentina desde "01 - FORECAST CONTROL S&P(S&P).csv"
hacia Azure PostgreSQL (ForecastOS).

Lo que hace:
  1. Lee y parsea el CSV (mismo parseo que generate_simulation.py).
  2. Filtra SOLO empleados con status "Active" — OUT se saltean completamente.
  3. Dry-run por defecto: imprime todo lo que haría, sin escribir nada.
  4. Con --apply: ejecuta en una sola transacción:
       - employees: active/charge/cl/country
       - offerings: crea el offering si Main Skill no existe en la DB
       - client_catalog: crea clientes nuevos
       - forecast_update: upsert por eid
       - forecast_periods: upsert por (eid, period_name)
       - absences: insert PTOs
       - tickets: insert PTOs (con confirmación interactiva por consola para
                  PTOs sin deducción)
  5. Post-transacción: llama recalculate_forecast_period por cada (eid, período) y
     luego restaura chg_hl, chg_sl, chg, SAH y los % desde el CSV. El proc siempre
     recalcula SAH desde el calendario (no usa el valor almacenado), por eso el
     restore también sobreescribe sah — de lo contrario quedaría el valor del proc
     que puede diferir del Excel cuando una ausencia abarca varios períodos.
  6. Genera migration_report.csv y unparsed_pto.csv.

Uso:
    python scripts/migrate_sp_control.py "ruta/al/CSV"           # dry-run
    python scripts/migrate_sp_control.py "ruta/al/CSV" --apply   # ejecuta

Períodos pasados:
  Por defecto solo se migra desde el período actual en adelante; los anteriores quedan
  congelados (ni upsert en forecast_periods ni recálculo). El CSV puede traer valores
  distintos o basura en períodos cerrados (ej. SAH enorme en Ago-P2). --desde-periodo
  NOMBRE mueve el corte; --desde-periodo todos migra también el histórico.

Empleados OUT:
  No se procesan, pero si el CSV los marca OUT y en la DB siguen active=TRUE, se
  pasan a active=FALSE, charge=FALSE y se les guarda termination_date (solo employees;
  no se tocan forecast_periods ni absences). La fecha sale del CSV ("0|OUT||15/10/2026");
  si falta o es futura hay que pasar --term-date eid=YYYY-MM-DD. Los totales cuentan al
  empleado hasta esa fecha (totals_service), así los períodos pasados no cambian.
  Se listan en el dry-run. --no-deactivate lo omite.

EIDs repetidos en el CSV:
  Se listan las filas de cada uno y hay que elegir cuál migrar con --keep eid=N
  (repetible; 0 = descartar al empleado). Con --apply, si queda alguno sin decidir
  el script se frena en vez de dejar que la última fila pise a la anterior.

PTOs con formato no estándar ("11 al 31 ene", "1 al 5 de Febrero 2027"):
  El script infiere las fechas y las LISTA siempre. No se migran salvo que se
  acepten: --fix-ptos las acepta todas; --apply en consola interactiva pregunta
  una por una. Sin año explícito se elige el año más cercano a hoy.

─── NOTAS PARA FUTUROS DEBUGS ──────────────────────────────────────────────

FILTRO ACTIVE (bug histórico):
  Antes faltaba el filtro de status=="Active" al nivel del loop de migración.
  Los OUT se procesaban con active=False en la tabla employees PERO sí se les
  hacía upsert en forecast_periods — horas de SAH sin CHG que bajaban el
  porcentaje global. El dashboard ya los excluye vía e.active=TRUE, pero las
  filas quedaban sucias en la DB. La corrección es filtrar en read_csv/run_migration.

SAH DE ABSENCE BLOCKS (bug histórico):
  recalculate_forecast_period suma TODAS las horas de cada ausencia que solape
  un período (campo a.hours de la tabla absences), sin recortar al rango del
  período. Si una ausencia cubre varios períodos (ej. 10/13 al 10/20 abarcando
  Oct-P1 y Oct-P2), el proc deduce 80h de absence_hours en CADA período, lo que
  deja sah = gross - 80 = 0 en ambos. El CHG restore posterior tampoco tocaba
  sah, con lo que quedaba 0 en la DB aunque el CSV marcara 64h y 56h.
  Corrección: el restore ahora también escribe sah = csv_sah, que es el valor
  neto correcto del Excel.

LÓGICA GROSS_SAH (eliminada):
  Se usaba has_overlap / gross_sah_for_period para escribir el SAH bruto del
  calendario antes de llamar al proc, asumiendo que el proc lo necesitaba como
  base. Pero el proc siempre recalcula gross_sah desde la tabla calendar (nunca
  lee el valor almacenado), así que el valor inicial era transitorio y se pisaba
  igual. Se eliminó para simplificar el flujo y evitar confusión en diagnósticos.

SAH NEGATIVO (bug histórico — Oct 2026):
  El filtro de períodos usaba `v["sah"] > 0`, lo que excluía empleados con SAH
  negativo en el CSV. Ejemplo: flavio.peralta tenía SAH = -40h en Oct-P1 (ajuste
  contable en el Excel), CHG = 0 → la fila no se migraba. El SUM(sah) en DB
  resultaba 7016 en vez de 6976, porque 7016 + (-40) = 6976.
  Corrección: cambiar el filtro a `v["sah"] != 0` para incluir correcciones negativas.
  Si ves un delta entre DB y CSV en el total de SAH, ejecutar:
    python scripts/find_negative_sah.py  (o verificar con pandas pos-only vs all)

VERIFICACIÓN RÁPIDA POST-MIGRACIÓN:
  1. SELECT SUM(sah) FROM forecast_periods WHERE period_name = 'Oct-P1'
     → debe dar 6976 (o el total del CSV para ese período)
  2. SELECT ROUND(SUM(chg)/SUM(sah)*100,1) FROM forecast_periods fp
       JOIN employees e ON e.eid = fp.eid AND e.active
       WHERE period_name = 'Oct-P1'
     → debe dar ≈ 88.3% (valor del Excel)
  3. python scripts/_oct_p1_check.py  — desglosa por offering
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

# Textos genéricos del Excel que NO son clientes reales: no se crean en
# client_catalog y el empleado queda sin cliente.
PLACEHOLDER_CLIENTS = {"proyecto actual"}

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

        # Try to extract date from start of token: "10/1 Día de mudanza" → Oct 1
        date_prefix = re.match(r'^(\d{1,2}/\d{1,2}(?:/\d{2,4})?)\s+', part)
        if date_prefix:
            date_part = date_prefix.group(1)
            m = re.match(r"^(\d{1,2})/(\d{1,2})$", date_part)
            if m:
                try:
                    d_ = date(year_hint, int(m.group(1)), int(m.group(2)))
                    ptos.append({"start_date": d_.isoformat(), "end_date": d_.isoformat(),
                                 "no_ticket": no_ticket})
                    parsed_any = True
                    continue
                except ValueError:
                    pass
            d_ = parse_date_str(date_part, year_hint)
            if d_:
                ptos.append({"start_date": d_.isoformat(), "end_date": d_.isoformat(),
                             "no_ticket": no_ticket})
                parsed_any = True
                continue

        if part:
            unparsed.append(part)

    return parsed_any


def parse_pto_b(pto_raw, eid, ptos, unparsed, year_hint=2026):
    raw = pto_raw.strip()
    no_ticket = "sin ticket" in raw.lower()

    # Strip all leading PTO prefixes (handles "Next PTO PTO: date" chaining)
    _changed = True
    while _changed:
        _changed = False
        for prefix in ("Next PTO ", "PTO: ", "PTO ", "next pto ", "pto: ", "pto "):
            if raw.lower().startswith(prefix.lower()):
                raw = raw[len(prefix):].strip()
                _changed = True
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


MONTHS_ES = {
    "ene": 1, "enero": 1, "feb": 2, "febrero": 2, "mar": 3, "marzo": 3,
    "abr": 4, "abril": 4, "may": 5, "mayo": 5, "jun": 6, "junio": 6,
    "jul": 7, "julio": 7, "ago": 8, "agosto": 8, "sep": 9, "sept": 9,
    "set": 9, "septiembre": 9, "oct": 10, "octubre": 10, "nov": 11,
    "noviembre": 11, "dic": 12, "diciembre": 12,
}

_W = r"[a-záéíóúñ]+"
_INFORMAL_RANGE_RE = re.compile(
    rf"^(\d{{1,2}})(?:\s+de)?(?:\s+({_W}))?\s+(?:al?|-)\s+(\d{{1,2}})(?:\s+de)?\s+({_W})"
    rf"(?:\s+(?:de\s+)?(\d{{4}}))?$", re.IGNORECASE)
_INFORMAL_SINGLE_RE = re.compile(
    rf"^(\d{{1,2}})(?:\s+de)?\s+({_W})(?:\s+(?:de\s+)?(\d{{4}}))?$", re.IGNORECASE)


def parse_informal_pto(pto_raw):
    """
    Best-effort para PTOs con meses en español que parse_pto_b rechaza
    ("11 al 31 ene", "1 al 5 de Febrero 2027 (5 días hábiles)").
    Devuelve {"start_date", "end_date", "no_ticket"} (ISO) o None.
    Sin año explícito se elige el año que deja la fecha más cerca de hoy.
    Las fechas inferidas SIEMPRE pasan por confirmación (review_pto_suggestions).
    """
    raw = pto_raw.strip()
    no_ticket = "sin ticket" in raw.lower()
    raw = re.sub(r"\(.*?\)", " ", raw)
    raw = re.sub(r"(?i)\bsin ticket\b", " ", raw)
    changed = True
    while changed:
        changed = False
        for prefix in ("next pto ", "pto: ", "pto "):
            if raw.strip().lower().startswith(prefix):
                raw = raw.strip()[len(prefix):]
                changed = True
    raw = re.sub(r"\s+", " ", raw).strip(" .")

    m = _INFORMAL_RANGE_RE.match(raw)
    if m:
        d1, m1, d2, m2, yr = m.groups()
        mon2 = MONTHS_ES.get(m2.lower())
        mon1 = MONTHS_ES.get(m1.lower()) if m1 else mon2
        if not mon1 or not mon2:
            return None
        d1, d2 = int(d1), int(d2)
    else:
        m = _INFORMAL_SINGLE_RE.match(raw)
        if not m:
            return None
        d1, m1, yr = m.groups()
        mon1 = mon2 = MONTHS_ES.get(m1.lower())
        if not mon1:
            return None
        d1 = d2 = int(d1)

    def build(year):
        y1 = year - 1 if mon1 > mon2 else year   # rango que cruza fin de año
        return date(y1, mon1, d1), date(year, mon2, d2)

    try:
        if yr:
            sd, ed = build(int(yr))
        else:
            cands = []
            for y in (TODAY.year - 1, TODAY.year, TODAY.year + 1):
                try:
                    cands.append(build(y))
                except ValueError:
                    pass
            if not cands:
                return None
            sd, ed = min(cands, key=lambda c: abs((c[0] - TODAY).days))
    except ValueError:
        return None
    if ed < sd:
        return None
    return {"start_date": sd.isoformat(), "end_date": ed.isoformat(),
            "no_ticket": no_ticket}


def parse_format_a(comment, eid):
    segs = comment.split(";")
    client = None
    roll_on = roll_off = None
    out_date = None
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
                if cl == "out":
                    # "0|OUT||15/10/2026": la fecha de baja viene en el último campo
                    out_date = parse_date_str(rf_str) or parse_date_str(ro_str) or out_date
                if cl in ("licencia", "loa"):
                    licencia = True
                if cl not in SPECIAL_CLIENTS:
                    # Always overwrite: format-A segments are chronological,
                    # so the LAST non-special block is the current project.
                    client = c
                    roll_on = parse_date_str(ro_str)
                    roll_off = parse_date_str(rf_str)
            continue

        # Date-based PTO in format A: "PTO: 9/28-10/2", "PTO 11/13", "Pto 10/1"
        m_pto_date = re.match(r"^pto:?\s+(.+)$", seg, re.IGNORECASE)
        if m_pto_date:
            parse_pto_b(m_pto_date.group(1).strip(), eid, ptos, unparsed)
            continue

        unparsed.append(seg)

    return {"client": client, "roll_on": roll_on, "roll_off": roll_off,
            "out_date": out_date,
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
                # 5-field format: CHG%|client|roll_on|roll_off|PTO — field 5 is the real PTO
                if len(parts) > 4:
                    pto5 = "|".join(parts[4:]).strip()
                    if pto5 and pto5 != "-":
                        parse_pto_b(pto5, eid, ptos, unparsed)
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

    if (parsed.get("client") or "").strip().lower() in PLACEHOLDER_CLIENTS:
        parsed["client"] = None

    roll_off = parsed["roll_off"]
    if roll_off is None and parsed.get("client"):
        # Last project block has no end date → employee is on an open-ended project.
        # Use far-future date so recalculate_forecast_period sees CHG > 0.
        roll_off = date(2099, 12, 31)
    elif roll_off is None and first_available:
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

    # Ruido ("|||", segmentos vacíos): sin letras ni dígitos, no es un PTO escondido.
    # El resto se intenta con el parser informal; lo inferido queda como sugerencia
    # y requiere confirmación (review_pto_suggestions), no se migra solo.
    pto_suggestions, still_unparsed = [], []
    for seg in parsed["unparsed_segments"]:
        if not re.search(r"\w", seg):
            continue
        guess = parse_informal_pto(seg)
        if guess:
            pto_suggestions.append({"raw_text": seg, **guess})
        else:
            still_unparsed.append(seg)
    parsed["unparsed_segments"] = still_unparsed

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
                             if v["sah"] != 0 or v["chg_hl"] != 0 or v["chg_sl"] != 0},
        "ptos":             parsed["ptos"],
        "ptos_need_confirm": ptos_need_confirm,
        "unparsed_segments": parsed["unparsed_segments"],
        "out_date":         parsed.get("out_date"),
        "pto_suggestions":  pto_suggestions,
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


async def working_hours_in_range(conn, db_eid, sd, ed):
    """Horas hábiles reales en [sd, ed] para el país del empleado."""
    result = await conn.fetchval("""
        SELECT COALESCE(SUM(CASE WHEN c.is_working_day THEN 8 ELSE 0 END), 0)
        FROM calendar c
        WHERE c.country = (
            SELECT CASE UPPER(TRIM(COALESCE(e.country, e.location)))
                WHEN 'AR' THEN 'Argentina'
                WHEN 'MX' THEN 'Mexico'
                WHEN 'CR' THEN 'Costa Rica'
                ELSE COALESCE(e.country, e.location)
            END
            FROM employees e WHERE e.eid = $1
        )
        AND c.date BETWEEN $2 AND $3
    """, db_eid, sd, ed)
    return max(int(result or 0), 8)


async def gross_sah_for_period(conn, db_eid, period_name):
    """SAH bruto del calendario para el período (sin deducir ausencias)."""
    return await conn.fetchval("""
        SELECT COALESCE(SUM(CASE WHEN c.is_working_day THEN 8 ELSE 0 END), 0)
        FROM calendar c, periods p
        WHERE p.period_name = $2
          AND c.date BETWEEN p.start_date AND p.end_date
          AND c.country = (
            SELECT CASE UPPER(TRIM(COALESCE(e.country, e.location)))
                WHEN 'AR' THEN 'Argentina'
                WHEN 'MX' THEN 'Mexico'
                WHEN 'CR' THEN 'Costa Rica'
                ELSE COALESCE(e.country, e.location)
            END FROM employees e WHERE e.eid = $1
          )
    """, db_eid, period_name)


# ─── PTO confirmation ─────────────────────────────────────────────────────────

async def restrict_periods(conn, employees, desde=None):
    """
    Congela los períodos anteriores al de corte: se sacan de emp["periods"], así no se
    hace upsert en forecast_periods ni se recalculan (ni el restore de CHG los toca).
    Corte por defecto: el período que contiene a hoy. --desde-periodo NOMBRE lo mueve;
    "todos" desactiva el filtro (recarga histórica). Los períodos que no existen en la
    tabla periods (futuros) se migran siempre.
    """
    rows = await conn.fetch(
        "SELECT period_name, MIN(start_date) AS sd FROM periods GROUP BY period_name"
    )
    starts = {r["period_name"]: r["sd"] for r in rows}

    if desde and desde.lower() == "todos":
        print("\nPeríodos: se migran TODOS (--desde-periodo todos), incluido el histórico.")
        return
    if desde:
        if desde not in starts:
            print(f"ERROR: --desde-periodo {desde!r} no existe en la tabla periods.")
            sys.exit(1)
        cutoff = starts[desde]
    else:
        cutoff = await conn.fetchval(
            "SELECT MIN(start_date) FROM periods WHERE start_date <= $1 AND end_date >= $1",
            TODAY,
        )
        if cutoff is None:
            print("\nAVISO: no se encontró el período actual; no se congela ningún período.")
            return

    cutoff_name = next((n for n, d in starts.items() if d == cutoff), str(cutoff))
    frozen = set()
    for e in employees:
        for pname in list(e["periods"]):
            sd = starts.get(pname)
            if sd is not None and sd < cutoff:
                del e["periods"][pname]
                frozen.add(pname)
    ordered = sorted(frozen, key=lambda n: starts[n])
    print(f"\nPeríodos desde {cutoff_name} ({cutoff}): se migran. "
          f"Congelados (no se tocan): {', '.join(ordered) if ordered else 'ninguno'}")


async def find_deactivations(conn, employees_all, term_dates=None):
    """
    Empleados que el CSV marca como OUT y que en la DB siguen active=TRUE.
    El script no procesa a los OUT (ver FILTRO ACTIVE), así que sin este paso
    quedan activos con datos viejos y cuentan en el dashboard. Mismo match que
    resolve_eids (por eid o por nombre). Un EID que además figura Active en el
    CSV nunca se desactiva. Devuelve [(eid, name, fecha_baja)].
    La fecha sale del CSV ("0|OUT||15/10/2026") o de --term-date eid=YYYY-MM-DD, y se
    guarda en employees.termination_date: los totales cuentan al empleado hasta esa
    fecha, así los períodos anteriores no cambian. None = no se pudo determinar.
    """
    term_dates = term_dates or {}
    csv_dates = {}
    for e in employees_all:
        if e["status"] != "Active" and e.get("out_date"):
            csv_dates.setdefault(e["eid"], e["out_date"])
    active_ids = {e["eid"].lower() for e in employees_all if e["status"] == "Active"}
    out_ids = {e["eid"] for e in employees_all if e["status"] != "Active"}
    rows = await conn.fetch("SELECT eid, name, active FROM employees")
    by_eid = {r["eid"]: r for r in rows}
    by_name = {(r["name"] or "").strip().lower(): r for r in rows}

    result, seen = [], set()
    for e in sorted(out_ids):
        if e.lower() in active_ids:
            continue
        r = by_eid.get(e) or by_name.get(e.lower())
        if r and r["active"] and r["eid"] not in seen and r["eid"].lower() not in active_ids:
            seen.add(r["eid"])
            result.append((r["eid"], r["name"], term_dates.get(e) or csv_dates.get(e)))
    return result


def review_duplicates(employees, apply, keep):
    """
    EIDs que aparecen más de una vez en el CSV. Los upserts son por
    (eid, período), así que sin resolver gana la ÚLTIMA fila y pisa a la anterior.
    Elección por EID: --keep eid=N (N = nº de fila del listado, 0 = descartar al
    empleado) o, con --apply en consola interactiva, se pregunta. Con --apply y
    algún repetido sin decidir el script se frena. En dry-run solo avisa.
    Devuelve la lista de empleados sin las filas descartadas.
    """
    groups = {}
    for i, e in enumerate(employees):
        groups.setdefault(e["eid"], []).append(i)
    dups = {k: v for k, v in groups.items() if len(v) > 1}
    if not dups:
        return employees

    print("\nEIDs repetidos en el CSV (hay que elegir qué fila migrar):")
    drop, pending = set(), []
    for eid, idxs in dups.items():
        print(f"   {eid}:")
        for n, i in enumerate(idxs, 1):
            e = employees[i]
            chg = sum(v["chg_hl"] + v["chg_sl"] for v in e["periods"].values())
            print(f"      fila {n}: cliente={e['client'] or '-'} | {len(e['periods'])} períodos"
                  f" | CHG total {chg:.0f}h | \"{e['raw_comment'][:70]}\"")
        choice = keep.get(eid)
        if choice is None and apply and sys.stdin.isatty():
            while True:
                try:
                    resp = input(f"  ¿Qué fila migrar para {eid}? [1-{len(idxs)}, 0=ninguna]: ").strip()
                except EOFError:   # stdin no interactivo: queda pendiente
                    break
                if resp.isdigit() and 0 <= int(resp) <= len(idxs):
                    choice = int(resp)
                    break
        if choice is None:
            pending.append(eid)
            continue
        if not 0 <= choice <= len(idxs):
            print(f"ERROR: --keep {eid}={choice} fuera de rango (1-{len(idxs)}, 0=ninguna).")
            sys.exit(1)
        drop |= {i for n, i in enumerate(idxs, 1) if n != choice}
        print(f"      -> {'se descarta al empleado' if choice == 0 else f'se migra la fila {choice}'}")

    if pending:
        if apply:
            print(f"\nERROR: repetidos sin decidir: {', '.join(pending)}. "
                  f"Pasar --keep eid=N para cada uno (0 = descartar).")
            sys.exit(1)
        print(f"  -> SIN DECIDIR ({', '.join(pending)}): hoy gana la última fila. "
              f"Pasar --keep eid=N antes de aplicar.")
    return [e for i, e in enumerate(employees) if i not in drop]


def review_pto_suggestions(employees, apply, fix_ptos=False):
    """
    PTOs que no respetan el formato pero cuyas fechas se pudieron inferir
    (ej. "11 al 31 ene"). Imprime el listado y decide cuáles se incluyen:
      --fix-ptos                  -> se aceptan todas
      --apply en consola (tty)    -> se pregunta una por una
      dry-run / sin tty           -> solo se listan, no se incluyen
    Las aceptadas se suman a emp["ptos"] y siguen el flujo normal (incluida
    la confirmación de ticket de confirm_pto). Devuelve (total, aceptadas).
    """
    items = [(e, s) for e in employees for s in e["pto_suggestions"]]
    if not items:
        return 0, 0

    print("\nPTOs con formato no estándar (fechas inferidas, revisar):")
    for e, s in items:
        print(f"   - {e['eid']}: \"{s['raw_text']}\"  ->  {s['start_date']} a {s['end_date']}")

    interactive = apply and not fix_ptos and sys.stdin.isatty()
    accepted = 0
    for e, s in items:
        if fix_ptos:
            ok = True
        elif interactive:
            ok = False
            while True:
                try:
                    resp = input(f"  ¿Incluir {e['eid']} {s['start_date']} -> {s['end_date']}? [s/n]: ").strip().lower()
                except EOFError:   # stdin no interactivo: no se incluye
                    break
                if resp in ("s", "si", "sí", "y", "yes"):
                    ok = True
                    break
                if resp in ("n", "no"):
                    break
                print("  Responder 's' o 'n'.")
        else:
            ok = False
        s["accepted"] = ok
        if ok:
            pto = {"start_date": s["start_date"], "end_date": s["end_date"],
                   "no_ticket": s["no_ticket"]}
            e["ptos"].append(pto)
            if not pto["no_ticket"]:
                e["ptos_need_confirm"].append(pto)
            accepted += 1

    if accepted == len(items):
        print(f"  -> Se incluyen las {accepted}.")
    elif accepted:
        print(f"  -> Se incluyen {accepted} de {len(items)}.")
    else:
        print("  -> NO se incluyen. Correr con --fix-ptos para aceptarlas "
              "(o --apply en una consola interactiva para confirmar una por una).")
    return len(items), accepted


def confirm_pto(eid, sd_str, ed_str, apply, yes_ptos=False):
    """
    Asks the user on the console whether to create+approve a PTO ticket.
    Returns True if the ticket should be created.
    In dry-run (apply=False) or yes_ptos=True always returns True.
    """
    if not apply or yes_ptos:
        return True

    print()
    print(f"  PTO sin deducción — {eid}")
    print(f"    Fechas: {sd_str} -> {ed_str}")
    while True:
        resp = input("  ¿Crear ticket y aprobarlo? [s/n]: ").strip().lower()
        if resp in ("s", "si", "sí", "y", "yes"):
            return True
        if resp in ("n", "no"):
            return False
        print("  Responder 's' o 'n'.")


# ─── main migration logic ─────────────────────────────────────────────────────

async def run_migration(csv_path, apply, yes_ptos=False, fix_ptos=False, keep=None, deactivate=True, term_dates=None,
                        desde_periodo=None):
    print(f"\nCSV: {csv_path}")
    employees_all, period_order, issues = read_csv(csv_path)
    # Solo migrar empleados Active. OUT se saltean COMPLETO (no se les toca
    # employees, forecast_periods ni absences). Ver nota "FILTRO ACTIVE" en el
    # docstring del módulo sobre el bug histórico que causaba SAH inflado.
    employees = [e for e in employees_all if e["status"] == "Active"]
    print(f"Empleados en CSV:      {len(employees_all)}")
    print(f"  Active (migrar):     {len(employees)}")
    print(f"  OUT (saltear):       {len(employees_all) - len(employees)}")
    print(f"Periodos detectados:   {len(period_order)}")

    employees = review_duplicates(employees, apply, keep or {})
    review_pto_suggestions(employees, apply, fix_ptos)

    # Stats (conteo usa employees_all para que OUT refleje el CSV real)
    nj      = sum(1 for e in employees if e["new_joiner"])
    new_off = [e["main_skill"] for e in employees if e["offering_is_new"] and e["main_skill"]]
    new_off_unique = sorted(set(new_off))
    all_clients = [e["client"] for e in employees if e["client"]]

    print(f"\n  Migrando: {len(employees)} Active  (NJ: {nj})")
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
        await restrict_periods(conn, employees, desde_periodo)

        resolved, missing = await resolve_eids(conn, employees)
        print(f"\nEIDs resueltos: {len(resolved)}  |  Sin match: {len(missing)}")
        for m in missing:
            print(f"   - {m}")

        to_deactivate = (await find_deactivations(conn, employees_all, term_dates)
                         if deactivate else [])
        if to_deactivate:
            print("\nEmpleados OUT en el CSV que siguen activos en la DB "
                  "(se pasan a active=FALSE, con fecha de baja):")
            bad_dates = []
            for d_eid, d_name, d_date in to_deactivate:
                if d_date is None or (d_date > TODAY and d_eid not in (term_dates or {})):
                    bad_dates.append(d_eid)
                    why = "SIN FECHA" if d_date is None else f"{d_date} es FUTURA"
                    print(f"   - {d_eid}  ({d_name})  baja: {why}  <-- revisar")
                else:
                    print(f"   - {d_eid}  ({d_name})  baja: {d_date}")
            if bad_dates:
                msg = (f"fecha de baja dudosa para: {', '.join(bad_dates)}. "
                       f"Pasar --term-date eid=YYYY-MM-DD para cada uno.")
                if apply:
                    print(f"\nERROR: {msg}")
                    sys.exit(1)
                print(f"  -> {msg}")
        elif deactivate:
            print("\nEmpleados a desactivar (OUT en CSV, activos en DB): ninguno")

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
  employees nuevos (NJ):     {len(missing)}
  employees a desactivar:    {len(to_deactivate)}
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

        tickets_created = 0
        tickets_skipped = 0
        fp_count = 0
        abs_count = 0
        recalc_count = 0
        nj_inserted = 0

        async with conn.transaction():
            if to_deactivate:
                # Solo employees: sin tocar forecast_periods ni absences.
                # termination_date: se respeta la que ya exista (p.ej. de un ticket de baja).
                for d_eid, _n, d_date in to_deactivate:
                    await conn.execute(
                        "UPDATE employees SET active = FALSE, charge = FALSE, "
                        "termination_date = COALESCE(termination_date, $2) WHERE eid = $1",
                        d_eid, d_date,
                    )
                print(f"  [employees] Desactivados: {len(to_deactivate)}")
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
                # Siempre usar el SAH del CSV como valor inicial. El proc
                # recalculate_forecast_period recalcula SAH desde la tabla calendar
                # (ignora el valor almacenado), y el restore post-transacción lo vuelve
                # a pisar con csv_sah de todas formas. Ver nota "LÓGICA GROSS_SAH" en
                # el docstring del módulo sobre por qué se eliminó has_overlap.
                for pname, pdata in emp["periods"].items():
                    chg_hl = pdata["chg_hl"]
                    chg_sl = pdata["chg_sl"]
                    sah    = pdata["sah"]

                    chg     = chg_hl + chg_sl
                    pct_hl  = round(chg_hl / sah * 100, 2) if sah > 0 else 0.0
                    pct_sl  = round(chg_sl / sah * 100, 2) if sah > 0 else 0.0
                    pct     = round(chg    / sah * 100, 2) if sah > 0 else 0.0

                    await conn.execute("""
                        INSERT INTO forecast_periods
                            (eid, period_name, chg_hl, chg_sl, sah,
                             chg, chg_cascadeadas,
                             chg_cascadeadas_hl, chg_cascadeadas_sl,
                             chg_pct_hl, chg_pct_sl, chg_pct, absence_hours)
                        VALUES ($1,$2,$3,$4,$5,$6,0,0,0,$7,$8,$9,0)
                        ON CONFLICT (eid, period_name) DO UPDATE SET
                            chg_hl             = $3,
                            chg_sl             = $4,
                            sah                = $5,
                            chg                = $6,
                            chg_cascadeadas    = 0,
                            chg_cascadeadas_hl = 0,
                            chg_cascadeadas_sl = 0,
                            chg_pct_hl         = $7,
                            chg_pct_sl         = $8,
                            chg_pct            = $9
                    """, db_eid, pname, chg_hl, chg_sl, sah,
                         chg, pct_hl, pct_sl, pct)
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
                    hours = (n_days * 8) if n_days else await working_hours_in_range(conn, db_eid, sd, ed)

                    # Insert absence — update hours if changed, skip if already exists
                    existing = await conn.fetchrow("""
                        SELECT id, hours FROM absences
                        WHERE eid=$1 AND type='pto'
                          AND start_date <= $3 AND end_date >= $2
                        LIMIT 1
                    """, db_eid, sd, ed)
                    if existing:
                        if existing["hours"] != hours:
                            await conn.execute(
                                "UPDATE absences SET hours=$1 WHERE id=$2",
                                hours, existing["id"]
                            )
                        continue
                    await conn.execute("""
                        INSERT INTO absences (eid, type, start_date, end_date, hours)
                        VALUES ($1,'pto',$2,$3,$4)
                    """, db_eid, sd, ed, hours)
                    abs_count += 1

                    # Ticket
                    if no_ticket:
                        continue

                    # PTOs with specific dates need interactive confirmation
                    create_ticket = confirm_pto(db_eid, sd_str, ed_str, apply, yes_ptos)

                    if create_ticket:
                        await conn.execute("""
                            INSERT INTO tickets
                                (type, eid, status, start_date, end_date, created_by, date)
                            VALUES ('pto',$1,'Aprobado',$2,$3,'migrate_sp_control',NOW())
                        """, db_eid, sd, ed)
                        tickets_created += 1
                    else:
                        tickets_skipped += 1

            # Handle missing employees: insert as new joiners
            missing_set = set(missing)
            for emp in employees:
                eid = emp["eid"]
                if eid not in missing_set:
                    continue

                active_flag = emp["status"] != "OUT"
                name = " ".join(p.capitalize() for p in re.split(r"[.\-]", eid))

                # insert employee
                await conn.execute("""
                    INSERT INTO employees
                        (eid, name, country, cl, active, charge, new_joiner, offering)
                    VALUES ($1, $2, 'AR', $3, $4, $4, TRUE, $5)
                    ON CONFLICT (eid) DO NOTHING
                """, eid, name,
                     float(emp["cl"]) if emp["cl"] else None,
                     active_flag,
                     emp["main_skill"] or None)

                resolved[eid] = eid  # so recalculate loop picks them up

                # client
                if emp["client"]:
                    _, created = await ensure_client(conn, emp["client"], apply)
                    if created:
                        print(f"  [client_catalog] Creado: '{emp['client']}'")

                # forecast_update
                has_chg = any(v["chg_hl"] > 0 for v in emp["periods"].values())
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
                """, eid, emp["client"], emp["roll_on"], emp["roll_off"],
                     emp["first_available"], emp["days_to_avail"],
                     "effective" if has_chg else "assumption")

                # forecast_periods (NJ — igual que empleados existentes: SAH del CSV)
                for pname, pdata in emp["periods"].items():
                    chg_hl = pdata["chg_hl"]
                    chg_sl = pdata["chg_sl"]
                    sah    = pdata["sah"]
                    chg = chg_hl + chg_sl
                    pct_hl = round(chg_hl / sah * 100, 2) if sah > 0 else 0.0
                    pct_sl = round(chg_sl / sah * 100, 2) if sah > 0 else 0.0
                    pct    = round(chg    / sah * 100, 2) if sah > 0 else 0.0
                    await conn.execute("""
                        INSERT INTO forecast_periods
                            (eid, period_name, chg_hl, chg_sl, sah,
                             chg, chg_cascadeadas,
                             chg_cascadeadas_hl, chg_cascadeadas_sl,
                             chg_pct_hl, chg_pct_sl, chg_pct, absence_hours)
                        VALUES ($1,$2,$3,$4,$5,$6,0,0,0,$7,$8,$9,0)
                        ON CONFLICT (eid, period_name) DO UPDATE SET
                            chg_hl             = $3,
                            chg_sl             = $4,
                            sah                = $5,
                            chg                = $6,
                            chg_cascadeadas    = 0,
                            chg_cascadeadas_hl = 0,
                            chg_cascadeadas_sl = 0,
                            chg_pct_hl         = $7,
                            chg_pct_sl         = $8,
                            chg_pct            = $9
                    """, eid, pname, chg_hl, chg_sl, sah,
                         chg, pct_hl, pct_sl, pct)
                    fp_count += 1

                # absences + PTO tickets
                for pto in emp["ptos"]:
                    sd_str = pto.get("start_date")
                    ed_str = pto.get("end_date")
                    no_ticket = pto.get("no_ticket", False)
                    n_days = pto.get("n_days")
                    pname_ref = pto.get("period")
                    if not sd_str and pname_ref:
                        pd_start, pd_end = await get_period_dates(conn, pname_ref)
                        if pd_start:
                            sd_str = pd_start.isoformat()
                            ed_str = pd_end.isoformat() if pd_end else sd_str
                    if not sd_str:
                        continue
                    sd = date.fromisoformat(sd_str)
                    ed = date.fromisoformat(ed_str) if ed_str else sd
                    hours = (n_days * 8) if n_days else await working_hours_in_range(conn, eid, sd, ed)
                    existing = await conn.fetchrow("""
                        SELECT id, hours FROM absences
                        WHERE eid=$1 AND type='pto'
                          AND start_date <= $3 AND end_date >= $2
                        LIMIT 1
                    """, eid, sd, ed)
                    if existing:
                        if existing["hours"] != hours:
                            await conn.execute(
                                "UPDATE absences SET hours=$1 WHERE id=$2",
                                hours, existing["id"]
                            )
                        continue
                    await conn.execute("""
                        INSERT INTO absences (eid, type, start_date, end_date, hours)
                        VALUES ($1,'pto',$2,$3,$4)
                    """, eid, sd, ed, hours)
                    abs_count += 1
                    if no_ticket:
                        continue
                    create_ticket = confirm_pto(eid, sd_str, ed_str, apply, yes_ptos)
                    if create_ticket:
                        await conn.execute("""
                            INSERT INTO tickets
                                (type, eid, status, start_date, end_date, created_by, date)
                            VALUES ('pto',$1,'Aprobado',$2,$3,'migrate_sp_control',NOW())
                        """, eid, sd, ed)
                        tickets_created += 1
                    else:
                        tickets_skipped += 1

                # NJ ticket (Aprobado)
                await conn.execute("""
                    INSERT INTO tickets
                        (type, eid, status, start_date, end_date, created_by, date)
                    VALUES ('nj',$1,'Aprobado',$2,$3,'migrate_sp_control',NOW())
                """, eid, emp["roll_on"], emp["roll_off"])
                nj_inserted += 1
                print(f"  [NJ] Insertado y aprobado: {eid}")

        print("\nRecalculando forecast_periods...")
        for emp in employees:
            db_eid = resolved.get(emp["eid"])
            if not db_eid:
                continue
            for pname, pdata in emp["periods"].items():
                await conn.execute(
                    "SELECT recalculate_forecast_period($1, $2)", db_eid, pname
                )
                # Restaurar CHG y SAH desde el CSV.
                #
                # El proc sobreescribe chg_hl, chg_sl, chg, chg_pct* y sah con sus
                # propios cálculos (roll_on/roll_off + calendar). Se restauran todos
                # desde el CSV para que los valores de la DB coincidan exactamente con
                # el Excel. SAH es clave: el proc puede diferir cuando una ausencia
                # abarca varios períodos (ver nota "SAH DE ABSENCE BLOCKS" en el
                # docstring del módulo). absence_hours lo setea el proc y no se toca
                # porque el dashboard no lo usa para calcular CHG%.
                chg_hl  = pdata["chg_hl"]
                chg_sl  = pdata["chg_sl"]
                csv_sah = pdata["sah"]
                await conn.execute("""
                    UPDATE forecast_periods SET
                        chg_hl     = $3::numeric,
                        chg_sl     = $4::numeric,
                        chg        = $3::numeric + $4::numeric,
                        sah        = $5::numeric,
                        chg_pct_hl = CASE WHEN $5::numeric > 0
                                         THEN ROUND($3::numeric / $5::numeric * 100, 2)
                                         ELSE 0 END,
                        chg_pct_sl = CASE WHEN $5::numeric > 0
                                         THEN ROUND($4::numeric / $5::numeric * 100, 2)
                                         ELSE 0 END,
                        chg_pct    = CASE WHEN $5::numeric > 0
                                         THEN ROUND(($3::numeric + $4::numeric) / $5::numeric * 100, 2)
                                         ELSE 0 END
                    WHERE eid = $1 AND period_name = $2
                """, db_eid, pname, chg_hl, chg_sl, csv_sah)
                recalc_count += 1
        print(f"  {recalc_count} períodos recalculados (CHG + SAH restaurados desde CSV).")

        print(f"\nResultado:")
        print(f"  employees actualizados:    {len(resolved)}")
        print(f"  forecast_update upserts:   {len(resolved)}")
        print(f"  forecast_periods upserts:  {fp_count}")
        print(f"  absences insertadas:       {abs_count}")
        print(f"  tickets PTO creados:       {tickets_created}")
        print(f"  tickets PTO omitidos:      {tickets_skipped}")
        print(f"  empleados NJ insertados:   {nj_inserted}")
        print(f"  empleados desactivados:    {len(to_deactivate)}")
        print(f"  períodos recalculados:     {recalc_count}")

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
        w.writerow(["eid", "raw_text", "suggested_start", "suggested_end", "included"])
        for item in issues["pto_unparsed"]:
            w.writerow([item["eid"], item["raw_text"], "", "", ""])
        for emp in employees:
            for s in emp["pto_suggestions"]:
                w.writerow([emp["eid"], s["raw_text"], s["start_date"], s["end_date"],
                            "si" if s.get("accepted") else "no"])

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
    p.add_argument("--yes-ptos", action="store_true",
                   help="Aprobar todos los tickets PTO sin confirmación interactiva.")
    p.add_argument("--no-deactivate", action="store_true",
                   help="No desactivar a los empleados OUT del CSV que siguen activos en la DB.")
    p.add_argument("--desde-periodo", metavar="PERIODO", default=None,
                   help="Primer período que se migra (ej. Oct-P2). Por defecto el período "
                        "actual: los anteriores quedan congelados. 'todos' = sin corte "
                        "(recarga histórica).")
    p.add_argument("--term-date", action="append", default=[], metavar="EID=YYYY-MM-DD",
                   help="Fecha de baja para un empleado OUT cuando el CSV no la trae o es "
                        "dudosa (futura). Último día que cuenta en los totales. Repetible.")
    p.add_argument("--keep", action="append", default=[], metavar="EID=N",
                   help="Para un EID repetido en el CSV, qué fila migrar (N = nº de fila "
                        "del listado, 0 = descartar al empleado). Repetible.")
    p.add_argument("--fix-ptos", action="store_true",
                   help="Aceptar los PTOs con formato no estándar cuyas fechas se "
                        "infirieron (ej. '11 al 31 ene'). Sin este flag solo se listan.")
    return p.parse_args()


if __name__ == "__main__":
    args = parse_args()
    keep = {}
    for item in args.keep:
        eid_, _, n_ = item.rpartition("=")
        if not eid_ or not n_.strip().isdigit():
            print(f"ERROR: --keep espera EID=N, recibido: {item!r}")
            sys.exit(1)
        keep[eid_.strip()] = int(n_)
    term_dates = {}
    for item in args.term_date:
        eid_, _, d_ = item.rpartition("=")
        try:
            term_dates[eid_.strip()] = date.fromisoformat(d_.strip())
        except ValueError:
            print(f"ERROR: --term-date espera EID=YYYY-MM-DD, recibido: {item!r}")
            sys.exit(1)
    if not os.path.exists(args.csv_path):
        print(f"ERROR: No existe el archivo: {args.csv_path}")
        sys.exit(1)

    mode = "APLICANDO" if args.apply else "DRY-RUN (no escribe nada)"
    print(f"\n=== migrate_sp_control.py [{mode}] ===")
    if args.apply and not args.yes_ptos:
        print("ADVERTENCIA: PTOs sin deducción requerirán confirmación por consola.")

    try:
        asyncio.run(run_migration(args.csv_path, args.apply, args.yes_ptos, args.fix_ptos, keep,
                                  not args.no_deactivate, term_dates,
                                  args.desde_periodo))
    except KeyboardInterrupt:
        print("\nInterrumpido por el usuario.")
        sys.exit(1)
    except Exception as e:
        print(f"\nERROR: {e}")
        raise
