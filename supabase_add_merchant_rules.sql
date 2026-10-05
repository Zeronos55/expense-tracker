-- Run this once in the Supabase SQL Editor (Project -> SQL Editor -> New query)
-- Stores learned merchant -> category rules used by auto-categorisation.
-- source is 'user' (your corrections, highest priority) or 'llm' (cached
-- Gemini result).

create table if not exists merchant_rules (
    merchant_key text primary key,
    merchant     text,
    category     text not null,
    source       text not null default 'user',
    updated_at   timestamptz not null default now()
);
