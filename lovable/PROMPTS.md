# Lovable prompts

Paste these into Lovable **in order**, one at a time, and check each works before the next.
They match the backend in this repo exactly (table names, the `reelcraft` edge function's
actions, storage paths), so keep the wording of names and paths.

Before prompt 1: connect the Lovable project to the Supabase project where you ran the
migration and deployed the edge functions (see the README).

---

## 1. Sign-in and home

```
ReelCraft uses Supabase. Add email sign-in (magic link) with Supabase Auth; every page except the
sign-in page requires a signed-in user.

Home screen (mobile-first, dark theme): three big options:
1. "Break down a reel" (Stage 1)
2. "My shoots" (list of rows from 'projects', newest first, with their status and the reference
   reel's file name)
3. "Edit my own footage" (Stage 3, built later; show it as "Coming soon" for now)
```

## 2. Stage 1: upload or paste a link, then the Shoot Plan

```
"Break down a reel" screen: a tab switch 'Upload a video' | 'Paste a link'.

Upload tab: pick a video file (max 500 MB). Upload it to Supabase Storage bucket 'reels' at the
path `${user.id}/${crypto.randomUUID()}-${file.name}` with a progress bar. Then insert a row into
'uploads' with { file_name, storage_path }.

Link tab: a text field 'Paste an Instagram, TikTok, Facebook or YouTube link'. Accept only http(s)
links whose host is instagram.com, tiktok.com, facebook.com, fb.watch, youtube.com or youtu.be
(or their subdomains); otherwise show a friendly error. Insert a row into 'uploads' with
{ source_url }. Under the field add a small note: 'Only public videos work. We analyse the style
and don't keep the video.'

Both tabs then call:
  supabase.functions.invoke('reelcraft', { body: { action: 'start-analysis', upload_id } })
and navigate to the Shoot Plan screen for that upload.

Shoot Plan screen ('Your Shoot Plan'): subscribe with Supabase Realtime to the 'uploads' row
(fall back to polling every 3 seconds). While status is 'queued' or 'processing', show
'Breaking down your reel…' with a friendly animation.
- 'failed': show the row's error and a 'Try again' button that calls start-analysis again.
- 'link_failed': show 'We couldn't grab that video. It may be private, or the platform blocked
  it. Screen-record it and upload it instead.' with a button that opens the Upload tab.
- 'done': read 'setups' for this upload ordered by setup_number. Above the cards show
  'This reel uses {uploads.cut_count} cuts from just {uploads.setup_count} setups.' Show each
  setup as a card: thumbnail (signed URL from bucket 'thumbs' for setups.thumbnail_path, 1 hour),
  'Setup {setup_number}', an angle badge (setups.angle, capitalised), the description,
  'Film about {film_duration_seconds} seconds', and the beginner tip with a lightbulb icon.
  At the bottom, a big 'Start Shooting' button (wired up in the next prompt).
```

## 3. Stage 2: Shoot Mode with on-set checks

```
'Start Shooting' on the Shoot Plan inserts a 'projects' row { upload_id, title } (title = the
upload's file_name or 'My shoot') and opens Shoot Mode for that project.

Shoot Mode (mobile-first, full screen):
- Live camera using getUserMedia + MediaRecorder (rear camera, highest resolution available,
  prefer 60fps). Overlay the current setup card at the top: setup number, angle badge,
  description, target seconds. Show a recording timer that turns green when it reaches
  film_duration_seconds.
- Instant checks before upload (in the browser): the recording length against the target, and
  the video's resolution from the track settings. If too short, show 'Too short: hold about N
  more seconds' with a Retake button, and don't upload.
- Upload the recording to bucket 'captures' at `${user.id}/${project.id}/${crypto.randomUUID()}.webm`
  (or .mp4 if MediaRecorder gives mp4). Insert a 'captures' row
  { project_id, setup_id, storage_path, file_name, source: 'phone' }, then call
  supabase.functions.invoke('reelcraft', { body: { action: 'check-clip', capture_id } }).
- Subscribe with Realtime to that 'captures' row. While check_status is 'checking' show
  'Checking your shot…'. When it's 'pass': a green tick, a small celebration animation, and
  'Next setup'. When it's 'retry' or 'failed': show check_feedback and a 'Retake' button.
- Show the per-cut checklist from captures.coverage (array of { order_index, status, reason }):
  green 'covered', amber 'weak', red 'missing', each with its reason.
- Progress bar: 'N of M setups captured' (a setup counts when it has a capture with
  check_status 'pass').
- If the phone is offline, save the recording in IndexedDB with a 'Waiting for signal' badge;
  when back online, upload it and run check-clip automatically.

Project screen (from 'My shoots'): list the setups with their captures and statuses, plus:
- 'Import from camera': choose video files (phone gallery or laptop). For each file, upload it
  to 'captures' at `${user.id}/${project.id}/${crypto.randomUUID()}-${file.name}`, insert a
  'captures' row with source 'import', setup_id null and file_name = the original file name,
  and call action 'import-clip'. When captures.suggested_setup_id fills in, preselect it in a
  dropdown 'This clip is for: Setup N'. When the user confirms, update the capture's setup_id
  and call 'check-clip'.
- A prominent 'Wrap check' button: call action 'wrap-check' with { project_id }. If the response
  has all_covered, show 'All covered, you're good to go!'. Otherwise list response.missing
  (each: 'Setup {setup_number}: {what_to_film}', the reason, and target_seconds) with a
  'Reshoot' button that opens Shoot Mode on that setup. If unchecked_count > 0, warn
  '{unchecked_count} clips haven't been AI-checked yet'.
```

## 4. Stage 2: assembly and the match report

```
On the project screen, when every setup has a passing capture, show 'Make my reel'. It calls
action 'assemble' with { project_id }. (If the response error says a setup still needs a clip,
show it.) Subscribe to the 'projects' row; while status is 'assembling' show 'Cooking your reel…'.
On 'failed' show projects.error with 'Try again'.

When status is 'done', show the results screen:
- A vertical video player for projects.preview_path (signed URL from bucket 'renders').
- 'Download for DaVinci Resolve': two buttons, 'Timeline (.fcpxml)' for timeline_path and
  'Timeline (.otio)' for otio_path (signed download URLs from 'renders'). Under them a short
  note: 'In Resolve: File > Import > Timeline, then relink to your original clips.'
- 'Download preview' for the MP4.
- A 'Match report' panel: projects.overall_score as a big number, then each row of
  'cut_matches' for this project ordered by order_index: the original cut's thumbnail (the cut's
  thumbnail_path, from 'cuts' by cut_id) next to the user's version (cut_matches.thumbnail_path),
  its score, and badges for each entry in fixes (e.g. 'Digital push-in', 'Reframed',
  'Stabilized', 'Slow motion', 'Colour matched'). Show flags as small warnings.
- If projects.reshoots is not empty, show them at the top in an amber box, each with its reason
  and a 'Reshoot now' button that opens Shoot Mode on that setup_number.
```

## 5. Stage 3: edit my own footage, and the Style Library

```
Make 'Edit my own footage' live. Flow:
1. Insert a 'raw_projects' row { title, mood } where mood is picked from chips: Romantic,
   Energetic, Cinematic, Moody (store lowercase).
2. Let the user pick several clips; upload each to bucket 'raw' at
   `${user.id}/${raw_project.id}/${crypto.randomUUID()}-${file.name}` and insert a 'raw_clips'
   row { raw_project_id, storage_path, file_name }.
3. Call action 'suggest' with { raw_project_id }. Subscribe to the 'raw_projects' row; while
   status is 'processing' show 'Finding styles that fit your footage…'.
4. When status is 'ready', show raw_projects.suggestions as up to 3 cards (swipeable on mobile),
   each with a video player (signed URL from 'renders' for preview_path), the style_name and
   mood tags. Tapping 'Use this' calls action 'choose' with { raw_project_id, index }.
5. After choosing, show the chosen preview (raw_projects.preview_path) and a chat box: 'Want
   changes? e.g. more slow motion'. Sending calls action 'revise' with
   { raw_project_id, instruction }. While status is 'revising' show a typing indicator; when
   it's back to 'ready' refresh the player. Show raw_projects.revisions as the chat history.
6. 'Export' calls action 'export'. When status is 'exported', offer 'Download video'
   (final_path) and 'Download for DaVinci Resolve' (timeline_path and otio_path) from 'renders'.

Add a 'Style Library' page: a gallery of rows from 'recipes' (style_name, genre, mood chips,
summary, '{cut_count} cuts · {setup_count} setups · {duration}s'), filterable by mood and genre.
```
