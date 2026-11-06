import { Link } from "react-router-dom";
import { supabase } from "../lib/supabase";
import { deleteUpload, must } from "../lib/api";
import { useLoad } from "../lib/hooks";
import type { Project, Upload } from "../lib/types";
import { Busy, ErrorBox, Page, StatusBadge } from "../components/ui";

export default function Shoots() {
  const { data, error, reload } = useLoad(async () => {
    const projects = must(await supabase.from("projects").select("*").order("created_at", { ascending: false })) as Project[];
    const uploads = must(await supabase.from("uploads").select("id,file_name,source_url,status,created_at")
      .order("created_at", { ascending: false }).limit(50)) as Upload[];
    return { projects, uploads: uploads.filter((u) => !projects.some((p) => p.upload_id === u.id)) };
  }, []);

  return (
    <Page back="/" title="My shoots">
      <ErrorBox>{error}</ErrorBox>
      {!data ? <Busy title="Loading…" /> : (
        <>
          {data.projects.length === 0 && <p className="muted">No shoots yet. Break down a reel, then tap Start Shooting.</p>}
          <div className="list">
            {data.projects.map((p) => (
              <Link key={p.id} to={`/project/${p.id}`} className="list-item">
                <div className="grow"><strong>{p.title || "Untitled shoot"}</strong>
                  <div className="muted small">{new Date(p.created_at).toLocaleDateString()}</div></div>
                <StatusBadge status={p.status} />
              </Link>
            ))}
          </div>
          {data.uploads.length > 0 && (
            <>
              <h2>Shoot plans not started</h2>
              <div className="list">
                {data.uploads.map((u) => (
                  <div key={u.id} className="row" style={{ gap: 8 }}>
                    <Link to={`/plan/${u.id}`} className="list-item grow">
                      <div className="grow"><strong style={{ overflowWrap: "anywhere" }}>{u.file_name || u.source_url}</strong>
                        <div className="muted small">{new Date(u.created_at).toLocaleString()}</div></div>
                      <StatusBadge status={u.status} />
                    </Link>
                    <button className="btn ghost" aria-label={`Delete ${u.file_name || "reel"}`} title="Delete"
                      onClick={async () => { if (await deleteUpload(u.id, u.file_name)) reload(); }}>🗑</button>
                  </div>
                ))}
              </div>
            </>
          )}
          <Link to="/breakdown" className="btn primary block" style={{ marginTop: 20 }}>Break down a new reel</Link>
        </>
      )}
    </Page>
  );
}
