import { useState, type FormEvent } from "react";
import { useParams } from "react-router-dom";
import { supabase } from "../lib/supabase";
import { action, must } from "../lib/api";
import { useLive, useLoad } from "../lib/hooks";
import type { RawProject } from "../lib/types";
import { Busy, DownloadButton, ErrorBox, Page, Player } from "../components/ui";

const BUSY = ["uploading", "processing", "revising", "exporting"];

export default function RawProjectPage() {
  const { rawId } = useParams();
  const [error, setError] = useState<string | null>(null);
  const [message, setMessage] = useState("");
  const { data: rp, reload } = useLoad(async () =>
    must(await supabase.from("raw_projects").select("*").eq("id", rawId!).single()) as RawProject, [rawId]);
  useLive("raw_projects", `id=eq.${rawId}`, reload, rp && BUSY.includes(rp.status) ? 3000 : 0);

  async function run(fn: () => Promise<unknown>) {
    setError(null);
    try {
      await fn();
      await reload();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }

  if (!rp) return <Page back="/footage"><Busy title="Loading…" /></Page>;
  const chosen = rp.chosen_index !== null && rp.chosen_index !== undefined;
  const base = (rp.title || "reelcraft").replace(/[^\w-]+/g, "_");

  async function send(e: FormEvent) {
    e.preventDefault();
    const text = message.trim();
    if (!text) return;
    setMessage("");
    await run(() => action("revise", { raw_project_id: rp!.id, instruction: text }));
  }

  return (
    <Page back="/footage" title={rp.title || "Your edit"}>
      <ErrorBox>{error || (rp.status !== "failed" ? rp.error : null)}</ErrorBox>

      {(rp.status === "uploading" || rp.status === "processing") &&
        <Busy title="Finding styles that fit your footage…" detail="Labelling your clips, picking styles from the library, and rendering previews." />}

      {rp.status === "failed" && (
        <div className="stack">
          <ErrorBox>{rp.error || "Something went wrong."}</ErrorBox>
          <button className="btn primary block" onClick={() => run(() => action("suggest", { raw_project_id: rp.id }))}>Try again</button>
        </div>
      )}

      {!chosen && rp.status === "ready" && (
        <>
          <p className="muted">Here's what I can cook. Pick one, then ask for changes.</p>
          <div className="carousel">
            {(rp.suggestions ?? []).map((s, i) => (
              <div key={i} className="card stack" style={{ gap: 10 }}>
                <Player path={s.preview_path} />
                <strong>{s.style_name || `Style ${i + 1}`}</strong>
                <div className="chips">{(s.moods ?? []).map((m) => <span key={m} className="badge">{m}</span>)}</div>
                <button className="btn primary block" onClick={() => run(() => action("choose", { raw_project_id: rp.id, index: i }))}>Use this</button>
              </div>
            ))}
          </div>
        </>
      )}

      {chosen && (
        <div className="stack">
          <Player path={rp.preview_path} />
          <div className="chat" aria-live="polite">
            {rp.revisions.map((r, i) => <div key={i} className="bubble">{r.instruction}</div>)}
            {rp.status === "revising" && <div className="muted small">Re-cutting your edit…</div>}
          </div>
          <form className="row" onSubmit={send}>
            <input type="text" className="grow" placeholder="Want changes? e.g. more slow motion" value={message}
              onChange={(e) => setMessage(e.target.value)} disabled={rp.status !== "ready" && rp.status !== "exported"}
              aria-label="Ask for changes" maxLength={500} />
            <button className="btn primary" disabled={!message.trim() || (rp.status !== "ready" && rp.status !== "exported")}>Send</button>
          </form>
          {rp.status === "exporting" && <Busy title="Exporting full quality…" />}
          {rp.status === "exported" ? (
            <div className="stack" style={{ gap: 8 }}>
              <DownloadButton path={rp.final_path} name={`${base}.mp4`}>Download video</DownloadButton>
              <div className="row">
                <div className="grow"><DownloadButton path={rp.timeline_path} name={`${base}.fcpxml`}>Resolve .fcpxml</DownloadButton></div>
                <div className="grow"><DownloadButton path={rp.otio_path} name={`${base}.otio`}>Resolve .otio</DownloadButton></div>
              </div>
            </div>
          ) : rp.status === "ready" && (
            <button className="btn primary big block" onClick={() => run(() => action("export", { raw_project_id: rp.id }))}>Export</button>
          )}
        </div>
      )}
    </Page>
  );
}
