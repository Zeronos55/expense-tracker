-- Run this once in the Supabase SQL Editor (Project -> SQL Editor -> New query).
-- Keeps the OCR text each Shortcut upload came from, so entries the parser
-- got wrong can be re-read later (History -> Needs review -> Auto-categorise
-- again) and shown on the edit page.
alter table expenses add column if not exists raw_text text;
