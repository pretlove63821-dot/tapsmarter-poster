# cloud-poster data contract (version 1)

Files shared between the Mac autopilot and the GitHub robot (all additions since the first release are
backward-compatible, so `version` stays `1`):

| File | Written by | Read by |
|---|---|---|
| `schedule.json` | Mac: `python -m autopilot cloud export` (never hand-edited) | robot: `post.py` |
| `state/posted.json` | robot (committed back by the workflow) | Mac (`cloud.py`, `growth.py`) and humans |
| `state/stats.json` | robot, at most every 3 h | Mac `python -m autopilot growth`, Telegram `/growth` |
| `state/trends.json` | robot, trend fetch once a day (scores every run) | Mac `growth` |
| `state/order.json` | robot, every run when it changes | robot (next slot) and Mac `growth` |

The repo is public: the Mac can read every state file from
`https://raw.githubusercontent.com/pretlove63821-dot/tapsmarter-poster/main/state/<file>` without a token.
None of the state files ever contains a token.

All text is UTF-8. JSON may use `ensure_ascii` either way: the robot only cares about the decoded string.

---

## 1. `schedule.json`

Canonical form is an **object**. (A bare list of items is also accepted; then every top-level default below applies.)

```json
{
  "version": 1,
  "generated_at": "2026-11-11T20:15:00+00:00",
  "timezone": "Europe/Rome",
  "slots": {"weekday": "18:00", "weekend": "16:00"},
  "slot_timezone": "America/New_York",
  "slot_times": {"weekday": ["12:00", "19:00"], "weekend": ["10:00", "18:00"]},
  "slot_calendar": ["2026-11-13T17:00:00+00:00", "2026-11-14T00:00:00+00:00", "..."],
  "release_tag": "videos",
  "items": [
    {
      "id": "wd-102",
      "date": "2026-11-13",
      "slot_time": "18:00",
      "slot_at": "2026-11-13T17:00:00+00:00",
      "caption": "iPhone call screening for unknown callers\n...\n\n#iphonetips #scamalert",
      "caption_sha256": "3f1c...e9",
      "asset": "wd-102.mp4",
      "asset_size": 31457280,
      "asset_sha256": "a0b1...ff",
      "thumb_offset_ms": 0,
      "cover_url": null,
      "first_comment": "The steps again: ...",
      "keyword": "SCREEN"
    }
  ]
}
```

### Top level

| Field | Required | Meaning |
|---|---|---|
| `version` | yes (object form) | Always `1` for this contract. Robot refuses other versions. |
| `generated_at` | no | ISO-8601 with offset. Shown in the run summary only. |
| `timezone` | no, default `"Europe/Rome"` | IANA zone in which every `date` and `slot_time` is expressed. DST is handled by the robot (zoneinfo). |
| `slot_timezone` | no, default `"America/New_York"` when `slot_times` is given | IANA zone of `slot_times`. |
| `slot_times` | recommended | `{"weekday": ["HH:MM", ...], "weekend": [...]}` (1-6 times each, a single `"HH:MM"` is accepted too) in `slot_timezone` (Sat/Sun of that zone = weekend). When present these are **the** posting slots, DST-aware every day, for as long as the plan runs (also on catch-up days past the last item). |
| `slot_calendar` | no | Explicit slot moments (ISO-8601 with offset). Added to the slots above (the robot uses the union), so a calendar without `slot_times` also works. |
| `slots` | no | Legacy / fallback: `{"weekday": "HH:MM" or ["HH:MM", ...], "weekend": ...}` in `timezone`. Used only when `slot_times` is absent: then the items dated a day define that day's slots, and `slots` covers days without items (catch-up). |
| `release_tag` | no, default `"videos"` | GitHub Release that holds the mp4 assets. |
| `start_date` | no | Informational: the first ET posting day the Mac exported from (`--start`). |
| `slot_timezone` | no, default `"America/New_York"` | (Mac exporter, 2 a day) IANA zone of `slot_times`. config.toml `[slots]` are US Eastern. |
| `slot_times` | no | (Mac exporter) `{"weekday": ["12:00", "19:00"], "weekend": ["10:00", "18:00"]}` in `slot_timezone`: every posting slot of a day (Sat/Sun in `slot_timezone` = weekend). An ET evening slot is the NEXT Rome calendar day (19:00 ET = 01:00 Rome). |
| `posts_per_slot` / `max_posts_per_day` / `min_gap_hours` | no | (Mac exporter) `1` / `2` / `3`: one Reel per slot, at most 2 per Rome calendar day, at least 3 h between two slots. The exporter already drops slots that would break these; the robot must enforce them too. |
| `slot_calendar` | no | (Mac exporter) Sorted list of every posting moment as UTC ISO-8601 (`"2026-11-13T17:00:00+00:00"`), from `start_date` until 14 days after the last item (catch-up room). Item `i` is planned for `slot_calendar[i]` when nothing is missed; a missed slot just shifts the queue to the next moment in this list. |
| `items` | yes | Posting order = list order (the `[plan] order` from config.toml, ids already posted by the Mac left out). |

### Item

| Field | Required | Meaning |
|---|---|---|
| `id` | yes | Video id, e.g. `wd-102`. Unique in the list. |
| `date` | yes | `YYYY-MM-DD` in `timezone`: the first day this video may go out. |
| `slot_time` | yes | `HH:MM` (24 h) in `timezone`: earliest time on `date`. |
| `slot_at` | recommended | ISO-8601 with offset: the exact planned moment. If present it is used instead of `date` + `slot_time` as "the earliest moment this video may go out". |
| `caption` | yes | The **final** caption exactly as the Mac would post it (output of `tick.build_job(...).caption`: body, product-free filtering, hashtags, `fit_caption` trimming, AI disclosure line). The robot sends it byte-for-byte, never trims or rewrites it. Max 2,200 characters, max 30 hashtags. |
| `caption_sha256` | recommended | Hex SHA-256 of `caption.encode("utf-8")`. If present and different, the robot refuses to post (protects against hand edits / bad merges). |
| `asset` | no, default `"<id>.mp4"` | Asset file name in the GitHub Release. `upload_videos.sh` uploads `Instagram/videos/<id>/<id>_instagram.mp4` under this name. |
| `asset_size` | recommended | Bytes. Download is rejected if the size differs. |
| `asset_sha256` | recommended | Hex SHA-256 of the mp4. Download is rejected if it differs (asset out of date: re-run `upload_videos.sh`). |
| `thumb_offset_ms` | no, default `0` | Sent as `thumb_offset` (same as the Mac publisher). Ignored when `cover_url` is used. |
| `cover_url` | no | Public https JPEG cover. Sent as `cover_url` only if it answers HTTP 200 `image/jp*` (same rule as the Mac). |
| `first_comment` | no | Posted once under the Reel right after publishing (same as the Mac's first comment; Preet pins it by hand). |
| `keyword` | no | Funnel keyword. Also used by the trend score and the "best topics" boost. |
| `title`, `category` | no | Optional extra words for the trend score (the caption's first line is always used). |
| `slot_at` | no | (Mac exporter) The planned moment as UTC ISO-8601; same instant as `date` + `slot_time` in `timezone`. |
| `slot_et` | no | (Mac exporter) The same moment in `slot_timezone`, e.g. `"2026-11-13T19:00-05:00"` (informational). |
| `slot_index` | no | (Mac exporter) 0 = first slot of that ET day, 1 = second. |
| `plan_index` | no | (Mac exporter) Position in config.toml `[plan] order` (ties in a trend reorder keep this order). |
| `title` | no | (Mac exporter) Script topic, e.g. "iPhone 80% charge limit". For trend scoring and summaries; never posted. |
| `category` | no | (Mac exporter) Content pillar: `"PHONE TRICK"`, `"SAFETY"`, `"SAVE MONEY"`, `"AI PROMPT"` or null. For own-performance topic scoring. |
| `phone_os` | no | (Mac exporter) `"ios"`, `"android"`, `"neutral"` or null. Informational. |

Unknown fields are ignored, so the exporter may add more.

### Robot rules derived from the schedule

* Every comparison is done in UTC; days are counted in `timezone` (Rome) for the daily limit and in
  `slot_timezone` for weekday/weekend. DST of both countries is handled by zoneinfo.
* **One Reel per slot.** A slot can be filled from its start until **4 hours** later (`SLOT_GRACE`). A slot that
  already has a Reel (any `posted` row, or any Reel on the account, published at or after the slot start) gets
  no second one.
* **At most 2 Reels per Rome calendar day**, and **at least 3 hours between two Reels** (if the last one went out
  late, the next slot waits until 3 h after it, inside its window).
* **Pace:** a slot takes a video only when at least one remaining video is due by then (`slot_at`, or `date` +
  `slot_time`, is not in the future). So the plan never runs ahead of the export (for example the evening slot
  of 2026-11-12 is skipped because the 2-a-day plan starts 2026-11-13).
* **Which video:** the first one of `state/order.json` (trend order, see section 5) when that file was made for
  this exact schedule, otherwise the first due video in list order. Never a posted or blocked video.
* **Catch-up never bursts.** A slot missed completely (outage longer than its 4-hour window) is just skipped: the
  next slot takes the next video, the queue shifts, nothing is posted twice to catch up.
* Before creating a container the robot lists the account's recent media and compares captions
  (whitespace-normalised). A match means "already on Instagram": it is recorded (`source: "found_on_instagram"`)
  and never posted again. Other Reels on the account (posted by hand) count for the slot, daily and 3-hour rules.
* Workflow crons (UTC): `5,25,45 13-23 * * *` and `5,25,45 0-2 * * *`, so every US-Eastern slot (weekday 12:00 +
  19:00, weekend 10:00 + 18:00) gets its first try within ~25 minutes and 6+ tries inside its window, all year.
* Run outcomes (`last_run.outcome` and the summary): `post`ed, `waiting` (a slot later today, or the 3-hour
  rule), `posted_today` (this slot already has its Reel, or 2 went out today), `nothing_due`, `finished`, ...

---

## 2. `state/posted.json`

```json
{
  "version": 1,
  "posted": [
    {
      "id": "wd-102",
      "date": "2026-11-12",
      "posted_at": "2026-11-12T11:03:41+00:00",
      "media_id": "18012345678901234",
      "permalink": "https://www.instagram.com/reel/AbCdEf/",
      "container_id": "17890000000000000",
      "caption_sha256": "3f1c...e9",
      "source": "robot",
      "slot": "2026-11-12T11:00:00+00:00",
      "pick": "trend score 5: battery (+5: 1 trend, 2 news)"
    }
  ],
  "dry_runs": [
    {"id": "wd-102", "date": "2026-11-12", "at": "2026-11-12T11:00:12+00:00"}
  ],
  "pending": null,
  "first_comments": ["18012345678901234"],
  "last_run": {"at": "2026-11-12T11:03:41+00:00", "outcome": "posted", "mode": "live"}
}
```

| Field | Meaning |
|---|---|
| `posted` | Every Reel that is live on Instagram and came from this schedule. `source` is `"robot"` (posted by the robot) or `"found_on_instagram"` (already there: e.g. a crash after publishing, or posted by hand). `date` is the calendar day in the schedule timezone. `media_id`/`permalink` may be `null` only for `found_on_instagram` rows where Instagram gave none. |
| `posted[*].slot` / `pick` | The slot (UTC) the Reel filled and why that video was chosen (`plan order` or the trend reason). `slot` is `null` for `found_on_instagram` rows. Older rows may lack both. |
| `dry_runs` | What DRY-RUN mode *would* have posted, one per slot (`{"id", "date", "at", "slot"}`). Ignored completely once `LIVE=true`. |
| `pending` | `{"id", "container_id", "started_at", "caption_sha256", "publish_attempted"}` while a live post is between "container created" and "published"; `null` otherwise. A later run reconciles it with Instagram (never re-posts blindly). `publish_attempted: true` is written just before `media_publish`; if that call's answer was unclear, the video is never redone until it shows up in `/media` (then it is recorded) or 3 days pass without it appearing. A clear refusal sets it back to `false`, so the next run simply redoes it. |
| `failures` | `{id: {"date", "count", "last_error", "permanent_days"}}`: live failures. After 3 failed tries on one day that video waits for the next day and the slot goes to the next video instead (one bad video never blocks posting); only when every remaining video is out of tries does the robot wait for the next day (those runs exit 0, so only the failing runs alert). `permanent_days` lists days with a problem only a fix on the Mac can solve (wrong/missing video file, edited caption, Instagram rejecting the format). |
| `blocked` | `{id: {"since", "reason", "caption_sha256", "asset_sha256", "asset_size"}}`: videos set aside after permanent problems on 2 days, so the rest of the plan keeps going. A blocked video comes back by itself as soon as the export carries a different caption or video for it. Token/permission problems never block a video. |
| `first_comments` | Media ids whose first comment was already attempted (never twice). |
| `last_run` | Informational. |

The Mac side may read `posted[*].media_id` / `permalink` / `caption_sha256` to import cloud posts into its ledger, but the robust path for the funnel is to list `{IG_ID}/media` from the API.

---

## Mac reader: `python -m autopilot growth` (autopilot/growth.py)

Reads `schedule.json` and `state/{posted,stats,order,trends}.json` from
`https://raw.githubusercontent.com/pretlove63821-dot/tapsmarter-poster/main/` (falls back to this folder). Read-only and
tolerant: every file is optional and unknown fields are ignored. What it uses, so the robot should keep these names:

* `stats.json`: `permission` (`"ok"` / `"needs_insights_permission"`), `note`, `updated_at`;
  `account.history[]` + `account.latest` (`at`, `followers_count`, `media_count`; a bare list of snapshots is accepted too);
  `media` as `{media_id: {...}}` (or a list) with `video_id` (or a `wd-` `id`), `permalink`, `timestamp`, `caption`,
  `like_count`, `comments_count` and the insight metrics either flat or under `insights` (`views`, `reach`, `saved`,
  `shares`, `total_interactions`, `ig_reels_avg_watch_time`; a `{"value": n}` wrapper is accepted). Without `video_id` the
  Mac maps `media_id` through `posted.json`. A per-media `error` containing `needs_insights_permission` is understood.
* `order.json`: `{"order": [video ids, next first]}` (or a bare list). Ids not in it keep schedule order after it.
* `trends.json`: `updated_at`, `ok`, and per-video `scores` (`{id: {"score", "matched": [...], "reason"}}`, or a list of
  `{"id", ...}` under `scores`/`ranking`). `reason` is shown as is; otherwise the matched words are.

---

## 3. `state/stats.json`

Written by the robot at most every 3 hours (also while the Mac sleeps). Stats never block or delay a post: any
problem is written into the file and the run carries on.

```json
{
  "version": 1,
  "updated_at": "2026-11-13T14:05:00+00:00",
  "permission": "ok",
  "note": "views, reach, saves and shares are on",
  "errors": [],
  "unsupported_metrics": [],
  "account": {
    "latest": {"at": "2026-11-13T14:05:00+00:00", "followers_count": 42, "media_count": 3},
    "history": [{"at": "2026-11-13T11:05:00+00:00", "followers_count": 40, "media_count": 3}, "..."]
  },
  "media": {
    "17900739435397375": {
      "media_id": "17900739435397375", "video_id": "wd-102", "keyword": "SCREEN", "category": null,
      "permalink": "https://www.instagram.com/reel/DfpXmhqTTFS/", "timestamp": "2026-11-12T15:22:48+0000",
      "caption": "iPhone call screening for unknown callers", "like_count": 5, "comments_count": 2,
      "views": 1500, "reach": 900, "saved": 12, "shares": 7, "total_interactions": 80,
      "ig_reels_avg_watch_time": 4100, "ig_reels_video_view_total_time": 99000,
      "updated_at": "2026-11-13T14:05:00+00:00"
    }
  },
  "daily": {"17900739435397375": [{"d": "2026-11-13", "views": 1500, "reach": 900, "likes": 5, "comments": 2,
                                   "shares": 7, "saved": 12}]}
}
```

| Field | Meaning |
|---|---|
| `permission` | `"ok"` (insights work), `"needs_insights_permission"` (Graph error code 10: the token lacks `instagram_manage_insights`; `note` is then exactly `"stats unavailable: needs insights permission"`), or `"unknown"` (not learned yet, e.g. no Reels). Followers, likes and comments are recorded either way. |
| `account.history` | One snapshot per stats run, last 60 days. `latest` = the newest one. |
| `media` | Latest numbers per Reel (`media_product_type` REELS, published in the last 60 days), keyed by media id. `video_id` comes from `posted.json`; `caption` is the first line only. Insight metrics appear only when Instagram gives them; a Reel whose insights failed has `"error"` (`"needs_insights_permission"` or a short message). |
| `daily` | Per Reel, the last snapshot of each Rome day, last 60 days (`d`, `views`, `reach`, `likes`, `comments`, `shares`, `saved`). |
| `unsupported_metrics` | Metrics this API version refuses for the account (code 100 while other metrics work); they are no longer asked for. |
| `errors` | Short, token-free messages from the last stats run. |

Insights come from `GET /{media_id}/insights?metric=views,reach,saved,shares,total_interactions,ig_reels_avg_watch_time,ig_reels_video_view_total_time`
(both `values[].value` and `total_value.value` answers are read). At most 60 insights calls and 4 minutes per run.

---

## 4. `state/trends.json`

```json
{
  "version": 1, "ok": true, "updated_at": "2026-11-13T00:05:00+00:00", "attempted_at": "2026-11-13T00:05:00+00:00",
  "error": null,
  "sources": [{"name": "Google Trends US", "ok": true, "count": 40}, {"name": "Google News: iPhone", "ok": true, "count": 20}],
  "titles": [{"t": "iphone battery limit", "src": "trends"}, {"t": "iOS 26.2 update ... - The Verge", "src": "news", "q": "iPhone"}],
  "order_status": "trends",
  "note": "Next 14 videos ordered by trend score (ties keep plan order).",
  "performance_topics": ["scam", "text"], "top_reels": ["wd-106"],
  "scores": {"wd-137": {"score": 5, "trend": 5, "performance": 0,
                        "matched": ["battery (+5: 1 trend, 2 news)"], "reason": "trend score 5: battery (+5: 1 trend, 2 news)"}}
}
```

* Fetched once per Rome day (retried 3 h after a failure), free and keyless: Google Trends daily RSS
  (`https://trends.google.com/trending/rss?geo=US`, item titles + news item titles, max 40) and Google News RSS
  searches (last day) for `iPhone`, `ChatGPT`, `Android`, `AI`, `scam`, `battery`, `iOS update` (max 20 each).
  Feeds with a DTD/entities (checked by the XML parser itself, in any encoding) or over 3 MB are refused; at most
  3 MB is read per feed and all feeds together get 90 s (later feeds are skipped).
* `ok` is true when at least one feed gave titles. Titles older than 36 hours are not used.
* **Score of a video** = trend points + performance points, from the words of its id, `title`, `keyword`,
  `category` and the caption's first line. Words are lower-cased, stopwords and numbers dropped, simple plurals
  cut and a small synonym map applied (`ChatGPT`/`OpenAI`/`Gemini` -> `ai`, `iOS`/`Apple` -> `iphone`,
  `charging` -> `battery`, `phishing`/`fraud` -> `scam`, ...). Each shared word adds
  `min(3 x Google-Trends titles + news titles, 6)`; a news search never counts its own search words, and the
  search topics themselves (`iphone`, `ai`, `android`, `scam`, `battery`, `update`) add at most 2 from news.
* **Performance points:** once at least 4 Reels have `views`, the best third (max 5) by `views + 25 x shares`
  gives "best topics" (their keyword/category + topic words); every video sharing one gets +2 per topic (max +4).

---

## 5. `state/order.json`

```json
{
  "version": 1, "generated_at": "...", "status": "trends",
  "note": "Next 14 videos ordered by trend score (ties keep plan order).",
  "schedule_sha256": "<sha256 of the JSON list of schedule item ids>",
  "based_on": {"trends_updated_at": "...", "stats_updated_at": "..."},
  "order": ["wd-137", "wd-103", "..."],
  "reasons": {"wd-137": "trend score 5: battery (+5: 1 trend, 2 news)", "wd-103": "no trend match (plan order)"},
  "upcoming": [{"slot": "2026-11-13T17:00:00+00:00", "slot_rome": "Fri 13 Nov 18:00", "id": "wd-137",
                "reason": "trend score 5: ..."}]
}
```

* An override of the **future queue only**: it never changes `schedule.json`, captions or first comments.
* `order` = every unposted, unblocked video. Only the next 14 videos of the plan can move (sorted by score,
  ties keep plan order); the rest stay in plan order. Deterministic: same inputs, same file.
  Fairness: a video already passed over by 4 posts planned after it is pinned first (plan order), so a video
  with no trend match cannot be pushed back forever.
* `status: "plan_order"` (trend fetch failed, titles older than 36 h, or `TRENDS=off`) means plan order.
* The robot uses it only when `schedule_sha256` matches the current `schedule.json` (a new export is in plan
  order until the same run writes a fresh `order.json`). Posted and blocked videos are never picked, whatever the
  file says.
* `upcoming` is a projection of the next 6 slots (the robot decides again at every slot).

