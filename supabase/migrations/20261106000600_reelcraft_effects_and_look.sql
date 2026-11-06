-- Edit effects detected on each reference cut, and the reel's overall colour look,
-- so the platform's renderer can reproduce them on the videographer's footage.
alter table public.cuts add column if not exists effects jsonb not null default '[]'::jsonb;
alter table public.uploads add column if not exists look text;
