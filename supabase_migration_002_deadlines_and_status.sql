-- =============================================================================
-- Floating Kanban Widget -- Migration 002
-- Adds: optional task deadlines, and a completion ("checklist") flag on cards.
--
-- Run this AFTER supabase_setup.sql, once, in the Supabase SQL Editor.
-- It only ADDS columns -- existing rows and RLS policies are untouched, so
-- this is safe to run on a project that's already in use.
-- =============================================================================

alter table public.cards
    add column if not exists deadline  timestamptz,
    add column if not exists is_done   boolean not null default false;

-- Speeds up the "what's overdue / due soon" queries the Analytics panel runs.
create index if not exists idx_cards_deadline on public.cards(deadline)
    where deadline is not null;

create index if not exists idx_cards_is_done on public.cards(is_done);

-- No RLS changes needed: the existing "Users access own cards" policy on
-- public.cards already covers these two new columns, since Postgres RLS
-- policies apply at the row level, not per-column.

-- No changes to the Realtime publication needed either -- public.cards is
-- already broadcasting, so updates to deadline/is_done propagate for free.
