-- Run this once in the Supabase SQL Editor (Project -> SQL Editor -> New query)
-- Adds the free-text "details" field used by the app's add/edit forms and the
-- /upload-from-shortcut endpoint.

alter table expenses add column if not exists details text;
