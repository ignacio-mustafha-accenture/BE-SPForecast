-- ============================================================
-- SEED DATA — ForecastOS dump de prueba completo
-- Ejecutar en Supabase SQL Editor
-- Preserva: periods, calendar, users, permissions, role_permissions
-- ============================================================


-- ============================================================
-- SECCIÓN 1 — LIMPIEZA
-- Orden respetando FK constraints
-- ============================================================

-- Desligar users de employees antes de borrar
UPDATE users SET eid = NULL WHERE eid IS NOT NULL;
-- Limpiar self-ref FK de people_lead
UPDATE employees SET people_lead = NULL WHERE people_lead IS NOT NULL;

DELETE FROM absences;
DELETE FROM ppa_log;
DELETE FROM tickets;
DELETE FROM chargeability_blocks;
DELETE FROM forecast_periods;
DELETE FROM forecast_update;
DELETE FROM client_catalog;
DELETE FROM employees;
DELETE FROM targets;


-- ============================================================
-- SECCIÓN 2 — TARGETS
-- ============================================================

INSERT INTO targets (country, target_pct, fiscal_year) VALUES
  ('Argentina',  87, 'FY26'),
  ('Mexico',     85, 'FY26'),
  ('Costa Rica', 85, 'FY26');


-- ============================================================
-- SECCIÓN 3 — EMPLOYEES (15 perfiles)
-- Insertar con people_lead = NULL, luego UPDATE relaciones
-- ============================================================

INSERT INTO employees (eid, name, country, location, cl, fte, new_joiner, active, charge, hire_date) VALUES
  ('garcia.sofia',       'Sofia Garcia',     'Argentina',  'AR', 10, 1.0, FALSE, TRUE,  TRUE,  '2024-01-15'),
  ('diaz.lucia',         'Lucia Diaz',       'Argentina',  'AR', 13, 1.0, FALSE, TRUE,  TRUE,  '2024-01-15'),
  ('martinez.lucas',     'Lucas Martinez',   'Argentina',  'AR',  8, 1.0, FALSE, TRUE,  TRUE,  '2024-01-15'),
  ('rodriguez.ana',      'Ana Rodriguez',    'Argentina',  'AR',  9, 1.0, FALSE, TRUE,  TRUE,  '2024-01-15'),
  ('perez.carlos',       'Carlos Perez',     'Argentina',  'AR', 11, 1.0, FALSE, TRUE,  FALSE, '2024-01-15'),
  ('nj.valentina.lopez', 'Valentina Lopez',  'Argentina',  'AR',  8, 1.0, TRUE,  TRUE,  TRUE,  '2026-08-01'),
  ('fernandez.diego',    'Diego Fernandez',  'Argentina',  'AR',  9, 1.0, FALSE, TRUE,  TRUE,  '2024-01-15'),
  ('gonzalez.maria',     'Maria Gonzalez',   'Argentina',  'AR', 10, 1.0, FALSE, TRUE,  TRUE,  '2024-01-15'),
  ('lopez.nicolas',      'Nicolas Lopez',    'Argentina',  'AR', 12, 1.0, FALSE, TRUE,  TRUE,  '2024-01-15'),
  ('romero.florencia',   'Florencia Romero', 'Argentina',  'AR',  8, 1.0, FALSE, TRUE,  TRUE,  '2024-01-15'),
  ('hernandez.pablo',    'Pablo Hernandez',  'Argentina',  'AR',  9, 1.0, FALSE, TRUE,  TRUE,  '2024-01-15'),
  ('vargas.camila',      'Camila Vargas',    'Mexico',     'MX', 10, 1.0, FALSE, TRUE,  TRUE,  '2024-01-15'),
  ('gutierrez.jose',     'Jose Gutierrez',   'Costa Rica', 'CR',  9, 1.0, FALSE, TRUE,  TRUE,  '2024-01-15'),
  ('blanco.andrea',      'Andrea Blanco',    'Argentina',  'AR', 11, 1.0, FALSE, FALSE, TRUE,  '2023-03-01'),
  ('torres.martin',      'Martin Torres',    'Argentina',  'AR',  8, 0.5, FALSE, TRUE,  TRUE,  '2024-01-15');

-- People lead: diaz.lucia → garcia.sofia; resto AR → garcia.sofia
UPDATE employees SET people_lead = 'garcia.sofia' WHERE eid = 'diaz.lucia';
UPDATE employees SET people_lead = 'garcia.sofia' WHERE eid IN (
  'martinez.lucas', 'rodriguez.ana', 'perez.carlos', 'nj.valentina.lopez',
  'fernandez.diego', 'gonzalez.maria', 'lopez.nicolas', 'romero.florencia',
  'hernandez.pablo', 'torres.martin'
);
-- garcia.sofia, vargas.camila, gutierrez.jose: people_lead = NULL (top-level)

UPDATE employees SET termination_date = '2026-07-01' WHERE eid = 'blanco.andrea';


-- ============================================================
-- SECCIÓN 4 — FORECAST_UPDATE (14 activos, sin blanco.andrea)
-- ============================================================

INSERT INTO forecast_update (eid, client, offering, roll_on, roll_off, chargeability_pct, te_approver, updated_at) VALUES
  ('garcia.sofia',       'Google',             'Tech-led',      '2026-01-01', '2026-12-31', 100, NULL,           NOW()),
  ('diaz.lucia',         'Mercado Libre',      'CTO',           '2026-01-01', '2026-12-31',  60, 'garcia.sofia', NOW()),
  ('martinez.lucas',     'Mercado Libre',      'CTO',           '2026-06-01', '2026-12-31', 100, 'garcia.sofia', NOW()),
  ('rodriguez.ana',      'Globant',            'Tech-led',      '2026-03-01', '2026-12-31',  50, 'garcia.sofia', NOW()),
  ('perez.carlos',       NULL,                 NULL,            NULL,         NULL,            0, NULL,           NOW()),
  ('nj.valentina.lopez', NULL,                 NULL,            '2026-08-01', '2026-12-31', 100, 'garcia.sofia', NOW()),
  ('fernandez.diego',    'Accenture Internal', 'Internal',      '2026-05-01', '2026-12-31',  25, 'garcia.sofia', NOW()),
  ('gonzalez.maria',     'DHL',                'Cost Take Out', '2026-04-01', '2026-12-31',  75, 'garcia.sofia', NOW()),
  ('lopez.nicolas',      'Cabify',             'Tech-led',      '2026-01-01', '2026-08-31', 100, 'garcia.sofia', NOW()),
  ('romero.florencia',   'Nuvei',              'Tech-led',      '2026-06-01', '2026-12-31', 100, 'garcia.sofia', NOW()),
  ('hernandez.pablo',    'Google',             'OM+SPY+Others', '2026-01-01', '2026-12-31', 100, NULL,           NOW()),
  ('vargas.camila',      'Aeromexico',         'Tech-led',      '2026-01-01', '2026-12-31', 100, NULL,           NOW()),
  ('gutierrez.jose',     'BAC',                'Tech-led',      '2026-03-01', '2026-12-31', 100, NULL,           NOW()),
  ('torres.martin',      'BPO Latam',          'Cost Take Out', '2026-05-01', '2026-12-31', 100, 'garcia.sofia', NOW());


-- ============================================================
-- SECCIÓN 5 — CHARGEABILITY_BLOCKS (~65 filas)
-- Fuente de verdad para recalculate_forecast_period()
-- ============================================================

INSERT INTO chargeability_blocks
  (eid, period_name, chargeability_pct, scenario_type, start_date, end_date, effectivization_date, created_by)
VALUES
-- garcia.sofia — effective 100%, 6 períodos
  ('garcia.sofia', 'Jul-P2', 100, 'effective', '2026-07-16', '2026-07-31', NULL,         'garcia.sofia'),
  ('garcia.sofia', 'Ago-P1', 100, 'effective', '2026-08-01', '2026-08-15', NULL,         'garcia.sofia'),
  ('garcia.sofia', 'Ago-P2', 100, 'effective', '2026-08-16', '2026-08-31', NULL,         'garcia.sofia'),
  ('garcia.sofia', 'Sep-P1', 100, 'effective', '2026-09-01', '2026-09-15', NULL,         'garcia.sofia'),
  ('garcia.sofia', 'Sep-P2', 100, 'effective', '2026-09-16', '2026-09-30', NULL,         'garcia.sofia'),
  ('garcia.sofia', 'Oct-P1', 100, 'effective', '2026-10-01', '2026-10-15', NULL,         'garcia.sofia'),
-- diaz.lucia — effective 60%, 6 períodos
  ('diaz.lucia', 'Jul-P2', 60, 'effective', '2026-07-16', '2026-07-31', NULL,            'garcia.sofia'),
  ('diaz.lucia', 'Ago-P1', 60, 'effective', '2026-08-01', '2026-08-15', NULL,            'garcia.sofia'),
  ('diaz.lucia', 'Ago-P2', 60, 'effective', '2026-08-16', '2026-08-31', NULL,            'garcia.sofia'),
  ('diaz.lucia', 'Sep-P1', 60, 'effective', '2026-09-01', '2026-09-15', NULL,            'garcia.sofia'),
  ('diaz.lucia', 'Sep-P2', 60, 'effective', '2026-09-16', '2026-09-30', NULL,            'garcia.sofia'),
  ('diaz.lucia', 'Oct-P1', 60, 'effective', '2026-10-01', '2026-10-15', NULL,            'garcia.sofia'),
-- martinez.lucas — assumption 100%, 6 períodos
  ('martinez.lucas', 'Jul-P2', 100, 'assumption', '2026-07-16', '2026-07-31', '2026-12-31', 'garcia.sofia'),
  ('martinez.lucas', 'Ago-P1', 100, 'assumption', '2026-08-01', '2026-08-15', '2026-12-31', 'garcia.sofia'),
  ('martinez.lucas', 'Ago-P2', 100, 'assumption', '2026-08-16', '2026-08-31', '2026-12-31', 'garcia.sofia'),
  ('martinez.lucas', 'Sep-P1', 100, 'assumption', '2026-09-01', '2026-09-15', '2026-12-31', 'garcia.sofia'),
  ('martinez.lucas', 'Sep-P2', 100, 'assumption', '2026-09-16', '2026-09-30', '2026-12-31', 'garcia.sofia'),
  ('martinez.lucas', 'Oct-P1', 100, 'assumption', '2026-10-01', '2026-10-15', '2026-12-31', 'garcia.sofia'),
-- rodriguez.ana — effective 50%, 6 períodos
  ('rodriguez.ana', 'Jul-P2', 50, 'effective', '2026-07-16', '2026-07-31', NULL,          'garcia.sofia'),
  ('rodriguez.ana', 'Ago-P1', 50, 'effective', '2026-08-01', '2026-08-15', NULL,          'garcia.sofia'),
  ('rodriguez.ana', 'Ago-P2', 50, 'effective', '2026-08-16', '2026-08-31', NULL,          'garcia.sofia'),
  ('rodriguez.ana', 'Sep-P1', 50, 'effective', '2026-09-01', '2026-09-15', NULL,          'garcia.sofia'),
  ('rodriguez.ana', 'Sep-P2', 50, 'effective', '2026-09-16', '2026-09-30', NULL,          'garcia.sofia'),
  ('rodriguez.ana', 'Oct-P1', 50, 'effective', '2026-10-01', '2026-10-15', NULL,          'garcia.sofia'),
-- nj.valentina.lopez — assumption 100%, desde Ago-P1 (5 bloques)
  ('nj.valentina.lopez', 'Ago-P1', 100, 'assumption', '2026-08-01', '2026-08-15', '2026-12-31', 'garcia.sofia'),
  ('nj.valentina.lopez', 'Ago-P2', 100, 'assumption', '2026-08-16', '2026-08-31', '2026-12-31', 'garcia.sofia'),
  ('nj.valentina.lopez', 'Sep-P1', 100, 'assumption', '2026-09-01', '2026-09-15', '2026-12-31', 'garcia.sofia'),
  ('nj.valentina.lopez', 'Sep-P2', 100, 'assumption', '2026-09-16', '2026-09-30', '2026-12-31', 'garcia.sofia'),
  ('nj.valentina.lopez', 'Oct-P1', 100, 'assumption', '2026-10-01', '2026-10-15', '2026-12-31', 'garcia.sofia'),
-- fernandez.diego — effective 25%, 6 períodos (RED)
  ('fernandez.diego', 'Jul-P2', 25, 'effective', '2026-07-16', '2026-07-31', NULL,        'garcia.sofia'),
  ('fernandez.diego', 'Ago-P1', 25, 'effective', '2026-08-01', '2026-08-15', NULL,        'garcia.sofia'),
  ('fernandez.diego', 'Ago-P2', 25, 'effective', '2026-08-16', '2026-08-31', NULL,        'garcia.sofia'),
  ('fernandez.diego', 'Sep-P1', 25, 'effective', '2026-09-01', '2026-09-15', NULL,        'garcia.sofia'),
  ('fernandez.diego', 'Sep-P2', 25, 'effective', '2026-09-16', '2026-09-30', NULL,        'garcia.sofia'),
  ('fernandez.diego', 'Oct-P1', 25, 'effective', '2026-10-01', '2026-10-15', NULL,        'garcia.sofia'),
-- gonzalez.maria — effective 75%, 6 períodos (YELLOW)
  ('gonzalez.maria', 'Jul-P2', 75, 'effective', '2026-07-16', '2026-07-31', NULL,         'garcia.sofia'),
  ('gonzalez.maria', 'Ago-P1', 75, 'effective', '2026-08-01', '2026-08-15', NULL,         'garcia.sofia'),
  ('gonzalez.maria', 'Ago-P2', 75, 'effective', '2026-08-16', '2026-08-31', NULL,         'garcia.sofia'),
  ('gonzalez.maria', 'Sep-P1', 75, 'effective', '2026-09-01', '2026-09-15', NULL,         'garcia.sofia'),
  ('gonzalez.maria', 'Sep-P2', 75, 'effective', '2026-09-16', '2026-09-30', NULL,         'garcia.sofia'),
  ('gonzalez.maria', 'Oct-P1', 75, 'effective', '2026-10-01', '2026-10-15', NULL,         'garcia.sofia'),
-- lopez.nicolas — effective 100%, roll-off 2026-08-31 → solo 3 bloques
  ('lopez.nicolas', 'Jul-P2', 100, 'effective', '2026-07-16', '2026-07-31', NULL,         'garcia.sofia'),
  ('lopez.nicolas', 'Ago-P1', 100, 'effective', '2026-08-01', '2026-08-15', NULL,         'garcia.sofia'),
  ('lopez.nicolas', 'Ago-P2', 100, 'effective', '2026-08-16', '2026-08-31', NULL,         'garcia.sofia'),
-- romero.florencia — assumption 100%, sin Ago-P1 (PTO esa quincena)
  ('romero.florencia', 'Jul-P2', 100, 'assumption', '2026-07-16', '2026-07-31', '2026-12-31', 'garcia.sofia'),
  ('romero.florencia', 'Ago-P2', 100, 'assumption', '2026-08-16', '2026-08-31', '2026-12-31', 'garcia.sofia'),
  ('romero.florencia', 'Sep-P1', 100, 'assumption', '2026-09-01', '2026-09-15', '2026-12-31', 'garcia.sofia'),
  ('romero.florencia', 'Sep-P2', 100, 'assumption', '2026-09-16', '2026-09-30', '2026-12-31', 'garcia.sofia'),
  ('romero.florencia', 'Oct-P1', 100, 'assumption', '2026-10-01', '2026-10-15', '2026-12-31', 'garcia.sofia'),
-- hernandez.pablo — effective 100%, 6 períodos (sick Jul-P2 2 días)
  ('hernandez.pablo', 'Jul-P2', 100, 'effective', '2026-07-16', '2026-07-31', NULL,       'garcia.sofia'),
  ('hernandez.pablo', 'Ago-P1', 100, 'effective', '2026-08-01', '2026-08-15', NULL,       'garcia.sofia'),
  ('hernandez.pablo', 'Ago-P2', 100, 'effective', '2026-08-16', '2026-08-31', NULL,       'garcia.sofia'),
  ('hernandez.pablo', 'Sep-P1', 100, 'effective', '2026-09-01', '2026-09-15', NULL,       'garcia.sofia'),
  ('hernandez.pablo', 'Sep-P2', 100, 'effective', '2026-09-16', '2026-09-30', NULL,       'garcia.sofia'),
  ('hernandez.pablo', 'Oct-P1', 100, 'effective', '2026-10-01', '2026-10-15', NULL,       'garcia.sofia'),
-- vargas.camila — effective 100%, 6 períodos (Mexico)
  ('vargas.camila', 'Jul-P2', 100, 'effective', '2026-07-16', '2026-07-31', NULL,         'garcia.sofia'),
  ('vargas.camila', 'Ago-P1', 100, 'effective', '2026-08-01', '2026-08-15', NULL,         'garcia.sofia'),
  ('vargas.camila', 'Ago-P2', 100, 'effective', '2026-08-16', '2026-08-31', NULL,         'garcia.sofia'),
  ('vargas.camila', 'Sep-P1', 100, 'effective', '2026-09-01', '2026-09-15', NULL,         'garcia.sofia'),
  ('vargas.camila', 'Sep-P2', 100, 'effective', '2026-09-16', '2026-09-30', NULL,         'garcia.sofia'),
  ('vargas.camila', 'Oct-P1', 100, 'effective', '2026-10-01', '2026-10-15', NULL,         'garcia.sofia'),
-- gutierrez.jose — effective 100%, 6 períodos (Costa Rica)
  ('gutierrez.jose', 'Jul-P2', 100, 'effective', '2026-07-16', '2026-07-31', NULL,        'garcia.sofia'),
  ('gutierrez.jose', 'Ago-P1', 100, 'effective', '2026-08-01', '2026-08-15', NULL,        'garcia.sofia'),
  ('gutierrez.jose', 'Ago-P2', 100, 'effective', '2026-08-16', '2026-08-31', NULL,        'garcia.sofia'),
  ('gutierrez.jose', 'Sep-P1', 100, 'effective', '2026-09-01', '2026-09-15', NULL,        'garcia.sofia'),
  ('gutierrez.jose', 'Sep-P2', 100, 'effective', '2026-09-16', '2026-09-30', NULL,        'garcia.sofia'),
  ('gutierrez.jose', 'Oct-P1', 100, 'effective', '2026-10-01', '2026-10-15', NULL,        'garcia.sofia'),
-- torres.martin — effective 100%, 6 períodos (FTE 0.5)
  ('torres.martin', 'Jul-P2', 100, 'effective', '2026-07-16', '2026-07-31', NULL,         'garcia.sofia'),
  ('torres.martin', 'Ago-P1', 100, 'effective', '2026-08-01', '2026-08-15', NULL,         'garcia.sofia'),
  ('torres.martin', 'Ago-P2', 100, 'effective', '2026-08-16', '2026-08-31', NULL,         'garcia.sofia'),
  ('torres.martin', 'Sep-P1', 100, 'effective', '2026-09-01', '2026-09-15', NULL,         'garcia.sofia'),
  ('torres.martin', 'Sep-P2', 100, 'effective', '2026-09-16', '2026-09-30', NULL,         'garcia.sofia'),
  ('torres.martin', 'Oct-P1', 100, 'effective', '2026-10-01', '2026-10-15', NULL,         'garcia.sofia');
-- perez.carlos — sin bloques (unassigned)
-- blanco.andrea — sin bloques (terminada)


-- ============================================================
-- SECCIÓN 6 — FORECAST_PERIODS (valores pre-computados)
-- SAH base: P2=80h (10 días×8h), P1=88h (11 días×8h) - aproximación sin feriados
-- FTE 0.5 → SAH×0.5; sick/PTO ajustan sah y absence_hours
-- chg = chg_hl + chg_sl; chg_cascadeadas = 0 en todo el seed
-- ============================================================

INSERT INTO forecast_periods
  (eid, period_name, chg, sah, chg_hl, chg_sl, chg_cascadeadas, absence_hours, chg_pct_hl, chg_pct_sl)
VALUES
-- garcia.sofia — effective 100%
  ('garcia.sofia', 'Jul-P2', 80,   80,   80,   0,    0, 0,  100, 100),
  ('garcia.sofia', 'Ago-P1', 88,   88,   88,   0,    0, 0,  100, 100),
  ('garcia.sofia', 'Ago-P2', 80,   80,   80,   0,    0, 0,  100, 100),
  ('garcia.sofia', 'Sep-P1', 88,   88,   88,   0,    0, 0,  100, 100),
  ('garcia.sofia', 'Sep-P2', 80,   80,   80,   0,    0, 0,  100, 100),
  ('garcia.sofia', 'Oct-P1', 88,   88,   88,   0,    0, 0,  100, 100),
-- diaz.lucia — effective 60%
  ('diaz.lucia',   'Jul-P2', 48,   80,   48,   0,    0, 0,   60,  60),
  ('diaz.lucia',   'Ago-P1', 52.8, 88,   52.8, 0,    0, 0,   60,  60),
  ('diaz.lucia',   'Ago-P2', 48,   80,   48,   0,    0, 0,   60,  60),
  ('diaz.lucia',   'Sep-P1', 52.8, 88,   52.8, 0,    0, 0,   60,  60),
  ('diaz.lucia',   'Sep-P2', 48,   80,   48,   0,    0, 0,   60,  60),
  ('diaz.lucia',   'Oct-P1', 52.8, 88,   52.8, 0,    0, 0,   60,  60),
-- martinez.lucas — assumption 100%
  ('martinez.lucas', 'Jul-P2', 80, 80,   0,    80,   0, 0,    0, 100),
  ('martinez.lucas', 'Ago-P1', 88, 88,   0,    88,   0, 0,    0, 100),
  ('martinez.lucas', 'Ago-P2', 80, 80,   0,    80,   0, 0,    0, 100),
  ('martinez.lucas', 'Sep-P1', 88, 88,   0,    88,   0, 0,    0, 100),
  ('martinez.lucas', 'Sep-P2', 80, 80,   0,    80,   0, 0,    0, 100),
  ('martinez.lucas', 'Oct-P1', 88, 88,   0,    88,   0, 0,    0, 100),
-- rodriguez.ana — effective 50%
  ('rodriguez.ana', 'Jul-P2', 40,  80,   40,   0,    0, 0,   50,  50),
  ('rodriguez.ana', 'Ago-P1', 44,  88,   44,   0,    0, 0,   50,  50),
  ('rodriguez.ana', 'Ago-P2', 40,  80,   40,   0,    0, 0,   50,  50),
  ('rodriguez.ana', 'Sep-P1', 44,  88,   44,   0,    0, 0,   50,  50),
  ('rodriguez.ana', 'Sep-P2', 40,  80,   40,   0,    0, 0,   50,  50),
  ('rodriguez.ana', 'Oct-P1', 44,  88,   44,   0,    0, 0,   50,  50),
-- perez.carlos — unassigned (charge=FALSE), sah normal, chg=0
  ('perez.carlos', 'Jul-P2', 0,   80,   0,    0,    0, 0,    0,   0),
  ('perez.carlos', 'Ago-P1', 0,   88,   0,    0,    0, 0,    0,   0),
  ('perez.carlos', 'Ago-P2', 0,   80,   0,    0,    0, 0,    0,   0),
  ('perez.carlos', 'Sep-P1', 0,   88,   0,    0,    0, 0,    0,   0),
  ('perez.carlos', 'Sep-P2', 0,   80,   0,    0,    0, 0,    0,   0),
  ('perez.carlos', 'Oct-P1', 0,   88,   0,    0,    0, 0,    0,   0),
-- nj.valentina.lopez — assumption 100%, sin fila Jul-P2 (empieza Ago-P1)
  ('nj.valentina.lopez', 'Ago-P1', 88, 88, 0, 88,  0, 0,    0, 100),
  ('nj.valentina.lopez', 'Ago-P2', 80, 80, 0, 80,  0, 0,    0, 100),
  ('nj.valentina.lopez', 'Sep-P1', 88, 88, 0, 88,  0, 0,    0, 100),
  ('nj.valentina.lopez', 'Sep-P2', 80, 80, 0, 80,  0, 0,    0, 100),
  ('nj.valentina.lopez', 'Oct-P1', 88, 88, 0, 88,  0, 0,    0, 100),
-- fernandez.diego — effective 25% (RED)
  ('fernandez.diego', 'Jul-P2', 20,  80,  20,  0,   0, 0,   25,  25),
  ('fernandez.diego', 'Ago-P1', 22,  88,  22,  0,   0, 0,   25,  25),
  ('fernandez.diego', 'Ago-P2', 20,  80,  20,  0,   0, 0,   25,  25),
  ('fernandez.diego', 'Sep-P1', 22,  88,  22,  0,   0, 0,   25,  25),
  ('fernandez.diego', 'Sep-P2', 20,  80,  20,  0,   0, 0,   25,  25),
  ('fernandez.diego', 'Oct-P1', 22,  88,  22,  0,   0, 0,   25,  25),
-- gonzalez.maria — effective 75% (YELLOW)
  ('gonzalez.maria', 'Jul-P2', 60,  80,  60,  0,   0, 0,   75,  75),
  ('gonzalez.maria', 'Ago-P1', 66,  88,  66,  0,   0, 0,   75,  75),
  ('gonzalez.maria', 'Ago-P2', 60,  80,  60,  0,   0, 0,   75,  75),
  ('gonzalez.maria', 'Sep-P1', 66,  88,  66,  0,   0, 0,   75,  75),
  ('gonzalez.maria', 'Sep-P2', 60,  80,  60,  0,   0, 0,   75,  75),
  ('gonzalez.maria', 'Oct-P1', 66,  88,  66,  0,   0, 0,   75,  75),
-- lopez.nicolas — effective 100%, roll-off Aug 31 → Sep-P1+ sin chg
  ('lopez.nicolas', 'Jul-P2', 80,  80,  80,  0,   0, 0,  100, 100),
  ('lopez.nicolas', 'Ago-P1', 88,  88,  88,  0,   0, 0,  100, 100),
  ('lopez.nicolas', 'Ago-P2', 80,  80,  80,  0,   0, 0,  100, 100),
  ('lopez.nicolas', 'Sep-P1',  0,  88,   0,  0,   0, 0,    0,   0),
  ('lopez.nicolas', 'Sep-P2',  0,  80,   0,  0,   0, 0,    0,   0),
  ('lopez.nicolas', 'Oct-P1',  0,  88,   0,  0,   0, 0,    0,   0),
-- romero.florencia — assumption 100%, Ago-P1=PTO (sah=0, absence_hours=88)
  ('romero.florencia', 'Jul-P2', 80,  80, 0, 80,  0,  0,   0, 100),
  ('romero.florencia', 'Ago-P1',  0,   0, 0,  0,  0, 88,   0,   0),
  ('romero.florencia', 'Ago-P2', 80,  80, 0, 80,  0,  0,   0, 100),
  ('romero.florencia', 'Sep-P1', 88,  88, 0, 88,  0,  0,   0, 100),
  ('romero.florencia', 'Sep-P2', 80,  80, 0, 80,  0,  0,   0, 100),
  ('romero.florencia', 'Oct-P1', 88,  88, 0, 88,  0,  0,   0, 100),
-- hernandez.pablo — effective 100%, Jul-P2 sick 2 días (sah=64, absence_hours=16)
  ('hernandez.pablo', 'Jul-P2', 64,  64,  64, 0,  0, 16,  100, 100),
  ('hernandez.pablo', 'Ago-P1', 88,  88,  88, 0,  0,  0,  100, 100),
  ('hernandez.pablo', 'Ago-P2', 80,  80,  80, 0,  0,  0,  100, 100),
  ('hernandez.pablo', 'Sep-P1', 88,  88,  88, 0,  0,  0,  100, 100),
  ('hernandez.pablo', 'Sep-P2', 80,  80,  80, 0,  0,  0,  100, 100),
  ('hernandez.pablo', 'Oct-P1', 88,  88,  88, 0,  0,  0,  100, 100),
-- vargas.camila — effective 100% (Mexico)
  ('vargas.camila', 'Jul-P2', 80, 80, 80, 0,  0, 0,  100, 100),
  ('vargas.camila', 'Ago-P1', 88, 88, 88, 0,  0, 0,  100, 100),
  ('vargas.camila', 'Ago-P2', 80, 80, 80, 0,  0, 0,  100, 100),
  ('vargas.camila', 'Sep-P1', 88, 88, 88, 0,  0, 0,  100, 100),
  ('vargas.camila', 'Sep-P2', 80, 80, 80, 0,  0, 0,  100, 100),
  ('vargas.camila', 'Oct-P1', 88, 88, 88, 0,  0, 0,  100, 100),
-- gutierrez.jose — effective 100% (Costa Rica)
  ('gutierrez.jose', 'Jul-P2', 80, 80, 80, 0, 0, 0,  100, 100),
  ('gutierrez.jose', 'Ago-P1', 88, 88, 88, 0, 0, 0,  100, 100),
  ('gutierrez.jose', 'Ago-P2', 80, 80, 80, 0, 0, 0,  100, 100),
  ('gutierrez.jose', 'Sep-P1', 88, 88, 88, 0, 0, 0,  100, 100),
  ('gutierrez.jose', 'Sep-P2', 80, 80, 80, 0, 0, 0,  100, 100),
  ('gutierrez.jose', 'Oct-P1', 88, 88, 88, 0, 0, 0,  100, 100),
-- torres.martin — effective 100%, FTE 0.5 → SAH×0.5
  ('torres.martin', 'Jul-P2', 40, 40, 40, 0, 0, 0,  100, 100),
  ('torres.martin', 'Ago-P1', 44, 44, 44, 0, 0, 0,  100, 100),
  ('torres.martin', 'Ago-P2', 40, 40, 40, 0, 0, 0,  100, 100),
  ('torres.martin', 'Sep-P1', 44, 44, 44, 0, 0, 0,  100, 100),
  ('torres.martin', 'Sep-P2', 40, 40, 40, 0, 0, 0,  100, 100),
  ('torres.martin', 'Oct-P1', 44, 44, 44, 0, 0, 0,  100, 100);


-- ============================================================
-- SECCIÓN 7 — ABSENCES
-- ============================================================

INSERT INTO absences (eid, date, type) VALUES
  -- hernandez.pablo: sick Jul 22-23 (miércoles y jueves)
  ('hernandez.pablo', '2026-07-22', 'SICK'),
  ('hernandez.pablo', '2026-07-23', 'SICK'),
  -- romero.florencia: PTO Ago 3-14 (días hábiles, excluyendo fines de semana)
  ('romero.florencia', '2026-08-03', 'PTO'),
  ('romero.florencia', '2026-08-04', 'PTO'),
  ('romero.florencia', '2026-08-05', 'PTO'),
  ('romero.florencia', '2026-08-06', 'PTO'),
  ('romero.florencia', '2026-08-07', 'PTO'),
  ('romero.florencia', '2026-08-10', 'PTO'),
  ('romero.florencia', '2026-08-11', 'PTO'),
  ('romero.florencia', '2026-08-12', 'PTO'),
  ('romero.florencia', '2026-08-13', 'PTO'),
  ('romero.florencia', '2026-08-14', 'PTO');


-- ============================================================
-- SECCIÓN 8 — TICKETS (10 registros)
-- ============================================================

INSERT INTO tickets
  (type, eid, status, date, created_by, nj_name, start_date, end_date,
   cl, location, people_lead, client_name, offering_type, chargeability_pct,
   scenario_type, effectivization_date, rejection_reason, te_approver)
VALUES
-- NJ — Valentina Lopez — Approved
  ('nj',      'nj.valentina.lopez', 'Approved', '2026-07-01', 'garcia.sofia',
   'Valentina Lopez', '2026-08-01', NULL,
   8, 'AR', 'garcia.sofia', NULL, NULL, NULL,
   'assumption', NULL, NULL, 'garcia.sofia'),
-- newproj — garcia.sofia / Google — Approved (effective)
  ('newproj', 'garcia.sofia',       'Approved', '2026-01-02', 'garcia.sofia',
   NULL, '2026-01-01', '2026-12-31',
   NULL, NULL, NULL, 'Google', 'Tech-led', 100,
   'effective', NULL, NULL, NULL),
-- newproj — rodriguez.ana / Globant — Approved (effective)
  ('newproj', 'rodriguez.ana',      'Approved', '2026-03-01', 'garcia.sofia',
   NULL, '2026-03-01', '2026-12-31',
   NULL, NULL, NULL, 'Globant', 'Tech-led', 50,
   'effective', NULL, NULL, NULL),
-- newproj — martinez.lucas / Mercado Libre — Approved (assumption)
  ('newproj', 'martinez.lucas',     'Approved', '2026-06-01', 'garcia.sofia',
   NULL, '2026-06-01', '2026-12-31',
   NULL, NULL, NULL, 'Mercado Libre', 'CTO', 100,
   'assumption', '2026-12-31', NULL, 'garcia.sofia'),
-- newproj — gonzalez.maria / DHL — Open (effective)
  ('newproj', 'gonzalez.maria',     'Open',     '2026-07-15', 'garcia.sofia',
   NULL, '2026-04-01', '2026-12-31',
   NULL, NULL, NULL, 'DHL', 'Cost Take Out', 75,
   'effective', NULL, NULL, 'garcia.sofia'),
-- pto — romero.florencia — Approved
  ('pto',     'romero.florencia',   'Approved', '2026-07-20', 'garcia.sofia',
   NULL, '2026-08-01', '2026-08-15',
   NULL, NULL, NULL, NULL, NULL, NULL,
   'assumption', NULL, NULL, NULL),
-- sick — hernandez.pablo — Approved
  ('sick',    'hernandez.pablo',    'Approved', '2026-07-22', 'garcia.sofia',
   NULL, '2026-07-22', '2026-07-23',
   NULL, NULL, NULL, NULL, NULL, NULL,
   'assumption', NULL, NULL, NULL),
-- baja — blanco.andrea — Approved
  ('baja',    'blanco.andrea',      'Approved', '2026-06-30', 'garcia.sofia',
   NULL, NULL, '2026-07-01',
   NULL, NULL, NULL, NULL, NULL, NULL,
   'assumption', NULL, NULL, NULL),
-- ongoing — rodriguez.ana — Open (60%, effective)
  ('ongoing', 'rodriguez.ana',      'Open',     '2026-07-24', 'garcia.sofia',
   NULL, NULL, '2026-12-31',
   NULL, NULL, NULL, NULL, NULL, 60,
   'effective', NULL, NULL, 'garcia.sofia'),
-- newproj — perez.carlos / BBVA — Rejected
  ('newproj', 'perez.carlos',       'Rejected', '2026-07-10', 'garcia.sofia',
   NULL, '2026-08-01', '2026-12-31',
   NULL, NULL, NULL, 'BBVA', 'Tech-led', 100,
   'effective', NULL, 'No hay demanda', NULL);


-- ============================================================
-- SECCIÓN 9 — CLIENT_CATALOG
-- ============================================================

INSERT INTO client_catalog (name) VALUES
  ('Google'),
  ('Mercado Libre'),
  ('Globant'),
  ('DHL'),
  ('Accenture Internal'),
  ('Cabify'),
  ('Nuvei'),
  ('Aeromexico'),
  ('BAC'),
  ('BPO Latam'),
  ('BBVA')
ON CONFLICT (name) DO NOTHING;


-- ============================================================
-- VERIFICACIÓN POST-CARGA
-- ============================================================
-- SELECT COUNT(*) FROM employees WHERE active = TRUE;      -- debe ser 14
-- SELECT COUNT(*) FROM chargeability_blocks;               -- debe ser ~65
-- SELECT COUNT(*) FROM forecast_periods;                   -- debe ser ~89 filas
-- SELECT COUNT(*) FROM tickets;                            -- debe ser 10
-- SELECT COUNT(*) FROM absences;                           -- debe ser 12 (2 sick + 10 PTO)
-- SELECT eid, period_name, chg_pct_hl, chg_pct_sl FROM forecast_periods
--   WHERE period_name = 'Jul-P2' ORDER BY eid;
