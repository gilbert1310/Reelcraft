import { useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { supabase } from "../lib/supabase";
import { action, must, safeName, uploadFile, userId } from "../lib/api";
import { useLive, useLoad } from "../lib/hooks";
import type { Capture, Cut, CutMatch, Project, Setup, WrapCheck } from "../lib/types";
import { AngleBadge, Busy, DownloadButton, ErrorBox, Page, Player, Progress, StatusBadge, Thumb } from "../components/ui";

export default function ProjectPage() {
  const { projectId } = useParams();
  const nav = useNavigate();
  const [error, setError] = useState<string | null>(null);
  const [wrap, setWrap] = useState<WrapCheck | null>(null);
  const [working, setWorking] = useState<string | null>(null);
  const [importProgress, setImportProgress] = useState<number | null>(null);

  const { data, reload } = useLoad(async () => {
    const project = must(await supabase.from("projects").select("*").eq("id", projectId!).single()) as Project;
    const [setups, captures, cuts, matches] = await Promise.all([
      supabase.from("setups").select("*").eq("upload_id", project.upload_id).order("setup_number"),
      supabase.from("captures").select("*").eq("project_id", project.id).order("created_at"),
      supabase.from("cuts").select("id,order_index,setup_number,thumbnail_path,action").eq("upload_id", project.upload_id).order("order_index"),
      project.status === "done"
        ? supabase.from("cut_matches").select("*").eq("project_id", project.id).order("order_index")
        : Promise.resolve({ data: [], error: null }),
    ]);
    return {
      project, setups: must(setups) as Setup[], captures: must(captures) as Capture[],
      cuts: must(cuts) as Cut[], matches: must(matches) as CutMatch[],
    };
  }, [projectId]);
  useLive("captures", `project_id=eq.${projectId}`, reload, 0);
  // Poll as a fallback while anything is in flight, in case realtime is blocked on this network.
  const inFlight = data?.project.status === "assembling" || data?.captures.some((c) =>
    c.check_status === "checking" || (!c.setup_id && !c.suggested_setup_id));
  useLive("projects", `id=eq.${projectId}`, reload, inFlight ? 4000 : 0);

  async function run(label: string, fn: () => Promise<void>) {
    setError(null);
    setWorking(label);
    try {
      await fn();
      await reload();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setWorking(null);
    }
  }

  if (!data) return <Page back="/shoots"><Busy title="Loading…" /></Page>;
  const { project, setups, captures, cuts, matches } = data;
  const passed = (s: Setup) => captures.some((c) => c.setup_id === s.id && c.check_status === "pass");
  const doneCount = setups.filter(passed).length;
  const unassigned = captures.filter((c) => !c.setup_id);

  async function importFiles(files: FileList) {
    const uid = await userId();
    const list = Array.from(files);
    for (let i = 0; i < list.length; i++) {
      const f = list[i];
      const path = `${uid}/${project.id}/${crypto.randomUUID()}-${safeName(f.name)}`;
      await uploadFile("captures", path, f, (p) => setImportProgress((i + p) / list.length));
      const cap = must(await supabase.from("captures").insert({
        project_id: project.id, storage_path: path, file_name: f.name, source: "import",
      }).select("id").single()) as { id: string };
      await action("import-clip", { capture_id: cap.id });
    }
    setImportProgress(null);
  }

  return (
    <Page back="/shoots" title={project.title || "Your shoot"}>
      <ErrorBox>{error}</ErrorBox>

      {project.status === "assembling" && <Busy title="Cooking your reel…" detail="Matching every cut to your footage and rendering a preview." />}
      {project.status === "failed" && (
        <div className="stack">
          <ErrorBox>{project.error || "The edit failed."}</ErrorBox>
          <button className="btn primary block" onClick={() => run("assemble", () => action("assemble", { project_id: project.id }).then(() => {}))}>Try again</button>
        </div>
      )}

      {project.status === "done" && <Results project={project} matches={matches} cuts={cuts}
        onReshoot={(n) => {
          const s = setups.find((x) => x.setup_number === n);
          nav(`/project/${project.id}/shoot${s ? `?setup=${s.id}` : ""}`);
        }} />}

      <h2>Setups · {doneCount} of {setups.length} captured</h2>
      <Progress value={setups.length ? doneCount / setups.length : 0} />
      <div className="stack" style={{ marginTop: 12 }}>
        {setups.map((s) => {
          const mine = captures.filter((c) => c.setup_id === s.id);
          return (
            <article key={s.id} className="card stack" style={{ gap: 10 }}>
              <div className="row" style={{ alignItems: "flex-start" }}>
                <Thumb path={s.thumbnail_path} small />
                <div className="grow">
                  <div className="row" style={{ gap: 8 }}><strong>Setup {s.setup_number}</strong><AngleBadge angle={s.angle} />
                    {passed(s) && <span className="badge good">✓ Captured</span>}</div>
                  <p className="muted small" style={{ margin: "4px 0 0" }}>{s.description}</p>
                </div>
              </div>
              {mine.map((c) => <CaptureRow key={c.id} c={c} />)}
              <Link className={`btn block ${passed(s) ? "" : "primary"}`} to={`/project/${project.id}/shoot?setup=${s.id}`}>
                {passed(s) ? "Shoot another take" : mine.length ? "Retake" : "Shoot this setup"}
              </Link>
            </article>
          );
        })}
      </div>

      {unassigned.length > 0 && (
        <>
          <h2>Imported clips</h2>
          <div className="stack">
            {unassigned.map((c) => <AssignClip key={c.id} c={c} setups={setups}
              onAssign={(setupId) => run("assign", async () => {
                must(await supabase.from("captures").update({ setup_id: setupId }).eq("id", c.id).select("id"));
                await action("check-clip", { capture_id: c.id });
              })} />)}
          </div>
        </>
      )}

      <h2>On set</h2>
      <div className="stack">
        <label className="btn block">
          <input type="file" accept="video/*" multiple hidden
            onChange={(e) => e.target.files?.length && run("import", () => importFiles(e.target.files!))} />
          📥 Import from camera
        </label>
        {importProgress !== null && <Progress value={importProgress} />}
        <button className="btn block" disabled={working === "wrap"}
          onClick={() => run("wrap", async () => setWrap(await action<WrapCheck>("wrap-check", { project_id: project.id })))}>
          {working === "wrap" ? "Checking…" : "🎯 Wrap check"}
        </button>
        {wrap && <WrapResult wrap={wrap} onReshoot={(setupId) => nav(`/project/${project.id}/shoot?setup=${setupId}`)} />}
      </div>

      {project.status !== "assembling" && (
        <div className="stack" style={{ marginTop: 24 }}>
          <button className="btn primary big block" disabled={doneCount < setups.length || working === "assemble"}
            onClick={() => run("assemble", () => action("assemble", { project_id: project.id }).then(() => {}))}>
            {project.status === "done" ? "Remake my reel" : "Make my reel"}
          </button>
          {doneCount < setups.length && captures.some((c) => c.setup_id) && (
            <button className="btn ghost block small" onClick={() =>
              run("assemble", () => action("assemble", { project_id: project.id, force: true }).then(() => {}))}>
              Make a draft with what I have
            </button>
          )}
        </div>
      )}
    </Page>
  );
}

function CaptureRow({ c }: { c: Capture }) {
  return (
    <div className="notice" style={{ padding: "10px 12px" }}>
      <div className="row" style={{ gap: 8 }}>
        <span className="small grow" style={{ overflowWrap: "anywhere" }}>{c.source === "import" ? "📥 " : "📱 "}{c.file_name || "Take"}</span>
        <StatusBadge status={c.check_status} />
      </div>
      {c.check_feedback && <p className="small muted" style={{ margin: "6px 0 0" }}>{c.check_feedback}</p>}
    </div>
  );
}

function AssignClip({ c, setups, onAssign }: { c: Capture; setups: Setup[]; onAssign: (setupId: string) => void }) {
  const [choice, setChoice] = useState<string>("");
  const value = choice || c.suggested_setup_id || "";
  return (
    <div className="card stack" style={{ gap: 8 }}>
      <strong className="small" style={{ overflowWrap: "anywhere" }}>{c.file_name}</strong>
      {!c.suggested_setup_id && <span className="muted small">Working out which setup this is…</span>}
      <label className="stack" style={{ gap: 4 }}>
        <span className="small muted">This clip is for:</span>
        <select value={value} onChange={(e) => setChoice(e.target.value)}>
          <option value="" disabled>Choose a setup</option>
          {setups.map((s) => <option key={s.id} value={s.id}>Setup {s.setup_number}{s.id === c.suggested_setup_id ? " (suggested)" : ""}</option>)}
        </select>
      </label>
      <button className="btn primary block" disabled={!value} onClick={() => onAssign(value)}>Confirm and check</button>
    </div>
  );
}

function WrapResult({ wrap, onReshoot }: { wrap: WrapCheck; onReshoot: (setupId: string) => void }) {
  return (
    <div className="stack">
      {wrap.all_covered
        ? <div className="notice good pop">✅ All covered, you're good to go!</div>
        : wrap.missing.map((m) => (
          <div key={m.cut_id} className="notice warn stack" style={{ gap: 6 }}>
            <strong>Setup {m.setup_number}</strong>
            <span className="small">{m.what_to_film}</span>
            <span className="small muted">{m.reason} · aim for {m.target_seconds}s</span>
            <button className="btn block" onClick={() => onReshoot(m.setup_id)}>Reshoot</button>
          </div>
        ))}
      {wrap.unchecked_count > 0 && <div className="notice warn small">{wrap.unchecked_count} clip(s) haven't been AI-checked yet.</div>}
    </div>
  );
}

function Results({ project, matches, cuts, onReshoot }: {
  project: Project; matches: CutMatch[]; cuts: Cut[]; onReshoot: (setupNumber: number) => void;
}) {
  const cutById = new Map(cuts.map((c) => [c.id, c]));
  const base = (project.title || "reelcraft").replace(/[^\w-]+/g, "_");
  return (
    <section className="stack" style={{ marginBottom: 8 }}>
      {project.reshoots?.length > 0 && (
        <div className="notice warn stack" style={{ gap: 8 }}>
          <strong>Some moments need a reshoot</strong>
          {project.reshoots.map((r, i) => (
            <div key={i} className="row">
              <span className="small grow">{r.reason}</span>
              <button className="btn" onClick={() => onReshoot(r.setup_number)}>Reshoot now</button>
            </div>
          ))}
        </div>
      )}
      <Player path={project.preview_path} />
      <div className="stack" style={{ gap: 8 }}>
        <strong>Download for DaVinci Resolve</strong>
        <div className="row">
          <div className="grow"><DownloadButton path={project.timeline_path} name={`${base}.fcpxml`}>.fcpxml file</DownloadButton></div>
          <div className="grow"><DownloadButton path={project.otio_path} name={`${base}.otio`}>.otio file</DownloadButton></div>
        </div>
        <span className="muted small">In Resolve: File › Import › Timeline, then relink to your original clips.</span>
        <DownloadButton path={project.preview_path} name={`${base}-preview.mp4`}>Download preview</DownloadButton>
      </div>
      <h2>Match report</h2>
      <div className="row"><span className="score">{project.overall_score ?? "–"}</span><span className="muted">overall match<br />out of 100</span></div>
      <div className="stack">
        {matches.map((m) => {
          const cut = m.cut_id ? cutById.get(m.cut_id) : undefined;
          return (
            <div key={m.id} className="card row" style={{ alignItems: "flex-start" }}>
              <Thumb path={cut?.thumbnail_path} small alt="Original" />
              <Thumb path={m.thumbnail_path} small alt="Yours" />
              <div className="grow stack" style={{ gap: 6 }}>
                <div className="row" style={{ gap: 8 }}><strong>Cut {m.order_index + 1}</strong>
                  <span className={`badge ${(m.score ?? 0) >= 70 ? "good" : (m.score ?? 0) >= 45 ? "warn" : "bad"}`}>{m.score}</span></div>
                <div className="chips">{(m.fixes ?? []).map((f) => <span key={f} className="badge">{f}</span>)}</div>
                {(m.flags ?? []).map((f) => <span key={f} className="small" style={{ color: "var(--warn)" }}>⚠ {f}</span>)}
              </div>
            </div>
          );
        })}
      </div>
    </section>
  );
}
