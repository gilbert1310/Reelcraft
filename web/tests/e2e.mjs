// Browser test of every screen against a fake Supabase (REST, storage, edge functions).
// Usage: npm run build with VITE_SUPABASE_URL=https://mock.supabase.co VITE_SUPABASE_ANON_KEY=anon,
// start `npx vite preview --port 4173`, then `node tests/e2e.mjs [screenshot-dir]`.
import { createRequire } from "node:module";
import { execFileSync } from "node:child_process";
import { existsSync, mkdirSync, readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const require = createRequire(import.meta.url);
const { chromium, devices } = require("playwright");

const HERE = dirname(fileURLToPath(import.meta.url));
const FIX = join(HERE, "fixtures");
const OUT = process.argv[2] || join(HERE, "screenshots");
mkdirSync(OUT, { recursive: true });
const APP = "http://localhost:4173";
const SB = "https://mock.supabase.co";
const UID = "11111111-1111-4111-8111-111111111111";

const video = join(FIX, "preview.mp4");
if (!existsSync(video)) {
  execFileSync("ffmpeg", ["-loglevel", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=540x960:rate=30:duration=4",
    "-pix_fmt", "yuv420p", video]);
}

// ---------- fake database ----------
let n = 0;
const id = () => `00000000-0000-4000-8000-${String(++n).padStart(12, "0")}`;
const now = () => new Date(Date.now() + n).toISOString();
const db = { uploads: [], setups: [], cuts: [], projects: [], captures: [], cut_matches: [], raw_projects: [],
  raw_clips: [], recipes: [], wrap_checks: [] };
const DEFAULTS = {
  uploads: { status: "queued", error: null, cut_count: null, setup_count: null },
  projects: { status: "shooting", reshoots: [], preview_path: null, overall_score: null, error: null },
  captures: { check_status: "pending", check_feedback: null, coverage: null, ai_checked: false, setup_id: null, suggested_setup_id: null, source: "phone" },
  raw_projects: { status: "uploading", suggestions: null, chosen_index: null, revisions: [], preview_path: null, error: null },
};
db.recipes.push(
  { id: id(), style_name: "Golden Hour Glide", genre: "wedding", moods: ["romantic", "cinematic"], summary: "Slow push-ins on the couple, cut on the beat with soft dissolves between angles.", duration: 18, cut_count: 14, setup_count: 3, created_at: now() },
  { id: id(), style_name: "Whip City", genre: "travel", moods: ["energetic"], summary: "Fast whip pans between handheld walking shots, two cuts per second.", duration: 12, cut_count: 22, setup_count: 4, created_at: now() },
);
const later = (ms, fn) => setTimeout(fn, ms);

function analyse(up) {
  const angles = ["behind", "front", "side"];
  const setups = angles.map((a, i) => ({
    id: id(), upload_id: up.id, user_id: UID, setup_number: i + 1, angle: a, subject: "couple",
    description: [
      "Medium-wide from behind as they walk away, camera at chest height following slowly. Soft golden backlight, shallow focus on the couple.",
      "Tight front shot, slow push-in on their faces as they turn to each other. Keep the sun behind them.",
      "Side profile tracking shot along the path, steady and smooth, matching their walking pace.",
    ][i],
    film_duration_seconds: [20, 12, 15][i], beginner_tip: [
      "Walk heel-to-toe and keep your elbows tucked in. It's the cheapest stabiliser there is.",
      "Tap to lock focus on the eyes before you start the push-in.",
      "Shoot at 60fps so this can become a slow-motion beat.",
    ][i], thumbnail_path: `${UID}/${up.id}/cut_${i}.jpg`, is_graphic: false,
    beats: [{ text: `Beat one of take ${i + 1}`, cuts: [i] }, { text: `Beat two of take ${i + 1}`, cuts: [i + 3] }],
  }));
  up.storage_path = up.storage_path || `${UID}/reference.mp4`;  // pretend the reference video is stored
  db.setups.push(...setups);
  for (let k = 0; k < 6; k++) {
    const s = setups[k % 3];
    db.cuts.push({ id: id(), upload_id: up.id, setup_id: s.id, setup_number: s.setup_number, order_index: k,
      start_time: k, end_time: k + 1, transition_in: "cut", speed: 1, action: "walking", thumbnail_path: `${UID}/${up.id}/cut_${k}.jpg` });
  }
  Object.assign(up, { status: "done", cut_count: 6, setup_count: 3, duration: 6 });
}

const ACTIONS = {
  "delete-upload": ({ upload_id }) => {
    db.uploads = db.uploads.filter((u) => u.id !== upload_id);
    return { ok: true };
  },
  "start-analysis": ({ upload_id, feedback }) => {
    const up = db.uploads.find((u) => u.id === upload_id);
    up.status = "processing";
    up.last_feedback = feedback;
    db.setups = db.setups.filter((x) => x.upload_id !== upload_id);
    db.cuts = db.cuts.filter((x) => x.upload_id !== upload_id);
    later(2500, () => analyse(up));
    return { job_id: id(), status: "processing" };
  },
  "check-clip": ({ capture_id }) => {
    const c = db.captures.find((x) => x.id === capture_id);
    c.check_status = "checking";
    later(2000, () => {
      const cuts = db.cuts.filter((x) => x.setup_id === c.setup_id);
      Object.assign(c, { check_status: "pass", ai_checked: true, check_feedback: "Great shot! A little shaky. The app will stabilise it.",
        coverage: cuts.map((x, i) => ({ cut_id: x.id, order_index: x.order_index, status: i === 1 ? "weak" : "covered",
          reason: i === 1 ? "Their faces are only briefly visible; a second longer would help." : "" })) });
    });
    return { job_id: id() };
  },
  "import-clip": ({ capture_id }) => {
    const c = db.captures.find((x) => x.id === capture_id);
    later(1500, () => { c.suggested_setup_id = db.setups[1].id; });
    return { job_id: id() };
  },
  "wrap-check": ({ project_id }) => {
    const p = db.projects.find((x) => x.id === project_id);
    const s = db.setups.filter((x) => x.upload_id === p.upload_id)[2];
    return { missing: [{ cut_id: "c", order_index: 5, setup_id: s.id, setup_number: 3,
      what_to_film: `${s.description} (not filmed yet).`, reason: "No clip yet", target_seconds: 15 }], unchecked_count: 1, all_covered: false };
  },
  assemble: ({ project_id }) => {
    const p = db.projects.find((x) => x.id === project_id);
    p.status = "assembling";
    later(2000, () => {
      const cuts = db.cuts.filter((x) => x.upload_id === p.upload_id);
      const fixes = [["Reframed", "Colour matched"], ["Digital push-in"], ["Stabilized", "Colour matched"], ["Slow motion (interpolated)"], [], ["Virtual pan"]];
      db.cut_matches.push(...cuts.map((c, i) => ({ id: id(), project_id, cut_id: c.id, order_index: c.order_index,
        score: [88, 92, 74, 61, 95, 83][i], fixes: fixes[i], flags: i === 3 ? ["Slow motion was made by frame interpolation. Film at 60fps for cleaner results."] : [],
        thumbnail_path: `${UID}/${project_id}/match_${i}.jpg` })));
      Object.assign(p, { status: "done", overall_score: 82, preview_path: `${UID}/${project_id}/preview.mp4`,
        timeline_path: `${UID}/${project_id}/timeline.fcpxml`, otio_path: `${UID}/${project_id}/timeline.otio`,
        reshoots: [{ setup_number: 3, cut_order_indexes: [5], reason: "Cut 6 needs 'the bride turning' and it wasn't found in your clips." }] });
    });
    return { job_id: id() };
  },
  suggest: ({ raw_project_id }) => {
    const rp = db.raw_projects.find((x) => x.id === raw_project_id);
    rp.status = "processing";
    later(2000, () => Object.assign(rp, { status: "ready", suggestions: [
      { recipe_id: db.recipes[0].id, style_name: "Golden Hour Glide", moods: ["romantic", "cinematic"], preview_path: `${UID}/raw/${rp.id}/suggestion_0.mp4` },
      { recipe_id: db.recipes[1].id, style_name: "Whip City", moods: ["energetic"], preview_path: `${UID}/raw/${rp.id}/suggestion_1.mp4` },
      { recipe_id: null, style_name: "Quiet Moments", moods: ["moody"], preview_path: `${UID}/raw/${rp.id}/suggestion_2.mp4` },
    ] }));
    return { job_id: id() };
  },
  choose: ({ raw_project_id, index }) => {
    const rp = db.raw_projects.find((x) => x.id === raw_project_id);
    Object.assign(rp, { chosen_index: index, preview_path: rp.suggestions[index].preview_path });
    return { ok: true };
  },
  revise: ({ raw_project_id, instruction }) => {
    const rp = db.raw_projects.find((x) => x.id === raw_project_id);
    rp.status = "revising";
    later(1500, () => Object.assign(rp, { status: "ready",
      revisions: [...rp.revisions, { instruction, preview_path: rp.preview_path, at: now() }] }));
    return { job_id: id() };
  },
  export: ({ raw_project_id }) => {
    const rp = db.raw_projects.find((x) => x.id === raw_project_id);
    rp.status = "exporting";
    later(1500, () => Object.assign(rp, { status: "exported", final_path: `${UID}/raw/${rp.id}/final.mp4`,
      timeline_path: `${UID}/raw/${rp.id}/timeline.fcpxml`, otio_path: `${UID}/raw/${rp.id}/timeline.otio` }));
    return { job_id: id() };
  },
};

// ---------- PostgREST-ish request handling ----------
function filterRows(table, params) {
  let rows = db[table];
  for (const [k, v] of params) {
    if (["select", "order", "limit", "offset", "columns"].includes(k)) continue;
    const m = /^(eq|is|cs)\.(.*)$/.exec(v);
    if (!m) continue;
    if (m[1] === "eq") rows = rows.filter((r) => String(r[k]) === decodeURIComponent(m[2]));
  }
  const order = params.get("order");
  if (order) {
    const [col, dir] = order.split(".");
    rows = [...rows].sort((a, b) => (a[col] > b[col] ? 1 : -1) * (dir === "desc" ? -1 : 1));
  }
  const limit = Number(params.get("limit"));
  return limit ? rows.slice(0, limit) : rows;
}

async function rest(route, req, table, params) {
  const single = (req.headers()["accept"] || "").includes("vnd.pgrst.object");
  const reply = (data) => route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(single ? data[0] ?? null : data) });
  const method = req.method();
  if (method === "GET") return reply(filterRows(table, params));
  if (method === "POST") {
    const body = req.postDataJSON();
    const rows = (Array.isArray(body) ? body : [body]).map((r) => ({ id: id(), user_id: UID, created_at: now(), ...DEFAULTS[table], ...r }));
    db[table].push(...rows);
    return reply(rows);
  }
  if (method === "PATCH") {
    const rows = filterRows(table, params);
    rows.forEach((r) => Object.assign(r, req.postDataJSON()));
    return reply(rows);
  }
  return route.fulfill({ status: 405, body: "" });
}

async function handle(route) {
  const req = route.request();
  const url = new URL(req.url());
  const p = url.pathname;
  if (req.method() === "OPTIONS") return route.fulfill({ status: 200, headers: cors(), body: "" });
  if (p.startsWith("/rest/v1/")) return rest(route, req, p.slice(9), url.searchParams);
  if (p === "/functions/v1/reelcraft") {
    const body = req.postDataJSON();
    const fn = ACTIONS[body.action];
    return route.fulfill({ status: fn ? 200 : 400, headers: cors(), contentType: "application/json",
      body: JSON.stringify(fn ? fn(body) : { error: "unknown action" }) });
  }
  if (p === "/auth/v1/user") return json(route, { id: UID, email: "gilbert@example.com", aud: "authenticated", role: "authenticated" });
  if (p.startsWith("/storage/v1/object/sign/")) {
    const path = p.slice("/storage/v1/object/sign/".length);
    return json(route, { signedURL: `/object/file/${path}?token=t` });
  }
  if (p.startsWith("/storage/v1/object/file/")) {
    const isVideo = p.endsWith(".mp4");
    const k = Number((p.match(/_(\d)\.jpg$/) || [0, 0])[1]) % 6;
    const file = isVideo ? video : join(FIX, `thumb${k}.jpg`);
    if (p.endsWith(".fcpxml") || p.endsWith(".otio")) return route.fulfill({ status: 200, body: "<fcpxml/>" });
    return route.fulfill({ status: 200, contentType: isVideo ? "video/mp4" : "image/jpeg", headers: cors(), body: readFileSync(file) });
  }
  if (p.startsWith("/storage/v1/object/")) return json(route, { Key: p });
  return route.fulfill({ status: 404, body: "" });
}
const cors = () => ({ "access-control-allow-origin": "*", "access-control-allow-headers": "*", "access-control-allow-methods": "*" });
const json = (route, body) => route.fulfill({ status: 200, headers: cors(), contentType: "application/json", body: JSON.stringify(body) });

// ---------- the test ----------
const browser = await chromium.launch({ args: ["--use-fake-device-for-media-stream", "--use-fake-ui-for-media-stream"] });
const context = await browser.newContext({ ...devices["iPhone 13"], permissions: ["camera", "microphone"] });
// iPhone emulation uses WebKit's UA but we run Chromium; keep MediaRecorder etc. available.
await context.route(`${SB}/**`, handle);
await context.addInitScript(([uid]) => {
  localStorage.setItem("sb-mock-auth-token", JSON.stringify({
    access_token: "header.eyJzdWIiOiIxIn0.sig", refresh_token: "r", token_type: "bearer",
    expires_in: 3600, expires_at: Math.floor(Date.now() / 1000) + 3600,
    user: { id: uid, email: "gilbert@example.com", aud: "authenticated", role: "authenticated" },
  }));
}, [UID]);
const page = await context.newPage();
const problems = [];
page.on("pageerror", (e) => problems.push(`pageerror: ${e.message}`));
page.on("console", (m) => m.type() === "error" && !/realtime|websocket|WebSocket/i.test(m.text()) && problems.push(`console: ${m.text()}`));
const shot = async (name) => page.screenshot({ path: join(OUT, `${name}.png`), fullPage: true });
const step = async (name, fn) => {
  try { await fn(); console.log(`✓ ${name}`); } catch (e) { problems.push(`${name}: ${e.message.split("\n")[0]}`); console.log(`✗ ${name}`); await shot(`FAIL-${name}`); }
};

await step("home", async () => {
  await page.goto(APP);
  await page.getByText("What are we making?").waitFor();
  await shot("01-home");
});

await step("stage 1: paste link → shoot plan", async () => {
  await page.getByText("Break down a reel").click();
  await page.getByRole("tab", { name: "Paste a link" }).click();
  await page.getByLabel("Reel link").fill("https://example.com/video");
  await page.getByRole("button", { name: "Analyse" }).click();
  await page.getByText("Paste a public Instagram").waitFor();
  await page.getByLabel("Reel link").fill("https://www.instagram.com/reel/abc123/");
  await shot("02-breakdown-link");
  await page.getByRole("button", { name: "Analyse" }).click();
  await page.getByText("Breaking down your reel…").waitFor();
  await shot("03-plan-busy");
  await page.getByText("Take 3", { exact: true }).waitFor({ timeout: 15000 });
  await page.getByText("This reel uses").waitFor();
  await page.getByText("Beat one of take 1").waitFor();
  await page.waitForTimeout(400);
  await shot("04-shoot-plan");
  await page.getByRole("button", { name: "Play: Beat 1: Beat one of take 1" }).click();
  await page.locator('video[aria-label="Beat 1: Beat one of take 1"]').waitFor();
  await page.waitForFunction(() => {
    const v = document.querySelector('video[aria-label="Beat 1: Beat one of take 1"]');
    return v && v.currentTime > 0.1;
  }, null, { timeout: 10000 });
  await page.waitForTimeout(600);
  await shot("04b-snippet-playing");
});

await step("stage 2: start shooting → project", async () => {
  await page.getByRole("button", { name: "Start Shooting" }).click();
  await page.getByText("0 of 3 captured").waitFor();
  await shot("05-project");
});

await step("stage 2: shoot mode records and checks a take", async () => {
  await page.getByRole("link", { name: "Shoot this setup" }).first().click();
  await page.getByText("Target 20s").waitFor();
  await page.waitForTimeout(800);
  await shot("06-shoot-ready");
  await page.getByRole("button", { name: "Start recording" }).click();
  await page.waitForTimeout(2200);
  await shot("07-shoot-recording");
  await page.getByRole("button", { name: "Stop recording" }).click();
  await page.getByText("Too short:").waitFor();
  await shot("08-shoot-too-short");
  await page.getByRole("button", { name: "Check it anyway" }).click();
  await page.getByText("Nailed it!").waitFor({ timeout: 15000 });
  await page.waitForTimeout(500);
  await shot("09-shoot-pass");
  if (!db.captures.some((c) => c.storage_path?.startsWith(`${UID}/`))) throw new Error("capture not stored under the user's folder");
  await page.getByRole("button", { name: "Next setup" }).click();
  await page.getByText("Target 12s").waitFor();
});

await step("stage 2: import from camera + wrap check", async () => {
  const pid = db.projects[0].id;
  await page.goto(`${APP}/project/${pid}`);
  await page.getByText("1 of 3 captured").waitFor();
  await page.locator('input[type=file][multiple]').setInputFiles(video);
  await page.getByText("Imported clips").waitFor({ timeout: 15000 });
  // The suggested setup arrives in the background and should be preselected.
  await page.waitForFunction(() => document.querySelector("select")?.value, null, { timeout: 15000 });
  await page.getByRole("button", { name: "Confirm and check" }).click();
  await page.getByText("2 of 3 captured").waitFor({ timeout: 15000 });
  await page.getByRole("button", { name: "🎯 Wrap check" }).click();
  await page.getByText("No clip yet").waitFor();
  await shot("10-project-wrap-check");
});

await step("stage 2: assemble → results + match report", async () => {
  await page.getByRole("button", { name: "Make a draft with what I have" }).click();
  await page.getByText("Cooking your reel…").waitFor();
  await page.getByText("Match report").waitFor({ timeout: 15000 });
  await page.waitForTimeout(800);
  await shot("11-results");
});

await step("stage 3: upload footage → suggestions → revise → export", async () => {
  await page.goto(`${APP}/footage`);
  await page.getByRole("button", { name: "Romantic" }).click();
  await page.getByLabel("Edit name").fill("Ama & Kofi wedding");
  await page.locator("input[type=file]").setInputFiles([video, video]);
  await shot("12-footage");
  await page.getByRole("button", { name: "Find styles for my footage" }).click();
  await page.getByText("Golden Hour Glide").waitFor({ timeout: 15000 });
  await page.waitForTimeout(600);
  await shot("13-suggestions");
  await page.getByRole("button", { name: "Use this" }).first().click();
  await page.getByLabel("Ask for changes").fill("More slow motion at the start");
  await page.getByRole("button", { name: "Send" }).click();
  await page.locator(".bubble", { hasText: "More slow motion at the start" }).waitFor({ timeout: 15000 });
  await page.getByRole("button", { name: "Export" }).click();
  await page.getByText("Download video").waitFor({ timeout: 15000 });
  await shot("14-exported");
});

await step("plans: no duplicates, re-analyse with a note, delete", async () => {
  // Same link again: the app offers the existing plan instead of adding a copy.
  page.once("dialog", (d) => d.dismiss());  // Cancel = open existing
  await page.goto(`${APP}/breakdown`);
  await page.getByRole("tab", { name: "Paste a link" }).click();
  await page.getByLabel("Reel link").fill("https://www.instagram.com/reel/abc123/");
  await page.getByRole("button", { name: "Analyse" }).click();
  await page.getByText("Your Shoot Plan").waitFor();
  if (db.uploads.length !== 1) throw new Error(`expected 1 upload, found ${db.uploads.length}`);
  await page.getByText("Take 1", { exact: true }).waitFor({ timeout: 15000 });
  await page.getByRole("button", { name: "Not right? Re-analyse" }).click();
  await page.getByPlaceholder(/Takes 1 and 4/).fill("Take 1 and take 3 are the same walk.");
  await shot("16-reanalyse");
  await page.getByRole("button", { name: "Re-analyse", exact: true }).click();
  await page.getByText("Breaking down your reel…").waitFor();
  if (db.uploads[0].last_feedback !== "Take 1 and take 3 are the same walk.") throw new Error("feedback not sent");
  await page.getByText("Take 1", { exact: true }).waitFor({ timeout: 15000 });
  // Delete from the plan page.
  page.once("dialog", (d) => d.accept());
  await page.getByRole("button", { name: "Delete this reel and its plan" }).click();
  await page.getByText("My shoots").waitFor();
  if (db.uploads.length !== 0) throw new Error("upload not deleted");
});

await step("library", async () => {
  await page.goto(`${APP}/library`);
  await page.getByText("Whip City").waitFor();
  await page.getByRole("button", { name: "energetic" }).click();
  if (await page.getByText("Golden Hour Glide").isVisible()) throw new Error("mood filter didn't filter");
  await shot("15-library");
});

await browser.close();
if (problems.length) {
  console.log("\nProblems:\n" + problems.join("\n"));
  process.exit(1);
}
console.log("\nAll flows passed.");
