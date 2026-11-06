// Shared helpers for the ReelCraft edge functions.
import { createClient, type SupabaseClient } from "npm:@supabase/supabase-js@2.49.4";

export const corsHeaders = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "authorization, x-client-info, apikey, content-type",
  "Access-Control-Allow-Methods": "POST, OPTIONS",
};

export class HttpError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

export function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { ...corsHeaders, "Content-Type": "application/json" },
  });
}

export function env(name: string): string {
  const v = Deno.env.get(name);
  if (!v) throw new HttpError(500, `Missing secret ${name}`);
  return v;
}

/** Service-role client: bypasses RLS, so always check ownership first. */
export function adminClient(): SupabaseClient {
  return createClient(env("SUPABASE_URL"), env("SUPABASE_SERVICE_ROLE_KEY"), {
    auth: { persistSession: false },
  });
}

/** Timing-safe string comparison for shared secrets. */
export function safeEqual(a: string, b: string): boolean {
  const ea = new TextEncoder().encode(a);
  const eb = new TextEncoder().encode(b);
  let diff = ea.length ^ eb.length;
  for (let i = 0; i < Math.max(ea.length, eb.length); i++) diff |= (ea[i] ?? 0) ^ (eb[i] ?? 0);
  return diff === 0;
}

export function must<T>(res: { data: T | null; error: { message: string } | null }, what: string): T {
  if (res.error) throw new HttpError(500, `${what}: ${res.error.message}`);
  if (res.data === null || res.data === undefined) throw new HttpError(404, `${what} not found`);
  return res.data;
}

export function parseVector(v: unknown): number[] | null {
  if (v == null) return null;
  if (Array.isArray(v)) return v as number[];
  if (typeof v === "string") return JSON.parse(v);
  return null;
}

export function toVector(v: number[] | null | undefined): string | null {
  return v ? `[${v.join(",")}]` : null;
}
