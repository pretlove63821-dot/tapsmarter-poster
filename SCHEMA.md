# cloud-poster data contract (version 1)

Two files are shared between the Mac autopilot and the GitHub robot:

| File | Written by | Read by |
|---|---|---|
| `schedule.json` | Mac: `python -m autopilot cloud export` (never hand-edited) | robot: `post.py` |
| `state/posted.json` | robot: `post.py` (committed back by the workflow) | Mac (optional, after `git pull`) and humans |

All text is UTF-8. JSON may use `ensure_ascii` either way: the robot only cares about the decoded string.

---

## 1. `schedule.json`

Canonical form is an **object**. (A bare list of items is also accepted; then every top-level default below applies.)

```json
{
  "version": 1,
  "generated_at": "2026-11-11T20:15:00+00:00",
  "timezone": "Europe/Rome",
  "slots": {"weekday": "12:00", "weekend": "10:00"},
  "release_tag": "videos",
  "items": [
    {
      "id": "wd-102",
      "date": "2026-11-12",
      "slot_time": "12:00",
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
| `slots` | no | `{"weekday": "HH:MM", "weekend": "HH:MM"}` in `timezone` (Sat/Sun = weekend). Used only to know *today's* slot on a catch-up day that has no item of its own: when an item is dated today, its own `slot_time` always wins (it is DST-aware per day). If absent too, the robot uses the slot of the video it is about to post. |
| `release_tag` | no, default `"videos"` | GitHub Release that holds the mp4 assets. |
| `items` | yes | Posting order = list order (the `[plan] order` from config.toml, ids already posted by the Mac left out). |

### Item

| Field | Required | Meaning |
|---|---|---|
| `id` | yes | Video id, e.g. `wd-102`. Unique in the list. |
| `date` | yes | `YYYY-MM-DD` in `timezone`: the first day this video may go out. |
| `slot_time` | yes | `HH:MM` (24 h) in `timezone`: earliest time on `date`. |
| `caption` | yes | The **final** caption exactly as the Mac would post it (output of `tick.build_job(...).caption`: body, product-free filtering, hashtags, `fit_caption` trimming, AI disclosure line). The robot sends it byte-for-byte, never trims or rewrites it. Max 2,200 characters, max 30 hashtags. |
| `caption_sha256` | recommended | Hex SHA-256 of `caption.encode("utf-8")`. If present and different, the robot refuses to post (protects against hand edits / bad merges). |
| `asset` | no, default `"<id>.mp4"` | Asset file name in the GitHub Release. `upload_videos.sh` uploads `Instagram/videos/<id>/<id>_instagram.mp4` under this name. |
| `asset_size` | recommended | Bytes. Download is rejected if the size differs. |
| `asset_sha256` | recommended | Hex SHA-256 of the mp4. Download is rejected if it differs (asset out of date: re-run `upload_videos.sh`). |
| `thumb_offset_ms` | no, default `0` | Sent as `thumb_offset` (same as the Mac publisher). Ignored when `cover_url` is used. |
| `cover_url` | no | Public https JPEG cover. Sent as `cover_url` only if it answers HTTP 200 `image/jp*` (same rule as the Mac). |
| `first_comment` | no | Posted once under the Reel right after publishing (same as the Mac's first comment; Preet pins it by hand). |
| `keyword` | no | Informational (summary, funnel matching on the Mac). |

Unknown fields are ignored, so the exporter may add more.

### Robot rules derived from the schedule

* "Today" and every slot are computed in `timezone`, DST-aware.
* A video is a **candidate** when its `id` is not in `state/posted.json` and its `date` is today or earlier.
* The robot posts the **first** candidate in list order, but only once **today's slot time has passed**, and **at most one Reel per calendar day** (in `timezone`) and per run.
* Catch-up: if a day was missed (outage), the overdue video goes out at the next day's slot; later videos slip by one day each. Never two in one day.
* Before creating a container the robot lists the account's recent media and compares captions (whitespace-normalised). A match means "already on Instagram": it is recorded (`source: "found_on_instagram"`) and never posted again.

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
      "source": "robot"
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
| `dry_runs` | What DRY-RUN mode *would* have posted, one per day. Ignored completely once `LIVE=true`. |
| `pending` | `{"id", "container_id", "started_at", "caption_sha256", "publish_attempted"}` while a live post is between "container created" and "published"; `null` otherwise. A later run reconciles it with Instagram (never re-posts blindly). `publish_attempted: true` is written just before `media_publish`; if that call's answer was unclear, the video is never redone until it shows up in `/media` (then it is recorded) or 3 days pass without it appearing. A clear refusal sets it back to `false`, so the next run simply redoes it. |
| `failures` | `{id: {"date", "count", "last_error", "permanent_days"}}`: live failures. After 3 failed tries on one day the robot waits for the next day (those later runs exit 0, so only the failing run alerts). `permanent_days` lists days with a problem only a fix on the Mac can solve (wrong/missing video file, edited caption, Instagram rejecting the format). |
| `blocked` | `{id: {"since", "reason", "caption_sha256", "asset_sha256", "asset_size"}}`: videos set aside after permanent problems on 2 days, so the rest of the plan keeps going. A blocked video comes back by itself as soon as the export carries a different caption or video for it. Token/permission problems never block a video. |
| `first_comments` | Media ids whose first comment was already attempted (never twice). |
| `last_run` | Informational. |

The Mac side may read `posted[*].media_id` / `permalink` / `caption_sha256` to import cloud posts into its ledger, but the robust path for the funnel is to list `{IG_ID}/media` from the API.
