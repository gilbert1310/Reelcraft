import { useCallback, useEffect, useRef, useState } from "react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router-dom";
import { supabase } from "../lib/supabase";
import { action, must, safeName, uploadFile, userId } from "../lib/api";
import { useLive, useLoad, useOnline } from "../lib/hooks";
import { listPending, removePending, savePending, type PendingClip } from "../lib/offline";
import type { Capture, Project, Setup } from "../lib/types";
import { AngleBadge, ErrorBox, Progress, StatusBadge } from "../components/ui";

type Phase = "camera" | "short" | "uploading" | "checking" | "result" | "queued";

const MIME_TYPES = ["video/mp4;codecs=avc1", "video/mp4", "video/webm;codecs=vp9", "video/webm;codecs=vp8", "video/webm"];

function pickMime(): string {
  if (typeof MediaRecorder === "undefined") return "";
  return MIME_TYPES.find((m) => MediaRecorder.isTypeSupported(m)) ?? "";
}

async function sendClip(projectId: string, setupId: string, blob: Blob, fileName: string,
                        onProgress?: (p: number) => void): Promise<string> {
  const uid = await userId();
  const path = `${uid}/${projectId}/${crypto.randomUUID()}-${safeName(fileName)}`;
  await uploadFile("captures", path, blob, onProgress);
  const cap = must(await supabase.from("captures").insert({
    project_id: projectId, setup_id: setupId, storage_path: path, file_name: fileName, source: "phone",
  }).select("id").single()) as { id: string };
  await action("check-clip", { capture_id: cap.id });
  return cap.id;
}

export default function ShootMode() {
  const { projectId } = useParams();
  const [params, setParams] = useSearchParams();
  const nav = useNavigate();
  const online = useOnline();

  const { data, reload } = useLoad(async () => {
    const project = must(await supabase.from("projects").select("*").eq("id", projectId!).single()) as Project;
    const setups = must(await supabase.from("setups").select("*").eq("upload_id", project.upload_id).order("setup_number")) as Setup[];
    const captures = must(await supabase.from("captures").select("id,setup_id,check_status").eq("project_id", project.id)) as Capture[];
    return { project, setups, captures };
  }, [projectId]);

  const setups = data?.setups ?? [];
  const passedIds = new Set((data?.captures ?? []).filter((c) => c.check_status === "pass").map((c) => c.setup_id));
  const current = setups.find((s) => s.id === params.get("setup")) ?? setups.find((s) => !passedIds.has(s.id)) ?? setups[0];
  const target = Number(current?.film_duration_seconds ?? 10);

  // ---- camera ----
  const videoRef = useRef<HTMLVideoElement>(null);
  const streamRef = useRef<MediaStream | null>(null);
  const recRef = useRef<MediaRecorder | null>(null);
  const chunks = useRef<Blob[]>([]);
  const started = useRef(0);
  const [cameraError, setCameraError] = useState<string | null>(null);
  const [resolution, setResolution] = useState<{ w: number; h: number } | null>(null);
  const [recording, setRecording] = useState(false);
  const [elapsed, setElapsed] = useState(0);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const stream = await navigator.mediaDevices.getUserMedia({
          audio: true,
          video: { facingMode: { ideal: "environment" }, width: { ideal: 3840 }, height: { ideal: 2160 }, frameRate: { ideal: 60 } },
        });
        if (cancelled) return stream.getTracks().forEach((t) => t.stop());
        streamRef.current = stream;
        const s = stream.getVideoTracks()[0]?.getSettings();
        if (s?.width && s?.height) setResolution({ w: s.width, h: s.height });
        if (videoRef.current) videoRef.current.srcObject = stream;
      } catch (e) {
        setCameraError(e instanceof Error && e.name === "NotAllowedError"
          ? "Camera access was blocked. Allow it in your browser settings, or upload a clip from your gallery."
          : "No camera available here. You can upload a clip from your gallery instead.");
      }
    })();
    return () => {
      cancelled = true;
      streamRef.current?.getTracks().forEach((t) => t.stop());
    };
  }, []);

  useEffect(() => {
    if (!recording) return;
    const t = window.setInterval(() => setElapsed((Date.now() - started.current) / 1000), 200);
    return () => window.clearInterval(t);
  }, [recording]);

  // ---- take flow ----
  const [phase, setPhase] = useState<Phase>("camera");
  const [blob, setBlob] = useState<{ data: Blob; name: string; seconds: number } | null>(null);
  const [progress, setProgress] = useState(0);
  const [captureId, setCaptureId] = useState<string | null>(null);
  const [capture, setCapture] = useState<Capture | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [pending, setPending] = useState<PendingClip[]>([]);

  function start() {
    const stream = streamRef.current;
    if (!stream) return;
    const mime = pickMime();
    const rec = new MediaRecorder(stream, mime ? { mimeType: mime, videoBitsPerSecond: 25_000_000 } : undefined);
    chunks.current = [];
    rec.ondataavailable = (e) => e.data.size && chunks.current.push(e.data);
    rec.onstop = () => {
      const type = rec.mimeType || mime || "video/webm";
      const ext = type.includes("mp4") ? "mp4" : "webm";
      const seconds = (Date.now() - started.current) / 1000;
      const take = { data: new Blob(chunks.current, { type }), name: `setup${current?.setup_number}-${Date.now()}.${ext}`, seconds };
      setBlob(take);
      if (seconds < target * 0.85) setPhase("short");
      else submit(take);
    };
    rec.start(1000);
    recRef.current = rec;
    started.current = Date.now();
    setElapsed(0);
    setRecording(true);
  }

  function stop() {
    recRef.current?.stop();
    setRecording(false);
  }

  async function submit(take: { data: Blob; name: string }) {
    if (!current || !projectId) return;
    setError(null);
    if (!navigator.onLine) {
      await savePending({ id: crypto.randomUUID(), projectId, setupId: current.id, blob: take.data, fileName: take.name, createdAt: Date.now() });
      setPending(await listPending(projectId));
      setPhase("queued");
      return;
    }
    try {
      setPhase("uploading");
      setProgress(0);
      const id = await sendClip(projectId, current.id, take.data, take.name, setProgress);
      setCaptureId(id);
      setCapture(null);
      setPhase("checking");
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setPhase("short");
    }
  }

  async function fromGallery(file: File) {
    setBlob({ data: file, name: file.name, seconds: 0 });
    await submit({ data: file, name: file.name });
  }

  // ---- watch the check ----
  const pollCapture = useCallback(async () => {
    if (!captureId) return;
    const { data: row } = await supabase.from("captures").select("*").eq("id", captureId).single();
    if (!row) return;
    setCapture(row as Capture);
    if (row.check_status !== "checking" && row.check_status !== "pending") {
      setPhase("result");
      reload();
    }
  }, [captureId, reload]);
  useLive("captures", phase === "checking" && captureId ? `id=eq.${captureId}` : null, pollCapture, 3000);

  // ---- offline queue ----
  const flushing = useRef(false);
  useEffect(() => {
    if (!projectId) return;
    listPending(projectId).then(setPending).catch(() => {});
  }, [projectId]);
  useEffect(() => {
    if (!online || !projectId || flushing.current) return;
    flushing.current = true;
    (async () => {
      for (const clip of await listPending(projectId)) {
        try {
          await sendClip(projectId, clip.setupId, clip.blob, clip.fileName);
          await removePending(clip.id);
        } catch {
          break;
        }
      }
      setPending(await listPending(projectId));
      flushing.current = false;
      reload();
    })();
  }, [online, projectId, reload]);

  function goTo(setup: Setup | undefined) {
    setPhase("camera");
    setCapture(null);
    setCaptureId(null);
    setBlob(null);
    setError(null);
    if (setup) setParams({ setup: setup.id }, { replace: true });
  }

  if (!data || !current) {
    return <div className="shoot"><div className="busy"><div className="spinner" /></div></div>;
  }
  const doneCount = setups.filter((s) => passedIds.has(s.id)).length;
  const nextSetup = setups.find((s) => s.id !== current.id && !passedIds.has(s.id));
  const lowRes = resolution && Math.min(resolution.w, resolution.h) < 1080;

  return (
    <div className="shoot">
      <video ref={videoRef} className="live" autoPlay playsInline muted />

      <div className="overlay-top stack" style={{ gap: 8 }}>
        <div className="row">
          <Link to={`/project/${projectId}`} className="btn ghost" style={{ minHeight: 40, padding: "6px 12px" }}>✕</Link>
          <div className="grow"><Progress value={setups.length ? doneCount / setups.length : 0} /></div>
          <span className="small">{doneCount} of {setups.length} setups</span>
        </div>
        <div className="setup-card">
          <div className="row" style={{ gap: 8 }}>
            <strong>Setup {current.setup_number}</strong><AngleBadge angle={current.angle} />
            <span className="grow" />
            <span className="small muted">Target {Math.round(target)}s</span>
          </div>
          <p className="small" style={{ margin: "6px 0 0" }}>{current.description}</p>
        </div>
        {!online && <span className="badge warn" style={{ justifySelf: "start" }}>No signal: takes will upload later</span>}
        {pending.length > 0 && <span className="badge warn" style={{ justifySelf: "start" }}>Waiting for signal: {pending.length} take(s)</span>}
        {lowRes && <span className="badge warn" style={{ justifySelf: "start" }}>Camera is {resolution!.w}×{resolution!.h}. 4K gives the editor room to reframe.</span>}
      </div>

      {phase === "camera" && (
        <div className="overlay-bottom stack" style={{ gap: 12 }}>
          {cameraError && <ErrorBox>{cameraError}</ErrorBox>}
          <div className={`timer ${elapsed >= target ? "ok" : ""}`}>
            {recording ? `${elapsed.toFixed(1)}s / ${Math.round(target)}s` : "Ready"}
          </div>
          {!cameraError && (
            <button className={`rec ${recording ? "on" : ""}`} aria-label={recording ? "Stop recording" : "Start recording"}
              onClick={recording ? stop : start} />
          )}
          {!recording && (
            <label className="btn ghost block small">
              <input type="file" accept="video/*" hidden onChange={(e) => e.target.files?.[0] && fromGallery(e.target.files[0])} />
              Upload from gallery instead
            </label>
          )}
        </div>
      )}

      {phase !== "camera" && (
        <div className="sheet stack" style={{ marginTop: "auto" }}>
          {phase === "short" && blob && (
            <>
              <ErrorBox>{error}</ErrorBox>
              {!error && <div className="notice warn"><strong>Too short:</strong> hold the shot about {Math.max(1, Math.ceil(target - blob.seconds))} more seconds.</div>}
              <button className="btn primary big block" onClick={() => goTo(undefined)}>Retake</button>
              <button className="btn ghost block" onClick={() => submit(blob)}>Check it anyway</button>
            </>
          )}
          {phase === "uploading" && (
            <>
              <strong>Uploading your take…</strong>
              <Progress value={progress} />
            </>
          )}
          {phase === "checking" && (
            <div className="busy" style={{ padding: 16 }}><div className="spinner" /><strong>Checking your shot…</strong>
              <span className="muted small">Comparing it with every cut from this setup.</span></div>
          )}
          {phase === "queued" && (
            <>
              <div className="notice warn">Saved on your phone. It will upload and get checked when you're back online.</div>
              {nextSetup ? <button className="btn primary big block" onClick={() => goTo(nextSetup)}>Next setup</button>
                : <button className="btn primary big block" onClick={() => nav(`/project/${projectId}`)}>Done</button>}
            </>
          )}
          {phase === "result" && capture && (
            <>
              {capture.check_status === "pass" ? (
                <div className="row"><span className="pop" style={{ fontSize: "2.4rem" }} aria-hidden>✅</span>
                  <div><strong>Nailed it!</strong><p className="small muted" style={{ margin: 0 }}>{capture.check_feedback}</p></div></div>
              ) : (
                <div className="notice error"><strong>Let's take that again.</strong><p className="small" style={{ margin: "4px 0 0" }}>{capture.check_feedback}</p></div>
              )}
              {(capture.coverage ?? []).length > 0 && (
                <div className="stack" style={{ gap: 6 }}>
                  {(capture.coverage ?? []).map((c) => (
                    <div key={c.order_index} className="row" style={{ alignItems: "flex-start", gap: 8 }}>
                      <StatusBadge status={c.status} />
                      <span className="small"><strong>Cut {c.order_index + 1}</strong>{c.reason ? `: ${c.reason}` : ""}</span>
                    </div>
                  ))}
                </div>
              )}
              {capture.check_status === "pass" ? (
                nextSetup ? <button className="btn primary big block" onClick={() => goTo(nextSetup)}>Next setup</button>
                  : <button className="btn primary big block" onClick={() => nav(`/project/${projectId}`)}>All setups done 🎉</button>
              ) : (
                <>
                  <button className="btn primary big block" onClick={() => goTo(undefined)}>Retake</button>
                  {nextSetup && <button className="btn ghost block" onClick={() => goTo(nextSetup)}>Skip for now</button>}
                </>
              )}
            </>
          )}
        </div>
      )}
    </div>
  );
}
