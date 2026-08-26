-- Client catalog for ForecastOS
-- Run in Supabase SQL Editor after schema_auth.sql

CREATE TABLE IF NOT EXISTS client_catalog (
    id         SERIAL PRIMARY KEY,
    name       VARCHAR(255) UNIQUE NOT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- Seed with existing clients already in forecast_update
INSERT INTO client_catalog (name)
SELECT DISTINCT client
FROM forecast_update
WHERE client IS NOT NULL AND client != ''
ON CONFLICT (name) DO NOTHING;
