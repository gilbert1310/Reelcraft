import { useEffect, useRef, useState } from "react";
import { useSignedUrl } from "../lib/hooks";

/**
 * A still from the reference reel that plays the matching moment on a loop when tapped.
 * Falls back to the still alone when the reference video isn't stored (pasted links).
 */
export function Snippet({ videoPath, posterPath, start, end, label }: {
  videoPath: string | null; posterPath: string | null; start: number; end: number; label: string;
}) {
  const poster = useSignedUrl("thumbs", posterPath);
  const [playing, setPlaying] = useState(false);
  const src = useSignedUrl("reels", playing ? videoPath : null);
  const ref = useRef<HTMLVideoElement>(null);

  useEffect(() => {
    const v = ref.current;
    if (!v || !src) return;
    const from = Math.max(0, start), to = Math.max(end, start + 0.4);
    const loop = () => { if (v.currentTime >= to || v.currentTime < from - 0.25) v.currentTime = from; };
    const ready = () => { v.currentTime = from; v.play().catch(() => {}); };
    v.addEventListener("timeupdate", loop);
    v.addEventListener("loadedmetadata", ready);
    return () => { v.removeEventListener("timeupdate", loop); v.removeEventListener("loadedmetadata", ready); };
  }, [src, start, end]);

  const box = { width: 64, aspectRatio: "9/16", borderRadius: 8, background: "var(--surface-2)", flexShrink: 0,
    objectFit: "cover" as const, display: "block", position: "relative" as const, overflow: "hidden" };
  if (playing && src) {
    return <video ref={ref} src={src} muted playsInline poster={poster ?? undefined} style={box} aria-label={label}
      onClick={() => setPlaying(false)} />;
  }
  return (
    <button type="button" onClick={() => videoPath && setPlaying(true)} aria-label={videoPath ? `Play: ${label}` : label}
      style={{ ...box, border: 0, padding: 0, cursor: videoPath ? "pointer" : "default" }}>
      {poster && <img src={poster} alt="" style={{ width: "100%", height: "100%", objectFit: "cover" }} />}
      {videoPath && <span aria-hidden style={{ position: "absolute", inset: 0, display: "grid", placeItems: "center",
        color: "#fff", fontSize: 18, textShadow: "0 1px 4px #000" }}>▶</span>}
    </button>
  );
}
