-- Logo/end cards and title cards: part of the edit pattern, but nothing to film.
alter table public.setups add column if not exists is_graphic boolean not null default false;
