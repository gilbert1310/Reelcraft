# ReelCraft

Break a reel down into a smart shoot plan, guide the videographer while filming, then assemble
their real footage into a finished edit, with a DaVinci Resolve timeline for the final polish.

This repo has the whole app for all three stages of the ReelCraft Build Plan: the web app, the
video service and the Supabase backend. Lovable isn't needed (`lovable/PROMPTS.md` is kept only as
an alternative).

```
web/                  The app (React + TypeScript + Vite), mobile-first. Talks to Supabase only.
  src/pages/          Sign-in, Home, Breakdown, Shoot Plan, Shoots, Project, Shoot Mode, Footage, Edit, Library
  tests/e2e.mjs       Clicks through every flow in a phone-sized browser against a fake Supabase
service/              Python video service (FastAPI). One service, one endpoint per job.
  app/breakdown.py    Stage 1: split → fingerprint → group into setups → describe → edit pattern + recipe
  app/checks.py       Stage 2: on-set technical + AI coverage checks, setup suggestion for imports
  app/assemble.py     Stage 2: match each reference cut to the user's footage, render, export
  app/correct.py      Match engine: virtual camera (reframe, push-in, pan), stabilise, shake, speed, colour
  app/render.py       Frame renderer (crops, colour, dissolve/whip/zoom transitions) → MP4
  app/timeline.py     FCPXML 1.10 + OpenTimelineIO export for DaVinci Resolve
  app/library.py      Stage 3: label raw clips, pick recipes, build/revise cut plans, export
  app/vision.py       Claude vision calls with typed JSON outputs
  tests/              End-to-end tests on generated footage (no API key or network needed)
supabase/
  migrations/         Tables, row-level security, storage buckets, realtime
  functions/reelcraft/           The app's API (one function, many actions)
  functions/reelcraft-callback/  Receives results from the Python service
lovable/PROMPTS.md    Front-end prompts, in order, matching this backend
```

## How the pieces fit

```
Lovable app ──upload──▶ Supabase Storage
     │
     └─invoke('reelcraft', {action})──▶ edge function ──signed URLs──▶ Python service (Railway)
                                             ▲                               │
     Realtime updates ◀── tables ◀── reelcraft-callback ◀──── results ───────┘
```

- The Python service never holds your Supabase keys. It gets short-lived signed URLs to read
  videos and to upload renders, and POSTs results to `reelcraft-callback` with a shared secret.
- Every job is a row in `jobs`; the app watches `uploads`, `captures`, `projects` and
  `raw_projects` with Supabase Realtime instead of polling.

### Edge function actions (`supabase.functions.invoke('reelcraft', { body: { action, ... } })`)

| Action | Body | What happens |
|---|---|---|
| `start-analysis` | `upload_id` | Stage 1 breakdown of an uploaded reel or a pasted link |
| `check-clip` | `capture_id` | Technical + AI coverage check of a take, per reference cut |
| `import-clip` | `capture_id` | Suggests which setup a pro-camera clip belongs to |
| `wrap-check` | `project_id` | Lists every reference cut not covered yet (runs instantly, no video work) |
| `assemble` | `project_id` (`force`) | Match engine + preview MP4 + `.fcpxml` + `.otio` |
| `suggest` | `raw_project_id` | Stage 3: label clips, pick 3 recipes, render 3 previews |
| `choose` | `raw_project_id`, `index` | Pick a suggestion |
| `revise` | `raw_project_id`, `instruction` | "More slow motion" → new plan + preview |
| `export` | `raw_project_id` | Full-res MP4 + Resolve timelines |

## Deploy

### 1. Supabase

Use a new Supabase project for ReelCraft (your existing projects belong to other apps). With
the [Supabase CLI](https://supabase.com/docs/guides/cli):

```bash
supabase link --project-ref YOUR_PROJECT_REF
```

```bash
supabase db push
```

```bash
supabase functions deploy reelcraft
```

```bash
supabase functions deploy reelcraft-callback --no-verify-jwt
```

```bash
supabase secrets set REELCRAFT_SERVICE_URL=https://YOUR-SERVICE.up.railway.app REELCRAFT_API_KEY=... REELCRAFT_CALLBACK_SECRET=...
```

Generate the two secrets with `openssl rand -hex 32`. Set `KEEP_REFERENCE_VIDEOS=true` only if
you want uploaded reference reels kept after analysis (by default only the recipe is kept).

### 2. Python service (Railway or Render)

Deploy the `service/` folder with its Dockerfile. Give it **at least 2 GB RAM (4 GB recommended)**,
because CLIP and 4K frames are memory-hungry. Environment variables (see `service/.env.example`):

| Variable | Value |
|---|---|
| `REELCRAFT_API_KEY` | same as the Supabase secret |
| `REELCRAFT_CALLBACK_SECRET` | same as the Supabase secret |
| `CALLBACK_URL_PREFIX` | `https://YOUR_PROJECT_REF.supabase.co/functions/v1/reelcraft-callback` |
| `ANTHROPIC_API_KEY` | your Anthropic API key |
| `VISION_MODEL` | `claude-opus-5-5` (default). `claude-sonnet-5-5` is about half the price. |
| `MAX_CONCURRENT_JOBS` | `2` (raise with more RAM/CPU) |

Check it's up at `GET /health`.

### 3. Web app (Vercel)

Import the repo in Vercel with **Root Directory `web`** (Vite is detected automatically) and set:

| Variable | Value |
|---|---|
| `VITE_SUPABASE_URL` | `https://YOUR_PROJECT_REF.supabase.co` |
| `VITE_SUPABASE_ANON_KEY` | the project's anon / publishable key |

Then in Supabase › Authentication › URL Configuration, set the Site URL to your Vercel URL so
sign-in links come back to the app. Shoot Mode needs HTTPS for the camera, which Vercel provides.

Run it locally with `cd web && npm install && npm run dev` (put the two variables in `web/.env`).

## Run the tests locally

Web app (builds against a fake Supabase, then clicks through every screen at iPhone size,
using Chromium's fake camera for Shoot Mode):

```bash
cd web && VITE_SUPABASE_URL=https://mock.supabase.co VITE_SUPABASE_ANON_KEY=anon npx vite build && (npx vite preview --port 4173 &) && node tests/e2e.mjs
```

Video service:

```bash
cd service && python -m venv .venv && . .venv/bin/activate && pip install -r requirements-dev.txt
```

```bash
VISION_PROVIDER=mock EMBEDDER=histogram python -m pytest -q tests
```

The tests generate a reference reel that cuts back and forth between two setups (6 cuts), then
check that it comes back as 2 setups with the right pattern. They also check coverage on new
"user" takes, render the assembled preview (including slow motion, dissolve, whip and zoom
transitions), confirm colour matching kicks in, and confirm the timelines open in OpenTimelineIO.
The Stage 3 suggest → revise → export loop is covered too. They run offline: `mock` replaces the
vision AI and `histogram` replaces CLIP.

## Review notes: where the build differs from the plan, and why

1. **Results come back through `reelcraft-callback`, not by Python writing to the database.**
   The plan had both patterns (Stage 1 prompt: Python with the service key; link section:
   `save-analysis`). One callback keeps the service-role key out of the Python host and works
   the same for every stage.
2. **One `reelcraft` edge function with actions** instead of a function per step: one deploy,
   one place for ownership checks.
3. **`check-clip` and `coverage-check` are one call.** The take is checked against the setup
   and against every reference cut it must cover. **Wrap check runs inside the edge function**
   from the stored coverage, so it's instant and needs no video work.
4. **Each reference cut's fingerprint, motion/framing/colour stats and thumbnail are saved at
   analysis time.** That's what lets the source reel be deleted ("store the recipe, never the
   video") while Stage 2 still compares against the original.
5. **No YOLO.** Ultralytics YOLO is AGPL-3.0, which would oblige you to open-source a paid
   product. Subject framing uses OpenCV's person detector with a saliency fallback. If you
   want better detection, swap in a permissively licensed model.
6. **No RIFE.** Slow motion from normal-speed footage uses ffmpeg's motion interpolation
   (`minterpolate`), so no GPU is needed. It's flagged in the match report, and filming at
   60fps still gives the best result.
7. **Colour match is baked into the preview only.** The Resolve timeline gets markers listing
   each cut's fixes (and flags). Grading belongs in Resolve.
8. **Previews are silent** (no copyrighted audio from the reference). `music_url` is supported
   if you add licensed music.
9. **Browser recordings are normalised.** MediaRecorder WebM files often have no duration, and
   10-bit HEVC from iPhones/cameras decodes unreliably frame by frame. Both are converted to
   H.264 MP4 before analysis. Timelines still point at the original file names.
10. **Vision model:** Claude Opus 5.5 at medium effort, with Anthropic's server-side refusal
    fallback on. Each AI call returns schema-validated JSON. If any AI step fails, the pipeline
    falls back to the fingerprint-only result instead of failing the job.

## Not verified yet (do these on your first deploy)

- **Real CLIP weights and real Claude calls.** This build environment couldn't reach Hugging Face
  or use an API key, so tests ran with the offline stand-ins. The Docker build downloads CLIP;
  run one real reel through and tune `GROUP_THRESHOLD` (start 0.85) and the cinematographer
  prompt, as the plan says (5 to 10 real reels).
- **Edge functions against a live project.** They type-check with Deno, and the migration ran
  on Postgres 16 with stand-ins for Supabase's `auth`, `storage` and pgvector (row-level
  security behaved as intended). They haven't been deployed to a real project.
- **Resolve import of transforms.** Cuts, in/out points and speed are standard. The animated
  scale/position keyframes in the FCPXML use FCP's convention (position in % of frame height);
  check one push-in after importing. The `.otio` carries the same data in clip metadata.
- **Link fetching** (yt-dlp) is a grey area under the platforms' terms and breaks when they
  change. Redeploy monthly to update yt-dlp, or move to a paid fetch service.

## Costs to watch

- **Vision AI:** Stage 1 makes about 2 + setups calls per reel. Each check-clip is 1 call;
  Stage 3 is about 1 call per 8 clips plus 1 per suggestion and revision. Frames are sent at
  512 px to keep this low.
- **Hosting:** the service is CPU-only. Measured on 4 vCPUs: 10 s of 1080×1920 output from 4K
  footage, with a push-in and colour match on every cut, renders in about 13 s. That's about
  1.3× the reel's length, plus download and analysis time.
- **Storage:** reference reels are deleted after analysis. Captures, raw clips and renders stay
  until you delete them, so add a cleanup job once you have users.
