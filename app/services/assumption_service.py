from datetime import date as _date
from loguru import logger


def pct_for_period(num: int, p_num: int) -> float:
    """Returns chargeability % for assumption `num` in period `p_num` (1-indexed)."""
    if num == 4:
        return 90.0
    if num in (1, 3):
        return 0.0 if p_num <= 2 else 50.0
    # num == 2
    return {1: 0.0, 2: 75.0}.get(p_num, 100.0)


async def get_assumption_num(conn, client_name: str | None, is_nj: bool, eid: str | None) -> int:
    """Determines which assumption (1-4) applies, in priority order."""
    if (client_name or "") == "ISG PE Assessment":
        return 4
    if is_nj:
        return 3
    if eid:
        row = await conn.fetchrow("SELECT ringfenced FROM employees WHERE eid=$1", eid)
        if row and row["ringfenced"]:
            return 2
    return 1


async def upsert_projection_blocks(
    conn,
    eid: str,
    ref_date: _date,
    num: int,
    effectivization_date: str | None,
    request_id: str = "-",
) -> int:
    """
    Inserts (or replaces) the 6 assumption blocks post ref_date for `eid`.
    Returns the number of blocks inserted.
    """
    periods = await conn.fetch(
        "SELECT period_name, start_date, end_date FROM periods WHERE end_date > $1 ORDER BY start_date LIMIT 6",
        ref_date,
    )
    if not periods:
        return 0

    for i, period in enumerate(periods):
        p_num = i + 1
        pct = pct_for_period(num, p_num)
        period_name = period["period_name"]
        end_date = period["end_date"]
        eff_date = effectivization_date or end_date.isoformat()

        await conn.execute(
            "DELETE FROM chargeability_blocks WHERE eid=$1 AND period_name=$2 AND scenario_type='assumption'",
            eid, period_name,
        )
        await conn.execute(
            """
            INSERT INTO chargeability_blocks
                (eid, period_name, chargeability_pct, scenario_type,
                 start_date, end_date, effectivization_date, created_by)
            VALUES ($1, $2, $3, 'assumption', $4, $5, $6::text::date, 'system')
            """,
            eid, period_name, pct,
            period["start_date"], end_date, eff_date,
        )

    logger.bind(request_id=request_id).info(
        "Assumption projection blocks upserted",
        eid=eid, assumption=num, periods=len(periods),
    )
    return len(periods)
