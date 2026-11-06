-- The shot planner's plain-language answer, e.g. "Filmed as 3 takes: ...".
alter table public.uploads add column if not exists plan_summary text;
