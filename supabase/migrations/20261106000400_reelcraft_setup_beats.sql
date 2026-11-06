-- Directions to call out during a take, each linked to the reference cuts that show it:
-- [{ "text": "...", "cuts": [0, 15] }]  (cut order_index values)
alter table public.setups add column if not exists beats jsonb not null default '[]'::jsonb;
