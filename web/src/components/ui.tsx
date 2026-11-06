import { type ReactNode } from "react";
import { Link, useNavigate } from "react-router-dom";
import { supabase } from "../lib/supabase";
import { useSignedUrl } from "../lib/hooks";

export function Page({ title, back, children, actions }: {
  title?: string; back?: string; children: ReactNode; actions?: ReactNode;
}) {
  return (
    <div className="shell">
      <header className="topbar">
        {back ? <Link className="back" to={back}>← Back</Link> : (
          <Link className="brand" to="/"><img src="/icon.svg" alt="" />ReelCraft</Link>
        )}
        <div className="spacer" />
        {actions}
      </header>
      {title && <h1>{title}</h1>}
      {children}
    </div>
  );
}

export function SignOut() {
  const nav = useNavigate();
  return (
    <button className="btn ghost small" style={{ minHeight: 36, padding: "6px 10px" }}
      onClick={async () => { await supabase.auth.signOut(); nav("/"); }}>Sign out</button>
  );
}

export function Busy({ title, detail }: { title: string; detail?: string }) {
  return (
    <div className="busy" role="status">
      <div className="spinner" />
      <div><strong>{title}</strong>{detail && <p className="muted small">{detail}</p>}</div>
    </div>
  );
}

export function ErrorBox({ children }: { children: ReactNode }) {
  if (!children) return null;
  return <div className="notice error" role="alert">{children}</div>;
}

export function Progress({ value }: { value: number }) {
  return (
    <div className="progress" role="progressbar" aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.round(value * 100)}>
      <div style={{ width: `${Math.max(0, Math.min(1, value)) * 100}%` }} />
    </div>
  );
}

export function Thumb({ bucket = "thumbs", path, small, alt = "" }: {
  bucket?: string; path: string | null | undefined; small?: boolean; alt?: string;
}) {
  const url = useSignedUrl(bucket, path);
  return url ? <img className={`thumb${small ? " sm" : ""}`} src={url} alt={alt} loading="lazy" />
    : <div className={`thumb${small ? " sm" : ""}`} aria-hidden />;
}

export function Player({ path, bucket = "renders" }: { path: string | null | undefined; bucket?: string }) {
  const url = useSignedUrl(bucket, path);
  if (!url) return <div className="player" aria-hidden />;
  return <video className="player" src={url} controls playsInline loop preload="metadata" />;
}

export function DownloadButton({ path, name, children, bucket = "renders" }: {
  path: string | null | undefined; name: string; children: ReactNode; bucket?: string;
}) {
  const url = useSignedUrl(bucket, path, name);
  return <a className="btn block" href={url ?? undefined} aria-disabled={!url} download={name}>{children}</a>;
}

export function AngleBadge({ angle }: { angle: string | null }) {
  return angle ? <span className="badge accent">{angle}</span> : null;
}

const STATUS_TONE: Record<string, string> = {
  pass: "good", covered: "good", done: "good", exported: "good", ready: "good",
  retry: "warn", weak: "warn", checking: "", pending: "", processing: "", assembling: "", shooting: "",
  failed: "bad", missing: "bad", link_failed: "bad",
};
const STATUS_LABEL: Record<string, string> = {
  pass: "Passed", retry: "Retake", checking: "Checking", pending: "Not checked", failed: "Failed",
  covered: "Covered", weak: "Weak", missing: "Missing", shooting: "Shooting", assembling: "Assembling",
  done: "Ready", processing: "Processing", queued: "Queued", link_failed: "Link failed", ready: "Ready",
  uploading: "Uploading", revising: "Revising", exporting: "Exporting", exported: "Exported",
};

export function StatusBadge({ status }: { status: string }) {
  return <span className={`badge ${STATUS_TONE[status] ?? ""}`}>{STATUS_LABEL[status] ?? status}</span>;
}
