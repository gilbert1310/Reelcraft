// Receives results from the Python video service and writes them to the database.
// Authenticated with the x-reelcraft-secret header (REELCRAFT_CALLBACK_SECRET), not a user JWT.
import type { SupabaseClient } from "npm:@supabase/supabase-js@2.49.4";
import { adminClient, env, HttpError, json, must, safeEqual, toVector } from "../_shared/common.ts";

type Row = Record<string, any>;

Deno.serve(async (req) => {
  try {
    if (req.method !== "POST") throw new HttpError(405, "POST only");
    const secret = req.headers.get("x-reelcraft-secret") ?? "";
    if (!safeEqual(secret, env("REELCRAFT_CALLBACK_SECRET"))) throw new HttpError(401, "Bad secret");
    const body = await req.json();
    const db = adminClient();
    const job = must(await db.from("jobs").select("*").eq("id", body.job_id).maybeSingle(), "job") as Row;
    if (job.status !== "processing") return json({ ok: true, duplicate: true });

    const ok = body.status === "done";
    const handler = HANDLERS[job.kind];
    try {
      await handler(db, job, ok ? body.result : null, ok ? null : (body.error ?? "Processing failed"), body.status);
    } catch (e) {
      console.error("Saving results failed", e);
      await db.from("jobs").update({ status: "failed", error: `Saving results failed: ${e}`.slice(0, 500),
        finished_at: new Date().toISOString() }).eq("id", job.id);
      throw e;
    }
    await db.from("jobs").update({
      status: body.status, error: body.error ?? null, seconds: body.seconds ?? null,
      finished_at: new Date().toISOString(),
    }).eq("id", job.id);
    if (body.detail) console.error(`job ${job.id} (${job.kind}) detail: ${body.detail}`);
    return json({ ok: true });
  } catch (e) {
    console.error(e);
    return json({ error: e instanceof Error ? e.message : String(e) }, e instanceof HttpError ? e.status : 500);
  }
});

type Handler = (db: SupabaseClient, job: Row, result: Row | null, error: string | null, status: string) => Promise<void>;

function bytes(b64: string): Uint8Array {
  return Uint8Array.from(atob(b64), (c) => c.charCodeAt(0));
}

async function putThumb(db: SupabaseClient, path: string, b64: string | null | undefined): Promise<string | null> {
  if (!b64) return null;
  const { error } = await db.storage.from("thumbs").upload(path, bytes(b64), { contentType: "image/jpeg", upsert: true });
  if (error) {
    console.error("thumb upload failed", path, error.message);
    return null;
  }
  return path;
}

async function check(res: { error: { message: string } | null }, what: string) {
  if (res.error) throw new Error(`${what}: ${res.error.message}`);
}

const HANDLERS: Record<string, Handler> = {
  async analysis(db, job, result, error, status) {
    const uploadId = job.ref_id;
    if (!result) {
      await db.from("uploads").update({ status: status === "link_failed" ? "link_failed" : "failed", error }).eq("id", uploadId);
      return;
    }
    const up = must(await db.from("uploads").select("*").eq("id", uploadId).single(), "upload") as Row;
    const userId = up.user_id;
    // Idempotent: replace any previous breakdown of this reel.
    await check(await db.from("setups").delete().eq("upload_id", uploadId), "clear setups");

    const setupRows = must(await db.from("setups").insert(result.setups.map((s: Row) => ({
      upload_id: uploadId, user_id: userId, setup_number: s.setup_number, description: s.description,
      angle: s.angle, subject: s.subject, motion: s.motion, film_duration_seconds: s.film_duration_seconds,
      beginner_tip: s.beginner_tip, embedding: toVector(s.embedding), is_graphic: Boolean(s.is_graphic),
      beats: Array.isArray(s.beats) ? s.beats : [],
    }))).select("id,setup_number"), "insert setups") as Row[];
    const setupId = new Map(setupRows.map((s) => [s.setup_number, s.id]));

    const cutRows = await Promise.all(result.cuts.map(async (c: Row) => ({
      upload_id: uploadId, user_id: userId, setup_id: setupId.get(c.setup_number), setup_number: c.setup_number,
      order_index: c.order_index, start_time: c.start_time, end_time: c.end_time, transition_in: c.transition_in,
      speed: c.speed, action: c.action, stats: c.stats, embedding: toVector(c.embedding),
      thumbnail_path: await putThumb(db, `${userId}/${uploadId}/cut_${c.order_index}.jpg`, c.thumbnail_b64),
    })));
    await check(await db.from("cuts").insert(cutRows), "insert cuts");

    // Setup card thumbnail = its first cut's frame.
    for (const s of setupRows) {
      const first = cutRows.find((c) => c.setup_id === s.id && c.thumbnail_path);
      if (first) await db.from("setups").update({ thumbnail_path: first.thumbnail_path }).eq("id", s.id);
    }

    const r = result.recipe;
    if (r) {
      await db.from("recipes").delete().eq("source_upload_id", uploadId);
      await check(await db.from("recipes").insert({
        user_id: userId, source_upload_id: uploadId, style_name: r.style_name, genre: r.genre,
        moods: r.moods ?? [], summary: r.summary, duration: r.duration, cut_count: r.cut_count,
        setup_count: r.setup_count, pacing: r.pacing, transitions: r.transitions, slowmo_beats: r.slowmo_beats,
        setup_pattern: r.setup_pattern, setup_types: r.setup_types, embedding: toVector(r.embedding),
      }), "insert recipe");
    }

    const v = result.video ?? {};
    await check(await db.from("uploads").update({
      status: "done", error: null, duration: v.duration, width: v.width, height: v.height, fps: v.fps,
      cut_count: result.cuts.length, setup_count: result.setups.length, plan_summary: result.plan_summary ?? null,
      review: result.review ?? null,
    }).eq("id", uploadId), "update upload");

    // The reference video is kept so the plan can be re-analysed; it's removed when the user
    // deletes the plan (action "delete-upload"). The style library stores patterns only.
  },

  async check(db, job, result, error) {
    if (!result) {
      await db.from("captures").update({ check_status: "failed", check_feedback: error }).eq("id", job.ref_id);
      return;
    }
    await check(await db.from("captures").update({
      check_status: result.check_status, check_feedback: result.check_feedback, coverage: result.coverage,
      measurements: { ...result.measurements, warnings: result.warnings, problems: result.problems },
      ai_checked: !(result.warnings ?? []).some((w: string) => w.startsWith("AI check unavailable")),
    }).eq("id", job.ref_id), "update capture");
  },

  async suggest_setup(db, job, result) {
    if (!result?.suggested_setup_number) return;
    const cap = must(await db.from("captures").select("project_id").eq("id", job.ref_id).single(), "capture") as Row;
    const project = must(await db.from("projects").select("upload_id").eq("id", cap.project_id).single(), "project") as Row;
    const { data: setup } = await db.from("setups").select("id").eq("upload_id", project.upload_id)
      .eq("setup_number", result.suggested_setup_number).maybeSingle();
    if (setup) await db.from("captures").update({ suggested_setup_id: setup.id }).eq("id", job.ref_id);
  },

  async assemble(db, job, result, error) {
    const projectId = job.ref_id;
    if (!result) {
      await db.from("projects").update({ status: "failed", error }).eq("id", projectId);
      return;
    }
    const p = job.meta.paths;
    const project = must(await db.from("projects").select("user_id").eq("id", projectId).single(), "project") as Row;
    await check(await db.from("cut_matches").delete().eq("project_id", projectId), "clear matches");
    const rows = await Promise.all((result.cut_matches ?? []).map(async (m: Row) => ({
      user_id: project.user_id, project_id: projectId, cut_id: m.cut_id, order_index: m.order_index,
      score: m.score, fixes: m.fixes, flags: m.flags, clip: m.clip, source_in: m.source_in,
      thumbnail_path: await putThumb(db, `${project.user_id}/${projectId}/match_${m.order_index}.jpg`, m.thumbnail_b64),
    })));
    if (rows.length) await check(await db.from("cut_matches").insert(rows), "insert matches");
    await check(await db.from("projects").update({
      status: "done", error: null, preview_path: p.preview, timeline_path: p.fcpxml, otio_path: p.otio,
      overall_score: result.overall_score, reshoots: result.reshoots ?? [], edit_list: result.edit_list,
    }).eq("id", projectId), "update project");
  },

  async suggest(db, job, result, error) {
    const id = job.ref_id;
    if (!result) {
      await db.from("raw_projects").update({ status: "failed", error }).eq("id", id);
      return;
    }
    const previews: string[] = job.meta.previews ?? [];
    const suggestions = (result.suggestions ?? []).map((s: Row) => ({
      recipe_id: s.recipe_id, style_name: s.style_name, moods: s.moods, match_score: s.match_score,
      plan: s.plan, preview_path: previews[s.index] ?? null,
    }));
    for (const lab of result.labels ?? []) {
      await db.from("raw_clips").update({ labels: lab, duration: lab.duration }).eq("id", lab.clip_id);
    }
    await check(await db.from("raw_projects").update({
      status: "ready", error: null, labels: result.labels, suggestions,
    }).eq("id", id), "update raw project");
  },

  async revise(db, job, result, error) {
    const id = job.ref_id;
    if (!result) {
      await db.from("raw_projects").update({ status: "ready", error }).eq("id", id);
      return;
    }
    const rp = must(await db.from("raw_projects").select("revisions").eq("id", id).single(), "raw project") as Row;
    const revisions = [...(rp.revisions ?? []),
      { instruction: job.meta.instruction, preview_path: job.meta.preview, at: new Date().toISOString() }];
    await check(await db.from("raw_projects").update({
      status: "ready", error: null, plan: result.plan, preview_path: job.meta.preview, revisions,
      labels: result.labels,
    }).eq("id", id), "update raw project");
  },

  async export(db, job, result, error) {
    const id = job.ref_id;
    if (!result) {
      await db.from("raw_projects").update({ status: "ready", error }).eq("id", id);
      return;
    }
    const p = job.meta.paths;
    await check(await db.from("raw_projects").update({
      status: "exported", error: null, final_path: p.final, timeline_path: p.fcpxml, otio_path: p.otio,
    }).eq("id", id), "update raw project");
  },
};
