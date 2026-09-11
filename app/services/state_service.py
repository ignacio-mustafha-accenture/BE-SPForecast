import time
import calendar as pycalendar
from datetime import date
from loguru import logger
import app.db as db
from app.config import settings

DEFAULT_TARGET_PCT = 87


async def _timed_fetch(conn, query: str, *args, request_id: str = "-"):
    start = time.monotonic()
    result = await conn.fetch(query, *args)
    ms = int((time.monotonic() - start) * 1000)
    if ms > settings.SLOW_QUERY_THRESHOLD_MS:
        logger.bind(request_id=request_id, duration_ms=ms).warning(
            "Slow query detected", query=query[:120]
        )
    return result


def _fallback_periods(window_offset: int) -> list:
    MN = ["Ene","Feb","Mar","Abr","May","Jun","Jul","Ago","Sep","Oct","Nov","Dic"]
    today = date.today()
    y, m = today.year, today.month - 1  # 0-indexed month
    h = 0 if today.day <= 15 else 1

    def add(y, m, h, n):
        t = y * 24 + m * 2 + h + n
        ny = t // 24
        t -= ny * 24
        return ny, t // 2, t % 2

    result = []
    for i in range(6):
        py, pm, ph = add(y, m, h, window_offset + i)
        s = 1 if ph == 0 else 16
        e = 15 if ph == 0 else pycalendar.monthrange(py, pm + 1)[1]
        pn = f"{MN[pm]}-P{ph+1}"
        result.append({
            "id": f"P{i+1}",
            "period_name": pn,
            "label": pn,
            "sah": 80,
            "sah_by_country": {},
            "isCurrent": window_offset == 0 and i == 0,
            "start_date": date(py, pm + 1, s).isoformat(),
            "end_date": date(py, pm + 1, e).isoformat(),
        })
    return result


async def resolve_period_window(conn, window_offset: int = 0) -> list:
    """Devuelve la ventana de 6 periodos que se muestra en la UI para un window_offset.

    Se usa desde get_state y desde totals_service para garantizar que los totales
    agreguen exactamente los mismos periodos, y en el mismo orden, que las columnas
    de la tabla.
    """
    try:
        period_rows = await conn.fetch("""
            SELECT p.period_name, p.start_date, p.end_date,
                   SUM(CASE WHEN c.is_working_day AND c.country='Argentina' THEN 8 ELSE 0 END) AS sah
            FROM periods p
            LEFT JOIN calendar c ON c.period_name = p.period_name
              AND EXTRACT(YEAR FROM c.date) IN (2025, 2026)
            GROUP BY p.period_name, p.start_date, p.end_date
            ORDER BY p.start_date
        """)
        today = date.today()
        seen_starts: dict = {}
        for r in sorted(period_rows, key=lambda r: (r["start_date"], r["period_name"])):
            if r["start_date"] not in seen_starts:
                seen_starts[r["start_date"]] = r
        rows_list = sorted(seen_starts.values(), key=lambda r: r["start_date"])
        cur = next(
            (i for i, r in enumerate(rows_list)
             if r["start_date"] <= today <= r["end_date"]),
            0,
        )
        slice_start = max(0, cur + window_offset)
        sliced = rows_list[slice_start: slice_start + 6]
        return [
            {
                "id": f"P{i+1}",
                "period_name": r["period_name"],
                "label": r["period_name"],
                "sah": float(r["sah"] or 80),
                "isCurrent": window_offset == 0 and i == 0,
                "start_date": r["start_date"].isoformat(),
                "end_date": r["end_date"].isoformat(),
            }
            for i, r in enumerate(sliced)
        ] or _fallback_periods(window_offset)
    except Exception:
        logger.exception("Failed to fetch periods, using fallback")
        return _fallback_periods(window_offset)


async def get_state(window_offset: int = 0) -> dict:
    async with db.pool.acquire() as conn:
        periods = await resolve_period_window(conn, window_offset)

        period_names = [p["period_name"] for p in periods]


        # El SAH sale del Excel via forecast_periods, no de contar dias habiles
        # del calendario: los feriados de esa tabla no coinciden con el Excel y
        # el header terminaba mostrando quincenas de 88h donde el Excel dice 96.
        # Por pais se toma el SAH mas frecuente, que es el del empleado full time.
        sah_rows = await conn.fetch(
            """
            SELECT period_name, country, sah
            FROM (
                SELECT fp.period_name,
                       e.country,
                       fp.sah,
                       ROW_NUMBER() OVER (
                           PARTITION BY fp.period_name, e.country
                           ORDER BY COUNT(*) DESC, fp.sah DESC
                       ) AS rn
                FROM forecast_periods fp
                JOIN employees e ON e.eid = fp.eid AND e.active
                WHERE fp.period_name = ANY($1) AND fp.sah > 0
                GROUP BY fp.period_name, e.country, fp.sah
            ) ranked
            WHERE rn = 1
            """,
            period_names,
        )
        sah_by_period: dict = {}
        for r in sah_rows:
            sah_by_period.setdefault(r["period_name"], {})[r["country"]] = float(r["sah"] or 0)
        for p in periods:
            by_country = sah_by_period.get(p["period_name"], {})
            p["sah_by_country"] = by_country
            # employees.country guarda el codigo ('AR'), no el nombre. La tabla
            # calendar usa 'Argentina', de ahi la confusion: buscar por nombre
            # nunca acertaba y caia siempre al max().
            if by_country:
                p["sah"] = by_country.get("AR") or max(by_country.values())

        pto_rows = await conn.fetch(
            "SELECT eid FROM absences WHERE type='PTO' AND start_date <= CURRENT_DATE AND end_date >= CURRENT_DATE"
        )
        active_pto_eids = {r["eid"] for r in pto_rows}


        emp_rows = await conn.fetch("""
            WITH latest_fu AS (
                SELECT DISTINCT ON (eid) * FROM forecast_update ORDER BY eid, updated_at DESC NULLS LAST
            )
            SELECT
                e.eid AS "EID",
                e.name AS "Name",
                COALESCE(e.country, e.location) AS "Country",
                CASE WHEN e.cl IS NOT NULL
                     THEN CAST(CAST(e.cl AS NUMERIC) AS INTEGER)::text
                     ELSE NULL END AS "CL",
                COALESCE(e.fte, 1.0) AS "FTE",
                TO_CHAR(e.hire_date,'DD/MM/YY') AS "HireDate",
                COALESCE(pl.name, e.people_lead::text) AS "Manager",
                COALESCE(te.name, fu.te_approver::text) AS "TEApprover",
                fu.offering AS "ProjectType",
                fu.client AS "Client",
                COALESCE(am.name, fu.account_manager::text) AS "AccountManager",
                fu.office AS "Office",
                TO_CHAR(fu.roll_on,'DD/MM/YY') AS "RollOn",
                TO_CHAR(fu.roll_off,'DD/MM/YY') AS "RollOff",
                TO_CHAR(fu.first_available,'DD/MM/YY') AS "FAD",
                CASE
                    WHEN fu.roll_off IS NULL THEN NULL
                    WHEN fu.roll_off <= CURRENT_DATE THEN 0
                    ELSE (fu.roll_off - CURRENT_DATE)
                END AS "DaysToAvailable",
                fu.chargeability_pct AS "ChargeabilityPct",
                TO_CHAR(fu.next_pto,'DD/MM/YY') AS "NextPTO",
                TO_CHAR(fu.next_pto_end,'DD/MM/YY') AS "NextPTOEnd",
                fu.next_pto_hours AS "NextPTOHours",
                fu.next_client AS "NextClientPTO",
                fu.notes AS "Notes",
                e.new_joiner AS "NewJoiner",
                TO_CHAR(e.termination_date,'DD/MM/YY') AS "TerminationDate",
                COALESCE(e.charge, TRUE) AS "Charge",
                COALESCE(e.ringfenced, FALSE) AS "Ringfenced",
                e.reserva_status AS "ReservaStatus",
                COALESCE(fu.isg_aligned = 'Yes', FALSE) AS "ISGAligned"
            FROM employees e
            LEFT JOIN latest_fu fu ON e.eid = fu.eid
            LEFT JOIN employees pl ON e.people_lead = pl.eid
            LEFT JOIN employees am ON fu.account_manager = am.eid
            LEFT JOIN employees te ON fu.te_approver = te.eid
            WHERE e.active = TRUE
            ORDER BY COALESCE(e.country, e.location), e.name
        """)

        # Forecast map desde forecast_periods, que es el total por (eid, periodo) y lo
        # unico que se actualiza en cada recalculo, aprobacion de ticket, efectivizacion,
        # alta/baja e import del Excel. Antes se agregaba employee_daily_hours, que solo
        # se puebla con los scripts manuales de scripts/: la vista global quedaba
        # mostrando datos viejos despues de cada operacion y, ademas, el ida y vuelta de
        # repartir el total en dias y volver a sumarlo metia deriva de redondeo
        # (sah 96 volvia como 96.03, chg 48 como 47.96).
        #
        # Semantica de cada metrica (se preserva la que ya exponia la vista global):
        #   chg_hl          = horas hard lock
        #   chg_sl          = horas soft lock
        #   chg_cascadeadas = horas PPA
        #   chg_neto        = chg_hl + chg_sl                    (SIN PPA)
        #   chg             = chg_hl + chg_sl + chg_cascadeadas  (CON PPA)
        #   CHG% HL         = (chg_hl + chg_cascadeadas) / sah * 100
        #   CHG% SL         = chg_sl / sah * 100
        #
        # chg, chg_pct_hl y chg_pct_sl se derivan de las columnas de horas en vez de leer
        # las columnas homonimas ya calculadas de forecast_periods. Motivo: una sola
        # fuente de verdad. Las columnas guardadas las escriben tres caminos distintos
        # (el stored proc recalculate_forecast_period, recalculate_service y
        # chargeability_service) con definiciones que no coinciden entre si, y ninguno
        # suma el PPA al porcentaje de HL. Derivando, el porcentaje siempre cierra contra
        # las horas que se muestran al lado. Verificado contra dev: fp.chg ya coincide con
        # chg_hl + chg_sl + chg_cascadeadas en las 3158 filas, y los porcentajes guardados
        # solo difieren en 25 filas por el ROUND a entero de chg_hl/chg_sl (<= 0.56 pp).
        fp_rows = await conn.fetch(
            """
            SELECT
                fp.eid,
                fp.period_name,
                COALESCE(fp.sah, 0)                                                 AS sah,
                COALESCE(fp.chg_hl, 0)                                              AS chg_hl,
                COALESCE(fp.chg_sl, 0)                                              AS chg_sl,
                COALESCE(fp.chg_cascadeadas_hl, 0)                                  AS chg_cascadeadas_hl,
                COALESCE(fp.chg_cascadeadas_sl, 0)                                  AS chg_cascadeadas_sl,
                COALESCE(fp.chg_cascadeadas_hl, 0)
                  + COALESCE(fp.chg_cascadeadas_sl, 0)                              AS chg_cascadeadas,
                COALESCE(fp.chg_hl, 0) + COALESCE(fp.chg_sl, 0)                    AS chg_neto,
                COALESCE(fp.chg_hl, 0) + COALESCE(fp.chg_sl, 0)
                  + COALESCE(fp.chg_cascadeadas_hl, 0)
                  + COALESCE(fp.chg_cascadeadas_sl, 0)                              AS chg,
                COALESCE(fp.absence_hours, 0)                                       AS absence_hours,
                CASE WHEN COALESCE(fp.sah, 0) > 0
                     THEN ROUND((COALESCE(fp.chg_hl, 0) + COALESCE(fp.chg_cascadeadas_hl, 0))
                                / fp.sah * 100, 2)
                     ELSE 0 END                                                     AS chg_pct_hl,
                CASE WHEN COALESCE(fp.sah, 0) > 0
                     THEN ROUND((COALESCE(fp.chg_sl, 0) + COALESCE(fp.chg_cascadeadas_sl, 0))
                                / fp.sah * 100, 2)
                     ELSE 0 END                                                     AS chg_pct_sl
            FROM forecast_periods fp
            WHERE fp.period_name = ANY($1)
            """,
            period_names,
        )
        # Subtipo de assumption por (eid, periodo) para el color de la celda
        kind_rows = await conn.fetch(
            """
            SELECT eid, period_name, assumption_kind
            FROM chargeability_blocks
            WHERE scenario_type = 'assumption'
              AND assumption_kind IS NOT NULL
              AND period_name = ANY($1)
            """,
            period_names,
        )
        kind_map: dict = {}
        for k in kind_rows:
            kind_map.setdefault(k["eid"], {})[k["period_name"]] = k["assumption_kind"]


        forecast_map: dict = {}
        for fp in fp_rows:
            if fp["eid"] not in forecast_map:
                forecast_map[fp["eid"]] = {}
            forecast_map[fp["eid"]][fp["period_name"]] = {
                "chg":                float(fp["chg"] or 0),
                "chg_neto":           float(fp["chg_neto"] or 0),
                "sah":                float(fp["sah"] or 0),
                "chg_hl":             float(fp["chg_hl"] or 0),
                "chg_sl":             float(fp["chg_sl"] or 0),
                "chg_cascadeadas":    float(fp["chg_cascadeadas"] or 0),
                "chg_cascadeadas_hl": float(fp["chg_cascadeadas_hl"] or 0),
                "chg_cascadeadas_sl": float(fp["chg_cascadeadas_sl"] or 0),
                "absence_hours":      float(fp["absence_hours"] or 0),
                "chg_pct_sl":         float(fp["chg_pct_sl"] or 0),
                "chg_pct_hl":         float(fp["chg_pct_hl"] or 0),
            }

        employees = []
        for e in emp_rows:
            row = dict(e)
            fp = forecast_map.get(row["EID"], {})
            chg_arr                = [float(fp.get(pn, {}).get("chg", 0))                for pn in period_names]
            chg_neto_arr           = [float(fp.get(pn, {}).get("chg_neto", 0))           for pn in period_names]
            sah_arr                = [float(fp.get(pn, {}).get("sah", 0))                for pn in period_names]
            chg_hl_arr             = [float(fp.get(pn, {}).get("chg_hl", 0))             for pn in period_names]
            chg_sl_arr             = [float(fp.get(pn, {}).get("chg_sl", 0))             for pn in period_names]
            chg_cascadeadas_arr    = [float(fp.get(pn, {}).get("chg_cascadeadas", 0))    for pn in period_names]
            chg_cascadeadas_hl_arr = [float(fp.get(pn, {}).get("chg_cascadeadas_hl", 0)) for pn in period_names]
            chg_cascadeadas_sl_arr = [float(fp.get(pn, {}).get("chg_cascadeadas_sl", 0)) for pn in period_names]
            absence_hours_arr      = [float(fp.get(pn, {}).get("absence_hours", 0))      for pn in period_names]
            chg_pct_sl_arr         = [float(fp.get(pn, {}).get("chg_pct_sl", 0))         for pn in period_names]
            chg_pct_hl_arr         = [float(fp.get(pn, {}).get("chg_pct_hl", 0))         for pn in period_names]

            ak = kind_map.get(row["EID"], {})
            assumption_kind_arr = [ak.get(pn) for pn in period_names]

            cur_fp = fp.get(period_names[0], {}) if period_names else {}
            client = row.get("Client") or ""
            no_client = not client or client.strip().lower() in ("unassigned", "")
            if row.get("NewJoiner"):
                scenario = "newJoiner"
            elif client == "ISG PE Assessment":
                scenario = "isgAssessment"
            elif row.get("Ringfenced") and row.get("ISGAligned"):
                scenario = "isgRingfenced"
            elif no_client:
                scenario = "noIsg"
            else:
                scenario = "effective"

            row.update({
                "ScenarioType": scenario,
                "chg":                chg_arr,
                "chg_neto":           chg_neto_arr,
                "sah":                sah_arr,
                "cp":                 chg_pct_hl_arr,
                "chg_hl":             chg_hl_arr,
                "chg_sl":             chg_sl_arr,
                "chg_cascadeadas":    chg_cascadeadas_arr,
                "chg_cascadeadas_hl": chg_cascadeadas_hl_arr,
                "chg_cascadeadas_sl": chg_cascadeadas_sl_arr,
                "absence_hours":      absence_hours_arr,
                "chg_pct_sl":         chg_pct_sl_arr,
                "chg_pct_hl":         chg_pct_hl_arr,
                "assumption_kind":    assumption_kind_arr,
                "NJFormat": (
                    f"{row['Name']} | {row['HireDate']} | CL{row['CL']} | {row['Country']}"
                    if row.get("NewJoiner") else None
                ),
                "FTE": float(row.get("FTE") or 1),
                "ChargeabilityPct": float(row.get("ChargeabilityPct") or 0),
                "DaysToAvailable": (
                    float(row["DaysToAvailable"])
                    if row.get("DaysToAvailable") is not None else None
                ),
                "NextPTOHours": float(row.get("NextPTOHours") or 0),
                "Charge": row.get("Charge") is not False,
                "IsOnPTO": row["EID"] in active_pto_eids,
                "Ringfenced": bool(row.get("Ringfenced") or False),
                "ISGAligned": bool(row.get("ISGAligned") or False),
            })
            employees.append(row)

        target_rows = await conn.fetch(
            "SELECT country, target_pct FROM targets WHERE fiscal_year='FY26' AND (valid_to IS NULL OR valid_to>=CURRENT_DATE)"
        )
        targets = {"general": DEFAULT_TARGET_PCT}
        for t in target_rows:
            targets[t["country"]] = float(t["target_pct"])

        ticket_rows = await conn.fetch("""
            SELECT t.id::text AS id, t.type, t.eid, t.detail, t.status,
                   TO_CHAR(t.date,'DD/MM/YY') AS date,
                   COALESCE(e.name, u.full_name, t.created_by::text) AS "by",
                   t.nj_name, t.cl, t.location, t.people_lead,
                   t.client_name, t.offering_type, t.chargeability_pct,
                   t.hours_to_move, t.from_period, t.to_period, t.comments,
                   t.start_date::text AS start_date,
                   t.end_date::text AS end_date,
                   t.rejection_reason,
                   COALESCE(t.scenario_type, 'assumption') AS scenario_type,
                   t.effectivization_date::text AS effectivization_date,
                   COALESCE(emp.name, t.nj_name) AS eid_name,
                   COALESCE(emp.country, emp.location) AS eid_country
            FROM tickets t
            LEFT JOIN employees e   ON t.created_by = e.eid
            LEFT JOIN users u       ON u.email = t.created_by
            LEFT JOIN employees emp ON t.eid = emp.eid
            ORDER BY t.id DESC
        """)
        tickets = [dict(r) for r in ticket_rows]

        ppa_rows = await conn.fetch("""
            SELECT p.id::text AS id, p.eid, e.name,
                   p.from_period AS "from", p.to_period AS "to",
                   p.hours AS hs, p.hours_chargeable, p.hours_standard,
                   p.reason, TO_CHAR(p.created_at,'DD/MM/YY') AS date
            FROM ppa_log p LEFT JOIN employees e ON p.eid=e.eid
            ORDER BY p.created_at DESC
        """)
        ppa_log = [dict(r) for r in ppa_rows]

    return {
        "periods": periods,
        "employees": employees,
        "targets": targets,
        "tickets": tickets,
        "ppa_log": ppa_log,
    }