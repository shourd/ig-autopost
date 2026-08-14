# ig-autopost

Publishing pipeline for [@snaps_by_sjoerd](https://instagram.com/snaps_by_sjoerd).
Two halves that never run at the same time:

- **Local curation app** (`app/`) — run by hand when new photos land. All human
  judgement happens here: sequencing, place labels, caption review.
- **GitHub Actions cron** (`.github/workflows/`) — publishes the queue on a
  schedule, unattended. Makes no decisions; reads `queue.yaml` top-down.

## Schedule

`config.yaml` → `schedule:` holds local wall-clock slots (Wednesday 11:30,
Friday 18:00, Sunday 10:30, `Europe/Amsterdam`), so the posting hour stays put
across a DST change instead of sliding by one. Each slot drifts by up to
`jitter_minutes` — deterministic, hashed from the slot itself, so the app and
the publisher always agree on a time while nothing lands on the same exact
minute every week. Add or remove entries under `slots:` to change frequency.

## Carousels

Name the files with a trailing letter and they go out as one post:

```
_DSF1234A.jpg   _DSF1234B.jpg   _DSF1234C.jpg   ->   one carousel, in that order
```

The rule is a trailing **A–J** on an otherwise identical stem, with at least one
sibling — so a lone `sunsetA.jpg` stays a single post. The grid shows only the
first photo with a count badge, exactly as the profile will; the side panel
shows every frame in order. One caption covers the post, drafted from the lead
photo alone — it's the one that stops the scroll. Meta's limit is 10 per
carousel and anything past that is dropped.

Publishing is a container per photo (`is_carousel_item`), each polled to
FINISHED, then a parent container with `media_type=CAROUSEL` that carries the
caption. A caption on a child is silently discarded, which is the kind of thing
you only find out from a live post, so there's a test pinning it.

## Reminders

`config.yaml` → `publish.reminders` puts a **Todoist task per upcoming post**,
due at that post's slot, carrying the caption and links to the images — so
posting can stay manual without being forgotten. Save rewrites them: reordering
the queue moves the due dates, and a photo that leaves the window (posted, held,
pushed back) loses its task, because a reminder to post something already live
is worse than none. Only the next `reminder_count` posts get one; the queue can
be a hundred deep.

The notification itself comes from the **macOS Reminders app** (`publish.
reminder_apple`), set `reminder_lead_minutes` — default 120 — before the slot,
and iCloud carries it to the phone. Todoist's own alarms answer `403
PREMIUM_ONLY` on a free plan, which is why the two channels split: Todoist holds
the caption and the links, Reminders does the buzzing. Both descriptions open
with the posting time in local terms, since the notification arrives well before
the moment it's talking about.

The first run raises a macOS permission dialog ("… wants to control Reminders"),
and osascript blocks until it's answered, so trigger it from a terminal you're
looking at rather than discovering it mid-Save:

```bash
uv run python -m src.reminders
```

If it was refused: System Settings → Privacy & Security → Automation, and allow
Reminders for your terminal. Set `reminder_apple_list` to put them in a specific
list, or leave it null for the default one. Todoist needs `TODOIST_API_TOKEN` in
`.env`; with the token absent that half is skipped and the reminders still work.

## Setup

```bash
brew install uv && uv sync && cp .env.example .env
```

## Usage

Drop exports into `photos/raw/`, then:

```bash
uv run app
```

Opens `localhost:8765` on a mock of the Instagram profile, so the queue is
judged in the place it will land. **The grid reads exactly like the real one
will once the queue has drained**: newest at the top-left, so the top row is the
*last* photo to post and the one going out next sits directly above the
already-published block.

- Each cell is the 4:5 post itself. The white borders dissolve into the white
  page, the same way they will on the profile.
- **Already-posted photos** appear below the queue, marked with a ✓. They come
  from `photos/posted/` — the publisher moves files there as posts go out. To
  see the queue against your real profile, drop existing exports in there too.
- Drag to reorder; neighbours slide out of the way. Order is saved as you drop.
- Each queued photo shows the date it will go out. The next one is badged
  **next**. Held photos are skipped and don't consume a slot.
- Click a photo to write its caption. **It saves as you type** — no Save needed
  for caption edits.
- An orange **!** means a photo still needs attention (no caption, no EXIF date,
  or too small to fill the frame). A plain number is a carousel and says how
  many photos are in it.
- **Post next photo now** publishes ahead of the schedule. Select a single photo
  and it becomes **Post this photo now** and publishes that one instead. Either
  way the first click is a dry run and a second confirms.
- **Save** renders, writes `queue.yaml`, commits, pushes, and rewrites the
  Todoist reminders.

`config.yaml` → `profile:` drives the mock header (username, bio, counts,
highlights). It is cosmetic; nothing there reaches the Meta API.

**Suggest three captions** asks Claude for three drafts of the selected photo —
one funny, one plain, one a little more poetic — and clicking one puts it in
the box. Nothing is ever applied by itself, and Save
never calls the model. Needs `ANTHROPIC_API_KEY` in `.env`; without it the
button says so and everything else keeps working. Turn the whole thing off with
`config.yaml` → `caption.enabled: false`.

Other entry points:

```bash
uv run pytest                                    # test suite
uv run python -m src.border photos/raw/x.jpg     # -> photos/processed/x.jpg
```

## What must be committed

`photos/raw/` is gitignored (large originals, local-only), but the Actions runner has no
access to this machine — anything it reads at publish time has to be in git:

| Path | Committed | Why |
|---|---|---|
| `photos/processed/` | **yes** | The runner uploads this JPEG. ~300 KB/post, ~20 MB/year. |
| `photos/posted/` | **yes** | Where files move after publishing; the move is a committed diff. |
| `queue.yaml` | **yes** | The runner's input and output. |
| `config.yaml` | **yes** | Border/caption/publish settings. |
| `photos/raw/` | no | Originals stay local. |
| `.env` | no | Secrets. `.env.example` is the committed template. |

`.gitignore` therefore ignores `photos/raw/` specifically and never `photos/`. Ignoring
the whole tree is the easiest way to make publishing fail with a confusing 404 on the
image URL.

## Secrets

Local dev reads `.env`; CI reads GitHub Actions secrets. Same names in both:
`ANTHROPIC_API_KEY`, `IG_USER_ID`, `META_ACCESS_TOKEN`, `META_APP_ID`,
`META_APP_SECRET`, `R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`,
`R2_BUCKET`, `R2_PUBLIC_BASE_URL`, `TODOIST_API_TOKEN`.

## Image geometry

`config.yaml` drives everything; nothing is hardcoded in `src/`. The photo is scaled
(aspect preserved, never upscaled) to fit a 1028×1298 box, then centred on a 1080×1350
white canvas. The limiting axis lands on exactly 26px.

| Source | Photo | L/R | T/B |
|---|---|---|---|
| 3:2 landscape | 1028×685 | 26 / 26 | 332 / 333 |
| 2:3 portrait | 865×1298 | 107 / 108 | 26 / 26 |
| 1:1 square | 1028×1028 | 26 / 26 | 161 / 161 |

1350−685 and 1080−865 are odd, so those margins carry the extra pixel on one side.

## Enabling auto-posting

Checked against Meta's own docs, August 2026 — current Graph API version is
**v26.0** (released 2026-07-29). Scope names changed in 2025; don't copy older
tutorials.

Two facts that shape everything below:

- **A Creator account is fine.** Both login paths list "Instagram Business
  Account **or** Instagram Creator Account" as supported. Only a *personal*
  account is excluded.
- **No App Review, and no Business Verification.** Both are only required to act
  on accounts you don't own. An app used solely by people with a role on it gets
  **Standard Access automatically**, and Business-type apps have every permission
  at Standard by default.

### Which login path

| | **Instagram Login** ← use this | Facebook Login |
|---|---|---|
| Facebook Page | **Not needed** | Required, linked to the IG account |
| Publishing permissions | `instagram_business_basic`, `instagram_business_content_publish` | `instagram_basic`, `instagram_content_publish`, `pages_read_engagement` |
| API host | `graph.instagram.com` | `graph.facebook.com` |
| Getting the token | **A "Generate token" button in the dashboard** | Graph API Explorer, then two exchanges |
| Token for the cron | 60 days, refreshable indefinitely with no human present | Page token — no expiry date |
| Also gives you | — | hashtag search, `location_id` tagging |

The Facebook Login path's one real advantage is a Page token that never expires.
That matters less than it looks: an Instagram Login token is refreshable via
`ig_refresh_token` once it's 24 hours old, and each refresh buys another 60 days
with no human in the loop, so a monthly cron keeps it alive indefinitely. Against
that, the Facebook path costs a Facebook Page, an extra product, four permissions
instead of two, and two manual token exchanges — and `/me/accounts` returning
`data: []` because the Page didn't attach is a common dead end.

Use Instagram Login. Take the Facebook path only if you want `location_id`
tagging, which needs Page infrastructure.

### Steps, in order

**1. Make the Instagram account professional.**
Instagram app → Settings → Account type and tools → Switch to professional
account. **Creator or Business both work.**

**2. Create the Meta app.** [developers.facebook.com/apps](https://developers.facebook.com/apps)
→ Create App. **Select the use case "Manage messaging and content on
Instagram" — do not pick "Other".** In the current dashboard, permissions hang
off use cases; an app created under "Other" has no Instagram permissions
attached, and they will not appear anywhere later. The app must be **Business**
type. Note the **App ID** and **App Secret**.

**3. Add yourself to App Roles — twice.** These are two separate roles and both
are needed:

- **Roles → Administrator**, your Facebook account. This is what makes Standard
  Access work; skipping it produces a permissions error that reads exactly like
  "you need App Review", which is why so many blog posts insist review is
  mandatory.
- **Roles → Instagram Testers → Add Instagram Testers**, your Instagram
  username. Then **accept the invitation from the Instagram side**: instagram.com
  → Settings → Apps and websites → **Tester invites** → Accept. The status in the
  Roles tab flips to *Active*.

The invitation is the most commonly missed step in the whole setup. Until it is
accepted, **Add account** on the token screen fails with
`Insufficient developer role`, which sounds like an app-configuration problem and
isn't.

**4. Add the publishing permission.** The "Manage messaging and content on
Instagram" use case ships with `instagram_business_basic`,
`instagram_business_manage_comments` and `instagram_business_manage_messages` —
**not** the one that posts. Click **Go to permissions and features** and add
`instagram_business_content_publish`. It should read *Standard Access — Ready to
use*; it does not need a review request. The other three are harmless; leave
them.

**5. Generate the token.** App Dashboard → **Instagram** → **API setup with
Instagram business login** → **2. Generate access tokens** → **Add account**,
then **Generate token**. Log in with Instagram, click Allow, copy the token. This
is already long-lived — 60 days, not one hour — so there is no exchange step. No
Graph API Explorer, no Facebook Page, no `/me/accounts`.

**6. Run the helper**: `uv run python -m src.token_setup`, choose path [1],
paste the token. It resolves the Instagram user ID, proves it can publish, and
writes `.env`.

<details>
<summary>Facebook Login path instead (only for <code>location_id</code> tagging)</summary>

Additionally create a Facebook Page at
[facebook.com/pages/create](https://www.facebook.com/pages/create) and link it in
Instagram → Settings → Accounts Centre. Add the **Facebook Login for Business**
product, and the permissions `instagram_basic`, `instagram_content_publish`,
`pages_show_list`, `pages_read_engagement`. Then
[Graph API Explorer](https://developers.facebook.com/tools/explorer/) → **Meta
App** = your app → **User or Page** = *Get User Access Token* → **Generate
Access Token**, granting the Page on the consent screen. Run the helper and
choose path [2].

Two errors worth recognising. Querying `/me` before generating a token returns
`OAuthException code 2500, "An active access token must be used to query
information about the current user"` — an app token used where a User token is
required. And `me/accounts` returning `{"data": []}` means no Page was granted:
either none exists, or the consent screen was completed without attaching it.
Note the Explorer's path box takes `me/accounts`, not `GET /me/accounts` — the
verb belongs in the dropdown, and including it yields
`Unknown path components`.
</details>

**7. Confirm the token actually publishes** before trusting the cron. The helper
does this for you, but by hand it is:

```
GET https://graph.instagram.com/v26.0/me?fields=id,username&access_token=<TOKEN>
GET https://graph.instagram.com/v26.0/<IG_USER_ID>/content_publishing_limit?access_token=<TOKEN>
```

The first must return your username; the second must return a quota rather than
an error. If both work, publishing will work. `content_publishing_limit` is the
better of the two checks — `/me` resolves for a token that still lacks the
publish permission, so only the second proves anything.

**8. Store the secrets.** `META_ACCESS_TOKEN`, `IG_USER_ID`, `META_TOKEN_KIND`
into `.env` locally (the helper does this) and into GitHub → Settings → Secrets
and variables → Actions. Never paste them into a chat or a commit.

**9. Set up image hosting.** Meta fetches the image over HTTPS from a public URL
at publish time — it does not accept an upload. Cloudflare R2 with a public
bucket is the plan; the alternative is `raw.githubusercontent.com`, which
requires making this repo public.

**10. Tell me step 7 returned your username**, and I'll build Phase 4 against it.

Phase 4 runs a scheduled token check whatever kind of token is in use. An
Instagram Login token is refreshed on that same schedule via `ig_refresh_token`
(valid once the token is 24h old, buying 60 more days each time); a Page token
can't lapse on a timer but can still be invalidated by a password change or a
revoked permission. `META_TOKEN_KIND` selects the rule. Either way a failure
opens a GitHub issue — finding out from a failed post is too late.

### Alternatives to running your own app

| Option | Cost of setup | Trade-off |
|---|---|---|
| **Meta Business Suite** scheduler | none | Free, schedules ~75 days ahead, but it's manual — the queue in this app would only be a planning tool, nothing publishes itself. |
| **Buffer API** (free plan, personal token) | ~10 min | Buffer owns the Meta app; you POST to Buffer instead. Adds a third party to the critical path and gives away caption/photo data. |
| Unofficial libraries (`instagrapi` etc.) | ~10 min | Logs in with your password, breaks Instagram's ToS, and gets accounts checkpointed or banned. Not worth a 101-post account. |

### Backfilling the existing profile

The grid preview shows already-posted photos from `photos/posted/`. To fill that
without any API access: Instagram → Settings → Accounts Centre → Your
information and permissions → **Download your information**, format **JSON**.
The ZIP arrives by email and contains every post's media plus captions and
timestamps, which is enough to reconstruct the grid in the right order.

Constraints the pipeline already respects: JPEG only (PNG and HEIC are
rejected), containers expire after 24 hours, 100 API posts per rolling 24 hours
(a weekly post is nowhere near it — check with
`GET /<IG_ID>/content_publishing_limit`), and 1080×1350 sits exactly on the 4:5
aspect floor, which is why `border.py` asserts that size.

## Captions

`claude-opus-5` with vision, constrained by a JSON schema to exactly three
captions — `{"funny": ..., "plain": ..., "poetic": ...}`. Three named
properties rather than an array because structured-output schemas don't support
`minItems`/`maxItems`, so "exactly three" has to be structural. Each is checked
against house style (one line, capital first letter, no hashtag, emoji,
exclamation mark, or year); a voice that fails gets one retry naming the
problem, and is dropped if it fails again rather than dragging the other two
down with it.

The key: an **API key from [console.anthropic.com](https://console.anthropic.com)**
→ Settings → API keys → Create key, pasted into `.env` as
`ANTHROPIC_API_KEY=sk-ant-…`. That's a Console account, billed per token and
separate from a Claude.ai subscription — three captions with a photo attached
runs to fractions of a cent. `.env` is git-ignored; the key never goes in
`config.yaml` or in chat.

A GitHub Actions secret is **not** enough for the curation app: it runs on this
machine and reads `.env`. Secrets are needed in both places — GitHub for the
unattended runs, `.env` for anything driven from the app.

Two things are deliberately kept away from the model:

- **The date.** It returns only the caption clause; Python appends
  `" (Mon, YYYY)"`. The month comes from a hardcoded English list — this machine's
  locale is Dutch and `strftime("%b")` returns `mrt`, which a test proves.
- **The place.** Resolved beforehand from the app's batch label and passed in as
  a stated fact. With no label, the model is told to write no location at all.
  It is never asked to infer a place from the image.

EXIF date priority is `DateTimeOriginal` → `DateTime` → `DateTimeDigitized`. Note
that Lightroom rewrites `DateTime` on export, so on these files it reads
2025-03-31 while the real capture date sits in the other two tags.

Output is always baseline sRGB JPEG at exactly 1080×1350, no EXIF, no ICC profile.
1080×1350 sits exactly on Meta's 4:5 aspect floor, so `add_border` asserts the canvas
size before saving — drift there fails container creation with an unhelpful error.
