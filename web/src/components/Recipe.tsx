import type { Review } from "../lib/types";

function Section({ title, items, open }: { title: string; items: string[]; open?: boolean }) {
  if (!items?.length) return null;
  return (
    <details className="card" open={open}>
      <summary style={{ cursor: "pointer", fontWeight: 700 }}>{title}</summary>
      <ul style={{ margin: "8px 0 0", paddingLeft: 18 }}>{items.map((t, i) => <li key={i}>{t}</li>)}</ul>
    </details>
  );
}

/** The producer's recipe for the whole reel, before the take-by-take shot list. */
export function Recipe({ review }: { review: Review }) {
  const m = review.measured;
  const r = m?.rhythm;
  const facts = [
    `${m.duration_s.toFixed(0)}s`, `${m.cuts} cuts`, `${m.average_cut_s.toFixed(1)}s per cut`,
    r?.bpm ? `${Math.round(r.bpm)} BPM` : null,
    r?.beat_sync_pct != null ? `${r.beat_sync_pct}% of cuts on the beat` : null,
    review.difficulty, review.time_estimate,
  ].filter(Boolean) as string[];
  return (
    <section className="stack" aria-label="Recipe">
      <div className="card stack" style={{ gap: 8 }}>
        <span className="small muted">THE RECIPE</span>
        <h2 style={{ margin: 0 }}>{review.title}</h2>
        <div className="chips">{facts.map((f) => <span key={f} className="badge" style={{ textTransform: "none" }}>{f}</span>)}</div>
        <p style={{ margin: 0 }}>{review.concept}</p>
        <p style={{ margin: 0 }}><strong>Hook:</strong> {review.hook}</p>
      </div>
      {review.structure?.length > 0 && (
        <details className="card" open>
          <summary style={{ cursor: "pointer", fontWeight: 700 }}>Structure</summary>
          <ol style={{ margin: "8px 0 0", paddingLeft: 0, listStyle: "none" }} className="stack">
            {review.structure.map((p, i) => (
              <li key={i} className="row" style={{ alignItems: "flex-start", gap: 10 }}>
                <span className="badge" style={{ flexShrink: 0 }}>{p.start_s.toFixed(1)}–{p.end_s.toFixed(1)}s</span>
                <span><strong style={{ textTransform: "capitalize" }}>{p.part}:</strong> {p.what_happens}</span>
              </li>
            ))}
          </ol>
        </details>
      )}
      <Section title="Look: grade, light, styling" items={review.look} />
      <Section title="Camera settings" items={review.camera} />
      <Section title="Edit recipe: pacing, transitions, effects" items={review.edit} />
      <Section title="Music & rhythm" items={review.music} />
      <Section title="Gear to bring" items={review.gear} />
      <Section title="Shoot-day plan" items={review.shoot_schedule} open />
      <Section title="Producer tips" items={review.producer_tips} />
    </section>
  );
}
