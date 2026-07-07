"""One-time data import from employees_dump_utf8.sql into Supabase."""
import asyncio
import os
import sys
from datetime import date, datetime
from decimal import Decimal

import asyncpg
from dotenv import load_dotenv

load_dotenv()

DUMP_PATH = r"C:\Users\ignacio.mustafha\Downloads\employees_dump_utf8.sql"


def parse_copy_block(text: str, start_marker: str) -> tuple[list[str], list[list[str]]]:
    """Extract column names and rows from a COPY...FROM stdin block."""
    idx = text.find(start_marker)
    if idx == -1:
        return [], []

    header_line = text[idx: text.index("\n", idx)]
    # Extract column names from "COPY public.table (col1, col2, ...) FROM stdin;"
    cols_start = header_line.index("(") + 1
    cols_end = header_line.index(")")
    columns = [c.strip() for c in header_line[cols_start:cols_end].split(",")]

    data_start = text.index("\n", idx) + 1
    data_end = text.index("\n\\.", data_start)
    block = text[data_start:data_end]

    rows = []
    for line in block.splitlines():
        if not line.strip():
            continue
        rows.append(line.split("\t"))

    return columns, rows


def coerce(value: str, col: str):
    """Convert tab-separated string value to Python type."""
    if value == "\\N":
        return None
    # boolean columns
    if col in ("new_joiner", "active", "is_forecast_manager"):
        return value == "t"
    # numeric columns
    if col in ("cl", "fte", "chg", "sah", "chg_pct", "days_available",
               "chargeability_pct", "next_pto_hours"):
        try:
            return float(value)
        except ValueError:
            return None
    # integer id columns
    if col == "id":
        return int(value)
    # date columns
    if col in ("hire_date", "termination_date", "roll_on", "roll_off",
               "first_available", "next_pto", "next_pto_end"):
        try:
            return date.fromisoformat(value)
        except ValueError:
            return None
    # timestamp columns
    if col == "updated_at":
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None
    return value


async def main():
    conn = await asyncpg.connect(
        host=os.getenv("DB_HOST"),
        port=int(os.getenv("DB_PORT", 5432)),
        user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASSWORD"),
        database=os.getenv("DB_NAME"),
        ssl="require",
    )

    with open(DUMP_PATH, encoding="utf-8") as f:
        text = f.read()

    # ── 1. EMPLOYEES ──────────────────────────────────────────────────────────
    emp_cols, emp_rows = parse_copy_block(text, "COPY public.employees (")
    print(f"Parsed {len(emp_rows)} employees")

    people_lead_idx = emp_cols.index("people_lead")
    eid_idx = emp_cols.index("eid")

    # Insert employees with people_lead=NULL to avoid self-referential FK issue
    cols_no_pl = [c for c in emp_cols if c != "people_lead"]
    placeholders = ", ".join(f"${i+1}" for i in range(len(cols_no_pl)))
    insert_emp = (
        f"INSERT INTO employees ({', '.join(cols_no_pl)}) VALUES ({placeholders}) "
        f"ON CONFLICT (id) DO NOTHING"
    )

    async with conn.transaction():
        for row in emp_rows:
            record = [coerce(row[emp_cols.index(c)], c) for c in cols_no_pl]
            await conn.execute(insert_emp, *record)

    print("  >> employees inserted (people_lead deferred)")

    # Now UPDATE people_lead
    async with conn.transaction():
        for row in emp_rows:
            pl = coerce(row[people_lead_idx], "people_lead")
            eid = coerce(row[eid_idx], "eid")
            if pl is not None:
                await conn.execute(
                    "UPDATE employees SET people_lead=$1 WHERE eid=$2", pl, eid
                )

    print("  >> people_lead updated")

    # ── 2. FORECAST_PERIODS ───────────────────────────────────────────────────
    fp_cols, fp_rows = parse_copy_block(text, "COPY public.forecast_periods (")
    print(f"Parsed {len(fp_rows)} forecast_periods rows")

    fp_ph = ", ".join(f"${i+1}" for i in range(len(fp_cols)))
    insert_fp = (
        f"INSERT INTO forecast_periods ({', '.join(fp_cols)}) VALUES ({fp_ph}) "
        f"ON CONFLICT (id) DO NOTHING"
    )

    async with conn.transaction():
        for row in fp_rows:
            record = [coerce(row[i], fp_cols[i]) for i in range(len(fp_cols))]
            await conn.execute(insert_fp, *record)

    print("  >> forecast_periods inserted")

    # ── 3. FORECAST_UPDATE ────────────────────────────────────────────────────
    fu_cols, fu_rows = parse_copy_block(text, "COPY public.forecast_update (")
    print(f"Parsed {len(fu_rows)} forecast_update rows")

    fu_ph = ", ".join(f"${i+1}" for i in range(len(fu_cols)))
    insert_fu = (
        f"INSERT INTO forecast_update ({', '.join(fu_cols)}) VALUES ({fu_ph}) "
        f"ON CONFLICT (id) DO NOTHING"
    )

    async with conn.transaction():
        for row in fu_rows:
            record = [coerce(row[i], fu_cols[i]) for i in range(len(fu_cols))]
            await conn.execute(insert_fu, *record)

    print("  >> forecast_update inserted")

    # ── 4. RESET SEQUENCES ────────────────────────────────────────────────────
    async with conn.transaction():
        for table, seq in [
            ("employees", "employees_id_seq"),
            ("forecast_periods", "forecast_periods_id_seq"),
            ("forecast_update", "forecast_update_id_seq"),
        ]:
            try:
                await conn.execute(
                    f"SELECT setval('{seq}', (SELECT MAX(id) FROM {table}))"
                )
            except Exception as e:
                print(f"  WARNING: Could not reset {seq}: {e}")

    print("  >> sequences reset")

    await conn.close()
    print("\nDone.")


if __name__ == "__main__":
    asyncio.run(main())
