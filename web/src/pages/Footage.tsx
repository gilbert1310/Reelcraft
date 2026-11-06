import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { supabase } from "../lib/supabase";
import { action, must, safeName, uploadFile, userId } from "../lib/api";
import { useLoad } from "../lib/hooks";
import type { RawProject } from "../lib/types";
import { ErrorBox, Page, Progress, StatusBadge } from "../components/ui";

const MOODS = ["Romantic", "Energetic", "Cinematic", "Moody"];

export default function Footage() {
  const nav = useNavigate();
  const [title, setTitle] = useState("");
  const [mood, setMood] = useState("Cinematic");
  const [files, setFiles] = useState<File[]>([]);
  const [progress, setProgress] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);
  const { data: past } = useLoad(async () =>
    must(await supabase.from("raw_projects").select("id,title,status,created_at").order("created_at", { ascending: false }).limit(20)) as RawProject[], []);

  async function create() {
    setError(null);
    try {
      if (!files.length) throw new Error("Add some clips first");
      const uid = await userId();
      const rp = must(await supabase.from("raw_projects").insert({ title: title.trim() || "My edit", mood: mood.toLowerCase() })
        .select("id").single()) as { id: string };
      setProgress(0);
      for (let i = 0; i < files.length; i++) {
        const f = files[i];
        const path = `${uid}/${rp.id}/${crypto.randomUUID()}-${safeName(f.name)}`;
        await uploadFile("raw", path, f, (p) => setProgress((i + p) / files.length));
        must(await supabase.from("raw_clips").insert({ raw_project_id: rp.id, storage_path: path, file_name: f.name }).select("id"));
      }
      await action("suggest", { raw_project_id: rp.id });
      nav(`/footage/${rp.id}`);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setProgress(null);
    }
  }

  const uploading = progress !== null;
  return (
    <Page back="/" title="Edit my own footage">
      <p className="muted">Upload your raw clips. We'll suggest three edits in styles that fit them.</p>
      <div className="stack">
        <input type="text" placeholder="Name this edit (e.g. Ama & Kofi wedding)" value={title}
          onChange={(e) => setTitle(e.target.value)} aria-label="Edit name" />
        <div>
          <div className="small muted" style={{ marginBottom: 8 }}>Mood</div>
          <div className="chips">
            {MOODS.map((m) => <button key={m} className="chip" aria-pressed={mood === m} onClick={() => setMood(m)}>{m}</button>)}
          </div>
        </div>
        <label className="drop">
          <input type="file" accept="video/*" multiple onChange={(e) => setFiles(Array.from(e.target.files ?? []))} />
          <div style={{ fontSize: "2rem" }} aria-hidden>🎞️</div>
          <strong>{files.length ? `${files.length} clip${files.length > 1 ? "s" : ""} selected` : "Choose your clips"}</strong>
          <div className="muted small">{files.length ? `${(files.reduce((a, f) => a + f.size, 0) / 1024 / 1024).toFixed(0)} MB total` : "Walks, close-ups, details, the venue…"}</div>
        </label>
        {uploading && <Progress value={progress} />}
        <ErrorBox>{error}</ErrorBox>
        <button className="btn primary big block" disabled={uploading || !files.length} onClick={create}>
          {uploading ? `Uploading ${Math.round((progress ?? 0) * 100)}%` : "Find styles for my footage"}
        </button>
      </div>
      {past && past.length > 0 && (
        <>
          <h2>Your edits</h2>
          <div className="list">
            {past.map((p) => (
              <Link key={p.id} to={`/footage/${p.id}`} className="list-item">
                <div className="grow"><strong>{p.title || "Untitled"}</strong>
                  <div className="muted small">{new Date(p.created_at).toLocaleDateString()}</div></div>
                <StatusBadge status={p.status} />
              </Link>
            ))}
          </div>
        </>
      )}
    </Page>
  );
}
