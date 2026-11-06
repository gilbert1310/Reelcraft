import { supabase, SUPABASE_ANON_KEY, SUPABASE_URL } from "./supabase";

/** Calls an action on the `reelcraft` edge function and throws a readable error. */
export async function action<T = Record<string, unknown>>(name: string, params: Record<string, unknown>): Promise<T> {
  const { data, error } = await supabase.functions.invoke("reelcraft", { body: { action: name, ...params } });
  if (error) {
    let message = error.message;
    const ctx = (error as { context?: Response }).context;
    if (ctx && typeof ctx.json === "function") {
      try {
        const body = await ctx.json();
        if (body?.error) message = body.error;
      } catch { /* keep the generic message */ }
    }
    throw new Error(message);
  }
  if (data?.error) throw new Error(data.error);
  return data as T;
}

/** Throws on a Supabase query error, returns data. */
export function must<T>(res: { data: T | null; error: { message: string } | null }): T {
  if (res.error) throw new Error(res.error.message);
  return res.data as T;
}

export async function userId(): Promise<string> {
  const { data } = await supabase.auth.getUser();
  if (!data.user) throw new Error("Please sign in again");
  return data.user.id;
}

export function safeName(name: string): string {
  return name.replace(/[^\w.\-]+/g, "_").slice(-80);
}

/** Uploads a file to Storage with progress (supabase-js has no progress events). */
export async function uploadFile(bucket: string, path: string, file: Blob,
                                 onProgress?: (fraction: number) => void): Promise<string> {
  const { data } = await supabase.auth.getSession();
  const token = data.session?.access_token;
  if (!token) throw new Error("Please sign in again");
  const encoded = path.split("/").map(encodeURIComponent).join("/");
  await new Promise<void>((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", `${SUPABASE_URL}/storage/v1/object/${bucket}/${encoded}`);
    xhr.setRequestHeader("Authorization", `Bearer ${token}`);
    xhr.setRequestHeader("apikey", SUPABASE_ANON_KEY);
    xhr.setRequestHeader("x-upsert", "true");
    xhr.setRequestHeader("Content-Type", file.type || "application/octet-stream");
    xhr.upload.onprogress = (e) => e.lengthComputable && onProgress?.(e.loaded / e.total);
    xhr.onload = () => (xhr.status < 300 ? resolve() : reject(new Error(uploadError(xhr))));
    xhr.onerror = () => reject(new Error("Upload failed. Check your connection and try again."));
    xhr.send(file);
  });
  onProgress?.(1);
  return path;
}

function uploadError(xhr: XMLHttpRequest): string {
  try {
    const body = JSON.parse(xhr.responseText);
    return body.message || body.error || `Upload failed (${xhr.status})`;
  } catch {
    return `Upload failed (${xhr.status})`;
  }
}

const urlCache = new Map<string, { url: string; expires: number }>();

/** Short-lived signed URL for a private file, cached for 50 minutes. */
export async function signedUrl(bucket: string, path: string | null | undefined, download?: string): Promise<string | null> {
  if (!path) return null;
  const key = `${bucket}/${path}/${download ?? ""}`;
  const hit = urlCache.get(key);
  if (hit && hit.expires > Date.now()) return hit.url;
  const { data, error } = await supabase.storage.from(bucket).createSignedUrl(path, 3600,
    download ? { download } : undefined);
  if (error || !data) return null;
  urlCache.set(key, { url: data.signedUrl, expires: Date.now() + 50 * 60 * 1000 });
  return data.signedUrl;
}

const ALLOWED_HOSTS = ["instagram.com", "tiktok.com", "facebook.com", "fb.watch", "youtube.com", "youtu.be"];

export function isSupportedLink(value: string): boolean {
  try {
    const u = new URL(value.trim());
    const host = u.hostname.toLowerCase();
    return (u.protocol === "https:" || u.protocol === "http:") &&
      ALLOWED_HOSTS.some((h) => host === h || host.endsWith(`.${h}`));
  } catch {
    return false;
  }
}

/** "Instagram reel", "TikTok video"… for naming link uploads. */
export function linkLabel(value: string): string {
  try {
    const host = new URL(value.trim()).hostname.toLowerCase();
    if (host.includes("instagram")) return "Instagram reel";
    if (host.includes("tiktok")) return "TikTok video";
    if (host.includes("facebook") || host.includes("fb.watch")) return "Facebook video";
    if (host.includes("youtu")) return "YouTube video";
  } catch { /* fall through */ }
  return "Linked video";
}

/** Deletes a shoot plan and everything made from it, after asking. Returns true if deleted. */
export async function deleteUpload(uploadId: string, name: string | null): Promise<boolean> {
  if (!window.confirm(`Delete "${name || "this reel"}" and its shoot plan? This can't be undone.`)) return false;
  await action("delete-upload", { upload_id: uploadId });
  return true;
}
