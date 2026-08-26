-- Holidays table + seed 2026
-- Correr en Supabase SQL Editor

CREATE TABLE IF NOT EXISTS holidays (
  id      SERIAL PRIMARY KEY,
  country TEXT   NOT NULL,
  date    DATE   NOT NULL,
  name    TEXT   NOT NULL,
  UNIQUE (country, date)
);

-- Argentina 2026
INSERT INTO holidays (country, date, name) VALUES
('AR', '2026-01-01', 'Año Nuevo'),
('AR', '2026-02-16', 'Carnaval'),
('AR', '2026-02-17', 'Carnaval'),
('AR', '2026-03-24', 'Día Nacional de la Memoria'),
('AR', '2026-04-02', 'Veteranos de Malvinas'),
('AR', '2026-04-03', 'Viernes Santo'),
('AR', '2026-05-01', 'Día del Trabajador'),
('AR', '2026-05-25', 'Revolución de Mayo'),
('AR', '2026-06-17', 'Paso a la Inmortalidad del Gral. Güemes'),
('AR', '2026-06-20', 'Día de la Bandera'),
('AR', '2026-07-09', 'Día de la Independencia'),
('AR', '2026-08-17', 'Paso a la Inmortalidad del Gral. San Martín'),
('AR', '2026-10-19', 'Día del Respeto a la Diversidad Cultural'),
('AR', '2026-11-23', 'Día de la Soberanía Nacional'),
('AR', '2026-12-08', 'Inmaculada Concepción de María'),
('AR', '2026-12-25', 'Navidad')
ON CONFLICT (country, date) DO NOTHING;

-- México 2026
INSERT INTO holidays (country, date, name) VALUES
('MX', '2026-01-01', 'Año Nuevo'),
('MX', '2026-02-02', 'Día de la Constitución'),
('MX', '2026-03-16', 'Natalicio de Benito Juárez'),
('MX', '2026-04-03', 'Viernes Santo'),
('MX', '2026-05-01', 'Día del Trabajo'),
('MX', '2026-09-16', 'Día de la Independencia'),
('MX', '2026-11-16', 'Día de la Revolución'),
('MX', '2026-12-25', 'Navidad')
ON CONFLICT (country, date) DO NOTHING;

-- Costa Rica 2026
INSERT INTO holidays (country, date, name) VALUES
('CR', '2026-01-01', 'Año Nuevo'),
('CR', '2026-04-02', 'Jueves Santo'),
('CR', '2026-04-03', 'Viernes Santo'),
('CR', '2026-04-11', 'Día de Juan Santamaría'),
('CR', '2026-05-01', 'Día del Trabajo'),
('CR', '2026-07-25', 'Anexión de Guanacaste'),
('CR', '2026-08-02', 'Virgen de los Ángeles'),
('CR', '2026-08-15', 'Día de la Madre'),
('CR', '2026-09-15', 'Día de la Independencia'),
('CR', '2026-10-12', 'Día de las Culturas'),
('CR', '2026-12-25', 'Navidad')
ON CONFLICT (country, date) DO NOTHING;
