import time
from loguru import logger
import app.db as db
from app.errors import AppError, ForecastException
from app.services import employee_service, state_service

EXCEL_TARGET_PCT = {"AR": 87.2, "MX": 53.0, "CR": 45.0}
DEFAULT_TARGET_PCT = 87.0

COUNTRY_ROWS = [
    ("AR", "Total S&P Arg"),
    ("MX", "Total S&P Mexico"),
    ("CR", "Total S&P Costa Rica"),
]
AR_OFFERING_ROWS = ["SO", "PR", "Tools", "S4", "Ariba", "Oracle"]

# employees.country / location no esta normalizado: convive el nombre completo con el codigo
COUNTRY_CODE_SQL = """
    CASE
        WHEN LOWER(COALESCE(e.country, e.location)) IN ('ar', 'arg', 'argentina')    THEN 'AR'
        WHEN LOWER(COALESCE(e.country, e.location)) IN ('mx', 'mexico', 'méxico')    THEN 'MX'
        WHEN LOWER(COALESCE(e.country, e.location)) IN ('cr', 'costa rica')          THEN 'CR'
        ELSE 'OTHER'
    END
"""

TARGET_COUNTRY_KEYS = {
    "ar": "AR", "arg": "AR", "argentina": "AR",
    "mx": "MX", "mexico": "MX", "méxico": "MX",
    "cr": "CR", "costa rica": "CR",
}

METRICS = ("chg_hl", "chg_sl", "chg_neto", "chg", "sah")


def _empty_metrics(n_periods: int) -> dict:
    return {m: [0.0] * n_periods for m in METRICS}


async def _resolve_targets(conn, log) -> dict:
    """Targets del Excel, avisando si la tabla targets de la base quedo desalineada."""
    try:
        rows = await conn.fetch(
            """SELECT country, target_pct FROM targets
               WHERE fiscal_year = 'FY26' AND (valid_to IS NULL OR valid_to >= CURRENT_DATE)"""
        )
        for r in rows:
            code = TARGET_COUNTRY_KEYS.get((r["country"] or "").strip().lower())
            if not code:
                continue
            db_pct = float(r["target_pct"] or 0)
            if abs(db_pct - EXCEL_TARGET_PCT.get(code, DEFAULT_TARGET_PCT)) > 0.05:
                log.warning(
                    "Target de la base desalineado con el Excel",
                    country=code, db_pct=db_pct, excel_pct=EXCEL_TARGET_PCT.get(code),
                )
    except Exception:
        log.exception("No se pudo comparar los targets contra la base")
    return dict(EXCEL_TARGET_PCT)


async def get_totals(
    window_offset: int = 0,
    country: str | None = None,
    cl: str | None = None,
    q: str | None = None,
    status: str | None = None,
    offering: str | None = None,
    te_approver: str | None = None,
    chg_bucket: str | None = None,
    request_id: str = "-",
) -> dict:
    """Totales agregados por pais y por offering sobre TODO el set que matchea los filtros.

    A diferencia del listado, ignora el paginado: la agregacion se hace con SUM en SQL,
    nunca trayendo los empleados a Python para sumarlos aca.

    Las horas por empleado y periodo salen de forecast_periods, el mismo origen que usa
    state_service, para que los totales coincidan exactamente con las filas de la tabla.
    """
    start = time.monotonic()
    log = logger.bind(action="employees:totals", request_id=request_id)

    conditions, filter_params = employee_service.build_employee_filters(
        country, cl, q, status,
        offering=offering, te_approver=te_approver, chg_bucket=chg_bucket,
    )
    where = " AND ".join(conditions)

    filtered_cte = f"""
        WITH latest_fu AS (
            SELECT DISTINCT ON (eid) * FROM forecast_update
            ORDER BY eid, updated_at DESC NULLS LAST
        ),
        fp_cur AS (
            SELECT DISTINCT ON (fp.eid) fp.eid, fp.chg_pct_hl
            FROM forecast_periods fp
            JOIN periods p ON fp.period_name = p.period_name
            WHERE p.start_date <= CURRENT_DATE AND p.end_date >= CURRENT_DATE
            ORDER BY fp.eid
        ),
        filtered AS (
            SELECT
                e.eid,
                {COUNTRY_CODE_SQL} AS country_code,
                COALESCE(NULLIF(TRIM(fu.offering), ''), 'SIN OFFERING') AS offering
            FROM employees e
            LEFT JOIN latest_fu fu ON e.eid = fu.eid
            LEFT JOIN fp_cur ON fp_cur.eid = e.eid
            WHERE {where}
        )
    """

    try:
        async with db.pool.acquire() as conn:
            periods = await state_service.resolve_period_window(conn, window_offset)
            period_names = [p["period_name"] for p in periods]
            idx = {pn: i for i, pn in enumerate(period_names)}
            n = len(period_names)

            hours_params = list(filter_params) + [period_names]
            periods_idx = len(hours_params)

            agg_rows = await conn.fetch(f"""
                {filtered_cte},
                -- Un renglon por (eid, periodo) leido directo de forecast_periods, que ya
                -- tiene el total del periodo. Antes esto agregaba employee_daily_hours,
                -- que solo se puebla con los scripts manuales de scripts/ y por lo tanto
                -- devolvia totales viejos y con deriva de redondeo. La semantica de las
                -- metricas es la misma que documenta state_service:
                --   chg_neto = chg_hl + chg_sl                    (SIN PPA)
                --   chg      = chg_hl + chg_sl + chg_cascadeadas  (CON PPA)
                hours AS (
                    SELECT
                        fp.eid,
                        fp.period_name,
                        COALESCE(fp.sah, 0)                             AS sah,
                        COALESCE(fp.chg_hl, 0)                          AS chg_hl,
                        COALESCE(fp.chg_sl, 0)                          AS chg_sl,
                        COALESCE(fp.chg_hl, 0) + COALESCE(fp.chg_sl, 0) AS chg_neto,
                        COALESCE(fp.chg_hl, 0) + COALESCE(fp.chg_sl, 0)
                                               + COALESCE(fp.chg_cascadeadas, 0) AS chg
                    FROM forecast_periods fp
                    WHERE fp.period_name = ANY(${periods_idx})
                )
                SELECT
                    f.country_code,
                    f.offering,
                    h.period_name,
                    SUM(h.sah)      AS sah,
                    SUM(h.chg_hl)   AS chg_hl,
                    SUM(h.chg_sl)   AS chg_sl,
                    SUM(h.chg_neto) AS chg_neto,
                    SUM(h.chg)      AS chg
                FROM filtered f
                JOIN hours h ON h.eid = f.eid
                GROUP BY f.country_code, f.offering, h.period_name
            """, *hours_params)

            hc_rows = await conn.fetch(f"""
                {filtered_cte}
                SELECT country_code, offering, COUNT(*) AS hc
                FROM filtered
                GROUP BY country_code, offering
            """, *filter_params)

            targets = await _resolve_targets(conn, log)
    except ForecastException:
        raise
    except Exception:
        log.exception("Unexpected error computing forecast totals")
        raise ForecastException(AppError.DB_ERROR)

    by_group: dict = {}
    for r in agg_rows:
        i = idx.get(r["period_name"])
        if i is None:
            continue
        key = (r["country_code"], (r["offering"] or "").strip().upper())
        acc = by_group.setdefault(key, _empty_metrics(n))
        for m in METRICS:
            acc[m][i] += float(r[m] or 0)

    hc_by_group: dict = {}
    for r in hc_rows:
        key = (r["country_code"], (r["offering"] or "").strip().upper())
        hc_by_group[key] = hc_by_group.get(key, 0) + int(r["hc"] or 0)

    def collect(country_code: str, offering_upper: str | None) -> dict:
        """Suma los grupos de un pais, opcionalmente restringido a un offering."""
        totals = _empty_metrics(n)
        hc = 0
        for (c, off), acc in by_group.items():
            if c != country_code or (offering_upper is not None and off != offering_upper):
                continue
            for m in METRICS:
                for i in range(n):
                    totals[m][i] += acc[m][i]
        for (c, off), value in hc_by_group.items():
            if c != country_code or (offering_upper is not None and off != offering_upper):
                continue
            hc += value
        return {**totals, "hc": hc}

    rows: list = []
    for code, label in COUNTRY_ROWS:
        target = targets.get(code, DEFAULT_TARGET_PCT)
        rows.append({
            "key": code,
            "label": label,
            "kind": "country",
            "country": code,
            "target_pct": target,
            **collect(code, None),
        })
        if code == "AR":
            for off in AR_OFFERING_ROWS:
                rows.append({
                    "key": f"AR::{off}",
                    "label": off,
                    "kind": "offering",
                    "country": "AR",
                    "target_pct": target,
                    **collect("AR", off.upper()),
                })

    duration = int((time.monotonic() - start) * 1000)
    log.bind(duration_ms=duration).info("Forecast totals computed", rows=len(rows), periods=n)

    return {
        "periods": [{"period_name": p["period_name"], "label": p["label"]} for p in periods],
        "rows": rows,
    }
