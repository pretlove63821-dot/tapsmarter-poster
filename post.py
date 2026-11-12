#!/usr/bin/env python3
"""Tap Smarter cloud poster: posts at most ONE Instagram Reel per run (and per day) from schedule.json.

Runs inside GitHub Actions (.github/workflows/post.yml) so Reels go out even when the Mac is asleep.
Python 3.12, standard library only (nothing to pip install on the runner).

Switches (GitHub repo variables, read from the environment):
  PAUSED=true  -> do nothing at all.
  LIVE=true    -> really post. Anything else = DRY-RUN: every check runs, nothing is created or published.
Secret: IG_TOKEN (never printed, masked, scrubbed from every message). Variable: IG_USER_ID.

The Instagram request shapes copy autopilot/publish/instagram_login.py (Unified-login "EAA..." token on
graph.facebook.com/v27.0, {IG_ID}/media REELS resumable -> rupload -> status_code -> media_publish).

Exit codes: 0 ok (posted / nothing due / dry-run / paused), 1 setup problem (fix something),
            2 failed but safe to retry next run, 3 unclear outcome (a person should look; never re-posted blindly).
See SCHEMA.md for schedule.json and state/posted.json.
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
CRON_WINDOW_UTC = (dtime(14, 5), dtime(21, 45))  # post.yml runs at :05/:25/:45, 14:05-21:45 UTC

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
                     follow_redirects: bool) -> Response:
    req = urllib.request.Request(url, data=body, method=method)
    for k, v in headers.items():
        if k.lower() == "authorization":
            req.add_unredirected_header(k, v)    # never forwarded to a redirect target (e.g. GitHub's S3 links)
        else:
            req.add_header(k, v)
    opener = _OPEN_FOLLOW if follow_redirects else _OPEN_STAY
    try:
        with opener.open(req, timeout=timeout) as r:
            return Response(r.status, {k.lower(): v for k, v in r.headers.items()}, r.read())
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


@dataclass
class Schedule:
    items: list[Item]
    tz: ZoneInfo
    tz_name: str
    slots: dict[str, dtime] = field(default_factory=dict)
    release_tag: str = DEFAULT_TAG
    generated_at: str | None = None


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
    slots = {}
    for k in ("weekday", "weekend"):
        if (raw.get("slots") or {}).get(k):
            slots[k] = _hhmm(raw["slots"][k], f"slots.{k}")
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
        ))
    return Schedule(items=items, tz=tz, tz_name=tz_name, slots=slots,
                    release_tag=str(raw.get("release_tag") or DEFAULT_TAG), generated_at=raw.get("generated_at"))


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


# ====================================================================== choosing today's video (pure)
@dataclass
class Decision:
    kind: str                     # post | waiting | posted_today | nothing_due | finished
    item: Item | None = None
    slot: datetime | None = None
    note: str = ""


def done_ids(state: dict, live: bool) -> set[str]:
    ids = {str(r.get("id")) for r in state.get("posted", [])}
    if not live:
        ids |= {str(r.get("id")) for r in state.get("dry_runs", [])}
    return ids


def posted_on(state: dict, day: date, live: bool) -> dict | None:
    rows = list(state.get("posted", [])) + ([] if live else list(state.get("dry_runs", [])))
    for r in rows:
        if r.get("date") == day.isoformat():
            return r
    return None


def fingerprint(item: Item) -> dict:
    return {"caption_sha256": sha256_text(item.caption), "asset_sha256": item.asset_sha256,
            "asset_size": item.asset_size}


def is_blocked(state: dict, item: Item) -> bool:
    """Set aside after repeated permanent failures, until the Mac exports a changed caption or video for it."""
    b = (state.get("blocked") or {}).get(item.id)
    if not isinstance(b, dict):
        return False
    return all(b.get(k) == v for k, v in fingerprint(item).items())


def slot_for_day(sched: Schedule, day: date, fallback: Item) -> dtime:
    """The item planned for that day knows its own (DST-aware) slot; `slots` only covers catch-up days."""
    for it in sched.items:
        if it.date == day:
            return it.slot_time
    key = "weekend" if day.weekday() >= 5 else "weekday"
    if key in sched.slots:
        return sched.slots[key]
    return fallback.slot_time


def choose(sched: Schedule, state: dict, now: datetime, live: bool) -> Decision:
    today = now.astimezone(sched.tz).date()
    done = done_ids(state, live)
    already = posted_on(state, today, live)
    remaining = [it for it in sched.items if it.id not in done and not is_blocked(state, it)]
    if already:
        nxt = remaining[0] if remaining else None
        return Decision("posted_today", nxt, note=f"{already.get('id')} already went out today; one Reel per day.")
    if not remaining:
        return Decision("finished", note="Every video in schedule.json is posted.")
    due = [it for it in remaining if it.date <= today]
    if not due:
        first = remaining[0]
        slot = datetime.combine(first.date, first.slot_time, tzinfo=sched.tz)
        return Decision("nothing_due", first, slot, note=f"Next video {first.id} is planned for {fmt(slot)}.")
    item = due[0]
    slot = datetime.combine(today, slot_for_day(sched, today, item), tzinfo=sched.tz)
    if now < slot:
        return Decision("waiting", item, slot, note=f"{item.id} goes out after today's slot {fmt(slot)}.")
    late = f" (planned for {item.date.isoformat()}: catching up)" if item.date < today else ""
    return Decision("post", item, slot, note=f"{item.id} is due now{late}.")


def fmt(dt: datetime) -> str:
    return dt.strftime("%a %d %b %Y %H:%M %Z")


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


def truthy(v: str | None) -> bool:
    return str(v or "").strip().lower() in ("true", "1", "yes", "on")


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
                      source: str, when: datetime | None) -> dict:
        when = when or self.now
        row = {"id": item.id, "date": when.astimezone(self.sched.tz).date().isoformat(),
               "posted_at": when.astimezone(timezone.utc).isoformat(timespec="seconds"),
               "media_id": media_id, "permalink": permalink, "container_id": container,
               "caption_sha256": sha256_text(item.caption), "source": source}
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

        for _ in range(len(self.sched.items) + 1):
            d = choose(self.sched, self.state, self.now, self.env.live)
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
                # the failing run already raised the alert; the rest of today's runs stay quiet (green)
                self.say(f"{item.id} failed {tries} times today; not trying again until tomorrow. Last error: "
                         f"{self.state['failures'][item.id].get('last_error')}")
                self.save("gave_up_today")
                return EXIT_OK
            try:
                self.validate(item)
                return self.post(item, d)
            except PosterError as e:
                if self.env.live and not isinstance(e, NeedsCheck):
                    self.note_failure(item, e)
                self.save("failed")
                raise
        raise PosterError("Selection loop did not settle (schedule.json may be inconsistent).")

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
        utc = d.slot.astimezone(timezone.utc).time()
        mins = lambda t: t.hour * 60 + t.minute
        # a slot up to 15 min before the first run is fine (it goes out at the first run); later than the
        # last run is not (it would only go out the next day)
        if not (mins(CRON_WINDOW_UTC[0]) - 15 <= mins(utc) <= mins(CRON_WINDOW_UTC[1])):
            self.say(f"Warning: the slot {fmt(d.slot)} is {utc:%H:%M} UTC, outside the robot's "
                     f"{CRON_WINDOW_UTC[0]:%H:%M}-{CRON_WINDOW_UTC[1]:%H:%M} UTC run "
                     "window; it will only go out on the next run inside the window. Widen the cron in post.yml.")

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
                return self.dry_run(item, video)
            return self.live_post(item, video)

    def dry_run(self, item: Item, video: Path | None) -> int:
        first = norm_caption(item.caption).split("\n", 1)[0]
        self.say(f"DRY-RUN: would post {item.id} now. Caption: {len(item.caption)} characters, starts "
                 f"\"{first[:80]}\", sha256 {sha256_text(item.caption)[:12]}. Nothing was created or published.")
        self.state["dry_runs"] = [r for r in self.state["dry_runs"] if r.get("id") != item.id] + [{
            "id": item.id, "date": self.today().isoformat(),
            "at": self.now.astimezone(timezone.utc).isoformat(timespec="seconds")}]
        self.save("dry_run")
        return EXIT_OK

    def live_post(self, item: Item, video: Path) -> int:
        video_url = self.public_video_url(item)
        container = self.create_container(item, video_url)
        self.state["pending"] = {"id": item.id, "container_id": container, "caption_sha256": sha256_text(item.caption),
                                 "started_at": self.now.astimezone(timezone.utc).isoformat(timespec="seconds")}
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
                                 source="robot", when=self.now)
        self.state["pending"] = None
        self.save("posted")
        self.say(f"POSTED {item.id}: {row['permalink'] or media_id}")
        self.first_comment(item, media_id)
        return EXIT_OK


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
    ap = argparse.ArgumentParser(description="Post today's Reel from schedule.json (one per day).")
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
