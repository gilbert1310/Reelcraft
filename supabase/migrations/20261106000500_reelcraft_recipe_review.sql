-- The producer's recipe for the whole reel (concept, hook, structure, look, camera, edit,
-- music & rhythm, gear, shoot-day plan, tips) plus the measured edit data.
alter table public.uploads add column if not exists review jsonb;
