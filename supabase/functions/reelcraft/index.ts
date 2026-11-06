// ReelCraft app API. The web app calls:
//   supabase.functions.invoke("reelcraft", { body: { action: "...", ...params } })
// Every action checks the row belongs to the signed-in user, records a job, and hands
// the heavy work to the Python video service, which reports back to reelcraft-callback.
import type { SupabaseClient } from "npm:@supabase/supabase-js@2.49.4";
import { adminClient, corsHeaders, env, HttpError, json, must, parseVector } from "../_shared/common.ts";

type Row = Record<string, any>;
const SIGNED_URL_SECONDS = 60 * 60 * 3;

Deno.serve(async (req) => {
  if (req.method === "OPTIONS") return new Response("ok", { headers: corsHeaders });
  try {
    const db = adminClient();
    const jwt = (req.headers.get("Authorization") ?? "").replace(/^Bearer\s+/i, "");
    const { data: auth } = await db.auth.getUser(jwt);
    if (!auth?.user) throw new HttpError(401, "Please sign in");
    const userId = auth.user.id;
    const body = await req.json().catch(() => ({}));
    const handler = ACTIONS[body.action as string];
    if (!handler) throw new HttpError(400, `Unknown action "${body.action}"`);
    return json(await handler({ db, userId, body }));
  } catch (e) {
    const status = e instanceof HttpError ? e.status : 500;
    console.error(e);
    return json({ error: e instanceof Error ? e.message : String(e) }, status);
  }
});

interface Ctx {
  db: SupabaseClient;
  userId: string;
  body: Row;
}

// ---------- helpers ----------

async function owned(db: SupabaseClient, table: string, id: unknown, userId: string, cols = "*"): Promise<Row> {
  if (typeof id !== "string") throw new HttpError(400, `Missing ${table} id`);
  const row = must(await db.from(table).select(cols).eq("id", id).maybeSingle(), table) as Row;
  if (row.user_id !== userId) throw new HttpError(404, `${table} not found`);
  return row;
}

async function signedUrl(db: SupabaseClient, bucket: string, path: string, userId?: string): Promise<string> {
  if (userId && !path.startsWith(`${userId}/`)) throw new HttpError(403, "That file isn't yours");
  const { data, error } = await db.storage.from(bucket).createSignedUrl(path, SIGNED_URL_SECONDS);
  if (error || !data) throw new HttpError(500, `Could not sign ${bucket}/${path}: ${error?.message}`);
  return data.signedUrl;
}

async function uploadUrl(db: SupabaseClient, bucket: string, path: string): Promise<string> {
  const { data, error } = await db.storage.from(bucket).createSignedUploadUrl(path, { upsert: true });
  if (error || !data) throw new HttpError(500, `Could not create upload URL: ${error?.message}`);
  return data.signedUrl;
}

/** Records a job and POSTs it to the video service. */
async function startJob(db: SupabaseClient, userId: string, kind: string, refId: string, endpoint: string,
                        payload: Row, meta: Row = {}): Promise<string> {
  const job = must(await db.from("jobs").insert({ user_id: userId, kind, ref_id: refId, meta })
    .select("id").single(), "job") as Row;
  const callback = `${env("SUPABASE_URL")}/functions/v1/reelcraft-callback`;
  const res = await fetch(`${env("REELCRAFT_SERVICE_URL").replace(/\/$/, "")}${endpoint}`, {
    method: "POST",
    headers: { "content-type": "application/json", "x-api-key": env("REELCRAFT_API_KEY") },
    body: JSON.stringify({ ...payload, job_id: job.id, callback_url: callback }),
  });
  if (!res.ok) {
    const text = await res.text();
    await db.from("jobs").update({ status: "failed", error: text.slice(0, 500), finished_at: new Date().toISOString() })
      .eq("id", job.id);
    throw new HttpError(502, `Video service rejected the job: ${text.slice(0, 200)}`);
  }
  return job.id;
}

async function setupsAndCuts(db: SupabaseClient, uploadId: string) {
  const setups = must(await db.from("setups").select("*").eq("upload_id", uploadId).order("setup_number"), "setups") as Row[];
  const cuts = must(await db.from("cuts").select("*").eq("upload_id", uploadId).order("order_index"), "cuts") as Row[];
  return { setups, cuts };
}

async function cutPayload(db: SupabaseClient, c: Row, withThumb = false): Promise<Row> {
  return {
    id: c.id, order_index: c.order_index, setup_number: c.setup_number,
    start_time: Number(c.start_time), end_time: Number(c.end_time), speed: Number(c.speed),
    transition_in: c.transition_in, action: c.action, stats: c.stats, embedding: parseVector(c.embedding),
    thumbnail_url: withThumb && c.thumbnail_path ? await signedUrl(db, "thumbs", c.thumbnail_path) : null,
  };
}

function setupPayload(s: Row): Row {
  return {
    setup_number: s.setup_number, description: s.description, angle: s.angle, subject: s.subject,
    film_duration_seconds: Number(s.film_duration_seconds), embedding: parseVector(s.embedding),
  };
}

// ---------- actions ----------

const ACTIONS: Record<string, (ctx: Ctx) => Promise<Row>> = {
  // Stage 1 ------------------------------------------------------------------
  async "start-analysis"({ db, userId, body }) {
    const up = await owned(db, "uploads", body.upload_id, userId);
    if (up.status === "processing") throw new HttpError(409, "Already analysing this reel");
    const payload: Row = { upload_id: up.id };
    if (up.source_url) payload.source_url = up.source_url;
    else payload.video_url = await signedUrl(db, "reels", up.storage_path, userId);
    await db.from("uploads").update({ status: "processing", error: null }).eq("id", up.id);
    try {
      const job_id = await startJob(db, userId, "analysis", up.id, "/analyze", payload);
      return { job_id, status: "processing" };
    } catch (e) {
      await db.from("uploads").update({ status: "failed", error: "Couldn't start the analysis. Try again." }).eq("id", up.id);
      throw e;
    }
  },

  // Stage 2: on set ------------------------------------------------------------
  async "check-clip"({ db, userId, body }) {
    const cap = await owned(db, "captures", body.capture_id, userId);
    if (!cap.setup_id) throw new HttpError(400, "Choose which setup this clip belongs to first");
    const setup = must(await db.from("setups").select("*").eq("id", cap.setup_id).single(), "setup") as Row;
    const project = await owned(db, "projects", cap.project_id, userId);
    if (setup.user_id !== userId || setup.upload_id !== project.upload_id) throw new HttpError(400, "That setup isn't part of this shoot");
    const cuts = must(await db.from("cuts").select("*").eq("setup_id", cap.setup_id).order("order_index"), "cuts") as Row[];
    await db.from("captures").update({ check_status: "checking", check_feedback: null }).eq("id", cap.id);
    const job_id = await startJob(db, userId, "check", cap.id, "/check-clip", {
      capture_id: cap.id,
      clip_url: await signedUrl(db, "captures", cap.storage_path, userId),
      setup: setupPayload(setup),
      cuts: await Promise.all(cuts.map((c) => cutPayload(db, c, true))),
      use_ai: body.use_ai !== false,
    });
    return { job_id, status: "checking" };
  },

  async "import-clip"({ db, userId, body }) {
    const cap = await owned(db, "captures", body.capture_id, userId);
    const project = await owned(db, "projects", cap.project_id, userId);
    const { setups, cuts } = await setupsAndCuts(db, project.upload_id);
    const firstCut = new Map<number, Row>();
    for (const c of cuts) if (!firstCut.has(c.setup_number)) firstCut.set(c.setup_number, c);
    const job_id = await startJob(db, userId, "suggest_setup", cap.id, "/suggest-setup", {
      capture_id: cap.id,
      clip_url: await signedUrl(db, "captures", cap.storage_path, userId),
      setups: await Promise.all(setups.map(async (s) => {
        const c = firstCut.get(s.setup_number);
        return {
          ...setupPayload(s),
          thumbnail_url: c?.thumbnail_path ? await signedUrl(db, "thumbs", c.thumbnail_path) : null,
        };
      })),
    });
    return { job_id };
  },

  // Runs here (no video work): which reference cuts aren't covered by any clip yet?
  async "wrap-check"({ db, userId, body }) {
    const project = await owned(db, "projects", body.project_id, userId);
    const { setups, cuts } = await setupsAndCuts(db, project.upload_id);
    const caps = must(await db.from("captures").select("id,setup_id,check_status,coverage,ai_checked")
      .eq("project_id", project.id).eq("user_id", userId), "captures") as Row[];
    const best = new Map<string, Row>(); // cut_id -> best coverage entry
    const rank: Record<string, number> = { covered: 2, weak: 1, missing: 0 };
    for (const cap of caps) {
      for (const cov of (cap.coverage ?? []) as Row[]) {
        const prev = best.get(cov.cut_id);
        if (!prev || rank[cov.status] > rank[prev.status]) best.set(cov.cut_id, cov);
      }
    }
    const setupById = new Map(setups.map((s) => [s.id, s]));
    const missing = cuts.filter((c) => (best.get(c.id)?.status ?? "missing") === "missing").map((c) => {
      const s = setupById.get(c.setup_id);
      const hasClip = caps.some((cap) => cap.setup_id === c.setup_id);
      return {
        cut_id: c.id, order_index: c.order_index, setup_id: c.setup_id, setup_number: c.setup_number,
        what_to_film: hasClip
          ? `${s?.description ?? "This setup"}. Make sure you capture: ${c.action || "the moment from cut " + (c.order_index + 1)}.`
          : `${s?.description ?? "This setup"} (not filmed yet).`,
        reason: best.get(c.id)?.reason ?? (hasClip ? "Not found in your clips" : "No clip yet"),
        target_seconds: Number(s?.film_duration_seconds ?? 10),
      };
    });
    const unchecked = caps.filter((c) => !c.ai_checked).length;
    const row = must(await db.from("wrap_checks").insert({
      user_id: userId, project_id: project.id, missing, unchecked_count: unchecked,
    }).select().single(), "wrap check") as Row;
    return { ...row, all_covered: missing.length === 0 };
  },

  async assemble({ db, userId, body }) {
    const project = await owned(db, "projects", body.project_id, userId);
    const { setups, cuts } = await setupsAndCuts(db, project.upload_id);
    const up = must(await db.from("uploads").select("fps,width,height").eq("id", project.upload_id).single(), "upload") as Row;
    const caps = must(await db.from("captures").select("*").eq("project_id", project.id).eq("user_id", userId), "captures") as Row[];
    const usable = caps.filter((c) => c.setup_id && (c.check_status === "pass" || (body.force && c.check_status !== "failed")));
    const setupNumber = new Map(setups.map((s) => [s.id, s.setup_number]));
    const missingSetups = setups.filter((s) => !usable.some((c) => c.setup_id === s.id));
    if (missingSetups.length && !body.force) {
      throw new HttpError(409, `Still need a passing clip for setup ${missingSetups.map((s) => s.setup_number).join(", ")}`);
    }
    if (!usable.length) throw new HttpError(409, "No clips to assemble yet");
    const base = `${userId}/${project.id}`;
    const paths = { preview: `${base}/preview.mp4`, fcpxml: `${base}/timeline.fcpxml`, otio: `${base}/timeline.otio` };
    await db.from("projects").update({ status: "assembling", error: null }).eq("id", project.id);
    const job_id = await startJob(db, userId, "assemble", project.id, "/assemble", {
      project_id: project.id,
      title: project.title ?? "ReelCraft edit",
      fps: Number(up.fps) || 30,
      captures: await Promise.all(usable.map(async (c) => ({
        id: c.id, url: await signedUrl(db, "captures", c.storage_path, userId),
        file_name: c.file_name ?? c.storage_path.split("/").pop(), setup_number: setupNumber.get(c.setup_id),
      }))),
      cuts: await Promise.all(cuts.map((c) => cutPayload(db, c))),
      outputs: {
        preview_upload_url: await uploadUrl(db, "renders", paths.preview),
        fcpxml_upload_url: await uploadUrl(db, "renders", paths.fcpxml),
        otio_upload_url: await uploadUrl(db, "renders", paths.otio),
      },
    }, { paths });
    return { job_id, status: "assembling" };
  },

  // Stage 3 ------------------------------------------------------------------
  async suggest({ db, userId, body }) {
    const rp = await owned(db, "raw_projects", body.raw_project_id, userId);
    const clips = await rawClips(db, rp.id, userId);
    let q = db.from("recipes").select("*").not("embedding", "is", null).order("created_at", { ascending: false }).limit(300);
    if (rp.mood) q = q.contains("moods", [String(rp.mood).toLowerCase()]);
    let recipes = must(await q, "recipes") as Row[];
    if (recipes.length < 3) {
      recipes = must(await db.from("recipes").select("*").not("embedding", "is", null)
        .order("created_at", { ascending: false }).limit(300), "recipes") as Row[];
    }
    const base = `${userId}/raw/${rp.id}`;
    const previews = [0, 1, 2].map((k) => `${base}/suggestion_${k}.mp4`);
    await db.from("raw_projects").update({ status: "processing", error: null }).eq("id", rp.id);
    const job_id = await startJob(db, userId, "suggest", rp.id, "/suggest", {
      raw_project_id: rp.id, mood: rp.mood, clips,
      recipes: recipes.map(recipePayload),
      outputs: await Promise.all(previews.map(async (p) => ({ preview_upload_url: await uploadUrl(db, "renders", p) }))),
    }, { previews });
    return { job_id, status: "processing" };
  },

  async choose({ db, userId, body }) {
    const rp = await owned(db, "raw_projects", body.raw_project_id, userId);
    const s = (rp.suggestions ?? [])[Number(body.index)];
    if (!s) throw new HttpError(400, "No such suggestion");
    await db.from("raw_projects").update({
      chosen_index: Number(body.index), chosen_recipe_id: s.recipe_id ?? null, plan: s.plan, preview_path: s.preview_path,
    }).eq("id", rp.id);
    return { ok: true };
  },

  async revise({ db, userId, body }) {
    const rp = await owned(db, "raw_projects", body.raw_project_id, userId);
    const instruction = String(body.instruction ?? "").trim().slice(0, 500);
    if (!instruction) throw new HttpError(400, "Say what you'd like changed");
    if (!rp.plan) throw new HttpError(409, "Choose a suggestion first");
    const n = (rp.revisions ?? []).length + 1;
    const preview = `${userId}/raw/${rp.id}/revision_${n}.mp4`;
    await db.from("raw_projects").update({ status: "revising" }).eq("id", rp.id);
    const job_id = await startJob(db, userId, "revise", rp.id, "/revise", {
      raw_project_id: rp.id, instruction, plan: rp.plan, labels: rp.labels ?? [],
      clips: await rawClips(db, rp.id, userId), recipe: await recipeFor(db, rp.chosen_recipe_id),
      outputs: { preview_upload_url: await uploadUrl(db, "renders", preview) },
    }, { preview, instruction });
    return { job_id, status: "revising" };
  },

  async export({ db, userId, body }) {
    const rp = await owned(db, "raw_projects", body.raw_project_id, userId);
    if (!rp.plan) throw new HttpError(409, "Choose a suggestion first");
    const base = `${userId}/raw/${rp.id}`;
    const paths = { final: `${base}/final.mp4`, fcpxml: `${base}/timeline.fcpxml`, otio: `${base}/timeline.otio` };
    await db.from("raw_projects").update({ status: "exporting" }).eq("id", rp.id);
    const job_id = await startJob(db, userId, "export", rp.id, "/export", {
      raw_project_id: rp.id, plan: rp.plan, clips: await rawClips(db, rp.id, userId),
      recipe: await recipeFor(db, rp.chosen_recipe_id), title: rp.title ?? "ReelCraft edit",
      outputs: {
        preview_upload_url: await uploadUrl(db, "renders", paths.final),
        fcpxml_upload_url: await uploadUrl(db, "renders", paths.fcpxml),
        otio_upload_url: await uploadUrl(db, "renders", paths.otio),
      },
    }, { paths });
    return { job_id, status: "exporting" };
  },
};

async function rawClips(db: SupabaseClient, rawProjectId: string, userId: string): Promise<Row[]> {
  const rows = must(await db.from("raw_clips").select("*").eq("raw_project_id", rawProjectId).eq("user_id", userId)
    .order("created_at"), "clips") as Row[];
  if (!rows.length) throw new HttpError(400, "Upload some clips first");
  return Promise.all(rows.map(async (c) => ({
    id: c.id, url: await signedUrl(db, "raw", c.storage_path, userId), file_name: c.file_name ?? c.storage_path.split("/").pop(),
  })));
}

function recipePayload(r: Row): Row {
  return {
    id: r.id, style_name: r.style_name, genre: r.genre, moods: r.moods, summary: r.summary,
    duration: Number(r.duration), pacing: r.pacing, transitions: r.transitions, slowmo_beats: r.slowmo_beats,
    setup_pattern: r.setup_pattern, setup_types: r.setup_types, embedding: parseVector(r.embedding),
  };
}

async function recipeFor(db: SupabaseClient, id: string | null): Promise<Row> {
  if (!id) return {};
  const { data } = await db.from("recipes").select("*").eq("id", id).maybeSingle();
  return data ? recipePayload(data) : {};
}
