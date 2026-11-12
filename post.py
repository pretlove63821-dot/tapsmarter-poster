#!/usr/bin/env python3
"""Tap Smarter cloud poster: posts at most ONE Instagram Reel per run (one per slot, 2 slots a day) from schedule.json,
then refreshes state/stats.json (every 3 h), state/trends.json (daily) and state/order.json (trend order).

Runs inside GitHub Actions (.github/workflows/post.yml) so Reels go out even when the Mac is asleep.
Python 3.12, standard library only (nothing to pip install on the runner).

Switches (GitHub repo variables, read from the environment):
  PAUSED=true  -> do nothing at all.
  LIVE=true    -> really post. Anything else = DRY-RUN: every check runs, nothing is created or published.
  STATS=off    -> no stats.   TRENDS=off -> no trend fetch, plan order only.
Secret: IG_TOKEN (never printed, masked, scrubbed from every message). Variable: IG_USER_ID.

The Instagram request shapes copy autopilot/publish/instagram_login.py (Unified-login "EAA..." token on
graph.facebook.com/v27.0, {IG_ID}/media REELS resumable -> rupload -> status_code -> media_publish).

Exit codes: 0 ok (posted / nothing due / dry-run / paused), 1 setup problem (fix something),
            2 failed but safe to retry next run, 3 unclear outcome (a person should look; never re-posted blindly).
Stats and trends never change the exit code and never delay a post (they run after the posting step).
See SCHEMA.md for schedule.json and the state/*.json files.
"""
from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
import random
import re
import sys
import tempfile
import time
import traceback
import unicodedata
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import date, datetime, time as dtime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote, urlencode
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
SCHEDULE_PATH = HERE / "schedule.json"
STATE_PATH = HERE / "state" / "posted.json"

API_VERSION = "v27.0"
GRAPH_FB = f"https://graph.facebook.com/{API_VERSION}"         # Unified-login / system-user "EAA..." tokens
GRAPH_IG = f"https://graph.instagram.com/{API_VERSION}"        # Instagram Login tokens (fallback)
RUPLOAD = f"https://rupload.facebook.com/ig-api-upload/{API_VERSION}"
GITHUB_API = "https://api.github.com"

DEFAULT_TZ = "Europe/Rome"
DEFAULT_TAG = "videos"
GRAPH_TIMEOUT = 60
UPLOAD_TIMEOUT = 600
DOWNLOAD_TIMEOUT = 600
BACKOFF = (2, 8, 30)
UPLOAD_ATTEMPTS = 5
UPLOAD_CHUNK = 4 * 1024 * 1024      # bytes per rupload request (one 30 MB request is refused)
PROCESS_DEADLINE = 900                     # wait up to 15 min for Meta to process the video
PENDING_GIVE_UP = timedelta(days=3)
MAX_TRIES_PER_DAY = 3                      # failed live attempts at one video per day, then wait for tomorrow
BLOCK_AFTER_DAYS = 2                       # days with a permanent problem in ONE video before it is set aside
MEDIA_PAGES = 3                            # 3 x 50 recent posts are compared before posting
IG_MAX_BYTES = 300 * 1024 * 1024
IG_CAPTION_MAX = 2200
IG_HASHTAG_MAX = 30
TRANSIENT_CODES = {1, 2, 4, 17, 32, 341, 613, 80001, 80002, 80004}
AUTH_CODES = {102, 190}
# post.yml crons (UTC). Both US-Eastern evening slots (19:00 / 18:00 ET = 23:00-00:00 UTC) get runs in both DST states.
CRON_SPECS = ("5,25,45 13-23 * * *", "5,25,45 0-2 * * *")

# ---- posting rhythm (2 Reels a day): see SCHEMA.md "Robot rules"
SLOT_GRACE = timedelta(hours=4)            # a slot can be filled until 4 h after it starts; later = missed (queue shifts)
MIN_GAP = timedelta(hours=3)               # at least 3 h between two Reels
MAX_PER_DAY = 2                            # never more than 2 Reels per Rome calendar day
SLOT_TZ_DEFAULT = "America/New_York"

# ---- stats (state/stats.json)
STATS_EVERY = timedelta(hours=3)
STATS_KEEP = timedelta(days=60)
INSIGHT_METRICS = ("views", "reach", "saved", "shares", "total_interactions", "ig_reels_avg_watch_time",
                   "ig_reels_video_view_total_time")
MAX_INSIGHT_MEDIA = 60                     # insights calls per stats run, at most
STATS_BUDGET = 240                         # seconds a stats run may take (the job has 40 minutes for everything)
STATS_MAX_ERRORS = 3                       # consecutive insights errors (not permission) before giving up this run
NEEDS_PERMISSION = "needs_insights_permission"
PERMISSION_NOTE = "stats unavailable: needs insights permission"

# ---- trends (state/trends.json, state/order.json)
TRENDS_RSS = "https://trends.google.com/trending/rss?geo=US"
NEWS_RSS = "https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"
NEWS_TERMS = ("iPhone", "ChatGPT", "Android", "AI", "scam", "battery", "iOS update")
TRENDS_RETRY = timedelta(hours=3)          # after a failed fetch
TRENDS_MAX_AGE = timedelta(hours=36)       # older trend titles are not used (plan order instead)
TREND_WINDOW = 14                          # only the next 14 videos of the plan can be moved (about a week)
TOKEN_CAP = 6                              # points one matched word can add
RSS_MAX_BYTES = 3 * 1024 * 1024
RSS_TIMEOUT = 20
TRENDS_BUDGET = 90                         # seconds all trend feeds together may take; later feeds are skipped
MAX_OVERTAKES = 4                          # a video passed over by this many later-planned posts is pinned first

EXIT_OK, EXIT_SETUP, EXIT_RETRY, EXIT_CHECK = 0, 1, 2, 3


# ====================================================================== secret scrubbing
_SECRET_PARAMS = ("access_token", "client_secret", "fb_exchange_token", "input_token", "appsecret_proof", "token")
_KV_RE = re.compile(r"(?i)((?:%s)['\"]?\s*[=:]\s*['\"]?)[^&\s'\",}<>()\[\]]+" % "|".join(_SECRET_PARAMS))
_AUTH_RE = re.compile(r"(?i)\b(Bearer|OAuth|basic|token)\s+(?=[A-Za-z0-9|._\-+/=]*\d)[A-Za-z0-9|._\-+/=]{8,}")
_TOKEN_SHAPES = re.compile(r"\b(?:EAA[A-Za-z0-9]{16,}|IG[A-Za-z0-9]{2}[A-Za-z0-9_\-]{30,}|gh[pousr]_[A-Za-z0-9]{16,}"
                           r"|github_pat_[A-Za-z0-9_]{20,})")
_KNOWN_SECRETS: set[str] = set()


def remember_secret(value: str | None) -> None:
    if value and len(value) >= 6:
        _KNOWN_SECRETS.add(value)
        _KNOWN_SECRETS.add(quote(value, safe=""))


def scrub(text: object) -> str:
    """Remove tokens and secret-looking values from any text meant for logs, summaries or errors."""
    s = str(text)
    for v in sorted(_KNOWN_SECRETS, key=len, reverse=True):
        s = s.replace(v, "***")
    s = _KV_RE.sub(r"\1***", s)
    s = _AUTH_RE.sub(r"\1 ***", s)
    return _TOKEN_SHAPES.sub("***", s)


# ====================================================================== errors
class PosterError(Exception):
    """Base error. Messages are always scrubbed."""
    exit_code = EXIT_RETRY

    def __init__(self, msg: str):
        super().__init__(scrub(msg))


class SetupError(PosterError):
    exit_code = EXIT_SETUP


class ItemError(SetupError):
    """A permanent problem with ONE video (bad file, missing upload, edited caption...): fixing it needs the Mac,
    and after BLOCK_AFTER_DAYS days the video is set aside so the rest of the plan keeps going."""


class NeedsCheck(PosterError):
    exit_code = EXIT_CHECK


class TransportError(PosterError):
    """The request may or may not have reached the server."""


class GraphError(PosterError):
    def __init__(self, msg: str, retryable: bool = False, code: int | None = None, subcode: int | None = None,
                 status: int | None = None, outcome_unknown: bool = False):
        super().__init__(msg)
        self.retryable, self.code, self.subcode, self.status = retryable, code, subcode, status
        self.outcome_unknown = outcome_unknown
        self.item_specific = False                # True: this video can never go out as it is (e.g. bad format)
        if code in AUTH_CODES:
            self.exit_code = EXIT_SETUP


def friendly(err: dict, status: int | None) -> GraphError:
    """Graph {"error": {...}} -> an actionable message (same classification as the Mac publisher)."""
    code, sub = err.get("code"), err.get("error_subcode")
    meta_says = err.get("error_user_msg") or err.get("message") or "no details"
    transient = bool(err.get("is_transient")) or code in TRANSIENT_CODES or (status or 0) >= 500 or status == 429
    if code in AUTH_CODES:
        msg = ("Instagram says the token no longer works. Make a new system-user token in Meta Business Settings "
               "and paste it into the repo secret IG_TOKEN (SETUP.md step 4).")
        transient = False
    elif code == 10 or (isinstance(code, int) and 200 <= code <= 299):
        msg = "Instagram refused: a permission is missing on the token (instagram_content_publish)."
        transient = False
    elif sub == 2207042:
        msg, transient = "Instagram's daily posting limit is reached. The next run tries again.", True
    elif sub == 2207026:
        msg, transient = "Instagram rejected the video format. Re-render the video and re-upload it.", False
    elif code == 368:
        msg, transient = "Meta temporarily blocked posting from this account. Check the Instagram app.", False
    elif transient:
        msg = "Meta is busy or rate-limiting. The next run tries again."
    else:
        msg = "Meta rejected the request."
    out = GraphError(f"{msg} (Meta: {meta_says}; code {code}{'/' + str(sub) if sub else ''})",
                     retryable=transient, code=code, subcode=sub, status=status)
    out.item_specific = sub == 2207026
    return out


# ====================================================================== HTTP (urllib, injectable)
@dataclass
class Response:
    status: int
    headers: dict[str, str]
    body: bytes

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    def json(self) -> dict:
        try:
            data = json.loads(self.body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return {}
        return data if isinstance(data, dict) else {"data": data}

    def text(self, n: int = 200) -> str:
        return self.body[:n].decode("utf-8", "replace")


Transport = Callable[[str, str, dict, "bytes | None", float, bool], Response]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):          # Graph/rupload answers are never redirects: don't follow
        return None


_OPEN_FOLLOW = urllib.request.build_opener()
_OPEN_STAY = urllib.request.build_opener(_NoRedirect())


def urllib_transport(method: str, url: str, headers: dict, body: bytes | None, timeout: float,
                     follow_redirects: bool, max_bytes: int | None = None) -> Response:
    """max_bytes: read at most that many bytes of the body (+1, so the caller can see it was too large)."""
    req = urllib.request.Request(url, data=body, method=method)
    for k, v in headers.items():
        if k.lower() == "authorization":
            req.add_unredirected_header(k, v)    # never forwarded to a redirect target (e.g. GitHub's S3 links)
        else:
            req.add_header(k, v)
    opener = _OPEN_FOLLOW if follow_redirects else _OPEN_STAY
    try:
        with opener.open(req, timeout=timeout) as r:
            data = r.read() if max_bytes is None else r.read(max_bytes + 1)
            return Response(r.status, {k.lower(): v for k, v in r.headers.items()}, data)
    except urllib.error.HTTPError as e:
        try:
            data = e.read()
        except Exception:
            data = b""
        return Response(e.code, {k.lower(): v for k, v in (e.headers or {}).items()}, data)
    except (urllib.error.URLError, OSError, http.client.HTTPException, ValueError) as e:
        raise TransportError(f"network problem: {type(e).__name__}: {e}") from None


# ====================================================================== Instagram Graph
def graph_base(token: str) -> str:
    return GRAPH_IG if token.startswith("IG") else GRAPH_FB


class Graph:
    """Token in the Authorization header only (never in URLs), retries with backoff on transient errors."""

    def __init__(self, token: str, transport: Transport, sleep: Callable[[float], None]):
        self.token, self.transport, self.sleep = token, transport, sleep
        self.base = graph_base(token)

    def pause(self, attempt: int, resp: Response | None = None) -> None:
        delay = BACKOFF[min(attempt, len(BACKOFF) - 1)] + random.uniform(0, 1)
        ra = (resp.headers.get("retry-after", "") if resp is not None else "")
        if ra.isdigit():
            delay = min(120, max(delay, int(ra)))
        self.sleep(delay)

    def call(self, method: str, path: str, *, params: dict | None = None, data: dict | None = None,
             retries: int = len(BACKOFF)) -> dict:
        url = f"{self.base}/{path.lstrip('/')}"
        if params:
            url += "?" + urlencode(params)
        headers = {"Authorization": f"Bearer {self.token}"}
        body = None
        if data is not None:
            body = urlencode(data).encode("utf-8")
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        write = method.upper() != "GET"
        last: GraphError | None = None
        for attempt in range(retries + 1):
            resp = None
            try:
                resp = self.transport(method, url, headers, body, GRAPH_TIMEOUT, False)
            except TransportError as e:
                last = GraphError(f"Could not reach Instagram ({e})", retryable=True, outcome_unknown=write)
            else:
                payload = resp.json()
                if resp.ok and "error" not in payload:
                    return payload
                err = payload.get("error") if isinstance(payload.get("error"), dict) else {
                    "message": f"HTTP {resp.status} {resp.text()}".strip()}
                last = friendly(err, resp.status)
                last.outcome_unknown = write and resp.status >= 500
            if not last.retryable or attempt == retries:
                break
            self.pause(attempt, resp)
        assert last is not None
        raise last


# ====================================================================== schedule & state
@dataclass
class Item:
    id: str
    date: date
    slot_time: dtime
    caption: str
    asset: str
    caption_sha256: str | None = None
    asset_size: int | None = None
    asset_sha256: str | None = None
    thumb_offset_ms: int | None = 0
    cover_url: str | None = None
    first_comment: str | None = None
    keyword: str | None = None
    title: str | None = None
    category: str | None = None
    slot_at: datetime | None = None           # exact planned moment (UTC) when the exporter gives one


@dataclass
class Schedule:
    items: list[Item]
    tz: ZoneInfo
    tz_name: str
    slots: dict[str, list[dtime]] = field(default_factory=dict)       # in `timezone` (legacy / fallback)
    release_tag: str = DEFAULT_TAG
    generated_at: str | None = None
    slot_tz: ZoneInfo | None = None                                   # set: slot_times are authoritative
    slot_times: dict[str, list[dtime]] = field(default_factory=dict)  # in slot_tz, e.g. ET 12:00 + 19:00
    slot_calendar: list[datetime] = field(default_factory=list)       # explicit UTC slot moments


def _times_list(v: Any, where: str) -> list[dtime]:
    vals = v if isinstance(v, list) else [v]
    if not vals or len(vals) > 6:
        raise SetupError(f"schedule.json: {where} needs 1 to 6 \"HH:MM\" times.")
    return sorted({_hhmm(x, where) for x in vals})


def _utc(v: Any, where: str) -> datetime:
    dt = parse_ig_time(v) if isinstance(v, str) else None
    if dt is None:
        raise SetupError(f"schedule.json: {where} must be an ISO time with offset, got {v!r}")
    return dt.astimezone(timezone.utc)


_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_ASSET_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}\.mp4$")


def _hhmm(v: Any, where: str) -> dtime:
    if not isinstance(v, str) or not re.fullmatch(r"\d{2}:\d{2}", v):
        raise SetupError(f"schedule.json: {where} must be \"HH:MM\", got {v!r}")
    try:
        return dtime.fromisoformat(v)
    except ValueError:
        raise SetupError(f"schedule.json: {where} is not a real time: {v!r}") from None


def sha256_text(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def parse_schedule(raw: Any) -> Schedule:
    if isinstance(raw, list):
        raw = {"version": 1, "items": raw}
    if not isinstance(raw, dict):
        raise SetupError("schedule.json must be a JSON object (see SCHEMA.md).")
    if raw.get("version", 1) != 1:
        raise SetupError(f"schedule.json version {raw.get('version')!r} is not supported (expected 1).")
    tz_name = str(raw.get("timezone") or DEFAULT_TZ)
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        raise SetupError(f"schedule.json: unknown timezone {tz_name!r}") from None
    slots: dict[str, list[dtime]] = {}
    raw_slots = raw.get("slots") or {}
    if not isinstance(raw_slots, dict):
        raise SetupError("schedule.json: slots must be {\"weekday\": ..., \"weekend\": ...}.")
    for k in ("weekday", "weekend"):
        if raw_slots.get(k):
            slots[k] = _times_list(raw_slots[k], f"slots.{k}")
    slot_tz = None
    slot_times: dict[str, list[dtime]] = {}
    if raw.get("slot_times"):
        st_raw = raw["slot_times"]
        if not isinstance(st_raw, dict) or not all(st_raw.get(k) for k in ("weekday", "weekend")):
            raise SetupError("schedule.json: slot_times needs both \"weekday\" and \"weekend\" lists.")
        slot_times = {k: _times_list(st_raw[k], f"slot_times.{k}") for k in ("weekday", "weekend")}
        stz_name = str(raw.get("slot_timezone") or SLOT_TZ_DEFAULT)
        try:
            slot_tz = ZoneInfo(stz_name)
        except Exception:
            raise SetupError(f"schedule.json: unknown slot_timezone {stz_name!r}") from None
    cal_raw = raw.get("slot_calendar") or []
    if not isinstance(cal_raw, list):
        raise SetupError("schedule.json: slot_calendar must be a list of ISO times.")
    calendar = sorted({_utc(v, f"slot_calendar[{n}]") for n, v in enumerate(cal_raw)})
    items_raw = raw.get("items")
    if not isinstance(items_raw, list):
        raise SetupError("schedule.json has no \"items\" list.")
    items: list[Item] = []
    seen: set[str] = set()
    for n, it in enumerate(items_raw):
        where = f"item {n + 1}"
        if not isinstance(it, dict):
            raise SetupError(f"schedule.json: {where} is not an object.")
        vid = it.get("id")
        if not isinstance(vid, str) or not _ID_RE.match(vid):
            raise SetupError(f"schedule.json: {where} has a bad id {vid!r}")
        if vid in seen:
            raise SetupError(f"schedule.json: id {vid} appears twice.")
        seen.add(vid)
        try:
            day = date.fromisoformat(str(it.get("date")))
        except ValueError:
            raise SetupError(f"schedule.json: {vid} has a bad date {it.get('date')!r} (YYYY-MM-DD).") from None
        cap = it.get("caption")
        if not isinstance(cap, str) or not cap.strip():
            raise SetupError(f"schedule.json: {vid} has no caption.")
        asset = it.get("asset") or f"{vid}.mp4"
        if not isinstance(asset, str) or not _ASSET_RE.match(asset):
            raise SetupError(f"schedule.json: {vid} has a bad asset name {asset!r}")
        size = it.get("asset_size")
        offset = it.get("thumb_offset_ms", 0)
        items.append(Item(
            id=vid, date=day, slot_time=_hhmm(it.get("slot_time"), f"{vid}.slot_time"), caption=cap, asset=asset,
            caption_sha256=(str(it["caption_sha256"]).lower() if it.get("caption_sha256") else None),
            asset_size=int(size) if isinstance(size, int) and not isinstance(size, bool) else None,
            asset_sha256=(str(it["asset_sha256"]).lower() if it.get("asset_sha256") else None),
            thumb_offset_ms=int(offset) if isinstance(offset, (int, float)) and not isinstance(offset, bool) else None,
            cover_url=(str(it["cover_url"]) if it.get("cover_url") else None),
            first_comment=(str(it["first_comment"]) if it.get("first_comment") else None),
            keyword=(str(it["keyword"]) if it.get("keyword") else None),
            title=(str(it["title"]) if it.get("title") else None),
            category=(str(it["category"]) if it.get("category") else None),
            slot_at=(_utc(it["slot_at"], f"{vid}.slot_at") if it.get("slot_at") else None),
        ))
    return Schedule(items=items, tz=tz, tz_name=tz_name, slots=slots,
                    release_tag=str(raw.get("release_tag") or DEFAULT_TAG), generated_at=raw.get("generated_at"),
                    slot_tz=slot_tz, slot_times=slot_times, slot_calendar=calendar)


def load_schedule(path: Path) -> Schedule:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise SetupError(f"{path.name} is missing. On the Mac run: python -m autopilot cloud export, then push.") from None
    except (OSError, ValueError) as e:
        raise SetupError(f"Can't read {path.name}: {e}") from None
    return parse_schedule(raw)


def empty_state() -> dict:
    return {"version": 1, "posted": [], "dry_runs": [], "pending": None, "first_comments": [], "failures": {},
            "blocked": {}, "last_run": None}


def load_state(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return empty_state()
    except (OSError, ValueError) as e:
        raise SetupError(f"state/posted.json is broken ({e}). Fix or restore it from git history.") from None
    if not isinstance(data, dict):
        raise SetupError("state/posted.json is not a JSON object.")
    base = empty_state()
    base.update(data)
    for k in ("posted", "dry_runs", "first_comments"):
        if not isinstance(base.get(k), list):
            base[k] = []
    for k in ("failures", "blocked"):
        if not isinstance(base.get(k), dict):
            base[k] = {}
    return base


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(path)


# ====================================================================== choosing the next video (pure)
@dataclass
class Decision:
    kind: str                     # post | waiting | posted_today (slot filled / day full) | nothing_due | finished | gave_up_today
    item: Item | None = None
    slot: datetime | None = None
    note: str = ""
    reason: str = "plan order"    # why this video (trend score or plan order)


UTC = timezone.utc


def done_ids(state: dict, live: bool) -> set[str]:
    ids = {str(r.get("id")) for r in state.get("posted", [])}
    if not live:
        ids |= {str(r.get("id")) for r in state.get("dry_runs", [])}
    return ids


def post_times(state: dict, live: bool, tz: ZoneInfo) -> list[datetime]:
    """When every Reel went out (UTC). Dry runs count only in dry-run mode. A hand-written row with only a date
    counts as posted at the end of that day (the safe side: it fills that day's slots)."""
    rows = list(state.get("posted", [])) + ([] if live else list(state.get("dry_runs", [])))
    out = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        dt = parse_ig_time(r.get("posted_at") or r.get("at"))
        if dt is None and r.get("date"):
            try:
                dt = datetime.combine(date.fromisoformat(str(r["date"])), dtime(23, 59, 59), tzinfo=tz)
            except ValueError:
                dt = None
        if dt is not None:
            out.append(dt.astimezone(UTC))
    return sorted(out)


def fingerprint(item: Item) -> dict:
    return {"caption_sha256": sha256_text(item.caption), "asset_sha256": item.asset_sha256,
            "asset_size": item.asset_size}


def is_blocked(state: dict, item: Item) -> bool:
    """Set aside after repeated permanent failures, until the Mac exports a changed caption or video for it."""
    b = (state.get("blocked") or {}).get(item.id)
    if not isinstance(b, dict):
        return False
    return all(b.get(k) == v for k, v in fingerprint(item).items())


def remaining_items(sched: Schedule, state: dict, live: bool) -> list[Item]:
    done = done_ids(state, live)
    return [it for it in sched.items if it.id not in done and not is_blocked(state, it)]


def planned_at(sched: Schedule, it: Item) -> datetime:
    """The earliest moment this video may go out (UTC)."""
    if it.slot_at is not None:
        return it.slot_at
    return datetime.combine(it.date, it.slot_time, tzinfo=sched.tz).astimezone(UTC)


def day_slots(sched: Schedule, d: date, fallback: list[dtime]) -> list[datetime]:
    """Posting slots of one calendar day (aware). With slot_times (new exports) the day is a `slot_timezone` day
    and the times are authoritative (DST-aware every day). Legacy exports: the items dated that day (in
    `timezone`) define its slots, else the `slots` block, else the next video's own time."""
    key = "weekend" if d.weekday() >= 5 else "weekday"
    if sched.slot_tz is not None:
        return [datetime.combine(d, t, tzinfo=sched.slot_tz) for t in sched.slot_times[key]]
    own = sorted({it.slot_time for it in sched.items if it.date == d})
    times = own or sched.slots.get(key) or fallback
    return [datetime.combine(d, t, tzinfo=sched.tz) for t in times]


def slots_between(sched: Schedule, start: datetime, end: datetime, fallback: list[dtime] | None = None) -> list[datetime]:
    """Every slot moment in [start, end] (UTC, sorted, unique). slot_calendar entries are included as given."""
    tz = sched.slot_tz or sched.tz
    out = {c for c in sched.slot_calendar if start <= c <= end}
    d = start.astimezone(tz).date() - timedelta(days=1)
    last = end.astimezone(tz).date() + timedelta(days=1)
    while d <= last:
        for sl in day_slots(sched, d, fallback or []):
            u = sl.astimezone(UTC)
            if start <= u <= end:
                out.add(u)
        d += timedelta(days=1)
    return sorted(out)


def schedule_sha(sched: Schedule) -> str:
    return sha256_text(json.dumps([it.id for it in sched.items]))


def ranked_items(sched: Schedule, remaining: list[Item], order: dict | None) -> tuple[list[Item], dict] | None:
    """The trend order from state/order.json, or None (= plan order). It is used only when it was computed for
    this very schedule; it can only reorder: posted / blocked videos were already taken out of `remaining`."""
    if not isinstance(order, dict) or order.get("status") != "trends":
        return None
    if order.get("schedule_sha256") != schedule_sha(sched):
        return None
    q = order.get("order")
    if not isinstance(q, list):
        return None
    pos: dict[str, int] = {}
    for n, vid in enumerate(q):
        if isinstance(vid, str) and vid not in pos:
            pos[vid] = n
    idx = {it.id: n for n, it in enumerate(sched.items)}
    ranked = sorted(remaining, key=lambda it: (pos.get(it.id, len(pos)), idx[it.id]))
    reasons = order.get("reasons") if isinstance(order.get("reasons"), dict) else {}
    return ranked, reasons


def choose(sched: Schedule, state: dict, now: datetime, live: bool, order: dict | None = None,
           extra_posts: list[datetime] | tuple = (), exclude: set[str] | frozenset = frozenset()) -> Decision:
    """One Reel per SLOT. A slot can be filled from its start until SLOT_GRACE later; a missed slot is simply
    skipped (the queue shifts, never a burst). Never 2 in one slot, never more than MAX_PER_DAY per Rome day,
    never closer than MIN_GAP to the previous Reel. A slot takes a video only when the plan has one due by
    then (so the plan's pace is kept), and then the best one: trend order if any, else the first due one.
    `exclude` = videos that used up today's tries: they still count for the pace, but another video goes instead."""
    now_u = now.astimezone(UTC)
    today = now.astimezone(sched.tz).date()
    remaining = remaining_items(sched, state, live)
    if not remaining:
        return Decision("finished", note="Every video in schedule.json is posted.")
    candidates = [it for it in remaining if it.id not in exclude]
    if not candidates:
        return Decision("gave_up_today", note="Every remaining video failed too often today; trying again "
                        "tomorrow.")
    posts = sorted(post_times(state, live, sched.tz) + [p.astimezone(UTC) for p in extra_posts])
    fallback = [remaining[0].slot_time]
    earliest = min(planned_at(sched, it) for it in remaining)
    ranked = ranked_items(sched, candidates, order)

    def pick(at: datetime) -> tuple[Item, str]:
        if ranked is not None:
            it = ranked[0][0]
            return it, str(ranked[1].get(it.id) or "trend order")
        due = [it for it in candidates if planned_at(sched, it) <= at]
        return (due or candidates)[0], "plan order"

    def per_day(day: date) -> int:
        return sum(1 for p in posts if p.astimezone(sched.tz).date() == day)

    recent = slots_between(sched, now_u - SLOT_GRACE - timedelta(days=1), now_u, fallback)
    live_slots = [sl for sl in recent if sl <= now_u < sl + SLOT_GRACE]
    current = live_slots[-1] if live_slots else None
    if current is not None:
        item, why = pick(now_u)
        if any(p >= current for p in posts):
            return Decision("posted_today", item, current, f"The Reel for the {fmt_slot(sched, current)} slot "
                            "already went out; one Reel per slot.", why)
        if per_day(today) >= MAX_PER_DAY:
            return Decision("posted_today", item, current, f"{MAX_PER_DAY} Reels already went out today "
                            f"(Rome); at most {MAX_PER_DAY} a day.", why)
        if earliest <= now_u:
            last = posts[-1] if posts else None
            if last is not None and now_u - last < MIN_GAP:
                ready = last + MIN_GAP
                tail = "" if ready < current + SLOT_GRACE else " That is after this slot's window, so it moves on."
                return Decision("waiting", item, ready, f"{item.id} goes out after today's slot, but at least 3 hours "
                                f"after the last Reel: not before {fmt_slot(sched, ready)}.{tail}", why)
            late = (f" (planned for {item.date.isoformat()}: catching up)" if item.date < today else "")
            moved = f" Picked by {why}." if why != "plan order" else ""
            return Decision("post", item, current, f"{item.id} is due now for the {fmt_slot(sched, current)} "
                            f"slot{late}.{moved}", why)
    # between slots, the slot is not usable, or nothing is due yet: name the next slot that will take a Reel
    start = max(now_u, earliest)
    item, why = pick(start)
    last_post = posts[-1] if posts else None
    upcoming = [sl for sl in slots_between(sched, start - SLOT_GRACE, start + timedelta(days=16), fallback)
                if sl + SLOT_GRACE > start and (last_post is None or sl > last_post)
                and per_day(sl.astimezone(sched.tz).date()) < MAX_PER_DAY]
    when = max(upcoming[0], earliest) if upcoming else earliest
    day_tz = sched.slot_tz or sched.tz                 # "today" = the slots' own calendar day
    if when.astimezone(day_tz).date() == now.astimezone(day_tz).date():
        return Decision("waiting", item, when, f"{item.id} goes out after today's slot {fmt_slot(sched, when)}.", why)
    return Decision("nothing_due", item, when, f"Next video {item.id} is planned for {fmt_slot(sched, when)}.", why)


def fmt(dt: datetime) -> str:
    return dt.strftime("%a %d %b %Y %H:%M %Z")


def fmt_slot(sched: Schedule, dt: datetime) -> str:
    out = fmt(dt.astimezone(sched.tz))
    if sched.slot_tz is not None:
        out += f" = {dt.astimezone(sched.slot_tz):%H:%M %Z}"
    return out


def cron_minutes(specs: tuple[str, ...] = CRON_SPECS) -> list[int]:
    """Minutes of the UTC day at which post.yml starts a run (e.g. 13*60+5)."""
    def expand(field_: str, top: int) -> set[int]:
        vals: set[int] = set()
        for part in field_.split(","):
            if part == "*":
                vals |= set(range(top + 1))
            elif "-" in part:
                a, b = part.split("-")
                vals |= set(range(int(a), int(b) + 1))
            else:
                vals.add(int(part))
        return vals
    out: set[int] = set()
    for spec in specs:
        m, h = spec.split()[:2]
        out |= {hh * 60 + mm for hh in expand(h, 23) for mm in expand(m, 59)}
    return sorted(out)


# ====================================================================== caption matching
def norm_caption(s: str) -> str:
    s = unicodedata.normalize("NFC", s or "").replace("\r\n", "\n").replace("\r", "\n")
    return "\n".join(line.rstrip() for line in s.split("\n")).strip()


def same_caption(a: str, b: str) -> bool:
    na, nb = norm_caption(a), norm_caption(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    squash = lambda t: re.sub(r"\s+", " ", t)
    return squash(na) == squash(nb)


def parse_ig_time(ts: Any) -> datetime | None:
    if not ts:
        return None
    for f in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S.%f%z"):
        try:
            return datetime.strptime(str(ts), f)
        except ValueError:
            continue
    try:
        dt = datetime.fromisoformat(str(ts))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


# ====================================================================== the run
@dataclass
class Env:
    live: bool
    paused: bool
    token: str
    ig_id: str
    gh_token: str
    repo: str
    in_actions: bool
    summary_path: str | None
    video_source: str = "url"
    stats: bool = True            # STATS=off disables state/stats.json
    trends: bool = True           # TRENDS=off disables trend fetching and the trend order (plan order only)


def truthy(v: str | None) -> bool:
    return str(v or "").strip().lower() in ("true", "1", "yes", "on")


def switched_off(v: str | None) -> bool:
    return str(v or "").strip().lower() in ("false", "0", "no", "off")


def read_env(environ: dict, force_dry_run: bool) -> Env:
    return Env(
        live=(str(environ.get("LIVE") or "").strip().lower() == "true") and not force_dry_run,
        paused=truthy(environ.get("PAUSED")),
        token=str(environ.get("IG_TOKEN") or "").strip(),
        ig_id=str(environ.get("IG_USER_ID") or "").strip(),
        gh_token=str(environ.get("GITHUB_TOKEN") or "").strip(),
        repo=str(environ.get("GITHUB_REPOSITORY") or "").strip(),
        video_source=str(environ.get("VIDEO_SOURCE") or "url").strip().lower(),
        in_actions=str(environ.get("GITHUB_ACTIONS") or "") == "true",
        summary_path=environ.get("GITHUB_STEP_SUMMARY") or None,
        stats=not switched_off(environ.get("STATS")),
        trends=not switched_off(environ.get("TRENDS")),
    )


class Poster:
    def __init__(self, env: Env, sched: Schedule, state: dict, state_path: Path, now: datetime, *,
                 transport: Transport = urllib_transport, sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic, video_dir: Path | None = None,
                 out: Callable[[str], None] = print):
        self.env, self.sched, self.state, self.state_path, self.now = env, sched, state, state_path, now
        self.transport, self.sleep, self.clock, self.video_dir, self.out = transport, sleep, clock, video_dir, out
        self.lines: list[str] = []
        self.graph = Graph(env.token, transport, sleep) if env.token and env.ig_id else None
        self._media: list[dict] | None = None
        self.mode = "LIVE" if env.live else "DRY-RUN"
        self._saved = self._content()
        state_dir = state_path.parent
        self.stats_path = state_dir / "stats.json"
        self.trends_path = state_dir / "trends.json"
        self.order_path = state_dir / "order.json"
        self.order = read_json(self.order_path) if env.trends else None

    # ---------------------------------------------------------------- helpers
    def say(self, msg: str) -> None:
        msg = scrub(msg)
        self.lines.append(msg)
        self.out(msg)

    def today(self) -> date:
        return self.now.astimezone(self.sched.tz).date()

    def _content(self) -> str:
        return json.dumps({k: v for k, v in self.state.items() if k != "last_run"}, sort_keys=True, default=str)

    def save(self, outcome: str) -> None:
        """Write state/posted.json, but only when something real changed (no commit every 30 minutes)."""
        if self._content() == self._saved:
            return
        self.state["last_run"] = {"at": self.now.astimezone(timezone.utc).isoformat(timespec="seconds"),
                                  "outcome": outcome, "mode": self.mode.lower()}
        save_state(self.state_path, self.state)
        self._saved = self._content()

    def recent_media(self, refresh: bool = False) -> list[dict]:
        if self.graph is None:
            return []
        if self._media is not None and not refresh:
            return self._media
        out: list[dict] = []
        after = None
        for _ in range(MEDIA_PAGES):
            params = {"fields": "id,caption,timestamp,permalink,media_product_type", "limit": 50}
            if after:
                params["after"] = after
            body = self.graph.call("GET", f"{self.env.ig_id}/media", params=params, retries=2)
            out.extend(d for d in (body.get("data") or []) if isinstance(d, dict))
            after = ((body.get("paging") or {}).get("cursors") or {}).get("after")
            if not after or not (body.get("paging") or {}).get("next"):
                break
        self._media = out
        return out

    def find_on_instagram(self, item: Item, refresh: bool = False) -> dict | None:
        for m in self.recent_media(refresh):
            if same_caption(str(m.get("caption") or ""), item.caption):
                return m
        return None

    def record_posted(self, item: Item, *, media_id: str | None, permalink: str | None, container: str | None,
                      source: str, when: datetime | None, slot: datetime | None = None,
                      reason: str | None = None) -> dict:
        when = when or self.now
        row = {"id": item.id, "date": when.astimezone(self.sched.tz).date().isoformat(),
               "posted_at": when.astimezone(timezone.utc).isoformat(timespec="seconds"),
               "media_id": media_id, "permalink": permalink, "container_id": container,
               "caption_sha256": sha256_text(item.caption), "source": source,
               "slot": slot.astimezone(timezone.utc).isoformat(timespec="seconds") if slot else None}
        if reason:
            row["pick"] = reason[:200]
        self.state["posted"] = [r for r in self.state["posted"] if r.get("id") != item.id] + [row]
        return row

    # ---------------------------------------------------------------- checks
    def validate(self, item: Item) -> None:
        if item.caption_sha256 and sha256_text(item.caption) != item.caption_sha256:
            raise ItemError(f"{item.id}: the caption in schedule.json was changed by hand (checksum differs). "
                             "Re-run the export on the Mac.")
        if len(item.caption) > IG_CAPTION_MAX:
            raise ItemError(f"{item.id}: caption is {len(item.caption)} characters; Instagram allows {IG_CAPTION_MAX}.")
        if len(re.findall(r"#\w+", item.caption)) > IG_HASHTAG_MAX:
            raise ItemError(f"{item.id}: more than {IG_HASHTAG_MAX} hashtags.")

    def quota_full(self) -> str | None:
        body = self.graph.call("GET", f"{self.env.ig_id}/content_publishing_limit",
                               params={"fields": "config,quota_usage"}, retries=2)
        row = (body.get("data") or [{}])[0]
        used, total = row.get("quota_usage"), (row.get("config") or {}).get("quota_total")
        if isinstance(used, int) and isinstance(total, int) and used >= total:
            return f"Instagram's 24-hour posting limit is used up ({used}/{total})."
        return None

    # ---------------------------------------------------------------- video file
    def _gh(self, path: str, accept: str = "application/vnd.github+json", timeout: float = GRAPH_TIMEOUT,
            follow: bool = False) -> Response:
        headers = {"Authorization": f"Bearer {self.env.gh_token}", "Accept": accept,
                   "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "tapsmarter-cloud-poster"}
        last: PosterError | None = None
        for attempt in range(3):
            try:
                resp = self.transport("GET", f"{GITHUB_API}{path}", headers, None, timeout, follow)
            except TransportError as e:
                last = e
            else:
                if resp.status < 500 and resp.status != 429:
                    return resp
                last = PosterError(f"GitHub answered HTTP {resp.status}")
            self.sleep(BACKOFF[min(attempt, len(BACKOFF) - 1)])
        raise PosterError(f"Could not reach GitHub to fetch the video ({last}).")

    def fetch_video(self, item: Item, workdir: Path) -> Path:
        if self.video_dir is not None:
            for cand in (self.video_dir / item.asset, self.video_dir / item.id / f"{item.id}_instagram.mp4"):
                if cand.is_file():
                    self.check_file(item, cand)
                    return cand
            raise ItemError(f"{item.id}: no local video in {self.video_dir}.")
        if not (self.env.gh_token and self.env.repo):
            raise SetupError("GITHUB_TOKEN / GITHUB_REPOSITORY are not set, so the video can't be downloaded.")
        tag = quote(self.sched.release_tag, safe="")
        rel = self._gh(f"/repos/{self.env.repo}/releases/tags/{tag}")
        if rel.status == 404:
            raise SetupError(f"There is no GitHub Release named '{self.sched.release_tag}'. Run upload_videos.sh.")
        if not rel.ok:
            raise PosterError(f"GitHub release lookup failed (HTTP {rel.status}).")
        rel_id = rel.json().get("id")
        asset_id = asset_size = None
        for page in range(1, 11):
            resp = self._gh(f"/repos/{self.env.repo}/releases/{rel_id}/assets?per_page=100&page={page}")
            if not resp.ok:
                raise PosterError(f"GitHub asset list failed (HTTP {resp.status}).")
            try:
                assets = json.loads(resp.body.decode("utf-8"))
            except ValueError:
                assets = []
            for a in assets if isinstance(assets, list) else []:
                if a.get("name") == item.asset and a.get("state", "uploaded") == "uploaded":
                    asset_id, asset_size = a.get("id"), a.get("size")
            if asset_id or not isinstance(assets, list) or len(assets) < 100:
                break
        if not asset_id:
            raise ItemError(f"{item.id}: the release '{self.sched.release_tag}' has no asset {item.asset}. "
                             "Run upload_videos.sh on the Mac.")
        resp = self._gh(f"/repos/{self.env.repo}/releases/assets/{asset_id}", accept="application/octet-stream",
                        timeout=DOWNLOAD_TIMEOUT, follow=True)
        if not resp.ok:
            raise PosterError(f"Downloading {item.asset} failed (HTTP {resp.status}).")
        if isinstance(asset_size, int) and len(resp.body) != asset_size:
            raise PosterError(f"Download of {item.asset} was cut short ({len(resp.body)} of {asset_size} bytes).")
        path = workdir / item.asset
        path.write_bytes(resp.body)
        self.check_file(item, path)
        return path

    def check_file(self, item: Item, path: Path) -> None:
        size = path.stat().st_size
        if size == 0 or size > IG_MAX_BYTES:
            raise ItemError(f"{item.id}: video is {size} bytes (must be 1 byte to 300 MB).")
        if item.asset_size is not None and size != item.asset_size:
            raise ItemError(f"{item.id}: video is {size} bytes but schedule.json expects {item.asset_size}. "
                             "Re-run upload_videos.sh.")
        if item.asset_sha256:
            h = hashlib.sha256()
            with open(path, "rb") as f:
                for chunk in iter(lambda: f.read(1 << 20), b""):
                    h.update(chunk)
            if h.hexdigest() != item.asset_sha256:
                raise ItemError(f"{item.id}: the uploaded video is not the current one (checksum differs). "
                                 "Re-run upload_videos.sh.")

    # ---------------------------------------------------------------- Instagram posting
    def usable_cover(self, url: str | None) -> str | None:
        if not url or not url.startswith("https://"):
            return None
        try:
            r = self.transport("HEAD", url, {"User-Agent": "tapsmarter-cloud-poster"}, None, 20, True)
        except TransportError:
            return None
        return url if r.status == 200 and r.headers.get("content-type", "").lower().startswith("image/jp") else None

    def public_video_url(self, item: Item) -> str | None:
        """The repo is public (2026-11-12), so Instagram can fetch the release asset itself. Meta's rupload refuses
        our ~30 MB Reels ("Request processing failed") while video_url works. Repo variable VIDEO_SOURCE=upload
        switches back to the byte upload."""
        if self.env.video_source == "upload" or not self.env.repo:
            return None
        return (f"https://github.com/{self.env.repo}/releases/download/"
                f"{quote(self.sched.release_tag, safe='')}/{quote(item.asset, safe='')}")

    def create_container(self, item: Item, video_url: str | None = None) -> str:
        base = {"media_type": "REELS", "caption": item.caption, "is_ai_generated": "true"}
        if video_url:
            base["video_url"] = video_url
        else:
            base["upload_type"] = "resumable"
        extra = {"share_to_feed": "true"}
        cover = self.usable_cover(item.cover_url)
        if cover:
            extra["cover_url"] = cover
        elif item.thumb_offset_ms is not None:
            extra["thumb_offset"] = str(int(item.thumb_offset_ms))
        try:
            body = self.graph.call("POST", f"{self.env.ig_id}/media", data={**base, **extra})
        except GraphError as e:
            if e.code != 100:
                raise
            self.say(f"Instagram refused optional settings {sorted(extra)}; trying again without them.")
            body = self.graph.call("POST", f"{self.env.ig_id}/media", data=base)
        if not body.get("id"):
            raise GraphError("Instagram did not return a container id.", retryable=True)
        return str(body["id"])

    def bytes_done(self, container: str) -> int | None:
        body = self.graph.call("GET", container, params={"fields": "id,status,status_code,video_status"}, retries=1)
        phase = (body.get("video_status") or {}).get("uploading_phase") or {}
        done = phase.get("bytes_transferred", phase.get("bytes_transfered"))
        return int(done) if done is not None else None

    def upload(self, container: str, video: Path) -> None:
        size = video.stat().st_size
        offset = 0
        last = "unknown error"
        url = f"{RUPLOAD}/{container}"
        for attempt in range(UPLOAD_ATTEMPTS):
            resp = None
            try:
                # Meta refuses a ~30 MB file in one request ("Request processing failed", 2026-11-12) but takes it
                # in 4 MB pieces: each piece answers 206 "Partial request", the last one 200 success.
                while True:
                    with open(video, "rb") as f:
                        f.seek(offset)
                        chunk = f.read(UPLOAD_CHUNK)
                    headers = {"Authorization": f"OAuth {self.env.token}", "offset": str(offset),
                               "file_size": str(size)}
                    resp = self.transport("POST", url, headers, chunk, UPLOAD_TIMEOUT, False)
                    if resp.status == 206 and offset + len(chunk) < size:
                        offset += len(chunk)
                        continue
                    break
                body = resp.json()
                if resp.ok and body.get("success"):
                    return
                if isinstance(body.get("error"), dict):
                    err = friendly(body["error"], resp.status)
                    if not err.retryable:
                        raise err
                    last = str(err)
                else:
                    dbg = body.get("debug_info") or {}
                    last = dbg.get("message") or resp.text(300) or f"HTTP {resp.status}"
                    if not (dbg.get("retriable") or resp.status >= 500 or resp.status == 429):
                        raise GraphError(f"Meta rejected the video upload: {last}")
            except TransportError as e:
                last = f"connection dropped ({e})"
            last = scrub(last)
            if attempt == UPLOAD_ATTEMPTS - 1:
                break
            self.say(f"Upload interrupted ({last}); resuming.")
            self.graph.pause(attempt, resp)
            try:
                done = self.bytes_done(container)
            except GraphError:
                done = None
            if done is not None:
                if done >= size:
                    return
                offset = max(0, int(done))
        raise GraphError(f"The video upload kept failing after {UPLOAD_ATTEMPTS} tries ({last}).", retryable=True)

    def wait_ready(self, container: str) -> bool:
        end = self.clock() + PROCESS_DEADLINE
        delay = 3.0
        while True:
            body: dict = {}
            try:
                body = self.graph.call("GET", container, params={"fields": "status_code,status"}, retries=1)
            except GraphError as e:
                if not e.retryable:
                    raise
                self.say(f"Status check failed ({e}); looking again.")
            code = body.get("status_code")
            if code in ("FINISHED", "PUBLISHED"):
                return True
            if code == "ERROR":
                status = str(body.get("status", ""))
                err = GraphError(f"Instagram could not process the video ({status or 'no details'}).",
                                 retryable="2207026" not in status)
                err.item_specific = "2207026" in status
                raise err
            if code == "EXPIRED":
                raise GraphError("The Instagram upload expired before publishing.", retryable=True)
            if self.clock() + delay > end:
                return False
            self.sleep(delay)
            delay = min(delay * 1.6, 60.0)

    def publish(self, item: Item, container: str) -> str:
        """media_publish, guarded: after an unclear failure, ask Instagram whether it already went out."""
        last: GraphError | None = None
        unclear = False                           # could any attempt have published without telling us?
        for attempt in range(4):
            try:
                body = self.graph.call("POST", f"{self.env.ig_id}/media_publish",
                                       data={"creation_id": container}, retries=0)
                if body.get("id"):
                    return str(body["id"])
                unclear = True
                last = GraphError("Instagram gave no media id.", retryable=True)
            except GraphError as e:
                unclear = unclear or e.outcome_unknown
                not_ready = e.code == 9007 or e.subcode == 2207027
                if not (e.retryable or not_ready or e.outcome_unknown):
                    if not unclear and isinstance(self.state.get("pending"), dict):
                        self.state["pending"]["publish_attempted"] = False     # a clear "no": safe to redo
                    raise
                last = e
            try:
                st = self.graph.call("GET", container, params={"fields": "status_code"}, retries=1)
                if st.get("status_code") == "PUBLISHED":
                    found = self.find_on_instagram(item, refresh=True)
                    if found and found.get("id"):
                        return str(found["id"])
                    raise NeedsCheck(f"{item.id}: Instagram says it is published but does not list it yet.")
            except GraphError as e:
                if not e.retryable:
                    raise
            self.graph.pause(attempt)
        raise NeedsCheck(f"{item.id}: publishing did not confirm ({last}). Check Instagram; the next run "
                         "checks again and never posts it twice.")

    def first_comment(self, item: Item, media_id: str) -> None:
        if not item.first_comment or media_id in self.state["first_comments"]:
            return
        self.state["first_comments"].append(media_id)
        self.save("posted")                       # recorded first: never posted twice
        try:
            self.graph.call("POST", f"{media_id}/comments", data={"message": item.first_comment}, retries=0)
            self.say("First comment posted (pin it by hand in the Instagram app).")
        except GraphError as e:
            self.say(f"The first comment was not posted ({e}).")

    # ---------------------------------------------------------------- pending reconcile
    def reconcile_pending(self) -> int | None:
        p = self.state.get("pending")
        if not p or not self.env.live:
            return None
        item = next((it for it in self.sched.items if it.id == p.get("id")), None)
        container = str(p.get("container_id") or "")
        started = parse_ig_time(p.get("started_at")) or self.now
        try:
            st = self.graph.call("GET", container, params={"fields": "status_code,status"}, retries=1) if container else {}
        except GraphError as e:
            if e.code == 100:
                st = {}
            elif self.now - started > PENDING_GIVE_UP:
                self.say(f"Gave up checking the old upload of {p.get('id')} after 3 days.")
                self.state["pending"] = None
                return None
            else:
                self.say(f"Could not check the unfinished upload of {p.get('id')} ({e}). Next run tries again.")
                return EXIT_RETRY
        attempted = bool(p.get("publish_attempted"))
        if (st.get("status_code") == "PUBLISHED" or attempted) and item is not None:
            found = self.find_on_instagram(item, refresh=True)
            if found:
                self.record_posted(item, media_id=str(found.get("id")), permalink=found.get("permalink"),
                                   container=container, source="robot", when=parse_ig_time(found.get("timestamp")))
                self.say(f"{item.id} was published by an earlier run; recorded it.")
            elif st.get("status_code") == "PUBLISHED" or self.now - started <= PENDING_GIVE_UP:
                # media_publish was sent and its answer was unclear: Meta may still be catching up. Never redo it
                # until Instagram lists it (then it is recorded) or 3 days have passed without it appearing.
                self.say(f"{item.id}: an earlier run sent it to Instagram but it is not listed yet; not posting it "
                         "again. Check the Instagram app; the next run looks again.")
                return EXIT_CHECK
            else:
                self.say(f"{item.id} never appeared on Instagram in 3 days after an unclear publish; it will be redone.")
        else:
            self.say(f"An earlier upload of {p.get('id')} never went public; it will be redone.")
        self.state["pending"] = None
        self.save("reconciled")
        return None

    # ---------------------------------------------------------------- main flow
    def run(self) -> int:
        self.say(f"Mode: {self.mode}. Now: {fmt(self.now.astimezone(self.sched.tz))}.")
        if self.graph is None:
            if self.env.live:
                raise SetupError("LIVE is on but IG_TOKEN (secret) or IG_USER_ID (variable) is missing.")
            self.say("IG_TOKEN or IG_USER_ID is not set: Instagram checks are skipped in this dry run.")
        code = self.reconcile_pending()
        if code is not None:
            self.save("needs_check" if code == EXIT_CHECK else "retry")
            return code

        out_of_tries: set[str] = set()           # videos that failed MAX_TRIES_PER_DAY times today
        for _ in range(2 * len(self.sched.items) + 2):
            d = choose(self.sched, self.state, self.now, self.env.live, self.order, exclude=out_of_tries)
            if d.kind == "post" and self.graph is not None:
                # Videos of the schedule already on Instagram (a lost state commit, posted by hand) are recorded
                # first; any other recent Reel on the account counts too: still one per slot, 2 a day, 3 h apart.
                if self.record_found_elsewhere():
                    continue
                d = choose(self.sched, self.state, self.now, self.env.live, self.order, self.instagram_times(),
                           exclude=out_of_tries)
            if d.kind != "post":
                self.say(d.note)
                self._window_hint(d)
                self.save(d.kind)
                return EXIT_OK
            item = d.item
            assert item is not None
            found = self.find_on_instagram(item)
            if found:
                when = parse_ig_time(found.get("timestamp"))
                row = self.record_posted(item, media_id=found.get("id"), permalink=found.get("permalink"),
                                         container=None, source="found_on_instagram", when=when)
                self.say(f"{item.id} is already on Instagram ({row['permalink'] or row['media_id']}); recorded, "
                         "not posted again.")
                continue                          # choose() now sees it (and today's limit if it was today)
            tries = self.failures_today(item)
            if self.env.live and tries >= MAX_TRIES_PER_DAY:
                # the failing run already raised the alert; this slot goes to the next video instead (every
                # other guard - slot, day, gap, find_on_instagram - is checked again for it)
                self.say(f"{item.id} failed {tries} times today; not trying it again until tomorrow, the next "
                         f"video goes instead. Last error: {self.state['failures'][item.id].get('last_error')}")
                out_of_tries.add(item.id)
                continue
            try:
                self.validate(item)
                return self.post(item, d)
            except PosterError as e:
                if self.env.live and not isinstance(e, NeedsCheck):
                    self.note_failure(item, e)
                self.save("failed")
                raise
        raise PosterError("Selection loop did not settle (schedule.json may be inconsistent).")

    def record_found_elsewhere(self) -> bool:
        """Record every not-yet-recorded schedule video that Instagram already shows. True if any was found."""
        found_any = False
        for it in remaining_items(self.sched, self.state, True):
            m = self.find_on_instagram(it)
            if m:
                row = self.record_posted(it, media_id=m.get("id"), permalink=m.get("permalink"), container=None,
                                         source="found_on_instagram", when=parse_ig_time(m.get("timestamp")))
                self.say(f"{it.id} is already on Instagram ({row['permalink'] or row['media_id']}); recorded, "
                         "not posted again.")
                found_any = True
        return found_any

    def instagram_times(self) -> list[datetime]:
        """Publish times of the account's recent media that our records do not already cover."""
        known = {str(r.get("media_id")) for r in self.state.get("posted", []) if r.get("media_id")}
        out = []
        for m in self.recent_media():
            ts = parse_ig_time(m.get("timestamp"))
            if ts is not None and str(m.get("id")) not in known and self.now - ts < timedelta(days=2):
                out.append(ts)
        return out

    def failures_today(self, item: Item) -> int:
        f = (self.state.get("failures") or {}).get(item.id) or {}
        return int(f.get("count") or 0) if f.get("date") == self.today().isoformat() else 0

    def note_failure(self, item: Item, e: Exception) -> None:
        fails = self.state.setdefault("failures", {})
        old = fails.get(item.id) or {}
        today = self.today().isoformat()
        bad_days = [d for d in (old.get("permanent_days") or []) if isinstance(d, str)]
        permanent = isinstance(e, ItemError) or bool(getattr(e, "item_specific", False))
        if permanent and today not in bad_days:
            bad_days.append(today)
        fails[item.id] = {"date": today, "count": self.failures_today(item) + 1, "last_error": scrub(e)[:300],
                          "permanent_days": bad_days[-BLOCK_AFTER_DAYS:]}
        if permanent and len(bad_days) >= BLOCK_AFTER_DAYS:
            self.state.setdefault("blocked", {})[item.id] = {
                "since": today, "reason": scrub(e)[:300], **fingerprint(item)}
            self.say(f"{item.id} has failed for good on {len(bad_days)} days, so it is set aside and the next video "
                     "goes out instead. Fix it on the Mac (new video or caption) and export again to bring it back.")

    def _window_hint(self, d: Decision) -> None:
        if d.slot is None:
            return
        utc = d.slot.astimezone(timezone.utc)
        slot_min = utc.hour * 60 + utc.minute
        grace = int(SLOT_GRACE.total_seconds() // 60)
        if not any((r - slot_min) % 1440 < grace for r in cron_minutes()):
            self.say(f"Warning: no robot run starts within {grace // 60} hours after the slot "
                     f"{fmt_slot(self.sched, d.slot)} ({utc:%H:%M} UTC), so that slot would be missed. "
                     "Widen the cron in post.yml.")

    def post(self, item: Item, d: Decision) -> int:
        self.say(f"Due: {item.id} ({d.note})")
        if self.graph is not None:
            full = self.quota_full()
            if full:
                self.say(full + " The next run tries again.")
                self.save("quota_full")
                return EXIT_RETRY
        with tempfile.TemporaryDirectory(prefix="reel-", dir=os.environ.get("RUNNER_TEMP") or None) as tmp:
            try:
                video = self.fetch_video(item, Path(tmp))
                self.say(f"Video ready: {item.asset} ({video.stat().st_size:,} bytes, checks passed).")
            except SetupError:
                raise
            except PosterError as e:
                if self.env.live:
                    raise
                self.say(f"Dry run: could not get the video ({e}).")
                video = None
            if not self.env.live:
                return self.dry_run(item, video, d)
            return self.live_post(item, video, d)

    def dry_run(self, item: Item, video: Path | None, d: Decision | None = None) -> int:
        first = norm_caption(item.caption).split("\n", 1)[0]
        self.say(f"DRY-RUN: would post {item.id} now. Caption: {len(item.caption)} characters, starts "
                 f"\"{first[:80]}\", sha256 {sha256_text(item.caption)[:12]}. Nothing was created or published.")
        self.state["dry_runs"] = [r for r in self.state["dry_runs"] if r.get("id") != item.id] + [{
            "id": item.id, "date": self.today().isoformat(),
            "at": self.now.astimezone(timezone.utc).isoformat(timespec="seconds"),
            "slot": d.slot.astimezone(timezone.utc).isoformat(timespec="seconds") if d and d.slot else None}]
        self.save("dry_run")
        return EXIT_OK

    def live_post(self, item: Item, video: Path, d: Decision | None = None) -> int:
        video_url = self.public_video_url(item)
        container = self.create_container(item, video_url)
        self.state["pending"] = {"id": item.id, "container_id": container, "caption_sha256": sha256_text(item.caption),
                                 "started_at": self.now.astimezone(timezone.utc).isoformat(timespec="seconds"),
                                 "slot": d.slot.astimezone(timezone.utc).isoformat(timespec="seconds")
                                 if d and d.slot else None}
        self.save("uploading")
        if video_url:
            self.say(f"Instagram container {container} created; Instagram is fetching the video from GitHub.")
        else:
            self.say(f"Instagram container {container} created; uploading the video.")
            self.upload(container, video)
        if not self.wait_ready(container):
            self.say("Instagram is still processing the video after 15 minutes; the next run checks it.")
            self.save("processing")
            return EXIT_RETRY
        self.state["pending"]["publish_attempted"] = True
        self.save("publishing")                   # written before media_publish: a crash now is never re-posted
        media_id = self.publish(item, container)
        info: dict = {}
        try:
            info = self.graph.call("GET", media_id, params={"fields": "id,permalink,timestamp,caption,media_product_type"})
        except GraphError as e:
            self.say(f"Published, but could not read it back yet ({e}).")
        if info and not same_caption(str(info.get("caption") or ""), item.caption):
            self.say("Warning: the caption Instagram shows differs from schedule.json.")
        row = self.record_posted(item, media_id=media_id, permalink=info.get("permalink"), container=container,
                                 source="robot", when=self.now, slot=d.slot if d else None,
                                 reason=d.reason if d else None)
        self.state["pending"] = None
        self.save("posted")
        self.say(f"POSTED {item.id}: {row['permalink'] or media_id}")
        self.first_comment(item, media_id)
        return EXIT_OK


    # ---------------------------------------------------------------- after posting: stats, trends, order
    def maintain(self) -> None:
        """Stats + trends + trend order. Runs after the posting decision, so it can never delay or block a post;
        any problem is reported and swallowed (the exit code is the posting outcome only)."""
        jobs = []
        if self.env.stats:
            jobs.append(("Stats", self.collect_stats))
        if self.env.trends:
            jobs.append(("Trends", self.update_trends_and_order))
        for name, job in jobs:
            try:
                job()
            except Exception as e:                # noqa: BLE001 - never let stats/trends break a run
                self.say(f"{name} skipped this run ({type(e).__name__}: {scrub(e)[:200]}); posting is not affected.")

    def _insights(self, mid: str, metrics: list[str]) -> dict[str, float]:
        body = self.graph.call("GET", f"{mid}/insights", params={"metric": ",".join(metrics)}, retries=1)
        return insight_values(body)

    def collect_stats(self) -> None:
        """At most every 3 hours: followers + media count, and per Reel likes/comments + insights."""
        st = normalise_stats(read_json(self.stats_path))
        last = parse_ig_time(st.get("updated_at"))
        if last is not None and self.now - last < STATS_EVERY:
            return
        if self.graph is None:
            self.say("Stats skipped: IG_TOKEN or IG_USER_ID is not set.")
            return
        now_iso = iso(self.now)
        today = self.today().isoformat()
        errors: list[str] = []
        try:
            acc = self.graph.call("GET", self.env.ig_id, params={"fields": "followers_count,media_count"}, retries=1)
            if not isinstance(acc.get("followers_count"), int):
                raise GraphError("Instagram gave no followers_count.")
            snap = {"at": now_iso, "followers_count": acc["followers_count"], "media_count": acc.get("media_count")}
            hist = list(st["account"].get("history") or [])
            st["account"] = {"latest": snap, "history": hist + [snap]}
        except GraphError as e:
            errors.append(f"account: {e}"[:200])
        media: list[dict] = []
        try:
            after = None
            for _ in range(2):
                params = {"fields": "id,caption,timestamp,permalink,media_product_type,like_count,comments_count",
                          "limit": 50}
                if after:
                    params["after"] = after
                body = self.graph.call("GET", f"{self.env.ig_id}/media", params=params, retries=1)
                media.extend(d for d in (body.get("data") or []) if isinstance(d, dict) and d.get("id"))
                paging = body.get("paging") or {}
                after = (paging.get("cursors") or {}).get("after")
                if not after or not paging.get("next"):
                    break
        except GraphError as e:
            errors.append(f"media list: {e}"[:200])
        by_media = {str(r.get("media_id")): str(r.get("id")) for r in self.state.get("posted", []) if r.get("media_id")}
        items = {it.id: it for it in self.sched.items}
        unsupported = {m for m in (st.get("unsupported_metrics") or []) if m in INSIGHT_METRICS}
        permission = None                         # None = not learned this run, True ok, False missing
        probed = False
        asked = 0
        streak = 0                                # consecutive insights errors
        t0 = self.clock()
        for m in media:
            if str(m.get("media_product_type") or "REELS") != "REELS":
                continue
            ts = parse_ig_time(m.get("timestamp"))
            if ts is not None and self.now - ts > STATS_KEEP:
                continue
            mid = str(m["id"])
            vid = by_media.get(mid)
            prev = st["media"].get(mid) or {}
            it = items.get(vid or "")
            row = {**prev, "media_id": mid, "video_id": vid or prev.get("video_id"),
                   "permalink": m.get("permalink"), "timestamp": m.get("timestamp"),
                   "caption": first_line(m.get("caption")), "like_count": m.get("like_count"),
                   "comments_count": m.get("comments_count"), "updated_at": now_iso,
                   "keyword": (it.keyword if it else None) or prev.get("keyword"),
                   "category": (it.category if it else None) or prev.get("category")}
            out_of_time = self.clock() - t0 > STATS_BUDGET or streak >= STATS_MAX_ERRORS
            if permission is False:
                row["error"] = NEEDS_PERMISSION
            elif asked < MAX_INSIGHT_MEDIA and not out_of_time:
                asked += 1
                metrics = [x for x in INSIGHT_METRICS if x not in unsupported]
                try:
                    vals = self._insights(mid, metrics)
                    permission = True
                    streak = 0
                    row.pop("error", None)
                except GraphError as e:
                    vals = {}
                    if is_permission_error(e):
                        permission = False
                        row["error"] = NEEDS_PERMISSION
                    elif e.code == 100 and not probed:
                        probed = True                     # learn which metrics this account/API refuses, once
                        ok, bad = {}, []
                        for x in metrics:
                            try:
                                ok.update(self._insights(mid, [x]))
                            except GraphError as e2:
                                if is_permission_error(e2):
                                    permission = False
                                    break
                                if e2.code == 100:
                                    bad.append(x)
                        if ok:                            # some work: the others are really unsupported
                            unsupported |= set(bad)
                            vals, permission = ok, True
                            row.pop("error", None)
                        else:
                            row["error"] = NEEDS_PERMISSION if permission is False else f"insights: {e}"[:160]
                    else:
                        row["error"] = f"insights: {e}"[:160]
                    if row.get("error") and row["error"] != NEEDS_PERMISSION:
                        streak += 1
                for k in INSIGHT_METRICS:
                    if k in vals:
                        row[k] = vals[k]
            st["media"][mid] = row
            day_row = {"d": today, **{k: row.get(k2) for k, k2 in (
                ("views", "views"), ("reach", "reach"), ("likes", "like_count"), ("comments", "comments_count"),
                ("shares", "shares"), ("saved", "saved")) if row.get(k2) is not None}}
            rows = [r for r in (st["daily"].get(mid) or []) if isinstance(r, dict) and r.get("d") != today]
            st["daily"][mid] = rows + [day_row]
        if permission is False:
            st["permission"], st["note"] = NEEDS_PERMISSION, PERMISSION_NOTE
        elif permission is True:
            st["permission"], st["note"] = "ok", "views, reach, saves and shares are on"
        elif not media and not errors:
            st["note"] = "no Reels yet"
        if streak >= STATS_MAX_ERRORS or self.clock() - t0 > STATS_BUDGET:
            errors.append("insights stopped early this run (errors or time limit); the next stats run continues")
        st["unsupported_metrics"] = sorted(unsupported)
        st["errors"] = [scrub(e) for e in errors]
        st["updated_at"] = now_iso
        prune_stats(st, self.now, self.sched.tz)
        write_json_if_changed(self.stats_path, st, ignore=())
        latest = (st.get("account") or {}).get("latest") or {}
        self.say(f"Stats: followers {latest.get('followers_count', '?')}, {len(st['media'])} Reels tracked"
                 + (f"; {PERMISSION_NOTE}" if st["permission"] == NEEDS_PERMISSION else "")
                 + (f"; problems: {'; '.join(st['errors'])}" if st["errors"] else "") + ".")

    def fetch_trends(self) -> dict:
        """Free public RSS, no keys: Google Trends daily (US) + Google News searches for the niche terms."""
        headers = {"User-Agent": "Mozilla/5.0 (compatible; tapsmarter-robot/1.0)",
                   "Accept": "application/rss+xml, application/xml;q=0.9, */*;q=0.5"}
        feeds = [("trends", None, TRENDS_RSS, 40)] + [
            ("news", q, NEWS_RSS.format(q=quote(q + " when:1d")), 20) for q in NEWS_TERMS]
        titles: list[dict] = []
        sources = []
        t0 = self.clock()
        bounded = {"max_bytes": RSS_MAX_BYTES} if self.transport is urllib_transport else {}
        for src, q, url, limit in feeds:
            name = "Google Trends US" if src == "trends" else f"Google News: {q}"
            if self.clock() - t0 > TRENDS_BUDGET:
                sources.append({"name": name, "ok": False, "error": f"skipped: trends took over {TRENDS_BUDGET} s"})
                continue
            try:
                r = self.transport("GET", url, headers, None, RSS_TIMEOUT, True, **bounded)
                if not r.ok:
                    raise PosterError(f"HTTP {r.status}")
                got = parse_rss_titles(r.body, limit)
                titles += [{"t": t, "src": src, **({"q": q} if q else {})} for t in got]
                sources.append({"name": name, "ok": True, "count": len(got)})
            except Exception as e:                # noqa: BLE001 - one bad feed never stops the others
                sources.append({"name": name, "ok": False, "error": scrub(e)[:120]})
        ok = any(x["ok"] and x.get("count") for x in sources)
        return {"ok": ok, "titles": titles, "sources": sources,
                "error": None if ok else "every trend feed failed: " + "; ".join(
                    f"{x['name']}: {x.get('error') or 'empty'}" for x in sources)[:300]}

    def update_trends_and_order(self) -> None:
        tr = read_json(self.trends_path) or {}
        fetched = parse_ig_time(tr.get("updated_at"))
        tried = parse_ig_time(tr.get("attempted_at"))
        stale = fetched is None or fetched.astimezone(self.sched.tz).date() != self.today()
        if stale and (tried is None or self.now - tried >= TRENDS_RETRY or tr.get("ok")):
            got = self.fetch_trends()
            tr["attempted_at"] = iso(self.now)
            tr["sources"] = got["sources"]
            if got["ok"]:
                tr.update({"ok": True, "updated_at": iso(self.now), "titles": got["titles"], "error": None})
            else:
                tr.update({"ok": False, "error": got["error"]})
            self.say("Trends: " + (f"{len(got['titles'])} titles fetched." if got["ok"] else
                                   f"fetch failed, plan order is used ({got['error'][:120]})."))
        stats = read_json(self.stats_path)
        order, scores = compute_order(self.sched, self.state, self.env.live, tr, stats, self.now)
        perf, top = performance_topics(stats)
        tr.update({"version": 1, "order_status": order["status"], "note": order["note"],
                   "performance_topics": sorted(perf), "top_reels": top, "scores": scores})
        tr.setdefault("titles", [])
        write_json_if_changed(self.trends_path, tr, ignore=())
        if write_json_if_changed(self.order_path, order):
            nxt = order["upcoming"][0] if order["upcoming"] else None
            self.say(f"Order: {order['note']}" + (f" Next: {nxt['id']} ({nxt['reason']})." if nxt else ""))
        self.order = order


# ====================================================================== small JSON files (state/*.json)
def read_json(path: Path) -> dict | None:
    """A state file as a dict, or None when missing or broken (stats/trends/order never stop the robot)."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def write_json_if_changed(path: Path, data: dict, ignore: tuple[str, ...] = ("generated_at",)) -> bool:
    """Write only when something besides the `ignore` keys changed (no commit every 20 minutes)."""
    old = read_json(path)
    strip = lambda d: json.dumps({k: v for k, v in (d or {}).items() if k not in ignore}, sort_keys=True)
    if old is not None and strip(old) == strip(data):
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)
    return True


def iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat(timespec="seconds")


def first_line(text: Any, n: int = 120) -> str:
    return norm_caption(str(text or "")).split("\n", 1)[0][:n]


# ====================================================================== stats (pure helpers)
def is_permission_error(e: GraphError) -> bool:
    return e.code == 10 or (isinstance(e.code, int) and 200 <= e.code <= 299)


def insight_values(body: dict) -> dict[str, float]:
    """{"data": [{"name", "values": [{"value"}]} | {"name", "total_value": {"value"}}]} -> {name: value}."""
    out: dict[str, float] = {}
    for d in body.get("data") or []:
        if not isinstance(d, dict) or not d.get("name"):
            continue
        v = None
        if isinstance(d.get("total_value"), dict):
            v = d["total_value"].get("value")
        elif isinstance(d.get("values"), list) and d["values"] and isinstance(d["values"][-1], dict):
            v = d["values"][-1].get("value")
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            out[str(d["name"])] = v
    return out


def empty_stats() -> dict:
    return {"version": 1, "updated_at": None, "permission": "unknown", "note": "", "errors": [],
            "account": {"latest": None, "history": []}, "media": {}, "daily": {}, "unsupported_metrics": []}


def normalise_stats(old: dict | None) -> dict:
    """An old stats.json with odd shapes (hand edits, older versions) never breaks the stats run."""
    st = empty_stats()
    if not isinstance(old, dict):
        return st
    for k in ("updated_at", "permission", "note"):
        if isinstance(old.get(k), str):
            st[k] = old[k]
    acc = old.get("account")
    if isinstance(acc, dict) and isinstance(acc.get("history"), list):
        st["account"]["history"] = [h for h in acc["history"] if isinstance(h, dict)]
    for k in ("media", "daily"):
        if isinstance(old.get(k), dict):
            st[k] = {str(mid): v for mid, v in old[k].items() if isinstance(v, (dict, list))}
    st["media"] = {k: v for k, v in st["media"].items() if isinstance(v, dict)}
    st["daily"] = {k: v for k, v in st["daily"].items() if isinstance(v, list)}
    if isinstance(old.get("unsupported_metrics"), list):
        st["unsupported_metrics"] = [m for m in old["unsupported_metrics"] if m in INSIGHT_METRICS]
    return st


def prune_stats(st: dict, now: datetime, tz: ZoneInfo) -> dict:
    """Keep 60 days of account snapshots and daily per-Reel rows; the latest row of every Reel seen in 60 days."""
    cut = now.astimezone(UTC) - STATS_KEEP
    cut_day = (now.astimezone(tz) - STATS_KEEP).date().isoformat()
    acc = st.get("account") if isinstance(st.get("account"), dict) else {}
    hist = [h for h in (acc.get("history") or []) if isinstance(h, dict)
            and (parse_ig_time(h.get("at")) or cut) > cut]
    st["account"] = {"latest": hist[-1] if hist else None, "history": hist}
    media = {k: v for k, v in (st.get("media") or {}).items() if isinstance(v, dict)
             and (parse_ig_time(v.get("updated_at")) or cut) > cut}
    st["media"] = media
    daily = {}
    for mid, rows in (st.get("daily") or {}).items():
        if mid not in media or not isinstance(rows, list):
            continue
        keep = [r for r in rows if isinstance(r, dict) and str(r.get("d") or "") > cut_day]
        if keep:
            daily[mid] = keep[-62:]
    st["daily"] = daily
    return st


# ====================================================================== trends (pure helpers)
STOPWORDS = frozenset("""a an and are as at be been but by can could did do does for from get gets got had has have
how if in into is it its just like make makes more most my new no not now of off on one only or our out over own
so than that the their them then there these they this to too up us use using very was we what when where which
who why will with without you your yours free easy tip tips trick tricks app apps phone phones settings setting
step steps way ways day days today here best really need needs want help helps let lets every never always still
about after before vs year years week time times first last next people thing things says said report reports
video watch see look comment guide link bio follow made voice visuals google two three four five ten check
number avoid top big real full back right side open live sure says set turn off know again""".split())

SYNONYMS = {
    "ios": "iphone", "apple": "iphone", "ipad": "iphone", "siri": "iphone", "airpods": "iphone", "iphones": "iphone",
    "pixel": "android", "samsung": "android", "galaxy": "android", "androids": "android",
    "chatgpt": "ai", "openai": "ai", "gpt": "ai", "gemini": "ai", "copilot": "ai", "chatbot": "ai",
    "chatbots": "ai", "claude": "ai", "llm": "ai",
    "scams": "scam", "scammer": "scam", "scammers": "scam", "fraud": "scam", "phishing": "scam", "spam": "scam",
    "robocall": "scam", "robocalls": "scam", "hacker": "scam", "hackers": "scam", "hacked": "scam",
    "charging": "battery", "charger": "battery", "charge": "battery", "batteries": "battery",
    "updates": "update", "upgrade": "update", "updated": "update",
    "photos": "photo", "camera": "photo", "picture": "photo", "pictures": "photo", "selfie": "photo",
    "passwords": "password", "passkey": "password", "passkeys": "password",
    "budget": "money", "budgeting": "money", "savings": "money", "taxes": "money", "tax": "money",
    "calls": "call", "calling": "call", "caller": "call", "callers": "call",
    "texts": "text", "texting": "text", "messages": "text", "message": "text", "imessage": "text", "sms": "text",
    "wifi": "wifi", "hotspot": "wifi",
}
CONCEPTS = frozenset(SYNONYMS.values())
QUERY_NEWS_CAP = 2                         # the search terms' own topics show up all over tech news: news adds <= 2
_WORD_RE = re.compile(r"[a-z0-9]+")


def canon_tokens(text: Any) -> list[str]:
    """Lowercase words without stopwords/numbers, synonyms folded ("ChatGPT" -> "ai"), simple plurals cut."""
    out: list[str] = []
    s = unicodedata.normalize("NFKD", str(text or "")).encode("ascii", "ignore").decode().lower()
    for w in _WORD_RE.findall(s):
        if w.isdigit() or (len(w) < 3 and w != "ai") or w in STOPWORDS:
            continue
        if w in SYNONYMS:
            w = SYNONYMS[w]
        elif len(w) > 4 and w.endswith("s") and not w.endswith(("ss", "us", "is", "as", "os")):
            w = w[:-1]
            w = SYNONYMS.get(w, w)
        if w not in STOPWORDS and w not in out:
            out.append(w)
    return out


QUERY_WORDS = frozenset(canon_tokens(" ".join(NEWS_TERMS)))


def item_words(it: Item) -> list[str]:
    return canon_tokens(" ".join(x for x in (it.id, it.title or "", it.keyword or "", it.category or "",
                                            first_line(it.caption)) if x))


def topics_of(keyword: Any, text: Any, category: Any = None) -> set[str]:
    out = {t for t in canon_tokens(text) if t in CONCEPTS}
    for extra in (keyword, category):
        out |= set(canon_tokens(extra))
    return out


def parse_rss_titles(body: bytes, limit: int) -> list[str]:
    """Item titles (and Google Trends' news_item_title) of an RSS feed. Refuses DTDs/entities and huge bodies."""
    if len(body) > RSS_MAX_BYTES:
        raise ValueError("feed too large")
    low = body.lower()
    if b"<!doctype" in low or b"<!entity" in low:
        raise ValueError("feed has a DTD")
    import html
    import xml.etree.ElementTree as ET
    from xml.parsers import expat

    def refuse(*_a):
        raise ValueError("feed has a DTD")
    # expat decodes the body itself (UTF-16 etc.), so this catches a DTD the byte scan above cannot see; the
    # doctype handler fires before any entity is declared or expanded
    pre = expat.ParserCreate()
    pre.StartDoctypeDeclHandler = refuse
    pre.EntityDeclHandler = refuse
    pre.SetParamEntityParsing(expat.XML_PARAM_ENTITY_PARSING_NEVER)
    try:
        pre.Parse(body, True)
    except expat.ExpatError as e:
        raise ValueError(f"feed is not valid XML: {e}") from None
    root = ET.fromstring(body)
    out: list[str] = []
    for item in root.iter():
        if not isinstance(item.tag, str) or item.tag.rsplit("}", 1)[-1] != "item":
            continue
        for child in item.iter():
            tag = child.tag.rsplit("}", 1)[-1] if isinstance(child.tag, str) else ""
            if tag in ("title", "news_item_title") and child.text:
                t = re.sub(r"\s+", " ", html.unescape(child.text)).strip()[:160]
                if t and t not in out:
                    out.append(t)
        if len(out) >= limit:
            break
    return out[:limit]


def strip_publisher(title: str) -> str:
    """Google News titles end in " - Publisher"."""
    return title.rsplit(" - ", 1)[0] if " - " in title else title


def trend_index(titles: list[dict]) -> dict[str, dict]:
    """word -> {"trends": n, "news": n}. Words of a news SEARCH TERM are not counted in that search's own results
    (every "battery" search result says battery, which tells nothing)."""
    idx: dict[str, dict] = {}
    for row in titles:
        src = "trends" if row.get("src") == "trends" else "news"
        excl = set(canon_tokens(row.get("q") or "")) if src == "news" else set()
        for w in canon_tokens(strip_publisher(str(row.get("t") or ""))):
            if w in excl:
                continue
            e = idx.setdefault(w, {"trends": 0, "news": 0})
            e[src] += 1
    return idx


def performance_topics(stats: dict | None) -> tuple[set[str], list[str]]:
    """Topics of the best Reels so far (views + 25 x shares), once at least 4 Reels have views."""
    media = (stats or {}).get("media") if isinstance((stats or {}).get("media"), dict) else {}
    rows = [(mid, m) for mid, m in media.items() if isinstance(m, dict)
            and isinstance(m.get("views"), (int, float)) and not isinstance(m.get("views"), bool)]
    if len(rows) < 4:
        return set(), []
    perf = lambda m: float(m.get("views") or 0) + 25.0 * float(m.get("shares") or 0)
    rows.sort(key=lambda r: (-perf(r[1]), str(r[1].get("timestamp") or ""), r[0]))
    top = rows[:max(1, min(5, len(rows) // 3))]
    topics: set[str] = set()
    names = []
    for _, m in top:
        topics |= topics_of(m.get("keyword"), m.get("caption"), m.get("category"))
        names.append(str(m.get("video_id") or m.get("media_id") or "?"))
    return topics, names


def score_item(it: Item, idx: dict[str, dict], perf: set[str]) -> dict:
    trend, matched = 0, []
    for w in sorted(item_words(it)):
        e = idx.get(w)
        if not e:
            continue
        news = min(e["news"], QUERY_NEWS_CAP) if w in QUERY_WORDS else e["news"]
        pts = min(3 * e["trends"] + news, TOKEN_CAP)
        trend += pts
        bits = [f"{e['trends']} trend" + ("s" if e["trends"] != 1 else "")] if e["trends"] else []
        bits += [f"{e['news']} news"] if e["news"] else []
        matched.append(f"{w} (+{pts}: {', '.join(bits)})")
    shared = sorted(topics_of(it.keyword, first_line(it.caption), it.category) & perf)
    boost = min(2 * len(shared), 4)
    if boost:
        matched.append(f"like top Reels: {', '.join(shared)} (+{boost})")
    score = trend + boost
    reason = (f"trend score {score}: " + "; ".join(matched)) if score else "no trend match (plan order)"
    return {"score": score, "trend": trend, "performance": boost, "matched": matched, "reason": reason[:300]}


def compute_order(sched: Schedule, state: dict, live: bool, trends: dict | None, stats: dict | None,
                  now: datetime) -> tuple[dict, dict]:
    """(order.json, scores) from cached trend titles + stats. Deterministic: same inputs, same order. Only the next
    TREND_WINDOW videos of the plan can move; ties keep plan order; a failed/stale fetch means plan order."""
    remaining = remaining_items(sched, state, live)
    idx_of = {it.id: n for n, it in enumerate(sched.items)}
    fetched = parse_ig_time((trends or {}).get("updated_at"))
    titles = (trends or {}).get("titles") if isinstance((trends or {}).get("titles"), list) else []
    fresh = bool((trends or {}).get("ok")) and fetched is not None and now - fetched <= TRENDS_MAX_AGE and titles
    perf, top_names = performance_topics(stats)
    scores: dict[str, dict] = {}
    if fresh:
        idx = trend_index([t for t in titles if isinstance(t, dict)])
        scores = {it.id: score_item(it, idx, perf) for it in remaining}
        # fairness: a video already passed over by MAX_OVERTAKES posts planned after it goes first (plan order)
        posted_idx = [idx_of[v] for v in done_ids(state, live) if v in idx_of]
        pinned = [it for it in remaining if sum(1 for n in posted_idx if n > idx_of[it.id]) >= MAX_OVERTAKES]
        pinned_ids = {it.id for it in pinned}
        for it in pinned:
            scores[it.id]["reason"] = (f"waited long enough (passed over by {MAX_OVERTAKES}+ later videos); "
                                       + scores[it.id]["reason"])[:300]
        rest = [it for it in remaining if it.id not in pinned_ids]
        window = pinned + sorted(rest[:max(0, TREND_WINDOW - len(pinned))],
                                 key=lambda it: (-scores[it.id]["score"], idx_of[it.id]))
        queue = window + rest[max(0, TREND_WINDOW - len(pinned)):]
        status = "trends"
        note = (f"Next {len(window)} videos ordered by trend score (ties keep plan order)"
                + (f"; top Reels so far: {', '.join(top_names)}" if top_names else "") + ".")
    else:
        queue = remaining
        status = "plan_order"
        why = (trends or {}).get("error") or ("trend titles are older than 36 hours" if fetched else
                                              "no trend data yet")
        note = f"Plan order: {why}."
    reasons = {it.id: scores[it.id]["reason"] for it in queue[:TREND_WINDOW] if it.id in scores}
    order = {"version": 1, "generated_at": iso(now), "status": status, "note": note,
             "schedule_sha256": schedule_sha(sched),
             "based_on": {"trends_updated_at": (trends or {}).get("updated_at"),
                          "stats_updated_at": (stats or {}).get("updated_at")},
             "order": [it.id for it in queue], "reasons": reasons}
    order["upcoming"] = project_upcoming(sched, state, live, order, now)
    return order, scores


def project_upcoming(sched: Schedule, state: dict, live: bool, order: dict, now: datetime, n: int = 6) -> list[dict]:
    """The next n slots and the video each would take (a projection: the robot decides again at every slot)."""
    now_u = now.astimezone(UTC)
    remaining = remaining_items(sched, state, live)
    ranked = ranked_items(sched, remaining, order)
    pool = list(ranked[0]) if ranked else list(remaining)
    reasons = ranked[1] if ranked else {}
    posts = post_times(state, live, sched.tz)
    fallback = [remaining[0].slot_time] if remaining else []
    out: list[dict] = []
    per_day: dict[date, int] = {}
    for p in posts:
        per_day[p.astimezone(sched.tz).date()] = per_day.get(p.astimezone(sched.tz).date(), 0) + 1
    last = posts[-1] if posts else None
    for sl in slots_between(sched, now_u - SLOT_GRACE, now_u + timedelta(days=40), fallback):
        if not pool or len(out) >= n:
            break
        day = sl.astimezone(sched.tz).date()
        if sl + SLOT_GRACE <= now_u or (last is not None and (last >= sl or sl - last < MIN_GAP)) \
                or per_day.get(day, 0) >= MAX_PER_DAY:
            continue
        due = [it for it in pool if planned_at(sched, it) <= sl]
        if not due:
            continue
        it = pool[0] if ranked else due[0]
        pool.remove(it)
        out.append({"slot": iso(sl), "slot_rome": f"{sl.astimezone(sched.tz):%a %d %b %H:%M}", "id": it.id,
                    "reason": str(reasons.get(it.id) or "plan order")})
        last = sl
        per_day[day] = per_day.get(day, 0) + 1
    return out


# ====================================================================== entry point
def write_summary(path: str | None, title: str, lines: list[str]) -> None:
    if not path:
        return
    body = f"## {scrub(title)}\n\n" + "\n".join(f"- {scrub(line)}" for line in lines) + "\n"
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(body)
    except OSError:
        pass


TITLES = {EXIT_OK: "Reel robot: OK", EXIT_SETUP: "Reel robot: needs a fix", EXIT_RETRY: "Reel robot: will retry",
          EXIT_CHECK: "Reel robot: please check Instagram"}


def main(argv: list[str] | None = None, environ: dict | None = None, *, transport: Transport = urllib_transport,
         sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic,
         out: Callable[[str], None] = print) -> int:
    ap = argparse.ArgumentParser(description="Post the current slot's Reel from schedule.json (one per slot).")
    ap.add_argument("--dry-run", action="store_true", help="never create or publish, whatever LIVE says")
    ap.add_argument("--now", help="pretend it is this ISO time (dry runs only), e.g. 2026-11-12T12:05+01:00")
    ap.add_argument("--video-dir", type=Path, help="use local mp4s (<dir>/<asset> or <dir>/<id>/<id>_instagram.mp4)")
    ap.add_argument("--schedule", type=Path, default=SCHEDULE_PATH)
    ap.add_argument("--state", type=Path, default=STATE_PATH)
    args = ap.parse_args(argv)
    environ = dict(os.environ if environ is None else environ)
    env = read_env(environ, args.dry_run)
    for secret in (env.token, env.gh_token):
        remember_secret(secret)
    if env.in_actions and env.token:
        out(f"::add-mask::{env.token}")          # GitHub hides it from the log (only ever printed inside Actions)
    lines: list[str] = []
    code = EXIT_OK
    try:
        if env.paused:
            lines.append("PAUSED is on: nothing was checked or posted. Set the PAUSED variable to false to resume.")
            out(lines[-1])
            return EXIT_OK
        now = datetime.now(timezone.utc)
        if args.now:
            if env.live:
                raise SetupError("--now only works in dry runs.")
            now = datetime.fromisoformat(args.now)
            if now.tzinfo is None:
                raise SetupError("--now needs a UTC offset, e.g. 2026-11-12T12:05+01:00")
        sched = load_schedule(args.schedule)
        state = load_state(args.state)
        poster = Poster(env, sched, state, args.state, now, transport=transport, sleep=sleep, clock=clock,
                        video_dir=args.video_dir, out=out)
        try:
            code = poster.run()
        finally:
            poster.maintain()                     # stats / trends / order: never changes the exit code
            lines = poster.lines
    except PosterError as e:
        code = e.exit_code
        lines.append(f"{type(e).__name__}: {e}")
        out(lines[-1])
    except Exception as e:                        # never leak a raw traceback with URLs/headers unscrubbed
        code = EXIT_RETRY
        lines.append(f"Unexpected error: {type(e).__name__}: {scrub(e)}")
        out(lines[-1])
        out(scrub(traceback.format_exc()))
    finally:
        write_summary(env.summary_path, TITLES.get(code, "Reel robot"), lines)
    return code


if __name__ == "__main__":
    sys.exit(main())
