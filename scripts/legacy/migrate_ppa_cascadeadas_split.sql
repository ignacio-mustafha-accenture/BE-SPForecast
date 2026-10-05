-- Split chg_cascadeadas into HL and SL components in forecast_periods
ALTER TABLE forecast_periods
  ADD COLUMN IF NOT EXISTS chg_cascadeadas_hl NUMERIC DEFAULT 0,
  ADD COLUMN IF NOT EXISTS chg_cascadeadas_sl NUMERIC DEFAULT 0;

-- Historical data: all pre-existing cascadeadas are assumed HL (chargeable)
UPDATE forecast_periods
SET chg_cascadeadas_hl = COALESCE(chg_cascadeadas, 0),
    chg_cascadeadas_sl = 0
WHERE COALESCE(chg_cascadeadas, 0) <> 0;

-- Add reverse lifecycle columns to ppa_log
ALTER TABLE ppa_log
  ADD COLUMN IF NOT EXISTS reversed_at  TIMESTAMP,
  ADD COLUMN IF NOT EXISTS reversed_by  VARCHAR(255);

-- Expand status constraint to allow 'reversed'
ALTER TABLE ppa_log DROP CONSTRAINT IF EXISTS ppa_log_status_check;
ALTER TABLE ppa_log ADD CONSTRAINT ppa_log_status_check
  CHECK (status IN ('pending', 'approved', 'rejected', 'reversed'));
