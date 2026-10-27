-- Run this once in the Supabase SQL Editor (Project -> SQL Editor -> New query)
-- Stores learned category keywords (e.g. CATERING -> Food & Dining) that you
-- review on the Rules page. status is 'suggested' (not used yet), 'approved'
-- (applied to every merchant containing the word) or 'rejected' (never
-- suggested again). source is 'llm' (Gemini pointed it out) or 'data' (mined
-- from merchants you already categorised).

create table if not exists keyword_rules (
    keyword          text primary key,
    category         text not null,
    status           text not null default 'suggested'
                     check (status in ('suggested', 'approved', 'rejected')),
    source           text not null default 'data',
    example_merchant text,
    updated_at       timestamptz not null default now()
);
