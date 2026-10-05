-- Migration: add SAH PPA adjustment support
-- Adds sah_ppa_adj to forecast_periods (cumulative approved SAH deltas per employee/period)
-- Adds hours_sah to ppa_log and tickets (hours of SAH to move)
-- Adds sah_ppa to employee_daily_hours (per-day SAH PPA delta, separate from calendar sah)

ALTER TABLE forecast_periods
    ADD COLUMN IF NOT EXISTS sah_ppa_adj NUMERIC DEFAULT 0;

ALTER TABLE ppa_log
    ADD COLUMN IF NOT EXISTS hours_sah INTEGER;

ALTER TABLE tickets
    ADD COLUMN IF NOT EXISTS hours_sah INTEGER;

ALTER TABLE employee_daily_hours
    ADD COLUMN IF NOT EXISTS sah_ppa NUMERIC DEFAULT 0;
