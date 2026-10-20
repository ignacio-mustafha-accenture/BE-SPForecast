"""
migrate_ppa_ticket_link.py
--------------------------
Agrega tickets.ppa_log_id (FK a ppa_log.id) y hace backfill de los tickets PPA existentes.

Antes el ticket PPA no guardaba el id de su ppa_log y se lo inferia por
(eid, from_period, to_period) ORDER BY created_at DESC, asi que dos PPAs del mismo
empleado con los mismos periodos se pisaban entre si.

Backfill:
  1. Por "(log:N)" en tickets.detail, si ese ppa_log existe.
  2. Para el resto: se agrupa por (eid, from_period, to_period, created_by); si el grupo
     tiene la misma cantidad de tickets que de ppa_log sin vincular, se emparejan en orden
     de id (ticket y log se crean en la misma transaccion) y solo si la fecha del ticket
     coincide con la de created_at del log. Cada ppa_log se usa una sola vez.
  3. Los que no se pueden resolver se listan para revision manual (no se adivina).

Uso:
    python scripts/migrate_ppa_ticket_link.py           # dry-run
    python scripts/migrate_ppa_ticket_link.py --apply   # ejecuta
"""
import argparse
import asyncio
import re
import sys

sys.path.insert(0, ".")

import asyncpg

from app.config import settings

LOG_RE = re.compile(r"\(log:(\d+)\)")


async def main(apply: bool):
    conn = await asyncpg.connect(
        host=settings.DB_HOST,
        port=settings.DB_PORT,
        user=settings.DB_USER,
        password=settings.DB_PASSWORD,
        database=settings.DB_NAME,
        ssl="require",
    )
    try:
        has_col = await conn.fetchval(
            """
            SELECT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name='tickets' AND column_name='ppa_log_id'
            )
            """
        )
        print(f"Columna tickets.ppa_log_id existe: {has_col}")

        tickets = await conn.fetch(
            """
            SELECT id, eid, detail, from_period, to_period, created_by, date
            FROM tickets WHERE type='ppa' ORDER BY id
            """
            if not has_col
            else """
            SELECT id, eid, detail, from_period, to_period, created_by, date
            FROM tickets WHERE type='ppa' AND ppa_log_id IS NULL ORDER BY id
            """
        )
        logs = await conn.fetch(
            "SELECT id, eid, from_period, to_period, created_by, created_at FROM ppa_log"
        )
        log_by_id = {l["id"]: l for l in logs}

        # ppa_logs ya vinculados (re-ejecucion) no se reutilizan
        used: set[int] = set()
        if has_col:
            used.update(r["ppa_log_id"] for r in await conn.fetch(
                "SELECT ppa_log_id FROM tickets WHERE ppa_log_id IS NOT NULL"
            ))

        assignments: dict[int, int] = {}

        # 1) (log:N) en detail
        rest = []
        for t in tickets:
            m = LOG_RE.search(t["detail"] or "")
            lid = int(m.group(1)) if m else None
            if lid is not None and lid in log_by_id and lid not in used:
                assignments[t["id"]] = lid
                used.add(lid)
            else:
                rest.append(t)
        print(f"Resueltos por (log:N): {len(assignments)}")

        # 2) por grupo, mismo cardinal y mismo orden de id
        def key(r):
            return (r["eid"], r["from_period"], r["to_period"], r["created_by"])

        groups: dict[tuple, list] = {}
        for t in rest:
            groups.setdefault(key(t), []).append(t)
        by_match = 0
        for k, ts in groups.items():
            ls = sorted((l for l in logs if key(l) == k and l["id"] not in used), key=lambda l: l["id"])
            ts = sorted(ts, key=lambda t: t["id"])
            if len(ls) != len(ts):
                continue
            if any(l["created_at"].date() != t["date"] for t, l in zip(ts, ls)):
                continue
            for t, l in zip(ts, ls):
                assignments[t["id"]] = l["id"]
                used.add(l["id"])
                by_match += 1
        print(f"Resueltos por grupo/orden de id: {by_match}")

        unresolved = [t for t in rest if t["id"] not in assignments]
        print(f"Sin resolver (revision manual): {len(unresolved)}")
        for t in unresolved:
            print(f"  ticket {t['id']} eid={t['eid']} {t['from_period']}->{t['to_period']} "
                  f"by={t['created_by']} date={t['date']}")

        if not apply:
            print("\nDry-run: no se escribio nada. Usar --apply para ejecutar.")
            return

        async with conn.transaction():
            await conn.execute(
                "ALTER TABLE tickets ADD COLUMN IF NOT EXISTS ppa_log_id integer REFERENCES ppa_log(id)"
            )
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_tickets_ppa_log_id ON tickets(ppa_log_id)"
            )
            await conn.executemany(
                "UPDATE tickets SET ppa_log_id=$2 WHERE id=$1",
                list(assignments.items()),
            )
        remaining = await conn.fetchval(
            "SELECT COUNT(*) FROM tickets WHERE type='ppa' AND ppa_log_id IS NULL"
        )
        print(f"\nAplicado. Tickets ppa con ppa_log_id NULL: {remaining}")
    finally:
        await conn.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    asyncio.run(main(args.apply))
