import { useMemo, useState } from "react";
import { supabase } from "../lib/supabase";
import { must } from "../lib/api";
import { useLoad } from "../lib/hooks";
import type { Recipe } from "../lib/types";
import { Busy, ErrorBox, Page } from "../components/ui";

export default function Library() {
  const { data, error } = useLoad(async () =>
    must(await supabase.from("recipes").select("id,style_name,genre,moods,summary,duration,cut_count,setup_count")
      .order("created_at", { ascending: false }).limit(200)) as Recipe[], []);
  const [mood, setMood] = useState<string | null>(null);
  const [genre, setGenre] = useState<string | null>(null);
  const moods = useMemo(() => [...new Set((data ?? []).flatMap((r) => r.moods))].sort(), [data]);
  const genres = useMemo(() => [...new Set((data ?? []).map((r) => r.genre).filter(Boolean) as string[])].sort(), [data]);
  const shown = (data ?? []).filter((r) => (!mood || r.moods.includes(mood)) && (!genre || r.genre === genre));

  return (
    <Page back="/" title="Style library">
      <p className="muted">Editing styles learned from every reel broken down on ReelCraft. Patterns only, never footage.</p>
      <ErrorBox>{error}</ErrorBox>
      {!data ? <Busy title="Loading…" /> : (
        <div className="stack">
          {genres.length > 0 && <div className="chips">
            {genres.map((g) => <button key={g} className="chip" aria-pressed={genre === g} onClick={() => setGenre(genre === g ? null : g)}>{g}</button>)}
          </div>}
          {moods.length > 0 && <div className="chips">
            {moods.map((m) => <button key={m} className="chip" aria-pressed={mood === m} onClick={() => setMood(mood === m ? null : m)}>{m}</button>)}
          </div>}
          {shown.length === 0 && <p className="muted">No styles yet. Break down a reel to add the first one.</p>}
          {shown.map((r) => (
            <article key={r.id} className="card stack" style={{ gap: 6 }}>
              <div className="row" style={{ gap: 8 }}><strong className="grow">{r.style_name || "Untitled style"}</strong>
                {r.genre && <span className="badge accent">{r.genre}</span>}</div>
              <p className="small" style={{ margin: 0 }}>{r.summary}</p>
              <div className="chips">{r.moods.map((m) => <span key={m} className="badge">{m}</span>)}</div>
              <span className="muted small">{r.cut_count} cuts · {r.setup_count} setups · {Math.round(Number(r.duration))}s</span>
            </article>
          ))}
        </div>
      )}
    </Page>
  );
}
