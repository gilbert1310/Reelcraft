-- ReelCraft schema: Stage 1 (breakdown), Stage 2 (guided shooting + assembly),
-- Stage 3 (style library + suggestions). Every user-owned row carries user_id so
-- row-level security is a simple "user_id = auth.uid()". Edge functions write with the
-- service role (which bypasses RLS) after checking ownership.

create extension if not exists vector with schema extensions;

-- ---------- helpers ----------
create or replace function public.reelcraft_touch() returns trigger
language plpgsql set search_path = '' as $$
begin new.updated_at = now(); return new; end $$;

-- ---------- jobs (one row per call to the video service) ----------
create table public.jobs (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users on delete cascade,
  kind text not null check (kind in ('analysis','check','suggest_setup','assemble','suggest','revise','export')),
  ref_id uuid not null,
  status text not null default 'processing' check (status in ('processing','done','failed','link_failed')),
  meta jsonb not null default '{}'::jsonb,   -- storage paths the job writes to
  error text,
  seconds numeric,
  created_at timestamptz not null default now(),
  finished_at timestamptz
);
create index on public.jobs (ref_id);

-- ---------- Stage 1 ----------
create table public.uploads (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null default auth.uid() references auth.users on delete cascade,
  file_name text,
  storage_path text,              -- reels/<user_id>/<file>; null for links
  source_url text,                -- Instagram / TikTok / Facebook / YouTube link
  status text not null default 'queued'
    check (status in ('queued','processing','done','failed','link_failed')),
  error text,
  duration numeric, width int, height int, fps numeric,
  cut_count int, setup_count int,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint uploads_has_source check (storage_path is not null or source_url is not null)
);
create trigger uploads_touch before update on public.uploads for each row execute function public.reelcraft_touch();

create table public.setups (
  id uuid primary key default gen_random_uuid(),
  upload_id uuid not null references public.uploads on delete cascade,
  user_id uuid not null references auth.users on delete cascade,
  setup_number int not null,
  description text, angle text, subject text, motion text,
  film_duration_seconds numeric, beginner_tip text,
  thumbnail_path text,
  embedding extensions.vector(512),
  unique (upload_id, setup_number)
);

create table public.cuts (
  id uuid primary key default gen_random_uuid(),
  upload_id uuid not null references public.uploads on delete cascade,
  setup_id uuid not null references public.setups on delete cascade,
  user_id uuid not null references auth.users on delete cascade,
  setup_number int not null,
  order_index int not null,
  start_time numeric not null, end_time numeric not null,
  transition_in text not null default 'cut',
  speed numeric not null default 1.0,   -- 1.0 = normal, 0.5 = slow motion
  action text,
  stats jsonb,                           -- camera motion, framing, colour (match engine)
  thumbnail_path text,
  embedding extensions.vector(512),
  unique (upload_id, order_index)
);
create index on public.cuts (setup_id);

-- ---------- Stage 2 ----------
create table public.projects (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null default auth.uid() references auth.users on delete cascade,
  upload_id uuid not null references public.uploads on delete cascade,   -- the reference reel
  title text,
  status text not null default 'shooting'
    check (status in ('shooting','assembling','done','failed')),
  preview_path text, timeline_path text, otio_path text,
  overall_score int,
  reshoots jsonb not null default '[]'::jsonb,
  edit_list jsonb,
  error text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
create trigger projects_touch before update on public.projects for each row execute function public.reelcraft_touch();

create table public.captures (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null default auth.uid() references auth.users on delete cascade,
  project_id uuid not null references public.projects on delete cascade,
  setup_id uuid references public.setups on delete set null,   -- null until assigned (imports)
  suggested_setup_id uuid references public.setups on delete set null,
  storage_path text not null,           -- captures/<user_id>/<project_id>/<file>
  file_name text,                       -- original camera file name (for relinking in Resolve)
  source text not null default 'phone' check (source in ('phone','import')),
  check_status text not null default 'pending'
    check (check_status in ('pending','checking','pass','retry','failed')),
  check_feedback text,
  coverage jsonb,                        -- per reference cut: covered / weak / missing + reason
  measurements jsonb,
  ai_checked boolean not null default false,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
create trigger captures_touch before update on public.captures for each row execute function public.reelcraft_touch();
create index on public.captures (project_id);

create table public.wrap_checks (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users on delete cascade,
  project_id uuid not null references public.projects on delete cascade,
  missing jsonb not null default '[]'::jsonb,
  unchecked_count int not null default 0,
  created_at timestamptz not null default now()
);

create table public.cut_matches (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users on delete cascade,
  project_id uuid not null references public.projects on delete cascade,
  cut_id uuid references public.cuts on delete cascade,
  order_index int not null,
  score int, fixes jsonb, flags jsonb,
  clip text, source_in numeric,
  thumbnail_path text
);
create index on public.cut_matches (project_id);

-- ---------- Stage 3 ----------
create table public.recipes (
  id uuid primary key default gen_random_uuid(),
  user_id uuid references auth.users on delete set null,     -- who contributed it
  source_upload_id uuid references public.uploads on delete set null,
  style_name text, genre text, moods text[] not null default '{}', summary text,
  duration numeric, cut_count int, setup_count int,
  pacing jsonb, transitions jsonb, slowmo_beats jsonb, setup_pattern jsonb, setup_types jsonb,
  embedding extensions.vector(512),
  is_public boolean not null default true,
  created_at timestamptz not null default now()
);
create index on public.recipes using gin (moods);

create table public.raw_projects (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null default auth.uid() references auth.users on delete cascade,
  title text,
  mood text,
  status text not null default 'uploading'
    check (status in ('uploading','processing','ready','revising','exporting','exported','failed')),
  labels jsonb,
  suggestions jsonb,               -- [{recipe_id, style_name, moods, plan, preview_path}]
  chosen_index int,
  chosen_recipe_id uuid references public.recipes on delete set null,
  plan jsonb,                      -- the current cut plan
  preview_path text,               -- preview of the current plan
  revisions jsonb not null default '[]'::jsonb,   -- [{instruction, preview_path, at}]
  final_path text, timeline_path text, otio_path text,
  error text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
create trigger raw_projects_touch before update on public.raw_projects for each row execute function public.reelcraft_touch();

create table public.raw_clips (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null default auth.uid() references auth.users on delete cascade,
  raw_project_id uuid not null references public.raw_projects on delete cascade,
  storage_path text not null,          -- raw/<user_id>/<raw_project_id>/<file>
  file_name text,
  labels jsonb,
  duration numeric,
  created_at timestamptz not null default now()
);
create index on public.raw_clips (raw_project_id);

-- Similar styles for the Style Library page.
create or replace function public.match_recipes(query_embedding extensions.vector(512), match_count int default 10,
                                                mood text default null)
returns table (id uuid, style_name text, genre text, moods text[], summary text, similarity float)
language sql stable security invoker set search_path = '' as $$
  select r.id, r.style_name, r.genre, r.moods, r.summary,
         1 - (r.embedding operator(extensions.<=>) query_embedding) as similarity
  from public.recipes r
  where r.embedding is not null and (mood is null or mood = any(r.moods))
  order by r.embedding operator(extensions.<=>) query_embedding
  limit match_count;
$$;

-- ---------- Row-level security ----------
do $$
declare t text;
begin
  foreach t in array array['jobs','uploads','setups','cuts','projects','captures','wrap_checks',
                           'cut_matches','raw_projects','raw_clips'] loop
    execute format('alter table public.%I enable row level security', t);
    execute format('create policy "own rows: select" on public.%I for select to authenticated using ((select auth.uid()) = user_id)', t);
  end loop;
  -- Tables the app writes to directly (everything else is written by edge functions).
  foreach t in array array['uploads','projects','captures','raw_projects','raw_clips'] loop
    execute format('create policy "own rows: insert" on public.%I for insert to authenticated with check ((select auth.uid()) = user_id)', t);
    execute format('create policy "own rows: update" on public.%I for update to authenticated using ((select auth.uid()) = user_id) with check ((select auth.uid()) = user_id)', t);
    execute format('create policy "own rows: delete" on public.%I for delete to authenticated using ((select auth.uid()) = user_id)', t);
  end loop;
end $$;

alter table public.recipes enable row level security;
create policy "library: read public or own" on public.recipes for select to authenticated
  using (is_public or (select auth.uid()) = user_id);

-- Status columns are owned by the server: users may not flip them from the client.
create or replace function public.reelcraft_guard_status() returns trigger
language plpgsql set search_path = '' as $$
begin
  if (select auth.role()) = 'authenticated' then
    if tg_table_name = 'uploads' then
      new.status = old.status; new.error = old.error; new.cut_count = old.cut_count; new.setup_count = old.setup_count;
    elsif tg_table_name = 'projects' then
      new.status = old.status; new.preview_path = old.preview_path; new.timeline_path = old.timeline_path;
      new.otio_path = old.otio_path; new.overall_score = old.overall_score; new.reshoots = old.reshoots;
    elsif tg_table_name = 'captures' then
      new.check_status = old.check_status; new.check_feedback = old.check_feedback;
      new.coverage = old.coverage; new.ai_checked = old.ai_checked; new.suggested_setup_id = old.suggested_setup_id;
    elsif tg_table_name = 'raw_projects' then
      new.status = old.status; new.suggestions = old.suggestions; new.plan = old.plan;
      new.preview_path = old.preview_path; new.final_path = old.final_path; new.timeline_path = old.timeline_path;
      new.otio_path = old.otio_path; new.revisions = old.revisions; new.labels = old.labels;
    end if;
  end if;
  return new;
end $$;
create trigger uploads_guard before update on public.uploads for each row execute function public.reelcraft_guard_status();
create trigger projects_guard before update on public.projects for each row execute function public.reelcraft_guard_status();
create trigger captures_guard before update on public.captures for each row execute function public.reelcraft_guard_status();
create trigger raw_projects_guard before update on public.raw_projects for each row execute function public.reelcraft_guard_status();

-- ---------- Storage ----------
insert into storage.buckets (id, name, public, file_size_limit)
values ('reels', 'reels', false, 524288000),
       ('captures', 'captures', false, 2147483648),
       ('raw', 'raw', false, 2147483648),
       ('renders', 'renders', false, 1073741824),
       ('thumbs', 'thumbs', false, 5242880)
on conflict (id) do nothing;

-- Files live under <user_id>/...; users can only touch their own folder.
create policy "reelcraft: read own files" on storage.objects for select to authenticated
  using (bucket_id in ('reels','captures','raw','renders','thumbs')
         and (storage.foldername(name))[1] = (select auth.uid())::text);
create policy "reelcraft: upload own files" on storage.objects for insert to authenticated
  with check (bucket_id in ('reels','captures','raw')
              and (storage.foldername(name))[1] = (select auth.uid())::text);
create policy "reelcraft: replace own files" on storage.objects for update to authenticated
  using (bucket_id in ('reels','captures','raw') and (storage.foldername(name))[1] = (select auth.uid())::text);
create policy "reelcraft: delete own files" on storage.objects for delete to authenticated
  using (bucket_id in ('reels','captures','raw','renders') and (storage.foldername(name))[1] = (select auth.uid())::text);

-- ---------- Realtime (the app can subscribe instead of polling) ----------
alter publication supabase_realtime add table public.uploads, public.captures, public.projects, public.raw_projects;
