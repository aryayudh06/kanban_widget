-- =============================================================================
-- Floating Kanban Widget -- Supabase schema, RLS policies, and Realtime setup
-- Run this once in the Supabase SQL Editor (Project -> SQL Editor -> New query)
-- =============================================================================

-- ---------- Tables ----------------------------------------------------------

create table if not exists public.boards (
    id          uuid primary key default gen_random_uuid(),
    user_id     uuid not null references auth.users(id) on delete cascade,
    title       text not null,
    created_at  timestamptz default now()
);

create table if not exists public.columns (
    id          uuid primary key default gen_random_uuid(),
    board_id    uuid not null references public.boards(id) on delete cascade,
    user_id     uuid not null references auth.users(id) on delete cascade,
    title       text not null,
    position    int not null default 0,
    created_at  timestamptz default now()
);

create table if not exists public.cards (
    id           uuid primary key default gen_random_uuid(),
    column_id    uuid not null references public.columns(id) on delete cascade,
    user_id      uuid not null references auth.users(id) on delete cascade,
    title        text not null,
    description  text,
    color_label  text default '#313244',
    position     int not null default 0,
    updated_at   timestamptz default now()
);

-- Helpful indexes for the queries the client makes most often
create index if not exists idx_columns_board_id on public.columns(board_id);
create index if not exists idx_cards_column_id on public.cards(column_id);

-- ---------- Row Level Security ----------------------------------------------
-- Every table is locked down so a row is only visible/writable by its owner.
-- This is the actual security boundary -- the anon key alone grants nothing.

alter table public.boards  enable row level security;
alter table public.columns enable row level security;
alter table public.cards   enable row level security;

drop policy if exists "Users access own boards" on public.boards;
create policy "Users access own boards"
    on public.boards
    for all
    using (auth.uid() = user_id)
    with check (auth.uid() = user_id);

drop policy if exists "Users access own columns" on public.columns;
create policy "Users access own columns"
    on public.columns
    for all
    using (auth.uid() = user_id)
    with check (auth.uid() = user_id);

drop policy if exists "Users access own cards" on public.cards;
create policy "Users access own cards"
    on public.cards
    for all
    using (auth.uid() = user_id)
    with check (auth.uid() = user_id);

-- ---------- Realtime ----------------------------------------------------------
-- Broadcast row-level INSERT/UPDATE/DELETE events so every signed-in device
-- gets pushed changes instantly over a WebSocket channel.

alter publication supabase_realtime add table public.columns;
alter publication supabase_realtime add table public.cards;
alter publication supabase_realtime add table public.boards;

-- ---------- Optional: seed a default board for a new user --------------------
-- You can call this from the client after first sign-up, or leave it manual.
-- insert into public.boards (user_id, title) values (auth.uid(), 'My Board');
