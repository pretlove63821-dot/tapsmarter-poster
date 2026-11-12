"""Offline tests for post.py (no network: every HTTP call goes to a fake). Run:
    cd cloud-poster && python3 -m unittest discover -s tests -q
"""
from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import post  # noqa: E402

TOKEN = "EAAGtestTOKEN1234567890abcdefXYZ"
GH_TOKEN = "ghs_testGithubToken1234567890abcd"
IG = "17841457478590795"
ROME = post.ZoneInfo("Europe/Rome")
VIDEO = b"\x00\x00\x00\x18ftypmp42" + b"x" * 2000


def caption(vid: str) -> str:
    return (f"Tip number {vid}\nYour phone can do this, and it's free.\n\nSettings → Apps → Phone.\n\n"
            f"Comment KEY{vid[-3:]} and we'll reply with the free guide.\n\nMade with an AI voice and AI visuals."
            "\n\n#iphonetips #techtips")


def make_schedule(ids=("wd-102", "wd-103", "wd-137", "wd-120"), start="2026-11-12", slots=True, **extra) -> dict:
    from datetime import date, timedelta
    d0 = date.fromisoformat(start)
    items = []
    for i, vid in enumerate(ids):
        d = d0 + timedelta(days=i)
        items.append({"id": vid, "date": d.isoformat(), "slot_time": "10:00" if d.weekday() >= 5 else "12:00",
                      "caption": caption(vid), "caption_sha256": post.sha256_text(caption(vid)),
                      "asset": f"{vid}.mp4", "asset_size": len(VIDEO), "first_comment": f"Steps for {vid}",
                      **extra})
    raw = {"version": 1, "timezone": "Europe/Rome", "items": items}
    if slots:
        raw["slots"] = {"weekday": "12:00", "weekend": "10:00"}
    return raw


def rome(y, m, d, hh, mm=0) -> datetime:
    return datetime(y, m, d, hh, mm, tzinfo=ROME)


class FakeMeta:
    """Fake Graph + rupload + GitHub REST. Records every call."""

    def __init__(self, media=None):
        self.calls: list[tuple[str, str, dict, bytes | None]] = []
        self.media = list(media or [])            # what {IG}/media lists
        self.container_status = "FINISHED"
        self.publish_status = 200
        self.quota = (0, 100)
        self.container_n = 0

    # -- helpers for asserting
    def posts_to(self, suffix: str) -> list:
        return [c for c in self.calls if c[0] == "POST" and urlsplit(c[1]).path.endswith(suffix)]

    def writes(self) -> list:
        return [c for c in self.calls if c[0] == "POST"]

    def __call__(self, method, url, headers, body, timeout, follow):
        self.calls.append((method, url, dict(headers), body))
        parts = urlsplit(url)
        path, q = parts.path, {k: v[0] for k, v in parse_qs(parts.query).items()}
        js = lambda obj, status=200: post.Response(status, {"content-type": "application/json"},
                                                   json.dumps(obj).encode())
        if parts.hostname == "api.github.com":
            assert headers["Authorization"] == f"Bearer {GH_TOKEN}"
            if path.endswith("/releases/tags/videos"):
                return js({"id": 7})
            if path.endswith("/releases/7/assets"):
                return js([{"id": 100 + i, "name": f"wd-{n}.mp4", "size": len(VIDEO), "state": "uploaded"}
                           for i, n in enumerate(("102", "103", "137", "120", "143"))])
            if "/releases/assets/" in path:
                assert follow and headers["Accept"] == "application/octet-stream"
                return post.Response(200, {}, VIDEO)
            return js({"message": "Not Found"}, 404)
        if parts.hostname == "rupload.facebook.com":
            assert headers["Authorization"] == f"OAuth {TOKEN}"
            assert headers["offset"] == "0" and headers["file_size"] == str(len(VIDEO))
            return js({"success": True})
        assert parts.hostname == "graph.facebook.com", url
        assert headers["Authorization"] == f"Bearer {TOKEN}"
        assert TOKEN not in url
        form = {k: v[0] for k, v in parse_qs((body or b"").decode(), keep_blank_values=True).items()}
        tail = path.split("/v27.0/", 1)[1]
        if method == "GET" and tail == f"{IG}/content_publishing_limit":
            return js({"data": [{"quota_usage": self.quota[0], "config": {"quota_total": self.quota[1]}}]})
        if method == "GET" and tail == f"{IG}/media":
            return js({"data": self.media})
        if method == "POST" and tail == f"{IG}/media":
            self.container_n += 1
            return js({"id": f"cont{self.container_n}"})
        if method == "POST" and tail == f"{IG}/media_publish":
            if self.publish_status != 200:
                cap = self._last_caption()
                self.media.insert(0, {"id": "media99", "caption": cap, "timestamp": "2026-11-12T11:01:00+0000",
                                      "permalink": "https://www.instagram.com/reel/X99/"})
                self.container_status = "PUBLISHED"
                return js({"error": {"message": "Service unavailable", "code": 2}}, self.publish_status)
            cap = self._last_caption()
            self.media.insert(0, {"id": "media1", "caption": cap, "timestamp": "2026-11-12T11:01:00+0000",
                                  "permalink": "https://www.instagram.com/reel/ABC/"})
            return js({"id": "media1"})
        if method == "POST" and tail.endswith("/comments"):
            return js({"id": "c1"})
        if method == "GET" and tail.startswith("cont"):
            return js({"status_code": self.container_status})
        if method == "GET" and tail.startswith("media"):
            m = next((m for m in self.media if m["id"] == tail), {})
            return js({"id": tail, "permalink": m.get("permalink"), "caption": m.get("caption"),
                       "media_product_type": "REELS"})
        return js({"error": {"message": "unknown path", "code": 100}}, 400)

    def _last_caption(self) -> str:
        create = self.posts_to(f"{IG}/media")[-1]
        return parse_qs(create[3].decode())["caption"][0]


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.sched_path = self.tmp / "schedule.json"
        self.state_path = self.tmp / "state" / "posted.json"
        self.summary = self.tmp / "summary.md"
        self.out = io.StringIO()
        self.write_schedule(make_schedule())

    def write_schedule(self, raw):
        self.sched_path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")

    def env(self, live=False, **kw):
        e = {"IG_TOKEN": TOKEN, "IG_USER_ID": IG, "GITHUB_TOKEN": GH_TOKEN, "GITHUB_REPOSITORY": "preet/reels",
             "GITHUB_STEP_SUMMARY": str(self.summary), "VIDEO_SOURCE": "upload"}
        if live:
            e["LIVE"] = "true"
        e.update(kw)
        return e

    def run_post(self, now: datetime, fake: FakeMeta, live=False, state=None, **envkw) -> int:
        if state is not None:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            self.state_path.write_text(json.dumps(state), encoding="utf-8")
        fixed = now

        class FixedNow(datetime):
            @classmethod
            def now(cls, tz=None):
                return fixed.astimezone(tz) if tz else fixed

        old = post.datetime
        post.datetime = FixedNow
        try:
            return post.main(["--schedule", str(self.sched_path), "--state", str(self.state_path)],
                             self.env(live, **envkw), transport=fake, sleep=lambda s: None,
                             clock=lambda: 0.0, out=lambda s: self.out.write(s + "\n"))
        finally:
            post.datetime = old

    def state(self) -> dict:
        return json.loads(self.state_path.read_text(encoding="utf-8"))


# ====================================================================== selection (pure)
class SelectionTests(unittest.TestCase):
    def sched(self, **kw):
        return post.parse_schedule(make_schedule(**kw))

    def test_waits_until_weekday_slot_in_winter_time(self):
        s = self.sched()
        # Thu 12 Nov 2026, CET (UTC+1): 12:00 Rome = 11:00 UTC
        self.assertEqual(post.choose(s, post.empty_state(), datetime(2026, 11, 12, 10, 59, tzinfo=timezone.utc),
                                     True).kind, "waiting")
        d = post.choose(s, post.empty_state(), datetime(2026, 11, 12, 11, 0, tzinfo=timezone.utc), True)
        self.assertEqual((d.kind, d.item.id), ("post", "wd-102"))

    def test_summer_time_slot_is_one_hour_earlier_in_utc(self):
        s = self.sched(start="2026-06-10")   # Wednesday, CEST (UTC+2): 12:00 Rome = 10:00 UTC
        self.assertEqual(post.choose(s, post.empty_state(), datetime(2026, 6, 10, 9, 59, tzinfo=timezone.utc),
                                     True).kind, "waiting")
        self.assertEqual(post.choose(s, post.empty_state(), datetime(2026, 6, 10, 10, 0, tzinfo=timezone.utc),
                                     True).kind, "post")

    def test_dst_change_days(self):
        # Sun 25 Oct 2026: clocks go back at 03:00 -> 10:00 Rome is 09:00 UTC (CET)
        s = self.sched(start="2026-10-25")
        self.assertEqual(post.choose(s, post.empty_state(), datetime(2026, 10, 25, 8, 59, tzinfo=timezone.utc),
                                     True).kind, "waiting")
        self.assertEqual(post.choose(s, post.empty_state(), datetime(2026, 10, 25, 9, 0, tzinfo=timezone.utc),
                                     True).kind, "post")
        # Sun 28 Mar 2027: clocks go forward at 02:00 -> 10:00 Rome is 08:00 UTC (CEST)
        s = self.sched(start="2027-03-28")
        self.assertEqual(post.choose(s, post.empty_state(), datetime(2027, 3, 28, 7, 59, tzinfo=timezone.utc),
                                     True).kind, "waiting")
        self.assertEqual(post.choose(s, post.empty_state(), datetime(2027, 3, 28, 8, 0, tzinfo=timezone.utc),
                                     True).kind, "post")

    def test_weekend_slot(self):
        s = self.sched(start="2026-11-14")   # Saturday
        self.assertEqual(s.items[0].slot_time.isoformat(), "10:00:00")
        self.assertEqual(post.choose(s, post.empty_state(), rome(2026, 11, 14, 9, 59), True).kind, "waiting")
        self.assertEqual(post.choose(s, post.empty_state(), rome(2026, 11, 14, 10, 0), True).kind, "post")
        # Monday: back to 12:00 (wd-103 dated Sun 15th is overdue, but waits for Monday's slot)
        st = {"posted": [{"id": "wd-102", "date": "2026-11-14"}]}
        self.assertEqual(post.choose(s, st, rome(2026, 11, 16, 11, 0), True).kind, "waiting")
        d = post.choose(s, st, rome(2026, 11, 16, 12, 1), True)
        self.assertEqual((d.kind, d.item.id), ("post", "wd-103"))

    def test_weekend_slot_without_slots_block_uses_item_dated_today(self):
        s = self.sched(start="2026-11-13", slots=False)       # Fri 12:00, Sat 10:00
        st = {"posted": [{"id": "wd-102", "date": "2026-11-13"}]}
        self.assertEqual(post.choose(s, st, rome(2026, 11, 14, 10, 5), True).item.id, "wd-103")
        self.assertEqual(post.choose(s, st, rome(2026, 11, 14, 10, 5), True).kind, "post")

    def test_item_dated_today_uses_its_own_slot_over_slots_block(self):
        raw = make_schedule(start="2027-03-09")                # Tue: US on DST, Italy not yet -> 17:00 Rome
        raw["slots"] = {"weekday": "18:00", "weekend": "16:00"}
        raw["items"][0]["slot_time"] = "17:00"
        s = post.parse_schedule(raw)
        self.assertEqual(post.choose(s, post.empty_state(), rome(2027, 3, 9, 16, 59), True).kind, "waiting")
        self.assertEqual(post.choose(s, post.empty_state(), rome(2027, 3, 9, 17, 0), True).kind, "post")
        # a catch-up day whose own item is already done falls back to the slots block
        st = {"posted": [{"id": "wd-103", "date": "2027-03-01"}]}
        raw["items"][0]["date"] = "2027-03-08"
        s = post.parse_schedule(raw)
        self.assertEqual(post.choose(s, st, rome(2027, 3, 10, 12, 0), True).kind, "post")

    def test_blocked_item_is_skipped_until_its_video_or_caption_changes(self):
        s = self.sched()
        fp = post.fingerprint(s.items[0])
        st = {"posted": [], "blocked": {"wd-102": {"since": "2026-11-12", **fp}}}
        self.assertEqual(post.choose(s, st, rome(2026, 11, 12, 12, 30), True).kind, "nothing_due")
        d = post.choose(s, st, rome(2026, 11, 13, 12, 30), True)          # the next video keeps its own date
        self.assertEqual((d.kind, d.item.id), ("post", "wd-103"))
        raw = make_schedule()
        raw["items"][0]["caption"] += " fixed"
        raw["items"][0]["caption_sha256"] = post.sha256_text(raw["items"][0]["caption"])
        d = post.choose(post.parse_schedule(raw), st, rome(2026, 11, 12, 12, 30), True)
        self.assertEqual(d.item.id, "wd-102")                  # re-exported with a new caption: back in

    def test_missed_days_catch_up_one_per_day(self):
        s = self.sched()
        st = {"posted": [{"id": "wd-102", "date": "2026-11-12"}]}
        # Fri 13th fully missed (outage). Sat 14th: before the 10:00 slot nothing; after it the OVERDUE wd-103.
        self.assertEqual(post.choose(s, st, rome(2026, 11, 14, 8, 30), True).kind, "waiting")
        d = post.choose(s, st, rome(2026, 11, 14, 10, 30), True)
        self.assertEqual((d.kind, d.item.id), ("post", "wd-103"))
        self.assertIn("catching up", d.note)
        st["posted"].append({"id": "wd-103", "date": "2026-11-14"})
        # Same day again: never a second Reel, even though wd-137 (dated 14th) is due.
        self.assertEqual(post.choose(s, st, rome(2026, 11, 14, 13, 0), True).kind, "posted_today")
        d = post.choose(s, st, rome(2026, 11, 15, 10, 1), True)
        self.assertEqual((d.kind, d.item.id), ("post", "wd-137"))

    def test_already_posted_ids_are_skipped_and_plan_end(self):
        s = self.sched()
        st = {"posted": [{"id": "wd-102", "date": "2026-11-01"}, {"id": "wd-103", "date": "2026-11-02"}]}
        self.assertEqual(post.choose(s, st, rome(2026, 11, 12, 12, 30), True).item.id, "wd-137")
        st["posted"] += [{"id": "wd-137", "date": "2026-11-03"}, {"id": "wd-120", "date": "2026-11-04"}]
        self.assertEqual(post.choose(s, st, rome(2026, 11, 20, 12, 30), True).kind, "finished")

    def test_nothing_due_before_first_date(self):
        d = post.choose(self.sched(), post.empty_state(), rome(2026, 11, 11, 23, 0), True)
        self.assertEqual((d.kind, d.item.id), ("nothing_due", "wd-102"))

    def test_dry_runs_count_only_in_dry_run_mode(self):
        s = self.sched()
        st = {"posted": [], "dry_runs": [{"id": "wd-102", "date": "2026-11-12"}]}
        self.assertEqual(post.choose(s, st, rome(2026, 11, 12, 13, 0), False).kind, "posted_today")
        d = post.choose(s, st, rome(2026, 11, 12, 13, 0), True)
        self.assertEqual((d.kind, d.item.id), ("post", "wd-102"))

    def test_bare_list_schedule_and_bad_input(self):
        s = post.parse_schedule(make_schedule()["items"])
        self.assertEqual((s.tz_name, len(s.items)), ("Europe/Rome", 4))
        bad = make_schedule()
        bad["items"][1]["id"] = "wd-102"
        with self.assertRaises(post.SetupError):
            post.parse_schedule(bad)
        bad = make_schedule()
        bad["items"][0]["slot_time"] = "25:00"
        with self.assertRaises(post.SetupError):
            post.parse_schedule(bad)
        bad = make_schedule()
        bad["items"][0]["asset"] = "../../etc/passwd"
        with self.assertRaises(post.SetupError):
            post.parse_schedule(bad)

    def test_caption_matching(self):
        c = caption("wd-102")
        self.assertTrue(post.same_caption(c, c.replace("\n", "\r\n") + "  "))
        self.assertFalse(post.same_caption(c, caption("wd-103")))
        self.assertFalse(post.same_caption("", c))


# ====================================================================== whole runs
class DryRunTests(Base):
    def test_dry_run_never_creates_or_publishes(self):
        fake = FakeMeta()
        code = self.run_post(rome(2026, 11, 12, 12, 5), fake)
        self.assertEqual(code, 0)
        self.assertEqual(fake.writes(), [])                          # no container, no upload, no publish
        self.assertFalse(any("rupload" in c[1] for c in fake.calls))
        self.assertTrue(any("/releases/assets/" in c[1] for c in fake.calls))   # the video was still checked
        st = self.state()
        self.assertEqual([r["id"] for r in st["dry_runs"]], ["wd-102"])
        self.assertEqual(st["posted"], [])
        self.assertIn("DRY-RUN: would post wd-102", self.out.getvalue())
        self.assertIn("would post wd-102", self.summary.read_text())
        # Second run the same day: nothing more (and no new download).
        fake2 = FakeMeta()
        self.assertEqual(self.run_post(rome(2026, 11, 12, 12, 35), fake2), 0)
        self.assertEqual(fake2.calls, [])

    def test_live_must_be_exactly_true(self):
        fake = FakeMeta()
        self.run_post(rome(2026, 11, 12, 12, 5), fake, LIVE="yes")
        self.assertEqual(fake.writes(), [])

    def test_dry_run_flag_beats_live_variable(self):
        fake = FakeMeta()
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        old = post.datetime

        class FixedNow(datetime):
            @classmethod
            def now(cls, tz=None):
                return rome(2026, 11, 12, 12, 5).astimezone(tz)
        post.datetime = FixedNow
        try:
            code = post.main(["--dry-run", "--schedule", str(self.sched_path), "--state", str(self.state_path)],
                             self.env(live=True), transport=fake, sleep=lambda s: None, out=lambda s: None)
        finally:
            post.datetime = old
        self.assertEqual(code, 0)
        self.assertEqual(fake.writes(), [])

    def test_dry_run_without_token_still_runs(self):
        fake = FakeMeta()
        code = self.run_post(rome(2026, 11, 12, 12, 5), fake, IG_TOKEN="")
        self.assertEqual(code, 0)
        self.assertFalse(any("graph.facebook.com" in c[1] for c in fake.calls))
        self.assertIn("checks are skipped", self.out.getvalue())

    def test_paused_does_nothing(self):
        fake = FakeMeta()
        self.assertEqual(self.run_post(rome(2026, 11, 12, 12, 5), fake, live=True, PAUSED="true"), 0)
        self.assertEqual(fake.calls, [])
        self.assertIn("PAUSED", self.summary.read_text())

    def test_idle_runs_do_not_rewrite_state(self):
        fake = FakeMeta()
        self.assertEqual(self.run_post(rome(2026, 11, 12, 9, 0), fake, live=True), 0)
        self.assertFalse(self.state_path.exists())                # waiting: nothing to commit
        self.assertIn("goes out after today's slot", self.out.getvalue())


class LiveTests(Base):
    def test_public_repo_lets_instagram_fetch_the_video_by_url(self):
        fake = FakeMeta()
        code = self.run_post(rome(2026, 11, 12, 12, 5), fake, live=True, VIDEO_SOURCE="")
        self.assertEqual(code, 0, self.out.getvalue())
        form = parse_qs(fake.posts_to(f"{IG}/media")[0][3].decode())
        self.assertEqual(form["video_url"][0],
                         "https://github.com/preet/reels/releases/download/videos/wd-102.mp4")
        self.assertNotIn("upload_type", form)
        self.assertFalse(any("rupload" in c[1] for c in fake.calls))
        self.assertEqual(len(fake.posts_to("/media_publish")), 1)

    def test_upload_goes_in_chunks_and_206_means_keep_going(self):
        class Chunky(FakeMeta):
            def __call__(self, method, url, headers, body, timeout, follow):
                if "rupload.facebook.com" in url:
                    self.calls.append((method, url, dict(headers), body))
                    off, size = int(headers["offset"]), int(headers["file_size"])
                    assert size == len(VIDEO) and body == VIDEO[off:off + len(body)]
                    if off + len(body) < size:
                        return post.Response(206, {"content-type": "application/json"},
                                             b'{"debug_info":{"retriable":true,"type":"PartialRequestError"}}')
                    return post.Response(200, {"content-type": "application/json"}, b'{"success":true}')
                return super().__call__(method, url, headers, body, timeout, follow)
        fake = Chunky()
        old = post.UPLOAD_CHUNK
        post.UPLOAD_CHUNK = max(1, len(VIDEO) // 3)
        try:
            code = self.run_post(rome(2026, 11, 12, 12, 5), fake, live=True)
        finally:
            post.UPLOAD_CHUNK = old
        self.assertEqual(code, 0, self.out.getvalue())
        up = [c for c in fake.calls if "rupload" in c[1]]
        self.assertGreaterEqual(len(up), 3)
        self.assertEqual(b"".join(c[3] for c in up), VIDEO)
        self.assertEqual([int(c[2]["offset"]) for c in up][0], 0)
        self.assertEqual(len(fake.posts_to("/media_publish")), 1)

    def test_live_posts_once_with_exact_caption(self):
        fake = FakeMeta()
        code = self.run_post(rome(2026, 11, 12, 12, 5), fake, live=True)
        self.assertEqual(code, 0, self.out.getvalue())
        create = fake.posts_to(f"{IG}/media")
        self.assertEqual(len(create), 1)
        form = parse_qs(create[0][3].decode())
        self.assertEqual(form["caption"][0].encode("utf-8"), caption("wd-102").encode("utf-8"))   # byte-identical
        self.assertEqual(form["media_type"][0], "REELS")
        self.assertEqual(form["upload_type"][0], "resumable")
        self.assertEqual(form["is_ai_generated"][0], "true")
        self.assertEqual(len(fake.posts_to("/media_publish")), 1)
        self.assertEqual(parse_qs(fake.posts_to("/media_publish")[0][3].decode())["creation_id"][0], "cont1")
        up = [c for c in fake.calls if "rupload" in c[1]]
        self.assertEqual((len(up), up[0][3]), (1, VIDEO))
        self.assertEqual(len(fake.posts_to("/comments")), 1)
        st = self.state()
        self.assertEqual([(r["id"], r["media_id"], r["source"], r["date"]) for r in st["posted"]],
                         [("wd-102", "media1", "robot", "2026-11-12")])
        self.assertEqual(st["posted"][0]["permalink"], "https://www.instagram.com/reel/ABC/")
        self.assertIsNone(st["pending"])
        self.assertEqual(st["first_comments"], ["media1"])
        # A second run that day posts nothing.
        fake2 = FakeMeta(media=fake.media)
        self.assertEqual(self.run_post(rome(2026, 11, 12, 12, 35), fake2, live=True), 0)
        self.assertEqual(fake2.writes(), [])

    def test_idempotent_when_already_on_instagram_today(self):
        # A crash after publishing lost state/posted.json: Instagram already shows wd-102 from today.
        fake = FakeMeta(media=[{"id": "m5", "caption": caption("wd-102").replace("\n", "\r\n"),
                                "timestamp": "2026-11-12T11:00:30+0000", "permalink": "https://www.instagram.com/reel/M5/"}])
        code = self.run_post(rome(2026, 11, 12, 12, 30), fake, live=True)
        self.assertEqual(code, 0)
        self.assertEqual(fake.writes(), [])                      # no container, nothing re-posted
        st = self.state()
        self.assertEqual([(r["id"], r["source"], r["media_id"]) for r in st["posted"]],
                         [("wd-102", "found_on_instagram", "m5")])
        self.assertIn("already on Instagram", self.out.getvalue())

    def test_found_from_earlier_day_then_posts_next(self):
        fake = FakeMeta(media=[{"id": "m4", "caption": caption("wd-102"), "timestamp": "2026-11-12T11:00:00+0000"}])
        code = self.run_post(rome(2026, 11, 13, 12, 10), fake, live=True)
        self.assertEqual(code, 0, self.out.getvalue())
        self.assertEqual(len(fake.posts_to("/media_publish")), 1)
        form = parse_qs(fake.posts_to(f"{IG}/media")[0][3].decode())
        self.assertEqual(form["caption"][0], caption("wd-103"))
        self.assertEqual([r["id"] for r in self.state()["posted"]], ["wd-102", "wd-103"])

    def test_unclear_publish_is_checked_not_repeated(self):
        fake = FakeMeta()
        fake.publish_status = 500                  # Meta errors, but the Reel actually went out
        code = self.run_post(rome(2026, 11, 12, 12, 5), fake, live=True)
        self.assertEqual(code, 0, self.out.getvalue())
        self.assertEqual(len(fake.posts_to("/media_publish")), 1)
        self.assertEqual(self.state()["posted"][0]["media_id"], "media99")

    def test_pending_from_crashed_run_is_reconciled(self):
        fake = FakeMeta(media=[{"id": "m7", "caption": caption("wd-102"), "timestamp": "2026-11-12T11:02:00+0000"}])
        fake.container_status = "PUBLISHED"
        state = dict(post.empty_state(), pending={"id": "wd-102", "container_id": "cont1",
                                                  "started_at": "2026-11-12T11:00:00+00:00"})
        code = self.run_post(rome(2026, 11, 12, 12, 40), fake, live=True, state=state)
        self.assertEqual(code, 0, self.out.getvalue())
        self.assertEqual(fake.writes(), [])
        st = self.state()
        self.assertEqual([(r["id"], r["source"]) for r in st["posted"]], [("wd-102", "robot")])
        self.assertIsNone(st["pending"])

    def test_unclear_publish_is_never_redone_while_meta_lags(self):
        fake = FakeMeta()
        state = dict(post.empty_state(), pending={"id": "wd-102", "container_id": "cont1", "publish_attempted": True,
                                                  "started_at": "2026-11-12T11:00:00+00:00"})
        fake.container_status = "FINISHED"          # Meta still says FINISHED and /media doesn't list it yet
        self.assertEqual(self.run_post(rome(2026, 11, 12, 12, 40), fake, live=True, state=state), post.EXIT_CHECK)
        self.assertEqual(fake.writes(), [])
        self.assertTrue(self.state()["pending"]["publish_attempted"])
        fake.media = [{"id": "m7", "caption": caption("wd-102"), "timestamp": "2026-11-12T11:02:00+0000"}]
        self.assertEqual(self.run_post(rome(2026, 11, 12, 13, 5), fake, live=True), post.EXIT_OK)
        self.assertEqual(fake.writes(), [])
        st = self.state()
        self.assertEqual([r["id"] for r in st["posted"]], ["wd-102"])
        self.assertIsNone(st["pending"])

    def test_unclear_publish_is_redone_only_after_three_days(self):
        fake = FakeMeta()
        state = dict(post.empty_state(), pending={"id": "wd-102", "container_id": "cont1", "publish_attempted": True,
                                                  "started_at": "2026-11-12T11:00:00+00:00"})
        self.assertEqual(self.run_post(rome(2026, 11, 15, 12, 40), fake, live=True, state=state), post.EXIT_OK)
        self.assertEqual(len(fake.posts_to("/media_publish")), 1)

    def test_clear_publish_refusal_is_redone_next_run(self):
        class Refuses(FakeMeta):
            def __call__(self, method, url, headers, body, timeout, follow):
                if url.endswith("/media_publish") and self.refuse:
                    self.calls.append((method, url, dict(headers), body))
                    return post.Response(400, {}, json.dumps({"error": {"code": 100, "message": "bad"}}).encode())
                return super().__call__(method, url, headers, body, timeout, follow)
        fake = Refuses()
        fake.refuse = True
        self.assertEqual(self.run_post(rome(2026, 11, 12, 12, 5), fake, live=True), post.EXIT_RETRY)
        self.assertFalse(self.state()["pending"]["publish_attempted"])
        fake.refuse = False
        self.assertEqual(self.run_post(rome(2026, 11, 12, 12, 25), fake, live=True), post.EXIT_OK, self.out.getvalue())
        self.assertEqual([r["id"] for r in self.state()["posted"]], ["wd-102"])

    def test_pending_that_never_published_is_redone(self):
        fake = FakeMeta()
        fake.container_status = "FINISHED"
        state = dict(post.empty_state(), pending={"id": "wd-102", "container_id": "cont0",
                                                  "started_at": "2026-11-12T11:00:00+00:00"})
        self.assertEqual(self.run_post(rome(2026, 11, 12, 12, 40), fake, live=True, state=state), 0)
        self.assertEqual(len(fake.posts_to("/media_publish")), 1)

    def test_caption_checksum_mismatch_refuses(self):
        raw = make_schedule()
        raw["items"][0]["caption"] += " (edited by hand)"
        self.write_schedule(raw)
        fake = FakeMeta()
        self.assertEqual(self.run_post(rome(2026, 11, 12, 12, 5), fake, live=True), post.EXIT_SETUP)
        self.assertEqual(fake.writes(), [])

    def test_quota_full_waits(self):
        fake = FakeMeta()
        fake.quota = (100, 100)
        self.assertEqual(self.run_post(rome(2026, 11, 12, 12, 5), fake, live=True), post.EXIT_RETRY)
        self.assertEqual(fake.writes(), [])

    def test_live_without_token_is_setup_error(self):
        fake = FakeMeta()
        self.assertEqual(self.run_post(rome(2026, 11, 12, 12, 5), fake, live=True, IG_TOKEN=""), post.EXIT_SETUP)
        self.assertEqual(fake.calls, [])

    def test_gives_up_for_the_day_after_three_failures(self):
        fake = FakeMeta()
        state = dict(post.empty_state(), failures={"wd-102": {"date": "2026-11-12", "count": 3, "last_error": "x"}})
        # the failing run already alerted; the rest of the day stays green and quiet
        self.assertEqual(self.run_post(rome(2026, 11, 12, 12, 5), fake, live=True, state=state), post.EXIT_OK)
        self.assertEqual(fake.writes(), [])
        self.assertIn("not trying again until tomorrow", self.out.getvalue())

    def test_permanent_problem_on_two_days_sets_the_video_aside(self):
        raw = make_schedule()
        raw["items"][0]["asset_size"] = len(VIDEO) + 1         # wrong video uploaded for wd-102 only
        self.write_schedule(raw)
        fake = FakeMeta()
        self.assertEqual(self.run_post(rome(2026, 11, 12, 18, 5), fake, live=True), post.EXIT_SETUP)
        self.assertEqual(self.state()["blocked"], {})
        self.assertEqual(self.run_post(rome(2026, 11, 13, 12, 5), fake, live=True), post.EXIT_SETUP)
        st = self.state()
        self.assertIn("wd-102", st["blocked"])
        self.assertIn("set aside", self.out.getvalue())
        self.assertEqual(fake.posts_to("/media_publish"), [])
        self.assertEqual(self.run_post(rome(2026, 11, 13, 12, 25), fake, live=True), post.EXIT_OK, self.out.getvalue())
        self.assertEqual([r["id"] for r in self.state()["posted"]], ["wd-103"])

    def test_token_problems_never_set_videos_aside(self):
        class Expired(FakeMeta):
            def __call__(self, method, url, headers, body, timeout, follow):
                if method == "POST" and url.endswith(f"{IG}/media"):
                    self.calls.append((method, url, dict(headers), body))
                    return post.Response(400, {}, json.dumps({"error": {"code": 190, "message": "expired"}}).encode())
                return super().__call__(method, url, headers, body, timeout, follow)
        for day in (12, 13, 16):
            self.assertEqual(self.run_post(rome(2026, 11, day, 12, 5), Expired(), live=True), post.EXIT_SETUP)
        self.assertEqual(self.state()["blocked"], {})

    def test_failure_is_counted(self):
        class Broken(FakeMeta):
            def __call__(self, method, url, headers, body, timeout, follow):
                if "rupload" in url:
                    self.calls.append((method, url, dict(headers), body))
                    return post.Response(400, {}, json.dumps({"debug_info": {"message": "bad"}}).encode())
                return super().__call__(method, url, headers, body, timeout, follow)
        fake = Broken()
        code = self.run_post(rome(2026, 11, 12, 12, 5), fake, live=True)
        self.assertEqual(code, post.EXIT_RETRY)
        st = self.state()
        self.assertEqual(st["failures"]["wd-102"]["count"], 1)
        self.assertEqual(st["posted"], [])
        self.assertEqual(fake.posts_to("/media_publish"), [])


# ====================================================================== secrets
class SecretTests(Base):
    def test_scrub_patterns(self):
        post.remember_secret(TOKEN)
        for text in (f"boom {TOKEN} end", f"https://x/?access_token={TOKEN}&a=1", f"Authorization: Bearer {TOKEN}",
                     f"OAuth {TOKEN}", "EAABwzLixnjYBOZCabcdefghijklmnop0123", f"token={GH_TOKEN}"):
            self.assertNotIn(TOKEN, post.scrub(text))
            self.assertNotIn("EAABwzLixnjYBOZC", post.scrub(text))
            self.assertNotIn(GH_TOKEN, post.scrub(text))
        self.assertEqual(post.scrub("OAuth access token expired"), "OAuth access token expired")

    def test_errors_never_carry_the_token(self):
        post.remember_secret(TOKEN)
        e = post.GraphError(f"Meta echoed {TOKEN}")
        self.assertNotIn(TOKEN, str(e))
        e = post.friendly({"message": f"Invalid OAuth access token {TOKEN}", "code": 190}, 400)
        self.assertNotIn(TOKEN, str(e))
        self.assertEqual(e.exit_code, post.EXIT_SETUP)

    def test_token_never_in_output_or_summary(self):
        class Leaky(FakeMeta):
            def __call__(self, method, url, headers, body, timeout, follow):
                if method == "POST" and url.endswith(f"{IG}/media"):
                    raise post.TransportError(f"connection reset while sending Bearer {TOKEN}")
                if method == "GET" and "content_publishing_limit" in url:
                    raise RuntimeError(f"weird {TOKEN} {GH_TOKEN}")
                return super().__call__(method, url, headers, body, timeout, follow)
        code = self.run_post(rome(2026, 11, 12, 12, 5), Leaky(), live=True)
        self.assertNotEqual(code, 0)
        for text in (self.out.getvalue(), self.summary.read_text(), self.state_path.read_text()
                     if self.state_path.exists() else ""):
            self.assertNotIn(TOKEN, text)
            self.assertNotIn(GH_TOKEN, text)

    def test_add_mask_only_inside_github_actions(self):
        fake = FakeMeta()
        self.run_post(rome(2026, 11, 12, 9, 0), fake)
        self.assertNotIn(TOKEN, self.out.getvalue())
        self.out = io.StringIO()
        self.run_post(rome(2026, 11, 12, 9, 0), fake, GITHUB_ACTIONS="true")
        lines = [ln for ln in self.out.getvalue().splitlines() if TOKEN in ln]
        self.assertEqual(lines, [f"::add-mask::{TOKEN}"])

    def test_authorization_is_never_forwarded_on_redirect(self):
        seen = {}

        class Opener:
            def open(self, req, timeout):
                seen["unredirected"] = dict(req.unredirected_hdrs)
                seen["redirected"] = dict(req.headers)
                raise urllib.error.URLError("offline test")
        import urllib.error
        old = post._OPEN_FOLLOW
        post._OPEN_FOLLOW = Opener()
        try:
            with self.assertRaises(post.TransportError):
                post.urllib_transport("GET", "https://api.github.com/x", {"Authorization": f"Bearer {GH_TOKEN}",
                                                                          "Accept": "a/b"}, None, 5, True)
        finally:
            post._OPEN_FOLLOW = old
        self.assertIn("Authorization", seen["unredirected"])
        self.assertNotIn("Authorization", seen["redirected"])


# ====================================================================== workflow file
class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.text = (HERE / ".github" / "workflows" / "post.yml").read_text(encoding="utf-8")

    def test_schedule_concurrency_and_permissions(self):
        self.assertIn('cron: "5,25,45 14-21 * * *"', self.text)
        self.assertIn("workflow_dispatch:", self.text)
        self.assertIn("group: post-reel", self.text)
        self.assertIn("cancel-in-progress: false", self.text)
        self.assertIn("permissions:\n  contents: write", self.text)

    def test_no_third_party_actions_and_secret_only_in_env(self):
        self.assertNotIn("uses:", self.text)
        self.assertEqual(self.text.count("secrets."), 1)
        self.assertIn("IG_TOKEN: ${{ secrets.IG_TOKEN }}", self.text)
        self.assertNotIn("inputs:", self.text)
        self.assertNotIn("upload-artifact", self.text)

    def test_cron_window_gives_many_tries_after_every_slot_all_year(self):
        runs = [h * 60 + m for h in range(14, 22) for m in (5, 25, 45)]
        self.assertEqual((runs[0], runs[-1]), (post.CRON_WINDOW_UTC[0].hour * 60 + post.CRON_WINDOW_UTC[0].minute,
                                               post.CRON_WINDOW_UTC[1].hour * 60 + post.CRON_WINDOW_UTC[1].minute))
        et = post.ZoneInfo("America/New_York")
        for y, mo, d in ((2026, 1, 15), (2026, 7, 15), (2026, 3, 20), (2026, 3, 29), (2026, 10, 25),
                         (2026, 10, 30), (2026, 11, 7), (2026, 11, 12), (2026, 12, 31)):
            for hh in (10, 12):           # the Mac's US-Eastern slots (= 16:00 / 18:00 Rome)
                for when in (datetime(y, mo, d, hh, tzinfo=et),):
                    utc = when.astimezone(timezone.utc)
                    slot = utc.hour * 60 + utc.minute
                    after = [r for r in runs if r >= slot]
                    self.assertTrue(after and after[0] - slot <= 25, when)       # first try soon after the slot
                    self.assertGreaterEqual(len(after), 15, when)              # and plenty of retries


if __name__ == "__main__":
    unittest.main()
