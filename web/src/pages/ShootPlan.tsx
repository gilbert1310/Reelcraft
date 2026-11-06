import { useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { supabase } from "../lib/supabase";
import { action, deleteUpload, must } from "../lib/api";
import { useLive, useLoad } from "../lib/hooks";
import type { Setup, Upload } from "../lib/types";
import { AngleBadge, Busy, ErrorBox, Page, Thumb } from "../components/ui";

export default function ShootPlan() {
  const { uploadId } = useParams();
  const nav = useNavigate();
  const [error, setError] = useState<string | null>(null);
  const [retrying, setRetrying] = useState(false);
  const [showRedo, setShowRedo] = useState(false);
  const [feedback, setFeedback] = useState("");
  const { data, reload } = useLoad(async () => {
    const upload = must(await supabase.from("uploads").select("*").eq("id", uploadId!).single()) as Upload;
    const setups = upload.status === "done"
      ? must(await supabase.from("setups").select("*").eq("upload_id", uploadId!).order("setup_number")) as Setup[]
      : [];
    return { upload, setups };
  }, [uploadId]);
  const busy = !data || data.upload.status === "queued" || data.upload.status === "processing";
  useLive("uploads", busy ? `id=eq.${uploadId}` : null, reload, 3000);

  async function retry() {
    if (retrying) return;
    setError(null);
    setRetrying(true);
    try {
      await action("start-analysis", { upload_id: uploadId, feedback: feedback.trim() || undefined });
      setShowRedo(false);
      setFeedback("");
      await reload();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setRetrying(false);
    }
  }

  async function startShooting() {
    try {
      const p = must(await supabase.from("projects").insert({
        upload_id: uploadId, title: data?.upload.file_name || "My shoot",
      }).select("id").single()) as { id: string };
      nav(`/project/${p.id}`);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }

  if (!data) return <Page back="/"><Busy title="Loading…" /></Page>;
  const { upload } = data;
  const setups = data.setups.filter((s) => !s.is_graphic);
  const graphics = data.setups.filter((s) => s.is_graphic);
  // A job can be lost if the video service restarts; after a while, offer a restart.
  const minutesRunning = (Date.now() - new Date(upload.updated_at).getTime()) / 60000;
  const tooLong = busy && minutesRunning > 10; // matches STALE_MINUTES in the edge function

  return (
    <Page back="/" title="Your Shoot Plan">
      {busy && <Busy title="Breaking down your reel…" detail="Finding the cuts and grouping them into setups. This takes a minute or two." />}
      {tooLong && (
        <div className="stack">
          <div className="notice warn">This is taking much longer than usual. The video service may have restarted.</div>
          <button className="btn block" onClick={retry} disabled={retrying}>{retrying ? "Restarting…" : "Restart analysis"}</button>
        </div>
      )}
      {upload.status === "failed" && (
        <div className="stack">
          <ErrorBox>{upload.error || "Something went wrong."}</ErrorBox>
          <button className="btn primary block" onClick={retry} disabled={retrying}>{retrying ? "Starting…" : "Try again"}</button>
        </div>
      )}
      {upload.status === "link_failed" && (
        <div className="stack">
          <ErrorBox>We couldn't grab that video. It may be private, or the platform blocked it. Screen-record it and upload it instead.</ErrorBox>
          <button className="btn primary block" onClick={() => nav("/breakdown?tab=upload")}>Upload instead</button>
        </div>
      )}
      {upload.status === "done" && (
        <div className="stack">
          {upload.plan_summary && <div className="notice"><strong>{upload.plan_summary}</strong></div>}
          <p className="muted">This reel uses <strong style={{ color: "var(--text)" }}>{upload.cut_count} cuts</strong> from
            just <strong style={{ color: "var(--text)" }}>{setups.length} {setups.length === 1 ? "take" : "takes"}</strong>.
            Film each one once, in one continuous take, for the time shown. The app cuts it up to match.</p>
          {setups.map((s) => (
            <article key={s.id} className="card row" style={{ alignItems: "flex-start" }}>
              <Thumb path={s.thumbnail_path} alt={`Setup ${s.setup_number}`} />
              <div className="grow stack" style={{ gap: 6 }}>
                <div className="row" style={{ gap: 8 }}><strong>Take {s.setup_number}</strong><AngleBadge angle={s.angle} /></div>
                <Directions text={s.description} />
                <span className="small"><strong>Film about {Math.round(Number(s.film_duration_seconds))} seconds</strong></span>
                {s.beginner_tip && <div className="tip"><span aria-hidden>💡</span><span>{s.beginner_tip}</span></div>}
              </div>
            </article>
          ))}
          {graphics.length > 0 && (
            <p className="muted small">Not filmed: {graphics.map((g) => g.description).join(" ")}</p>
          )}
          <ErrorBox>{error}</ErrorBox>
          <button className="btn primary big block" onClick={startShooting}>Start Shooting</button>
          {!showRedo ? (
            <button className="btn ghost block" onClick={() => setShowRedo(true)}>Not right? Re-analyse</button>
          ) : (
            <div className="card stack">
              <strong>Re-analyse this reel</strong>
              <label className="stack" style={{ gap: 6 }}>
                <span className="small muted">What's off? (optional) The AI will take it into account.</span>
                <textarea rows={3} maxLength={1000} value={feedback} onChange={(e) => setFeedback(e.target.value)}
                  placeholder="e.g. Takes 1 and 4 are the same walk. There's also a close-up of the bag." />
              </label>
              <div className="row">
                <button className="btn ghost grow" onClick={() => setShowRedo(false)}>Cancel</button>
                <button className="btn primary grow" onClick={retry} disabled={retrying}>{retrying ? "Starting…" : "Re-analyse"}</button>
              </div>
            </div>
          )}
        </div>
      )}
      {busy && <ErrorBox>{error}</ErrorBox>}
      {!busy && (
        <button className="btn ghost block small" style={{ marginTop: 24, color: "var(--bad)" }}
          onClick={async () => { if (await deleteUpload(upload.id, upload.file_name)) nav("/shoots"); }}>
          Delete this reel and its plan
        </button>
      )}
    </Page>
  );
}

/** Shows "how to film" and then the numbered beats to call out, one per line. */
export function Directions({ text }: { text: string | null }) {
  if (!text) return null;
  const m = text.match(/^(.*?)(Direct (?:her|him|them) through:)(.*)$/s);
  if (!m) return <p style={{ margin: 0 }}>{text}</p>;
  const beats = m[3].split(/\s*\d+[.)]\s+/).map((b) => b.trim()).filter(Boolean);
  return (
    <div className="stack" style={{ gap: 6 }}>
      <p style={{ margin: 0 }}>{m[1].trim()}</p>
      <strong className="small">{m[2]}</strong>
      <ol style={{ margin: 0, paddingLeft: 20 }}>{beats.map((b, i) => <li key={i}>{b}</li>)}</ol>
    </div>
  );
}
