#!/usr/bin/env python3
"""
generate_simulation.py
Reads "01 - FORECAST CONTROL S&P(S&P).csv" and generates migration_simulation.html
No DB connection required. Uses stdlib only.

Usage:
    python scripts/generate_simulation.py "path/to/01 - FORECAST CONTROL S&P(S&P).csv"
    # → generates migration_simulation.html in current directory
"""

import argparse
import csv
import json
import os
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

ENCODING = "latin-1"
TODAY = date.today()

# CSV column indices (0-based, validated against actual header)
COL_STATUS      = 0
COL_OFFERING    = 1
COL_SUB_OFF     = 2
COL_COMENTARIOS = 8
COL_FIRST_AVAIL = 9
COL_LEVEL       = 10
COL_EID         = 11

# Period name mapping — must match DB convention in load_periods.py
MONTH_MAP = {
    "JUL": "Jul", "AUG": "Ago", "SEP": "Sep", "OCT": "Oct",
    "NOV": "Nov", "DEC": "Dic", "JAN": "Ene", "FEB": "Feb",
    "MAR": "Mar", "APR": "Abr", "MAY": "May", "JUN": "Jun",
}

SPECIAL_CLIENTS = {"", "out", "licencia", "nj", "sl", "loa"}

# Offerings ya existentes en la DB (import_offerings_clients.py::VALID_OFFERINGS)
KNOWN_OFFERINGS = {"SO", "PR", "Tools", "S4", "Ariba", "Oracle"}

# Keywords that flag a PTO comment as informal/unparsed
INFORMAL_PTO_KEYWORDS = re.compile(
    r"\b(ago|ene|feb|abr|jul|ago|sept|nov|dic|enero|febrero|marzo|abril|mayo|junio|"
    r"julio|agosto|septiembre|octubre|noviembre|diciembre|loa|loar|acn days|ver notas|"
    r"d[íi]a ec|turno|vacacion)\b",
    re.IGNORECASE,
)


# ─── helpers ───────────────────────────────────────────────────────────────────

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
    """Parse DD/MM/YYYY, DD/MM/YY, DD/MM (with year_hint), M/D/YY, M/D."""
    if not s:
        return None
    s = s.strip()
    for fmt in ("%d/%m/%Y", "%d/%m/%y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            pass
    # DD/MM without year
    m = re.match(r"^(\d{1,2})/(\d{1,2})$", s)
    if m:
        d, mo = int(m.group(1)), int(m.group(2))
        try:
            return date(year_hint, mo, d)
        except ValueError:
            pass
    # M/D without year (US-style month first, used in PTO fields)
    m = re.match(r"^(\d{1,2})/(\d{1,2})$", s)
    if m:
        mo, d = int(m.group(1)), int(m.group(2))
        try:
            return date(year_hint, mo, d)
        except ValueError:
            pass
    return None


def parse_pto_md(token, year_hint=2026):
    """Parse M/D or M/D/YY PTO token (month-first US notation)."""
    token = token.strip()
    m = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{2,4})$", token)
    if m:
        mo, d, yr = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if yr < 100:
            yr += 2000
        try:
            return date(yr, mo, d)
        except ValueError:
            pass
    m = re.match(r"^(\d{1,2})/(\d{1,2})$", token)
    if m:
        mo, d = int(m.group(1)), int(m.group(2))
        try:
            return date(year_hint, mo, d)
        except ValueError:
            pass
    return None


def excel_serial_to_date(n):
    try:
        n = int(str(n).strip())
        if n > 40000:
            return (datetime(1899, 12, 30) + timedelta(days=n)).date()
    except (TypeError, ValueError):
        pass
    return None


def days_to_avail(roll_off):
    if roll_off is None:
        return None
    delta = (roll_off - TODAY).days
    return max(0, delta)


# ─── period column detection ────────────────────────────────────────────────────

def detect_period_columns(header):
    """
    Returns:
      {col_index: (period_name, col_type)}
      period_name like "Jul-P1" or "Ene-P1-27"
      col_type in {"chg_hl", "chg_sl", "sah"}
    Also returns ordered list of unique period_names.
    """
    cols = {}
    period_order = []
    seen = set()

    for i, h in enumerate(header):
        h = h.strip()
        # CHG HL JUL P1 2026
        m = re.match(r"^CHG (HL|SL) (\w+) (P[12]) (\d{4})$", h)
        if m:
            hl_sl, month, phalf, year = m.groups()
            abbr = MONTH_MAP.get(month.upper())
            if abbr:
                suffix = "" if year == "2026" else f"-{year[2:]}"
                pname = f"{abbr}-{phalf}{suffix}"
                cols[i] = (pname, "chg_hl" if hl_sl == "HL" else "chg_sl")
                if pname not in seen:
                    seen.add(pname)
                    period_order.append(pname)
            continue
        # SAH P1 JUL P1 2026
        m = re.match(r"^SAH P[12] (\w+) (P[12]) (\d{4})$", h)
        if m:
            month, phalf, year = m.groups()
            abbr = MONTH_MAP.get(month.upper())
            if abbr:
                suffix = "" if year == "2026" else f"-{year[2:]}"
                pname = f"{abbr}-{phalf}{suffix}"
                cols[i] = (pname, "sah")
    return cols, period_order


# ─── PTO parsing ────────────────────────────────────────────────────────────────

def parse_pto_b(pto_raw, eid):
    """
    Parse the pto_info field from Formato B comments.
    Returns (list_of_pto_dicts, list_of_unparsed_strings).

    Each pto dict: {start_date, end_date, n_days, no_ticket}
    Dates as ISO strings or None.
    """
    ptos = []
    unparsed = []

    raw = pto_raw.strip()
    if not raw:
        return ptos, unparsed

    # Strip leading "Next PTO" / "PTO:" / "PTO" / "PTO:"
    raw = re.sub(r"^Next\s+PTO\s*:?\s*", "", raw, flags=re.IGNORECASE).strip()
    raw = re.sub(r"^PTO\s*:?\s*", "", raw, flags=re.IGNORECASE).strip()

    if not raw or raw == "-":
        return ptos, unparsed

    no_ticket = "sin ticket" in raw.lower()
    # Remove "sin ticket" clause for further parsing
    raw_clean = re.sub(r"\(?sin ticket\)?", "", raw, flags=re.IGNORECASE).strip(" .,")

    # --- Excel serial --------------------------------------------------
    if re.match(r"^\d{5,}$", raw_clean):
        d = excel_serial_to_date(int(raw_clean))
        if d:
            ptos.append({
                "start_date": d.isoformat(),
                "end_date": d.isoformat(),
                "n_days": 1,
                "no_ticket": no_ticket,
            })
            return ptos, unparsed

    # --- Informal / non-parseable text: Spanish months, LOA, ACN Days, etc.
    if INFORMAL_PTO_KEYWORDS.search(raw_clean):
        unparsed.append(raw)
        return ptos, unparsed

    # --- Try to parse the cleaned string into date segments ------------
    # Normalize: replace "al " and " y " with commas for easier splitting
    normalized = raw_clean
    normalized = re.sub(r"\s+al?\s+", " a ", normalized, flags=re.IGNORECASE)
    normalized = re.sub(r"\s+y\s+", ", ", normalized, flags=re.IGNORECASE)
    # Collapse spaces around dashes so "9/7 -9/18" becomes "9/7-9/18"
    normalized = re.sub(r"\s*-\s*(?=\d)", "-", normalized)
    normalized = re.sub(r"(?<=\d)-\s+(?=\d)", "-", normalized)

    # --- "date1 a date2" ranges ----------------------------------------
    range_a = re.split(r"\s+a\s+", normalized, maxsplit=1, flags=re.IGNORECASE)
    if len(range_a) == 2:
        d1 = parse_pto_md(range_a[0].strip()) or parse_date_str(range_a[0].strip())
        # Second part may have trailing text or additional dates after " - "
        second = range_a[1].strip()
        # Split on first separator (period, comma, or " - ")
        second_parts = re.split(r"[.,]|\s+-\s*", second)
        second_main = second_parts[0].strip()
        d2 = parse_pto_md(second_main) or parse_date_str(second_main)
        if d1 and d2:
            n = max(1, (d2 - d1).days + 1)
            ptos.append({
                "start_date": d1.isoformat(),
                "end_date": d2.isoformat(),
                "n_days": n,
                "no_ticket": no_ticket,
            })
            # Check for additional dates after the range
            extra_parts = second_parts[1:]
            for extra in extra_parts:
                extra = extra.strip()
                if extra and not INFORMAL_PTO_KEYWORDS.search(extra):
                    d_extra = parse_pto_md(extra) or parse_date_str(extra)
                    if d_extra:
                        ptos.append({
                            "start_date": d_extra.isoformat(),
                            "end_date": d_extra.isoformat(),
                            "n_days": 1,
                            "no_ticket": no_ticket,
                        })
                    elif extra and extra != "-":
                        unparsed.append(extra)
            return ptos, unparsed
        elif d1 and not d2:
            # d2 failed — try the whole second as a fallback list
            parsed_any = _parse_comma_or_dash_list(second, no_ticket, ptos, unparsed)
            if parsed_any:
                return ptos, unparsed

    # --- Comma or dash-separated list ----------------------------------
    unp_before = len(unparsed)
    parsed_any = _parse_comma_or_dash_list(normalized, no_ticket, ptos, unparsed)

    # Only add whole raw string if the inner parser didn't add anything at all
    if not parsed_any and len(unparsed) == unp_before and normalized.strip():
        unparsed.append(raw)

    return ptos, unparsed


def _parse_comma_or_dash_list(text, no_ticket, ptos, unparsed):
    """
    Parse a comma- or dash-separated list of dates/ranges.
    Modifies ptos and unparsed in place. Returns True if anything parsed.
    """
    # Split by commas first
    tokens = [t.strip() for t in re.split(r",", text) if t.strip()]
    parsed_any = False

    for token in tokens:
        if not token or token == "-":
            continue
        # Normalize spaces around dashes within each token
        token = re.sub(r"\s*-\s*(?=\d)", "-", token)
        token = re.sub(r"(?<=\d)-\s+(?=\d)", "-", token)

        # Sub-range like "9/28-9/29-9/30" or "9/7-9/18" or "8/31-9/4"
        if re.search(r"\d+/\d+-\s*\d+/\d+", token):
            parts = re.split(r"-\s*(?=\d+/)", token)
            if len(parts) >= 2:
                d1 = parse_pto_md(parts[0])
                d2 = parse_pto_md(parts[-1])
                if d1 and d2:
                    n = max(1, (d2 - d1).days + 1)
                    ptos.append({
                        "start_date": d1.isoformat(),
                        "end_date": d2.isoformat(),
                        "n_days": n,
                        "no_ticket": no_ticket,
                    })
                    parsed_any = True
                    continue
        # Pattern M/D-M-D (second date uses dashes instead of slash), e.g. "10/6-10-09"
        m = re.match(r"^(\d{1,2})/(\d{1,2})-(\d{1,2})-(\d{1,2})$", token)
        if m:
            m1, d1v, m2, d2v = int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4))
            try:
                start = date(2026, m1, d1v)
                end   = date(2026, m2, d2v)
                if abs((end - start).days) <= 60:
                    n = max(1, (end - start).days + 1)
                    ptos.append({"start_date": start.isoformat(), "end_date": end.isoformat(),
                                 "n_days": n, "no_ticket": no_ticket})
                    parsed_any = True
                    continue
            except ValueError:
                pass
        # Consecutive dash-separated same-month days "9/28-9/29-9/30"
        if re.match(r"^(\d+/\d+)(-\d+/\d+)+$", token):
            day_strs = token.split("-")
            days = [parse_pto_md(d) for d in day_strs]
            days = [d for d in days if d]
            if days:
                for d in days:
                    ptos.append({
                        "start_date": d.isoformat(),
                        "end_date": d.isoformat(),
                        "n_days": 1,
                        "no_ticket": no_ticket,
                    })
                parsed_any = True
                continue
        # Single M/D date
        d = parse_pto_md(token)
        if d:
            ptos.append({
                "start_date": d.isoformat(),
                "end_date": d.isoformat(),
                "n_days": 1,
                "no_ticket": no_ticket,
            })
            parsed_any = True
            continue
        # Single full date DD/MM/YY or DD/MM/YYYY
        d = parse_date_str(token)
        if d:
            ptos.append({
                "start_date": d.isoformat(),
                "end_date": d.isoformat(),
                "n_days": 1,
                "no_ticket": no_ticket,
            })
            parsed_any = True
            continue
        # Excel serial in a list position
        if re.match(r"^\d{5,}$", token):
            d = excel_serial_to_date(int(token))
            if d:
                ptos.append({
                    "start_date": d.isoformat(),
                    "end_date": d.isoformat(),
                    "n_days": 1,
                    "no_ticket": no_ticket,
                })
                parsed_any = True
                continue
        # Could not parse
        if token and not INFORMAL_PTO_KEYWORDS.search(token):
            unparsed.append(token)

    return parsed_any


# ─── Formato A parser ────────────────────────────────────────────────────────

def parse_format_a(comment, eid):
    """
    Semicolon-separated Formato A (SAP/MM/Ariba).
    Returns dict with: client, roll_on, roll_off, ptos, unparsed_segments,
                       new_joiner, licencia
    """
    result = {
        "client": None, "roll_on": None, "roll_off": None,
        "ptos": [], "unparsed_segments": [],
        "new_joiner": False, "licencia": False,
    }

    # PTO pattern: "8 PTO SEP P1"
    PTO_RE = re.compile(r"^(\d+)\s+PTO\s+(\w+)\s+(P[12])$", re.IGNORECASE)
    # HE pattern: "8 HE SEP P2"
    HE_RE = re.compile(r"^(\d+)\s+HE\s+(\w+)\s+(P[12])$", re.IGNORECASE)
    # Project block: starts with digit and pipe
    PROJ_RE = re.compile(r"^(\d+)\|(.*)$")

    for seg in comment.split(";"):
        seg = seg.strip()
        if not seg:
            continue

        m = PTO_RE.match(seg)
        if m:
            n_days, month, phalf = int(m.group(1)), m.group(2).upper(), m.group(3)
            abbr = MONTH_MAP.get(month)
            if abbr:
                pname = f"{abbr}-{phalf}"
                result["ptos"].append({
                    "period_name": pname,
                    "n_days": n_days,
                    "start_date": None,
                    "end_date": None,
                    "no_ticket": False,
                })
            else:
                result["unparsed_segments"].append(seg)
            continue

        if HE_RE.match(seg):
            # Log HE but don't insert
            continue

        m = PROJ_RE.match(seg)
        if m:
            parts = seg.split("|")
            if len(parts) < 4:
                parts += [""] * (4 - len(parts))
            chg_pct_str, client_raw, roll_on_str, roll_off_str = (
                parts[0].strip(), parts[1].strip(), parts[2].strip(), parts[3].strip()
            )
            client_low = client_raw.lower()

            if client_low == "nj":
                result["new_joiner"] = True
            if client_low in {"licencia", "loa"}:
                result["licencia"] = True

            roll_on = parse_date_str(roll_on_str)
            roll_off = parse_date_str(roll_off_str)

            # Only set client from blocks where client is a real name
            if client_low not in SPECIAL_CLIENTS and result["client"] is None:
                result["client"] = client_raw
                result["roll_on"] = roll_on
                result["roll_off"] = roll_off
            continue

        # Didn't match anything
        result["unparsed_segments"].append(seg)

    return result


# ─── Formato B parser ────────────────────────────────────────────────────────

def parse_format_b(comment, eid):
    """
    Pipe-separated Formato B (SO/PR/Tools), no semicolons.
    Returns dict with: client, roll_on, roll_off, ptos, unparsed_segments,
                       new_joiner, licencia
    """
    result = {
        "client": None, "roll_on": None, "roll_off": None,
        "ptos": [], "unparsed_segments": [],
        "new_joiner": False, "licencia": False,
    }

    parts = comment.split("|")
    if len(parts) < 2:
        result["unparsed_segments"].append(comment)
        return result

    # Pad to 4 fields
    while len(parts) < 4:
        parts.append("")

    chg_pct_str = parts[0].strip()
    client_raw  = parts[1].strip()
    date_field  = parts[2].strip()
    pto_field   = parts[3].strip()

    client_low = client_raw.lower()
    if client_low == "nj":
        result["new_joiner"] = True
    if client_low in {"licencia", "loa"}:
        result["licencia"] = True
    if client_low not in SPECIAL_CLIENTS:
        result["client"] = client_raw

    # --- Parse date field ---
    if " to " in date_field:
        # "DD/MM/YYYY to DD/MM/YYYY" or "- to DD/MM/YYYY"
        sides = [s.strip() for s in date_field.split(" to ", 1)]
        result["roll_on"]  = parse_date_str(sides[0]) if sides[0] != "-" else None
        result["roll_off"] = parse_date_str(sides[1]) if sides[1] != "-" else None
        # In this format, pto_field is always the PTO info
        if pto_field:
            ptos, unp = parse_pto_b(pto_field, eid)
            result["ptos"].extend(ptos)
            result["unparsed_segments"].extend(unp)
    else:
        # Single date = roll_on; field 4 is roll_off (if date) or pto_info
        result["roll_on"] = parse_date_str(date_field) if date_field != "-" else None

        # Determine if field 4 is roll_off or pto_info
        pf = pto_field.strip()
        if re.match(r"^\d{2}/\d{2}/\d{4}$", pf):
            result["roll_off"] = parse_date_str(pf)
        elif pf and pf != "-":
            ptos, unp = parse_pto_b(pto_field, eid)
            result["ptos"].extend(ptos)
            result["unparsed_segments"].extend(unp)

    return result


# ─── employee row parser ─────────────────────────────────────────────────────

def parse_employee(row, period_cols):
    def g(i):
        return clean(row[i]) if len(row) > i else None

    eid        = g(COL_EID)
    status     = g(COL_STATUS) or "Active"
    off        = g(COL_OFFERING)
    sub        = g(COL_SUB_OFF)
    main_skill = g(4)          # Main Skill column → offering label for the DB
    level      = g(COL_LEVEL)
    cl     = to_int_cl(level)
    fa_str = g(COL_FIRST_AVAIL)
    first_available = parse_date_str(fa_str)
    comment = g(COL_COMENTARIOS) or ""

    # Period data
    periods = {}
    for col_i, (pname, col_type) in period_cols.items():
        val = to_float(g(col_i))
        if pname not in periods:
            periods[pname] = {"chg_hl": 0.0, "chg_sl": 0.0, "sah": 0.0}
        periods[pname][col_type] = val

    # Derive chg and chg_pct per period
    for pname, pdata in periods.items():
        pdata["chg"] = pdata["chg_hl"] + pdata["chg_sl"]
        sah = pdata["sah"]
        pdata["chg_pct"] = round(pdata["chg"] / sah * 100, 1) if sah > 0 else 0.0

    # Comentarios
    if ";" in comment:
        parsed = parse_format_a(comment, eid)
        fmt = "A"
    elif "|" in comment:
        parsed = parse_format_b(comment, eid)
        fmt = "B"
    else:
        parsed = {
            "client": None, "roll_on": None, "roll_off": None,
            "ptos": [], "unparsed_segments": [comment] if comment else [],
            "new_joiner": False, "licencia": False,
        }
        fmt = "?"

    roll_off = parsed["roll_off"] or first_available
    dta = days_to_avail(roll_off)

    # EID format issues
    eid_issues = []
    if eid and "." not in eid:
        eid_issues.append("sin_punto")
    if eid and len(eid) < 5:
        eid_issues.append("muy_corto")

    offering_is_new = bool(main_skill and main_skill not in KNOWN_OFFERINGS)

    # PTOs that need interactive confirmation: have specific dates (not just period refs)
    # and are NOT "sin ticket" (those already know they have no ticket)
    ptos_need_confirm = [
        p for p in parsed["ptos"]
        if p.get("start_date") and not p.get("no_ticket")
    ]

    return {
        "eid":               eid or "",
        "status":            status,
        "cl":                cl,
        "offering":          off or "",
        "sub_offering":      sub or "",
        "main_skill":        main_skill or "",
        "offering_is_new":   offering_is_new,
        "client":            parsed["client"],
        "roll_on":         parsed["roll_on"].isoformat() if parsed["roll_on"] else None,
        "roll_off":        roll_off.isoformat() if roll_off else None,
        "first_available": first_available.isoformat() if first_available else None,
        "days_to_avail":   dta,
        "comment_raw":     comment,
        "format":          fmt,
        "new_joiner":      parsed["new_joiner"],
        "licencia":        parsed["licencia"],
        "periods":         {k: v for k, v in periods.items()
                            if v["sah"] > 0 or v["chg"] > 0},
        "ptos":               parsed["ptos"],
        "ptos_need_confirm":  len(ptos_need_confirm),
        "unparsed_segments":  parsed["unparsed_segments"],
        "eid_issues":         eid_issues,
        "level_name":         level or "",
    }


# ─── CSV reading ─────────────────────────────────────────────────────────────

def read_csv(path):
    employees = []
    issues = {
        "eid_format":      [],  # {eid, issue}
        "pto_unparsed":    [],  # {eid, raw_text}
        "comment_format":  [],  # {eid, comment}
    }

    with open(path, encoding=ENCODING, newline="") as f:
        reader = csv.reader(f)
        header = next(reader)

    period_cols, period_order = detect_period_columns(header)

    with open(path, encoding=ENCODING, newline="") as f:
        reader = csv.reader(f)
        next(reader)  # skip header
        for row in reader:
            eid = clean(row[COL_EID]) if len(row) > COL_EID else None
            if not eid or "." not in eid:
                # Skip rows without a valid EID
                continue
            try:
                emp = parse_employee(row, period_cols)
            except Exception as exc:
                issues["comment_format"].append({"eid": eid, "error": str(exc)})
                continue

            employees.append(emp)

            for issue in emp["eid_issues"]:
                issues["eid_format"].append({"eid": eid, "issue": issue})
            for unp in emp["unparsed_segments"]:
                issues["pto_unparsed"].append({"eid": eid, "raw_text": unp})

    return employees, period_order, issues


# ─── period aggregates ───────────────────────────────────────────────────────

def compute_period_aggregates(employees, period_order):
    """
    Returns list of {period_name, headcount, sah, chg_hl, chg_sl, chg, chg_pct}
    aggregated across all employees for each period.
    """
    agg = {p: {"headcount": 0, "sah": 0.0, "chg_hl": 0.0, "chg_sl": 0.0, "chg": 0.0}
           for p in period_order}

    for emp in employees:
        for pname, pdata in emp["periods"].items():
            if pname not in agg:
                continue
            if pdata["sah"] > 0 or pdata["chg"] > 0:
                agg[pname]["headcount"] += 1
                agg[pname]["sah"]    += pdata["sah"]
                agg[pname]["chg_hl"] += pdata["chg_hl"]
                agg[pname]["chg_sl"] += pdata["chg_sl"]
                agg[pname]["chg"]    += pdata["chg"]

    result = []
    for p in period_order:
        a = agg[p]
        sah = a["sah"]
        chg_pct = round(a["chg"] / sah * 100, 1) if sah > 0 else 0.0
        result.append({
            "period_name": p,
            "headcount":   a["headcount"],
            "sah":         round(a["sah"], 1),
            "chg_hl":      round(a["chg_hl"], 1),
            "chg_sl":      round(a["chg_sl"], 1),
            "chg":         round(a["chg"], 1),
            "chg_pct":     chg_pct,
        })
    return result


# ─── summary stats ───────────────────────────────────────────────────────────

def compute_catalog_preview(employees):
    """
    Returns two lists for the HTML:
    - offerings: {name, count, is_new}   sorted by name
    - clients:   {name, count}            sorted by name (all need DB check at runtime)
    """
    from collections import Counter
    off_counter = Counter()
    client_counter = Counter()

    for emp in employees:
        ms = emp.get("main_skill", "")
        if ms:
            off_counter[ms] += 1
        c = emp.get("client")
        if c:
            client_counter[c] += 1

    offerings = sorted(
        [{"name": k, "count": v, "is_new": k not in KNOWN_OFFERINGS}
         for k, v in off_counter.items()],
        key=lambda x: x["name"],
    )
    clients = sorted(
        [{"name": k, "count": v} for k, v in client_counter.items()],
        key=lambda x: x["name"],
    )
    return offerings, clients


def compute_summary(employees, issues):
    total = len(employees)
    active = sum(1 for e in employees if e["status"] == "Active")
    out    = sum(1 for e in employees if e["status"] == "OUT")
    nj     = sum(1 for e in employees if e["new_joiner"])
    loa    = sum(1 for e in employees if e["licencia"])
    fmt_a  = sum(1 for e in employees if e["format"] == "A")
    fmt_b  = sum(1 for e in employees if e["format"] == "B")
    fmt_q  = sum(1 for e in employees if e["format"] == "?")

    total_periods = sum(len(e["periods"]) for e in employees)
    total_ptos    = sum(len(e["ptos"]) for e in employees)
    total_unp     = len(issues["pto_unparsed"])

    return {
        "total": total, "active": active, "out": out,
        "new_joiner": nj, "licencia": loa,
        "format_a": fmt_a, "format_b": fmt_b, "format_other": fmt_q,
        "total_periods": total_periods,
        "total_ptos": total_ptos,
        "unparsed_count": total_unp,
    }


# ─── HTML generation ─────────────────────────────────────────────────────────

HTML_TEMPLATE = """\
<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="utf-8">
<title>Migration Simulation — S&P Forecast</title>
<style>
:root {
  --accent: #6c47ff;
  --accent2: #00b69b;
  --danger: #ef4444;
  --warn: #f59e0b;
  --bg: #f8f9fa;
  --card: #ffffff;
  --border: #e5e7eb;
  --text: #1f2937;
  --muted: #6b7280;
  --row-hover: #f3f4f6;
  --status-active: #dcfce7;
  --status-out: #fee2e2;
  --status-nj: #fef9c3;
}
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: system-ui,-apple-system,sans-serif; background:var(--bg); color:var(--text); font-size:14px; }
.header { background: var(--accent); color:#fff; padding:20px 32px; }
.header h1 { font-size:22px; font-weight:700; }
.header .meta { margin-top:4px; font-size:12px; opacity:.85; }
.container { max-width:1600px; margin:0 auto; padding:24px 32px; }
.cards { display:grid; grid-template-columns:repeat(auto-fill,minmax(130px,1fr)); gap:12px; margin-bottom:24px; }
.card { background:var(--card); border:1px solid var(--border); border-radius:10px; padding:14px 16px; }
.card .value { font-size:26px; font-weight:700; }
.card .label { font-size:11px; color:var(--muted); margin-top:2px; text-transform:uppercase; letter-spacing:.04em; }
.card.accent .value { color:var(--accent); }
.card.green .value { color:var(--accent2); }
.card.red .value { color:var(--danger); }
.card.warn .value { color:var(--warn); }
.controls { display:flex; gap:10px; margin-bottom:16px; flex-wrap:wrap; align-items:center; }
.controls input, .controls select {
  padding:7px 12px; border:1px solid var(--border); border-radius:6px; font-size:13px; background:#fff;
}
.controls input { width:200px; }
h2 { font-size:16px; font-weight:600; margin-bottom:12px; }
.table-wrap { overflow-x:auto; background:var(--card); border:1px solid var(--border); border-radius:10px; }
table { width:100%; border-collapse:collapse; }
th { background:#f3f4f6; padding:10px 12px; text-align:left; font-size:12px; font-weight:600;
     text-transform:uppercase; letter-spacing:.04em; color:var(--muted); border-bottom:1px solid var(--border);
     position:sticky; top:0; white-space:nowrap; }
td { padding:9px 12px; border-bottom:1px solid #f0f0f0; vertical-align:top; }
tr.emp-row:hover td { background:var(--row-hover); }
tr.emp-row { cursor:pointer; }
tr.detail-row td { background:#fafafa; padding:0; }
tr.detail-row.hidden { display:none; }
.detail-inner { padding:16px 20px; display:grid; grid-template-columns:1fr 1fr; gap:20px; }
.detail-section h3 { font-size:12px; font-weight:600; text-transform:uppercase; color:var(--muted);
                      margin-bottom:8px; letter-spacing:.05em; }
.period-table { width:100%; border-collapse:collapse; font-size:12px; }
.period-table th { background:#f0f0f0; padding:4px 8px; text-align:right; }
.period-table td { padding:4px 8px; text-align:right; border-bottom:1px solid #f0f0f0; }
.period-table td:first-child, .period-table th:first-child { text-align:left; }
.badge { display:inline-block; padding:2px 7px; border-radius:4px; font-size:11px; font-weight:600; }
.badge-active { background:var(--status-active); color:#15803d; }
.badge-out    { background:var(--status-out);    color:#991b1b; }
.badge-nj     { background:var(--status-nj);     color:#92400e; }
.badge-loa    { background:#e0e7ff; color:#4338ca; }
.pto-list { list-style:none; font-size:12px; }
.pto-list li { padding:3px 0; border-bottom:1px solid #f0f0f0; display:flex; gap:8px; }
.pto-list .no-ticket { color:var(--warn); font-size:10px; }
.unparsed-list { list-style:none; font-size:12px; }
.unparsed-list li { color:var(--danger); padding:2px 4px; background:#fef2f2; border-radius:3px; margin-bottom:2px; }
.comment-raw { font-size:11px; font-family:monospace; color:var(--muted); word-break:break-all; margin-top:6px; }
.issues-section { margin-top:32px; }
.issues-section .issue-card { background:var(--card); border:1px solid var(--border); border-radius:10px;
                               padding:16px; margin-bottom:16px; }
.issues-section .issue-card h3 { font-size:14px; font-weight:600; margin-bottom:10px; }
.issue-row { font-size:12px; padding:4px 0; border-bottom:1px solid #f0f0f0; display:flex; gap:12px; }
.issue-eid { font-weight:600; min-width:160px; }
.issue-text { color:var(--muted); }
.section-header { display:flex; justify-content:space-between; align-items:center; margin-bottom:12px; }
.count-badge { background:var(--accent); color:#fff; padding:2px 8px; border-radius:10px; font-size:11px; }
.format-b { color:#7c3aed; font-size:11px; font-weight:600; }
.format-a { color:#0369a1; font-size:11px; font-weight:600; }
.expand-icon { float:right; font-size:16px; transition:transform .2s; }
.expanded .expand-icon { transform:rotate(90deg); }
</style>
</head>
<body>
<div class="header">
  <h1>Migration Simulation — S&amp;P Forecast</h1>
  <div class="meta" id="meta-bar"></div>
</div>
<div class="container">
  <div class="cards" id="summary-cards"></div>

  <div class="section-header">
    <h2>Empleados ({{ total }})</h2>
    <div class="controls">
      <input id="filter-eid" type="text" placeholder="Buscar EID o cliente...">
      <select id="filter-status">
        <option value="">Todos los status</option>
        <option value="Active">Active</option>
        <option value="OUT">OUT</option>
      </select>
      <select id="filter-format">
        <option value="">Todos los formatos</option>
        <option value="A">Formato A (semicolons)</option>
        <option value="B">Formato B (pipes)</option>
      </select>
    </div>
  </div>

  <div class="table-wrap">
    <table id="emp-table">
      <thead>
        <tr>
          <th></th>
          <th>EID</th>
          <th>Status</th>
          <th>CL</th>
          <th>Fmt</th>
          <th>Offering</th>
          <th>Sub-Off</th>
          <th>Cliente</th>
          <th>Roll-On</th>
          <th>Roll-Off</th>
          <th>First Avail</th>
          <th>Days Avail</th>
          <th>Main Skill</th>
          <th>PTOs</th>
          <th>Confirm?</th>
          <th>⚠</th>
        </tr>
      </thead>
      <tbody id="emp-tbody"></tbody>
    </table>
  </div>

  <div id="catalog-section" style="margin-top:32px"></div>

  <div class="period-summary-section" id="period-summary-section" style="margin-top:32px">
    <div style="display:flex;align-items:center;gap:12px;margin-bottom:8px">
      <h2 style="margin:0">Resumen por per&#237;odo — CHG equipo vs Target</h2>
      <label style="font-size:13px;color:var(--muted);display:flex;align-items:center;gap:6px">
        Target AR:
        <input id="target-input" type="number" min="0" max="150" step="0.5"
               style="width:70px;padding:4px 8px;border:1px solid var(--border);border-radius:6px;font-size:13px;font-weight:600">
        %
      </label>
    </div>
    <div id="period-table-container"></div>
  </div>

  <div class="issues-section" id="issues-section"></div>
</div>

<script>
const DATA = REPLACE_DATA_JSON;
const PERIODS = REPLACE_PERIODS_JSON;
const TODAY_STR = "REPLACE_TODAY";
const GENERATED_AT = "REPLACE_GENERATED_AT";

// ─── render summary ─────────────────────────────────────────────────────────
function renderMeta() {
  const s = DATA.summary;
  document.getElementById('meta-bar').innerHTML =
    `Generado: ${GENERATED_AT} &nbsp;|&nbsp; Fuente: ${DATA.source_file}`;
}

function renderCards() {
  const s = DATA.summary;
  const cards = [
    {label:'Total empleados', value:s.total, cls:'accent'},
    {label:'Active', value:s.active, cls:'green'},
    {label:'OUT', value:s.out, cls:'red'},
    {label:'New Joiner', value:s.new_joiner, cls:'warn'},
    {label:'Licencia/LOA', value:s.licencia, cls:''},
    {label:'Periodos con datos', value:s.total_periods, cls:''},
    {label:'PTOs parseados', value:s.total_ptos, cls:'green'},
    {label:'PTOs no parseados', value:s.unparsed_count, cls:'red'},
    {label:'Formato A', value:s.format_a, cls:''},
    {label:'Formato B', value:s.format_b, cls:''},
  ];
  document.getElementById('summary-cards').innerHTML = cards.map(c =>
    `<div class="card ${c.cls}"><div class="value">${c.value}</div><div class="label">${c.label}</div></div>`
  ).join('');
}

// ─── render main table ──────────────────────────────────────────────────────
function statusBadge(emp) {
  if (emp.status === 'OUT') return '<span class="badge badge-out">🔴 OUT</span>';
  if (emp.new_joiner)        return '<span class="badge badge-nj">🟡 NJ</span>';
  if (emp.licencia)          return '<span class="badge badge-loa">📋 LOA</span>';
  return '<span class="badge badge-active">🟢 Active</span>';
}

function fmtDate(iso) {
  if (!iso) return '<span style="color:#ccc">—</span>';
  const [y,m,d] = iso.split('-');
  return `${d}/${m}/${y.slice(2)}`;
}

function renderTable(emps) {
  const tbody = document.getElementById('emp-tbody');
  tbody.innerHTML = '';

  emps.forEach((emp, idx) => {
    const warnCount = emp.unparsed_segments.length + emp.eid_issues.length;
    const tr = document.createElement('tr');
    tr.className = 'emp-row';
    tr.dataset.idx = idx;
    tr.innerHTML = `
      <td><span class="expand-icon">▶</span></td>
      <td><strong>${emp.eid}</strong></td>
      <td>${statusBadge(emp)}</td>
      <td>${emp.cl ?? '—'}</td>
      <td class="format-${emp.format.toLowerCase()}">${emp.format}</td>
      <td>${emp.offering}</td>
      <td>${emp.sub_offering}</td>
      <td>${emp.client ?? '<span style="color:#ccc">—</span>'}</td>
      <td>${fmtDate(emp.roll_on)}</td>
      <td>${fmtDate(emp.roll_off)}</td>
      <td>${fmtDate(emp.first_available)}</td>
      <td>${emp.days_to_avail !== null ? emp.days_to_avail : '—'}</td>
      <td>${emp.main_skill ? (emp.offering_is_new ? '<span style="background:#fee2e2;color:#dc2626;padding:2px 6px;border-radius:4px;font-size:11px;font-weight:700">★ '+escHtml(emp.main_skill)+'</span>' : escHtml(emp.main_skill)) : '<span style="color:#ccc">—</span>'}</td>
      <td>${emp.ptos.length > 0 ? '<span style="color:#6c47ff">'+emp.ptos.length+' PTO</span>' : '—'}</td>
      <td>${emp.ptos_need_confirm > 0 ? '<span style="background:#fef3c7;color:#92400e;padding:2px 6px;border-radius:4px;font-size:11px;font-weight:600">'+emp.ptos_need_confirm+' confirmar</span>' : '—'}</td>
      <td>${warnCount > 0 ? '<span style="color:#ef4444">⚠ '+warnCount+'</span>' : ''}</td>
    `;

    const detailTr = document.createElement('tr');
    detailTr.className = 'detail-row hidden';
    const td = document.createElement('td');
    td.colSpan = 16;
    td.innerHTML = renderDetail(emp);
    detailTr.appendChild(td);

    tr.addEventListener('click', () => {
      const open = !detailTr.classList.contains('hidden');
      detailTr.classList.toggle('hidden', open);
      tr.classList.toggle('expanded', !open);
    });

    tbody.appendChild(tr);
    tbody.appendChild(detailTr);
  });
}

function renderDetail(emp) {
  // Periods table
  const periodEntries = Object.entries(emp.periods);
  let periodsHtml = '<p style="color:#aaa;font-size:12px">Sin datos de período</p>';
  if (periodEntries.length > 0) {
    periodsHtml = `<table class="period-table">
      <thead><tr><th>Período</th><th>CHG HL</th><th>CHG SL</th><th>CHG Neto</th><th>SAH</th><th>CHG%</th></tr></thead>
      <tbody>
        ${periodEntries.map(([pname, p]) =>
          `<tr><td>${pname}</td><td>${p.chg_hl}</td><td>${p.chg_sl}</td><td><strong>${p.chg}</strong></td><td>${p.sah}</td><td>${p.chg_pct}%</td></tr>`
        ).join('')}
      </tbody>
    </table>`;
  }

  // PTOs list
  let ptosHtml = '<p style="color:#aaa;font-size:12px">Sin PTOs</p>';
  if (emp.ptos.length > 0) {
    ptosHtml = `<ul class="pto-list">
      ${emp.ptos.map(p => {
        const pname = p.period_name || '';
        const range = p.start_date
          ? (p.start_date === p.end_date
              ? fmtDate(p.start_date)
              : fmtDate(p.start_date) + ' → ' + fmtDate(p.end_date))
          : pname || '?';
        const ticket = p.no_ticket ? '<span class="no-ticket">[sin ticket]</span>' : '';
        return `<li><span>${range}</span><span>${p.n_days}d</span>${ticket}</li>`;
      }).join('')}
    </ul>`;
  }

  // Unparsed segments
  let unparsedHtml = '';
  if (emp.unparsed_segments.length > 0) {
    unparsedHtml = `
      <div class="detail-section">
        <h3>Segmentos no parseados</h3>
        <ul class="unparsed-list">
          ${emp.unparsed_segments.map(s => `<li>${escHtml(s)}</li>`).join('')}
        </ul>
      </div>`;
  }

  return `
    <div class="detail-inner">
      <div>
        <div class="detail-section">
          <h3>Períodos (${periodEntries.length})</h3>
          ${periodsHtml}
        </div>
        <div class="detail-section" style="margin-top:16px">
          <h3>PTOs (${emp.ptos.length})</h3>
          ${ptosHtml}
        </div>
      </div>
      <div>
        <div class="detail-section">
          <h3>Comentarios raw</h3>
          <div class="comment-raw">${escHtml(emp.comment_raw)}</div>
          <div style="margin-top:8px;font-size:11px;color:var(--muted)">Formato: ${emp.format} &nbsp;|&nbsp; NJ: ${emp.new_joiner} &nbsp;|&nbsp; Licencia: ${emp.licencia}</div>
        </div>
        ${unparsedHtml}
      </div>
    </div>`;
}

// ─── period summary (targets view) ──────────────────────────────────────────
function renderPeriodTable(target) {
  if (!DATA.period_agg || DATA.period_agg.length === 0) return '';

  const rows = DATA.period_agg.map(p => {
    const actual = p.chg_pct;
    const delta  = actual - target;
    const aboveTarget = actual >= target;
    const pctColor   = aboveTarget ? '#15803d' : actual >= target * 0.9 ? '#d97706' : '#dc2626';
    const deltaStr   = (delta >= 0 ? '+' : '') + delta.toFixed(1) + ' pp';
    const deltaColor = aboveTarget ? '#15803d' : '#dc2626';
    const barWidth   = Math.min(Math.max(actual, 0), 100);
    const targetPos  = Math.min(target, 100);
    const bar = `
      <div style="position:relative;display:inline-block;width:100px;height:10px;background:#e5e7eb;border-radius:3px;vertical-align:middle;margin-right:8px">
        <div style="position:absolute;left:0;top:0;height:10px;width:${barWidth}px;background:${pctColor};border-radius:3px"></div>
        <div style="position:absolute;left:${targetPos}px;top:-2px;height:14px;width:2px;background:#374151" title="Target ${target}%"></div>
      </div>`;
    return `<tr>
      <td style="font-weight:600">${p.period_name}</td>
      <td style="text-align:right">${p.headcount}</td>
      <td style="text-align:right">${p.sah}</td>
      <td style="text-align:right">${p.chg_hl}</td>
      <td style="text-align:right">${p.chg_sl}</td>
      <td style="text-align:right"><strong>${p.chg}</strong></td>
      <td style="text-align:right">${bar}<span style="color:${pctColor};font-weight:600">${actual}%</span></td>
      <td style="text-align:right;color:${deltaColor};font-weight:600">${deltaStr}</td>
    </tr>`;
  }).join('');

  return `
    <p style="font-size:12px;color:var(--muted);margin-bottom:12px">
      CHG Neto = CHG HL + CHG SL (sin PPA/cascadeadas)
      &nbsp;&#124;&nbsp; La l&#237;nea vertical en la barra marca el target
    </p>
    <div class="table-wrap">
      <table>
        <thead>
          <tr>
            <th>Per&#237;odo</th>
            <th style="text-align:right">Headcount</th>
            <th style="text-align:right">SAH total</th>
            <th style="text-align:right">CHG HL</th>
            <th style="text-align:right">CHG SL</th>
            <th style="text-align:right">CHG Neto</th>
            <th style="text-align:right">CHG% (&#124; = ${target}%)</th>
            <th style="text-align:right">Delta</th>
          </tr>
        </thead>
        <tbody>${rows}</tbody>
      </table>
    </div>`;
}

function renderPeriodSummary() {
  const inp = document.getElementById('target-input');
  if (!inp) return;
  inp.value = DATA.target_pct;
  document.getElementById('period-table-container').innerHTML = renderPeriodTable(DATA.target_pct);

  inp.addEventListener('input', () => {
    const t = parseFloat(inp.value) || DATA.target_pct;
    document.getElementById('period-table-container').innerHTML = renderPeriodTable(t);
  });
}

// ─── catalog section ────────────────────────────────────────────────────────
function renderCatalog() {
  const el = document.getElementById('catalog-section');
  if (!el) return;

  const offs = DATA.catalog_offerings || [];
  const clients = DATA.catalog_clients || [];

  const newOffs = offs.filter(o => o.is_new);

  const offRows = offs.map(o => {
    const badge = o.is_new
      ? '<span style="background:#fee2e2;color:#dc2626;padding:2px 8px;border-radius:4px;font-size:11px;font-weight:700;margin-left:8px">NUEVO — se creará en la DB</span>'
      : '<span style="background:#d1fae5;color:#065f46;padding:2px 8px;border-radius:4px;font-size:11px;font-weight:600;margin-left:8px">ya existe</span>';
    return `<tr><td><strong>${escHtml(o.name)}</strong>${badge}</td><td style="text-align:right">${o.count}</td></tr>`;
  }).join('');

  const clientRows = clients.map(c =>
    `<tr><td>${escHtml(c.name)}</td><td style="text-align:right">${c.count}</td></tr>`
  ).join('');

  el.innerHTML = `
    <h2 style="margin-bottom:16px">Catálogo — preview para la DB</h2>
    ${newOffs.length > 0 ? `<div style="background:#fff7ed;border:1px solid #fed7aa;border-radius:8px;padding:12px 16px;margin-bottom:16px;color:#92400e;font-size:13px">
      <strong>⚠ ${newOffs.length} offering(s) nuevo(s)</strong>: ${newOffs.map(o=>'<strong>'+escHtml(o.name)+'</strong>').join(', ')} — el script de migración los creará en la DB.
    </div>` : ''}
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:24px">
      <div>
        <h3 style="margin-bottom:8px;font-size:14px">Offerings / Main Skill (${offs.length})</h3>
        <table style="width:100%;border-collapse:collapse;font-size:13px">
          <thead><tr style="border-bottom:1px solid var(--border)">
            <th style="text-align:left;padding:6px 4px">Nombre</th>
            <th style="text-align:right;padding:6px 4px">Empleados</th>
          </tr></thead>
          <tbody>${offRows}</tbody>
        </table>
      </div>
      <div>
        <h3 style="margin-bottom:8px;font-size:14px">Clientes en CSV (${clients.length})</h3>
        <p style="font-size:12px;color:var(--muted);margin-bottom:8px">El script verificará cada nombre contra <code>client_catalog</code> y creará los que no existan.</p>
        <table style="width:100%;border-collapse:collapse;font-size:13px">
          <thead><tr style="border-bottom:1px solid var(--border)">
            <th style="text-align:left;padding:6px 4px">Nombre</th>
            <th style="text-align:right;padding:6px 4px">Empleados</th>
          </tr></thead>
          <tbody>${clientRows}</tbody>
        </table>
      </div>
    </div>
  `;
}

// ─── issues section ─────────────────────────────────────────────────────────
function renderIssues() {
  const container = document.getElementById('issues-section');
  const sections = [];

  if (DATA.issues.eid_format.length > 0) {
    sections.push({
      title: 'EIDs con formato inusual',
      color: '#ef4444',
      items: DATA.issues.eid_format.map(i =>
        `<div class="issue-row"><span class="issue-eid">${i.eid}</span><span class="issue-text">${i.issue}</span></div>`
      )
    });
  }

  if (DATA.issues.pto_unparsed.length > 0) {
    sections.push({
      title: 'PTOs no parseados',
      color: '#f59e0b',
      items: DATA.issues.pto_unparsed.map(i =>
        `<div class="issue-row"><span class="issue-eid">${i.eid}</span><span class="issue-text">${escHtml(i.raw_text)}</span></div>`
      )
    });
  }

  container.innerHTML = sections.length === 0
    ? ''
    : `<h2 style="margin-bottom:16px">Problemas encontrados</h2>` +
      sections.map(s => `
        <div class="issue-card">
          <h3 style="color:${s.color}">${s.title}
            <span class="count-badge" style="background:${s.color}">${s.items.length}</span>
          </h3>
          ${s.items.join('')}
        </div>
      `).join('');
}

// ─── filtering ──────────────────────────────────────────────────────────────
function applyFilters() {
  const eidQ = document.getElementById('filter-eid').value.toLowerCase();
  const statusQ = document.getElementById('filter-status').value;
  const fmtQ = document.getElementById('filter-format').value;

  const filtered = DATA.employees.filter(e => {
    if (eidQ && !e.eid.toLowerCase().includes(eidQ) && !(e.client||'').toLowerCase().includes(eidQ)) return false;
    if (statusQ && e.status !== statusQ) return false;
    if (fmtQ && e.format !== fmtQ) return false;
    return true;
  });
  renderTable(filtered);
}

function escHtml(s) {
  return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

// ─── init ────────────────────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', () => {
  renderMeta();
  renderCards();
  renderTable(DATA.employees);
  renderCatalog();
  renderPeriodSummary();
  renderIssues();

  document.getElementById('filter-eid').addEventListener('input', applyFilters);
  document.getElementById('filter-status').addEventListener('change', applyFilters);
  document.getElementById('filter-format').addEventListener('change', applyFilters);
});
</script>
</body>
</html>
"""


def generate_html(employees, period_order, issues, source_file, target_pct=87.0):
    summary = compute_summary(employees, issues)
    period_agg = compute_period_aggregates(employees, period_order)
    catalog_offerings, catalog_clients = compute_catalog_preview(employees)
    data = {
        "source_file":        os.path.basename(source_file),
        "summary":            summary,
        "employees":          employees,
        "periods":            period_order,
        "period_agg":         period_agg,
        "target_pct":         target_pct,
        "catalog_offerings":  catalog_offerings,
        "catalog_clients":    catalog_clients,
        "issues": {
            "eid_format":   issues["eid_format"],
            "pto_unparsed": issues["pto_unparsed"],
        },
    }

    generated_at = datetime.now().strftime("%d/%m/%Y %H:%M")

    html = HTML_TEMPLATE
    html = html.replace("{{ total }}", str(len(employees)))
    html = html.replace("REPLACE_DATA_JSON", json.dumps(data, ensure_ascii=False, default=str))
    html = html.replace("REPLACE_PERIODS_JSON", json.dumps(period_order, ensure_ascii=False))
    html = html.replace("REPLACE_TODAY", TODAY.isoformat())
    html = html.replace("REPLACE_GENERATED_AT", generated_at)
    return html


# ─── main ────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(
        description="Genera migration_simulation.html desde el CSV de control S&P"
    )
    ap.add_argument("csv_path", help="Ruta al CSV 01 - FORECAST CONTROL S&P(S&P).csv")
    ap.add_argument("-o", "--output", default="migration_simulation.html",
                    help="Archivo HTML de salida (default: migration_simulation.html)")
    ap.add_argument("--target-pct", type=float, default=87.0,
                    help="Target CHG%% para AR (default: 87, igual que DEFAULT_TARGET_PCT en state_service.py)")
    args = ap.parse_args()

    csv_path = args.csv_path
    if not os.path.exists(csv_path):
        print(f"Error: no se encuentra el archivo '{csv_path}'", file=sys.stderr)
        sys.exit(1)

    print(f"Leyendo CSV: {csv_path}")
    employees, period_order, issues = read_csv(csv_path)

    print(f"Empleados encontrados: {len(employees)}")
    print(f"Periodos detectados:   {len(period_order)}")
    print(f"Períodos: {period_order}")
    print()

    active = sum(1 for e in employees if e["status"] == "Active")
    out    = sum(1 for e in employees if e["status"] == "OUT")
    nj     = sum(1 for e in employees if e["new_joiner"])
    loa    = sum(1 for e in employees if e["licencia"])
    fmt_a  = sum(1 for e in employees if e["format"] == "A")
    fmt_b  = sum(1 for e in employees if e["format"] == "B")

    print(f"  Active:          {active}")
    print(f"  OUT:             {out}")
    print(f"  New Joiner:      {nj}")
    print(f"  Licencia/LOA:    {loa}")
    print(f"  Formato A (;):   {fmt_a}")
    print(f"  Formato B (|):   {fmt_b}")
    print()

    total_ptos = sum(len(e["ptos"]) for e in employees)
    total_unp  = len(issues["pto_unparsed"])
    print(f"PTOs parseados:    {total_ptos}")
    print(f"PTOs no parseados: {total_unp}")
    if issues["pto_unparsed"]:
        for item in issues["pto_unparsed"][:10]:
            print(f"  [{item['eid']}] {item['raw_text']!r}")
        if len(issues["pto_unparsed"]) > 10:
            print(f"  ... y {len(issues['pto_unparsed']) - 10} más")
    print()

    html = generate_html(employees, period_order, issues, csv_path, args.target_pct)

    out_path = args.output
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)

    print(f"HTML generado: {out_path}")
    print(f"  -> Abrir en el browser para revisar antes de correr migrate_sp_control.py")


if __name__ == "__main__":
    main()
