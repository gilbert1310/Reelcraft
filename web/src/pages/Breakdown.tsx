import { useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { supabase } from "../lib/supabase";
import { action, isSupportedLink, linkLabel, must, safeName, uploadFile, userId } from "../lib/api";
import { ErrorBox, Page, Progress } from "../components/ui";

const MAX_MB = 500;

export default function Breakdown() {
  const [params] = useSearchParams();
  const [tab, setTab] = useState<"upload" | "link">(params.get("tab") === "link" ? "link" : "upload");
  const [file, setFile] = useState<File | null>(null);
  const [link, setLink] = useState("");
  const [progress, setProgress] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const nav = useNavigate();

  async function start() {
    setError(null);
    setBusy(true);
    try {
      let row: { id: string };
      if (tab === "upload") {
        if (!file) throw new Error("Choose a video first");
        if (file.size > MAX_MB * 1024 * 1024) throw new Error(`Videos up to ${MAX_MB} MB, please`);
        const uid = await userId();
        const path = `${uid}/${crypto.randomUUID()}-${safeName(file.name)}`;
        setProgress(0);
        await uploadFile("reels", path, file, setProgress);
        row = must(await supabase.from("uploads").insert({ file_name: file.name, storage_path: path }).select("id").single());
      } else {
        if (!isSupportedLink(link)) throw new Error("Paste a public Instagram, TikTok, Facebook or YouTube link");
        row = must(await supabase.from("uploads").insert({ source_url: link.trim(), file_name: linkLabel(link) }).select("id").single());
      }
      await action("start-analysis", { upload_id: row.id });
      nav(`/plan/${row.id}`);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setProgress(null);
    } finally {
      setBusy(false);
    }
  }

  return (
    <Page back="/" title="Break down a reel">
      <p className="muted">We'll turn it into a short list of camera setups to film.</p>
      <div className="stack">
        <div className="tabs" role="tablist">
          <button role="tab" aria-selected={tab === "upload"} onClick={() => setTab("upload")}>Upload a video</button>
          <button role="tab" aria-selected={tab === "link"} onClick={() => setTab("link")}>Paste a link</button>
        </div>
        {tab === "upload" ? (
          <label className="drop">
            <input type="file" accept="video/*" onChange={(e) => setFile(e.target.files?.[0] ?? null)} />
            <div style={{ fontSize: "2rem" }} aria-hidden>⬆️</div>
            <strong>{file ? file.name : "Choose a reel"}</strong>
            <div className="muted small">{file ? `${(file.size / 1024 / 1024).toFixed(1)} MB` : `MP4 or MOV, up to ${MAX_MB} MB`}</div>
          </label>
        ) : (
          <div className="stack" style={{ gap: 6 }}>
            <input type="url" inputMode="url" placeholder="Paste an Instagram, TikTok, Facebook or YouTube link"
              value={link} onChange={(e) => setLink(e.target.value)} aria-label="Reel link" />
            <span className="muted small">Only public videos work. We analyse the style and don't keep the video.</span>
          </div>
        )}
        {progress !== null && <Progress value={progress} />}
        <ErrorBox>{error}</ErrorBox>
        <button className="btn primary big block" onClick={start}
          disabled={busy || (tab === "upload" ? !file : !link.trim())}>
          {busy ? (progress !== null && progress < 1 ? `Uploading ${Math.round(progress * 100)}%` : "Starting…") : "Analyse"}
        </button>
      </div>
    </Page>
  );
}
