import { useCallback, useEffect, useRef, useState } from "react";
import type { Session } from "@supabase/supabase-js";
import { supabase } from "./supabase";
import { signedUrl } from "./api";

export function useSession(): { session: Session | null; loading: boolean } {
  const [session, setSession] = useState<Session | null>(null);
  const [loading, setLoading] = useState(true);
  useEffect(() => {
    supabase.auth.getSession().then(({ data }) => {
      setSession(data.session);
      setLoading(false);
    });
    const { data } = supabase.auth.onAuthStateChange((_e, s) => setSession(s));
    return () => data.subscription.unsubscribe();
  }, []);
  return { session, loading };
}

/** Loads data with an async function; `reload()` re-runs it. */
export function useLoad<T>(fn: () => Promise<T>, deps: unknown[]) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const fnRef = useRef(fn);
  fnRef.current = fn;
  const reload = useCallback(async () => {
    try {
      setData(await fnRef.current());
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, []);
  useEffect(() => {
    setLoading(true);
    reload();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);
  return { data, error, loading, reload, setData };
}

/**
 * Subscribes to changes on a table (Supabase Realtime) and also polls, so the screen
 * stays current even if realtime is blocked by the network. Calls onChange on updates.
 */
export function useLive(table: string, filter: string | null, onChange: () => void, pollMs = 4000) {
  const cb = useRef(onChange);
  cb.current = onChange;
  useEffect(() => {
    if (!filter) return;
    const channel = supabase
      .channel(`${table}:${filter}:${Math.random().toString(36).slice(2)}`)
      .on("postgres_changes", { event: "*", schema: "public", table, filter }, () => cb.current())
      .subscribe();
    const timer = pollMs ? window.setInterval(() => cb.current(), pollMs) : 0;
    return () => {
      supabase.removeChannel(channel);
      if (timer) window.clearInterval(timer);
    };
  }, [table, filter, pollMs]);
}

export function useSignedUrl(bucket: string, path: string | null | undefined, download?: string): string | null {
  const [url, setUrl] = useState<string | null>(null);
  useEffect(() => {
    let alive = true;
    setUrl(null);
    signedUrl(bucket, path, download).then((u) => alive && setUrl(u));
    return () => {
      alive = false;
    };
  }, [bucket, path, download]);
  return url;
}

export function useOnline(): boolean {
  const [online, setOnline] = useState(navigator.onLine);
  useEffect(() => {
    const on = () => setOnline(true);
    const off = () => setOnline(false);
    window.addEventListener("online", on);
    window.addEventListener("offline", off);
    return () => {
      window.removeEventListener("online", on);
      window.removeEventListener("offline", off);
    };
  }, []);
  return online;
}
