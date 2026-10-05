-- Add ringfenced flag to employees table
-- Run once in Supabase SQL Editor

ALTER TABLE employees
ADD COLUMN IF NOT EXISTS ringfenced BOOLEAN NOT NULL DEFAULT FALSE;
