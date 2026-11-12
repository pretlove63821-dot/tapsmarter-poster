"""Offline tests for the 2-a-day slots, stats (state/stats.json) and trend order (state/trends.json,
state/order.json). No network: every HTTP call goes to a fake. Run:
    cd cloud-poster && python3 -m unittest discover -s tests -q
"""
from __future__ import annotations

import io
import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
import unittest

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "tests"))

import post  # noqa: E402
from test_post import GH_TOKEN, IG, TOKEN, VIDEO, Base, FakeMeta, caption  # noqa: E402

ROME = post.ZoneInfo("Europe/Rome")
ET = post.ZoneInfo("America/New_York")
UTC = timezone.utc
TIMES = {"weekday": ["12:00", "19:00"], "weekend": ["10:00", "18:00"]}

TITLES = {  # first caption line per video (what trend scoring reads, with the keyword)
    "wd-103": ("Plan a week of dinners from a fridge photo", "DINNER"),
    "wd-137": ("How to set an 80% charge limit on iPhone (help your battery last)", "BATTERY"),
    "wd-120": ("ChatGPT prompt to make a simple monthly budget", "BUDGET"),
    "wd-143": ("Spot a fake delivery text scam in 5 seconds", "SCAM"),
    "wd-109": ("Turn on Stolen Device Protection on iPhone", "STOLEN"),
    "wd-108": ("Android: find your lost phone even when it is off", "FIND"),
}


def cap(vid: str) -> str:
    if vid in TITLES:
        return TITLES[vid][0] + "\n" + caption(vid).split("\n", 1)[1]
    return caption(vid)


def et_moments(start: date, n: int) -> list[datetime]:
    """Like the Mac's slot_moments: every ET slot, 3 h apart, max 2 per Rome day."""
    out, per_day, d = [], {}, start
    while len(out) < n:
        for t in TIMES["weekend" if d.weekday() >= 5 else "weekday"]:
            s = datetime.combine(d, datetime.strptime(t, "%H:%M").time(), tzinfo=ET)
            rd = s.astimezone(ROME).date()
            if out and s - out[-1] < timedelta(hours=3) or per_day.get(rd, 0) >= 2:
                continue
            out.append(s)
            per_day[rd] = per_day.get(rd, 0) + 1
        d += timedelta(days=1)
    return out[:n]


def two_a_day(ids=("wd-103", "wd-137", "wd-120", "wd-143", "wd-109", "wd-108"), start="2026-11-13",
              calendar=False) -> dict:
    moments = et_moments(date.fromisoformat(start), len(ids))
    items = []
    for vid, m in zip(ids, moments):
        r = m.astimezone(ROME)
        c = cap(vid)
        items.append({"id": vid, "date": r.date().isoformat(), "slot_time": f"{r:%H:%M}",
                      "slot_at": m.astimezone(UTC).isoformat(), "caption": c,
                      "caption_sha256": post.sha256_text(c), "asset": f"{vid}.mp4", "asset_size": len(VIDEO),
                      "keyword": TITLES.get(vid, ("", vid.upper()))[1]})
    raw = {"version": 1, "timezone": "Europe/Rome", "slots": {"weekday": "18:00", "weekend": "16:00"},
           "slot_timezone": "America/New_York", "slot_times": TIMES, "items": items}
    if calendar:
        raw["slot_calendar"] = [m.astimezone(UTC).isoformat() for m in et_moments(date.fromisoformat(start), 30)]
    return raw


def et(y, m, d, hh, mm=0) -> datetime:
    return datetime(y, m, d, hh, mm, tzinfo=ET)


def row(vid, when: datetime, media_id=None) -> dict:
    return {"id": vid, "date": when.astimezone(ROME).date().isoformat(),
            "posted_at": when.astimezone(UTC).isoformat(timespec="seconds"), "media_id": media_id,
            "source": "robot"}


# ====================================================================== 2 slots a day (pure)
class TwoSlotTests(unittest.TestCase):
    def sched(self, **kw):
        return post.parse_schedule(two_a_day(**kw))

    def test_parse_lists_and_slot_timezone(self):
        s = self.sched()
        self.assertEqual(s.slot_tz.key, "America/New_York")
        self.assertEqual([t.isoformat("minutes") for t in s.slot_times["weekday"]], ["12:00", "19:00"])
        self.assertEqual(s.items[0].slot_at, et(2026, 11, 13, 12).astimezone(UTC))
        legacy = post.parse_schedule({"version": 1, "slots": {"weekday": ["19:00", "12:00"], "weekend": "10:00"},
                                      "items": two_a_day()["items"]})
        self.assertEqual([t.hour for t in legacy.slots["weekday"]], [12, 19])
        with self.assertRaises(post.SetupError):
            post.parse_schedule({**two_a_day(), "slot_times": {"weekday": ["12:00"]}})
        with self.assertRaises(post.SetupError):
            post.parse_schedule({**two_a_day(), "slot_timezone": "Mars/Base"})

    def test_weekday_noon_and_evening_slots_winter(self):
        s = self.sched()
        st = post.empty_state()
        self.assertEqual(post.choose(s, st, et(2026, 11, 13, 11, 59), True).kind, "waiting")
        d = post.choose(s, st, et(2026, 11, 13, 12, 5), True)          # Fri 12:05 ET = 18:05 Rome
        self.assertEqual((d.kind, d.item.id, d.slot), ("post", "wd-103", et(2026, 11, 13, 12).astimezone(UTC)))
        st["posted"].append(row("wd-103", et(2026, 11, 13, 12, 6)))
        self.assertEqual(post.choose(s, st, et(2026, 11, 13, 12, 45), True).kind, "posted_today")
        self.assertEqual(post.choose(s, st, et(2026, 11, 13, 18, 59), True).kind, "waiting")
        d = post.choose(s, st, et(2026, 11, 13, 19, 5), True)          # 19:05 ET = 01:05 Rome Saturday
        self.assertEqual((d.kind, d.item.id), ("post", "wd-137"))

    def test_weekend_slots_10_and_18(self):
        s = self.sched()
        st = {"posted": [row("wd-103", et(2026, 11, 13, 12, 6)), row("wd-137", et(2026, 11, 13, 19, 6))]}
        self.assertEqual(post.choose(s, st, et(2026, 11, 14, 9, 59), True).kind, "waiting")
        d = post.choose(s, st, et(2026, 11, 14, 10, 5), True)
        self.assertEqual((d.kind, d.item.id), ("post", "wd-120"))
        st["posted"].append(row("wd-120", et(2026, 11, 14, 10, 6)))
        self.assertEqual(post.choose(s, st, et(2026, 11, 14, 17, 59), True).kind, "waiting")
        d = post.choose(s, st, et(2026, 11, 14, 18, 5), True)
        self.assertEqual((d.kind, d.item.id), ("post", "wd-143"))

    def test_dst_both_sides_and_gap_weeks(self):
        cases = [  # (ET date, ET slot hour, UTC hour)  US DST ends Sun 1 Nov 2026, starts Sun 14 Mar 2027
            (date(2026, 10, 30), 19, 23), (date(2026, 11, 2), 12, 17), (date(2026, 11, 2), 19, 0),
            (date(2026, 10, 31), 18, 22), (date(2026, 11, 1), 10, 15), (date(2027, 3, 13), 18, 23),
            (date(2027, 3, 15), 12, 16), (date(2027, 3, 15), 19, 23),
        ]
        for d, hh, utc_h in cases:
            s = post.parse_schedule(two_a_day(start=(d - timedelta(days=2)).isoformat(), ids=tuple(
                f"wd-{n}" for n in range(200, 212))))
            slots = post.slots_between(s, datetime.combine(d, datetime.min.time(), tzinfo=ET),
                                       datetime.combine(d, datetime.max.time(), tzinfo=ET))
            self.assertIn(datetime.combine(d, datetime.min.time(), tzinfo=ET).replace(hour=hh).astimezone(UTC),
                          slots, (d, hh))
            match = [x for x in slots if x.astimezone(ET).hour == hh]
            self.assertEqual(match[0].hour, utc_h if utc_h else 0, (d, hh))

    def test_at_least_three_hours_between_reels(self):
        s = self.sched()
        # wd-103 went out late (14:30 ET, e.g. a long Meta outage); the 19:00 slot waits until 17:30... it already
        # can: 19:00 - 14:30 = 4.5 h. But a manual Reel at 17:00 ET makes it wait until 20:00.
        st = {"posted": [row("wd-103", et(2026, 11, 13, 14, 30))]}
        self.assertEqual(post.choose(s, st, et(2026, 11, 13, 19, 5), True).kind, "post")
        extra = [et(2026, 11, 13, 17, 0)]
        d = post.choose(s, st, et(2026, 11, 13, 19, 5), True, extra_posts=extra)
        self.assertEqual(d.kind, "waiting")
        self.assertEqual(d.slot, et(2026, 11, 13, 20, 0).astimezone(UTC))
        self.assertIn("3 hours", d.note)
        self.assertEqual(post.choose(s, st, et(2026, 11, 13, 20, 5), True, extra_posts=extra).kind, "post")

    def test_never_two_in_one_slot_even_with_lost_state(self):
        s = self.sched()
        st = post.empty_state()
        # our records lost the 12:06 post, but Instagram shows a Reel at 12:06 -> the slot is taken
        d = post.choose(s, st, et(2026, 11, 13, 13, 0), True, extra_posts=[et(2026, 11, 13, 12, 6)])
        self.assertEqual(d.kind, "posted_today")

    def test_never_more_than_two_per_rome_day(self):
        s = self.sched()
        rome_day = [datetime(2026, 11, 14, 1, 10, tzinfo=ROME), datetime(2026, 11, 14, 11, 0, tzinfo=ROME)]
        st = {"posted": [row("wd-103", rome_day[0]), row("wd-137", rome_day[1])]}
        d = post.choose(s, st, et(2026, 11, 14, 10, 5), True)     # 16:05 Rome, third Reel of that Rome day
        self.assertEqual(d.kind, "posted_today")
        self.assertIn("at most 2 a day", d.note)

    def test_catch_up_never_bursts(self):
        s = self.sched()
        st = {"posted": [row("wd-103", et(2026, 11, 13, 12, 6))]}
        # robot down from Fri 19:00 through Sat 10:00 (2 slots missed); it comes back Sat 13:30 ET:
        # the 10:00 slot's window is over (4 h) -> nothing now, ONE Reel at 18:00, the queue simply shifts.
        d = post.choose(s, st, et(2026, 11, 14, 14, 30), True)
        self.assertIn(d.kind, ("waiting", "nothing_due"))
        self.assertEqual(d.slot, et(2026, 11, 14, 18).astimezone(UTC))
        d = post.choose(s, st, et(2026, 11, 14, 18, 5), True)
        self.assertEqual((d.kind, d.item.id), ("post", "wd-137"))       # next in line, not the 3 overdue at once
        self.assertIn("catching up", d.note)
        st["posted"].append(row("wd-137", et(2026, 11, 14, 18, 6)))
        for minute in (25, 45, 65, 125):
            self.assertEqual(post.choose(s, st, et(2026, 11, 14, 18) + timedelta(minutes=minute), True).kind,
                             "posted_today")
        d = post.choose(s, st, et(2026, 11, 15, 10, 5), True)
        self.assertEqual((d.kind, d.item.id), ("post", "wd-120"))

    def test_late_inside_window_still_posts(self):
        s = self.sched()
        d = post.choose(s, post.empty_state(), et(2026, 11, 13, 15, 55), True)   # 3 h 55 after the slot
        self.assertEqual(d.kind, "post")
        d = post.choose(s, post.empty_state(), et(2026, 11, 13, 16, 1), True)    # window over: wait for 19:00
        self.assertEqual((d.kind, d.slot), ("waiting", et(2026, 11, 13, 19).astimezone(UTC)))

    def test_todays_second_slot_is_skipped_before_the_plan_starts(self):
        s = self.sched()                                  # plan starts Fri 13 Nov at 12:00 ET
        st = {"posted": [row("wd-102", datetime(2026, 11, 12, 15, 22, tzinfo=UTC))]}
        d = post.choose(s, st, et(2026, 11, 12, 19, 5), True)
        self.assertEqual(d.kind, "nothing_due")
        self.assertEqual(d.slot, et(2026, 11, 13, 12).astimezone(UTC))

    def test_slot_calendar_is_used(self):
        raw = two_a_day(calendar=True)
        raw.pop("slot_times")
        raw.pop("slot_timezone")
        s = post.parse_schedule(raw)
        self.assertEqual(len(s.slot_calendar), 30)
        d = post.choose(s, post.empty_state(), et(2026, 11, 13, 19, 10), True)
        self.assertEqual(d.kind, "post")
        self.assertEqual(d.slot, et(2026, 11, 13, 19).astimezone(UTC))

    def test_every_slot_has_runs_within_its_window(self):
        runs = post.cron_minutes()
        s = self.sched()
        for sl in post.slots_between(s, et(2026, 11, 13, 0), et(2027, 11, 13, 0)):
            m = sl.hour * 60 + sl.minute
            self.assertTrue(any((r - m) % 1440 < 30 for r in runs), sl)


# ====================================================================== trends (pure)
def trends_doc(now: datetime, titles: list[dict], ok=True) -> dict:
    return {"version": 1, "ok": ok, "updated_at": now.astimezone(UTC).isoformat(), "titles": titles}


T_BATTERY = [{"t": "iPhone 17 battery drain after update - The Verge", "src": "news", "q": "iPhone"},
             {"t": "Why your phone battery dies fast in winter", "src": "news", "q": "AI"},
             {"t": "charging limit", "src": "trends"}]


class TrendTests(unittest.TestCase):
    def setUp(self):
        self.s = post.parse_schedule(two_a_day())
        self.now = et(2026, 11, 13, 9, 0)

    def test_tokenise_and_synonyms(self):
        self.assertEqual(post.canon_tokens("ChatGPT & the new iOS 26 update: scams!"), ["ai", "iphone", "update", "scam"])
        self.assertIn("battery", post.canon_tokens("Charging"))

    def test_search_term_words_do_not_count_in_their_own_results(self):
        idx = post.trend_index([{"t": "Battery recall news", "src": "news", "q": "battery"}])
        self.assertNotIn("battery", idx)
        self.assertEqual(idx["recall"], {"trends": 0, "news": 1})

    def test_scoring_is_explainable_and_reorders_window(self):
        order, scores = post.compute_order(self.s, post.empty_state(), True, trends_doc(self.now, T_BATTERY), None,
                                           self.now)
        self.assertEqual(order["status"], "trends")
        self.assertEqual(order["order"][0], "wd-137")                       # the battery video moves up
        self.assertGreater(scores["wd-137"]["score"], 0)
        self.assertTrue(any(m.startswith("battery") for m in scores["wd-137"]["matched"]))
        self.assertIn("trend score", order["reasons"]["wd-137"])
        # ties keep plan order
        rest = [v for v in order["order"] if scores[v]["score"] == 0]
        self.assertEqual(rest, [v for v in [it.id for it in self.s.items] if v in rest])

    def test_deterministic(self):
        a = post.compute_order(self.s, post.empty_state(), True, trends_doc(self.now, T_BATTERY), None, self.now)
        b = post.compute_order(self.s, post.empty_state(), True, trends_doc(self.now, list(T_BATTERY)), None,
                               self.now)
        self.assertEqual(json.dumps(a, sort_keys=True), json.dumps(b, sort_keys=True))

    def test_fallback_to_plan_order(self):
        plan = [it.id for it in self.s.items]
        for tr in (None, trends_doc(self.now, T_BATTERY, ok=False),
                   trends_doc(self.now - timedelta(hours=40), T_BATTERY), trends_doc(self.now, [])):
            order, _ = post.compute_order(self.s, post.empty_state(), True, tr, None, self.now)
            self.assertEqual((order["status"], order["order"]), ("plan_order", plan))
            d = post.choose(self.s, post.empty_state(), et(2026, 11, 13, 12, 5), True, order)
            self.assertEqual(d.item.id, "wd-103")

    def test_next_slot_takes_the_best_and_never_posted_or_blocked(self):
        order, _ = post.compute_order(self.s, post.empty_state(), True, trends_doc(self.now, T_BATTERY), None,
                                      self.now)
        d = post.choose(self.s, post.empty_state(), et(2026, 11, 13, 12, 5), True, order)
        self.assertEqual((d.kind, d.item.id), ("post", "wd-137"))
        self.assertIn("trend", d.reason)
        # once wd-137 is posted (even with the old order file), it is never picked again
        st = {"posted": [row("wd-137", et(2026, 11, 13, 12, 6))]}
        d = post.choose(self.s, st, et(2026, 11, 13, 19, 5), True, order)
        self.assertNotEqual(d.item.id, "wd-137")
        # blocked videos are never picked, whatever their score
        blocked = {"wd-137": {"since": "2026-11-13", **post.fingerprint(self.s.items[1])}}
        order2, _ = post.compute_order(self.s, {"posted": [], "blocked": blocked}, True,
                                       trends_doc(self.now, T_BATTERY), None, self.now)
        self.assertNotIn("wd-137", order2["order"])
        d = post.choose(self.s, {"posted": [], "blocked": blocked}, et(2026, 11, 13, 12, 5), True, order)
        self.assertNotEqual(d.item.id, "wd-137")

    def test_video_out_of_tries_is_skipped_in_trend_order(self):
        order, _ = post.compute_order(self.s, post.empty_state(), True, trends_doc(self.now, T_BATTERY), None,
                                      self.now)
        d = post.choose(self.s, post.empty_state(), et(2026, 11, 13, 12, 5), True, order, exclude={"wd-137"})
        self.assertEqual(d.kind, "post")
        self.assertNotEqual(d.item.id, "wd-137")

    def test_order_for_another_schedule_is_ignored(self):
        order, _ = post.compute_order(self.s, post.empty_state(), True, trends_doc(self.now, T_BATTERY), None,
                                      self.now)
        other = post.parse_schedule(two_a_day(ids=("wd-137", "wd-103", "wd-120")))
        self.assertIsNone(post.ranked_items(other, other.items, order))

    def test_window_is_bounded(self):
        ids = tuple(f"wd-{n}" for n in range(300, 320)) + ("wd-137",)
        s = post.parse_schedule(two_a_day(ids=ids))
        order, _ = post.compute_order(s, post.empty_state(), True, trends_doc(self.now, T_BATTERY), None, self.now)
        self.assertEqual(order["order"][-1], "wd-137")       # 21st in the plan: outside the 14-video window

    def test_performance_topics_boost_similar_videos(self):
        media = {f"m{n}": {"video_id": f"x{n}", "views": v, "shares": 0, "keyword": kw, "caption": c}
                 for n, (v, kw, c) in enumerate([(9000, "SCAM", "Spot a fake text scam"), (100, "DINNER", "Dinner"),
                                                 (120, "BUDGET", "Budget"), (90, "FIND", "Find phone")])}
        topics, names = post.performance_topics({"media": media})
        self.assertIn("scam", topics)
        self.assertEqual(names, ["x0"])
        order, scores = post.compute_order(self.s, post.empty_state(), True, trends_doc(self.now, [
            {"t": "unrelated football final", "src": "trends"}]), {"media": media}, self.now)
        self.assertEqual(order["order"][0], "wd-143")
        self.assertEqual(scores["wd-143"]["performance"], 4)       # scam + text, capped at 4
        self.assertEqual(post.performance_topics({"media": dict(list(media.items())[:3])}), (set(), []))

    def test_upcoming_projection(self):
        order, _ = post.compute_order(self.s, post.empty_state(), True, trends_doc(self.now, T_BATTERY), None,
                                      self.now)
        up = order["upcoming"]
        self.assertEqual(len(up), 6)
        self.assertEqual(up[0]["id"], "wd-137")
        self.assertEqual(up[0]["slot"], et(2026, 11, 13, 12).astimezone(UTC).isoformat())
        self.assertEqual(len({u["id"] for u in up}), 6)

    def test_rss_parsing_and_safety(self):
        rss = (b'<?xml version="1.0"?><rss xmlns:ht="https://trends.google.com/trending/rss"><channel>'
               b'<title>Daily</title><item><title>iphone 17</title><ht:news_item><ht:news_item_title>'
               b'Apple &amp; battery</ht:news_item_title></ht:news_item></item><item><title>lakers</title></item>'
               b'</channel></rss>')
        self.assertEqual(post.parse_rss_titles(rss, 10), ["iphone 17", "Apple & battery", "lakers"])
        bomb = b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]><rss><channel><item><title>&a;</title>' \
               b'</item></channel></rss>'
        with self.assertRaises(ValueError):
            post.parse_rss_titles(bomb, 10)
        with self.assertRaises(ValueError):
            post.parse_rss_titles(b"x" * (post.RSS_MAX_BYTES + 1), 10)
        # a DTD hidden in UTF-16 (the byte scan cannot see it) is refused by the parser itself
        utf16 = ('<?xml version="1.0" encoding="UTF-16"?><!DOCTYPE r [<!ENTITY a "aaaaaaaaaa">'
                 '<!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;&a;&a;">]><rss><channel><item><title>&b;</title></item>'
                 '</channel></rss>').encode("utf-16")
        with self.assertRaises(ValueError):
            post.parse_rss_titles(utf16, 10)
        ok16 = '<?xml version="1.0" encoding="UTF-16"?><rss><channel><item><title>hi</title></item></channel></rss>'
        self.assertEqual(post.parse_rss_titles(ok16.encode("utf-16"), 10), ["hi"])

    def test_trend_feeds_have_a_total_deadline(self):
        import tempfile
        tick = [0.0]

        def clock():
            return tick[0]

        def slow(method, url, headers, body, timeout, follow):
            tick[0] += 40                              # every feed takes 40 s
            rss = b'<rss><channel><item><title>iphone</title></item></channel></rss>'
            return post.Response(200, {}, rss)

        tmp = Path(tempfile.mkdtemp())
        env = post.read_env({"TRENDS": "on"}, True)
        p = post.Poster(env, self.s, post.empty_state(), tmp / "posted.json", self.now, transport=slow,
                        clock=clock, out=lambda m: None)
        got = p.fetch_trends()
        self.assertTrue(got["ok"])
        fetched = [x for x in got["sources"] if x["ok"]]
        self.assertEqual(len(fetched), 3)              # 0, 40, 80 s started; the rest skipped after 90 s
        self.assertTrue(all("skipped" in x["error"] for x in got["sources"] if not x["ok"]))

    def test_transport_reads_at_most_max_bytes(self):
        class R:
            status, headers = 200, {}

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self, n=-1):
                self.asked = n
                return b"x" * (10 if n < 0 else min(n, 10))

        r = R()

        class Opener:
            def open(self, req, timeout=None):
                return r

        old = post._OPEN_FOLLOW
        post._OPEN_FOLLOW = Opener()
        try:
            resp = post.urllib_transport("GET", "https://example.test/x", {}, None, 5, True, max_bytes=4)
        finally:
            post._OPEN_FOLLOW = old
        self.assertEqual((r.asked, resp.body), (5, b"xxxxx"))
        with self.assertRaises(ValueError):
            post.parse_rss_titles(b"<rss>" + b"x" * post.RSS_MAX_BYTES, 10)

    def test_long_skipped_video_is_pinned_first(self):
        ids = ("wd-120",) + tuple(f"wd-{n}" for n in range(300, 306)) + ("wd-137",)
        s = post.parse_schedule(two_a_day(ids=ids))
        order, _ = post.compute_order(s, post.empty_state(), True, trends_doc(self.now, T_BATTERY), None, self.now)
        self.assertEqual(order["order"][0], "wd-137")
        # 4 videos planned after wd-120 already went out: it is not passed over again, whatever the scores
        st = {"posted": [row(v, et(2026, 11, 13, 12) + timedelta(hours=5 * n)) for n, v in
                         enumerate(("wd-300", "wd-301", "wd-302", "wd-303"))]}
        order, scores = post.compute_order(s, st, True, trends_doc(self.now, T_BATTERY), None, self.now)
        self.assertEqual(order["order"][:2], ["wd-120", "wd-137"])
        self.assertIn("waited long enough", order["reasons"]["wd-120"])


# ====================================================================== whole runs with stats + trends
class GrowthMeta(FakeMeta):
    """FakeMeta + account fields, media insights and the public RSS feeds."""

    def __init__(self, media=None, insights="ok", rss_ok=True):
        super().__init__(media)
        self.insights = insights        # ok | perm | partial | none
        self.rss_ok = rss_ok
        self.followers = 42

    def __call__(self, method, url, headers, body, timeout, follow):
        parts = urlsplit(url)
        js = lambda obj, status=200: post.Response(status, {"content-type": "application/json"},
                                                   json.dumps(obj).encode())
        if parts.hostname in ("trends.google.com", "news.google.com"):
            self.calls.append((method, url, dict(headers), body))
            assert "Authorization" not in headers
            if not self.rss_ok:
                return post.Response(503, {}, b"down")
            if parts.hostname == "trends.google.com":
                return post.Response(200, {}, b"<rss><channel><item><title>iphone battery limit</title></item>"
                                              b"</channel></rss>")
            return post.Response(200, {}, b"<rss><channel><item><title>Charging habits that kill batteries - X"
                                          b"</title></item></channel></rss>")
        if parts.hostname == "graph.facebook.com":
            tail = parts.path.split("/v27.0/", 1)[1]
            q = {k: v[0] for k, v in parse_qs(parts.query).items()}
            if method == "GET" and tail == IG:
                self.calls.append((method, url, dict(headers), body))
                return js({"followers_count": self.followers, "media_count": len(self.media), "id": IG})
            if method == "GET" and tail.endswith("/insights"):
                self.calls.append((method, url, dict(headers), body))
                metrics = q["metric"].split(",")
                if self.insights == "perm":
                    return js({"error": {"code": 10, "message": "(#10) Application does not have permission"}}, 400)
                if self.insights == "partial" and "ig_reels_video_view_total_time" in metrics:
                    return js({"error": {"code": 100, "message": "metric not supported"}}, 400)
                vals = {"views": 1500, "reach": 900, "saved": 12, "shares": 7, "total_interactions": 80,
                        "ig_reels_avg_watch_time": 4100, "ig_reels_video_view_total_time": 99000}
                return js({"data": [{"name": m, "period": "lifetime", "values": [{"value": vals[m]}]}
                                    for m in metrics]})
        return super().__call__(method, url, headers, body, timeout, follow)


class GrowthRunTests(Base):
    def setUp(self):
        super().setUp()
        self.write_schedule(two_a_day())

    def env(self, live=False, **kw):
        e = super().env(live, **kw)
        e.update({"STATS": kw.get("STATS", "on"), "TRENDS": kw.get("TRENDS", "on")})
        return e

    def read(self, name):
        p = self.state_path.parent / name
        return json.loads(p.read_text()) if p.exists() else None

    def media(self):
        return [{"id": "m1", "caption": cap("wd-102"), "timestamp": "2026-11-12T15:22:48+0000",
                 "permalink": "https://www.instagram.com/reel/A/", "media_product_type": "REELS",
                 "like_count": 5, "comments_count": 2}]

    def test_stats_ok_and_trend_order_used_by_the_next_slot(self):
        fake = GrowthMeta(media=self.media())
        state = dict(post.empty_state(), posted=[row("wd-102", datetime(2026, 11, 12, 15, 22, tzinfo=UTC), "m1")])
        self.assertEqual(self.run_post(et(2026, 11, 13, 9, 30), fake, state=state), 0, self.out.getvalue())
        st = self.read("stats.json")
        self.assertEqual(st["permission"], "ok")
        self.assertEqual(st["account"]["latest"]["followers_count"], 42)
        self.assertEqual((st["media"]["m1"]["views"], st["media"]["m1"]["video_id"]), (1500, "wd-102"))
        self.assertEqual(st["daily"]["m1"][0]["views"], 1500)
        order = self.read("order.json")
        self.assertEqual((order["status"], order["order"][0]), ("trends", "wd-137"))
        self.assertIn("battery", self.read("trends.json")["scores"]["wd-137"]["reason"])
        # 12:05 ET: the slot takes the trend pick (dry run here), with the reason recorded
        fake2 = GrowthMeta(media=self.media())
        self.assertEqual(self.run_post(et(2026, 11, 13, 12, 5), fake2), 0, self.out.getvalue())
        self.assertEqual(self.state()["dry_runs"][-1]["id"], "wd-137")
        self.assertIn("would post wd-137", self.out.getvalue())
        self.assertFalse(any("insights" in c[1] for c in fake2.calls))   # stats: not again within 3 hours
        self.assertFalse(any("google.com" in c[1] for c in fake2.calls))  # trends: once a day
        for name in ("stats.json", "trends.json", "order.json", "posted.json"):
            text = (self.state_path.parent / name).read_text()
            self.assertNotIn(TOKEN, text)
            self.assertNotIn(GH_TOKEN, text)

    def test_live_post_records_slot_and_pick(self):
        fake = GrowthMeta()
        self.assertEqual(self.run_post(et(2026, 11, 13, 12, 5), fake, live=True), 0, self.out.getvalue())
        r = self.state()["posted"][-1]
        self.assertEqual(r["slot"], et(2026, 11, 13, 12).astimezone(UTC).isoformat())
        self.assertEqual(len(fake.posts_to("/media_publish")), 1)
        # the same slot never gets a second Reel
        fake2 = GrowthMeta(media=fake.media)
        self.assertEqual(self.run_post(et(2026, 11, 13, 12, 25), fake2, live=True), 0)
        self.assertEqual(fake2.writes(), [])

    def test_hand_posted_reel_in_the_slot_blocks_the_robot(self):
        fake = GrowthMeta(media=[{"id": "h1", "caption": "something posted by hand",
                                  "timestamp": et(2026, 11, 13, 12, 1).astimezone(UTC).isoformat()}])
        self.assertEqual(self.run_post(et(2026, 11, 13, 12, 25), fake, live=True), 0, self.out.getvalue())
        self.assertEqual(fake.writes(), [])
        self.assertIn("already went out", self.out.getvalue())

    def test_insights_permission_missing_is_graceful(self):
        fake = GrowthMeta(media=self.media(), insights="perm")
        self.assertEqual(self.run_post(et(2026, 11, 13, 12, 5), fake, live=True), 0, self.out.getvalue())
        self.assertEqual(len(fake.posts_to("/media_publish")), 1)            # posting not blocked
        st = self.read("stats.json")
        self.assertEqual((st["permission"], st["note"]), (post.NEEDS_PERMISSION, post.PERMISSION_NOTE))
        self.assertEqual(st["account"]["latest"]["followers_count"], 42)     # account fields still recorded
        self.assertEqual(st["media"]["m1"]["like_count"], 5)
        self.assertEqual(st["media"]["m1"]["error"], post.NEEDS_PERMISSION)
        self.assertIn(post.PERMISSION_NOTE, self.out.getvalue())
        self.assertEqual(sum("insights" in c[1] for c in fake.calls), 1)    # learned once, not per Reel

    def test_unsupported_metric_is_tolerated(self):
        fake = GrowthMeta(media=self.media(), insights="partial")
        self.run_post(et(2026, 11, 13, 9, 5), fake)
        st = self.read("stats.json")
        self.assertEqual(st["unsupported_metrics"], ["ig_reels_video_view_total_time"])
        self.assertEqual(st["media"]["m1"]["views"], 1500)
        self.assertEqual(st["permission"], "ok")

    def test_stats_crash_or_trend_failure_never_changes_the_outcome(self):
        class Boom(GrowthMeta):
            def __call__(self, method, url, headers, body, timeout, follow):
                if url.split("?")[0].endswith(f"/v27.0/{IG}"):
                    raise RuntimeError("stats exploded")
                return super().__call__(method, url, headers, body, timeout, follow)
        fake = Boom(rss_ok=False)
        self.assertEqual(self.run_post(et(2026, 11, 13, 12, 5), fake, live=True), 0, self.out.getvalue())
        self.assertEqual(len(fake.posts_to("/media_publish")), 1)
        self.assertIn("Stats skipped", self.out.getvalue())
        tr = self.read("trends.json")
        self.assertFalse(tr["ok"])
        self.assertEqual(self.read("order.json")["status"], "plan_order")

    def test_broken_state_files_are_tolerated(self):
        d = self.state_path.parent
        d.mkdir(parents=True, exist_ok=True)
        for name in ("stats.json", "trends.json", "order.json"):
            (d / name).write_text("{broken", encoding="utf-8")
        self.assertEqual(self.run_post(et(2026, 11, 13, 12, 5), GrowthMeta(), live=True), 0, self.out.getvalue())
        self.assertEqual(self.read("stats.json")["permission"], "ok")

    def test_trends_off_means_plan_order(self):
        d = self.state_path.parent
        d.mkdir(parents=True, exist_ok=True)
        s = post.parse_schedule(two_a_day())
        order, _ = post.compute_order(s, post.empty_state(), True, trends_doc(et(2026, 11, 13, 9), T_BATTERY),
                                      None, et(2026, 11, 13, 9))
        (d / "order.json").write_text(json.dumps(order))
        fake = GrowthMeta()
        self.run_post(et(2026, 11, 13, 12, 5), fake, TRENDS="off", STATS="off")
        self.assertEqual(self.state()["dry_runs"][-1]["id"], "wd-103")
        self.assertFalse(any("google.com" in c[1] for c in fake.calls))


class StatsEdgeTests(GrowthRunTests.__base__):
    def setUp(self):
        super().setUp()
        self.write_schedule(two_a_day())

    def env(self, live=False, **kw):
        e = super().env(live, **kw)
        e.update({"STATS": "on", "TRENDS": "off"})
        return e

    def test_odd_old_stats_file_and_error_streak(self):
        d = self.state_path.parent
        d.mkdir(parents=True, exist_ok=True)
        (d / "stats.json").write_text(json.dumps({"media": [], "daily": "x", "account": [1, 2],
                                                  "unsupported_metrics": ["views", "bogus"]}))

        class Flaky(GrowthMeta):
            def __call__(self, method, url, headers, body, timeout, follow):
                if "/insights" in url:
                    self.calls.append((method, url, dict(headers), body))
                    return post.Response(500, {}, b'{"error":{"code":1,"message":"unknown"}}')
                return super().__call__(method, url, headers, body, timeout, follow)
        media = [{"id": f"m{n}", "caption": f"Reel {n}", "timestamp": "2026-11-12T15:00:00+0000",
                  "media_product_type": "REELS", "like_count": n} for n in range(10)]
        fake = Flaky(media=media)
        self.assertEqual(self.run_post(et(2026, 11, 13, 9, 30), fake), 0, self.out.getvalue())
        st = json.loads((d / "stats.json").read_text())
        self.assertEqual(len(st["media"]), 10)                        # likes still recorded for every Reel
        self.assertEqual(st["unsupported_metrics"], ["views"])
        insight_calls = sum("/insights" in c[1] for c in fake.calls)
        self.assertLessEqual(insight_calls, post.STATS_MAX_ERRORS * 2)  # stopped early (each call retried once)
        self.assertTrue(any("stopped early" in e for e in st["errors"]))


class StatsPruneTests(unittest.TestCase):
    def test_prune_keeps_60_days(self):
        now = datetime(2026, 12, 31, 12, tzinfo=UTC)
        old, new = (now - timedelta(days=61)).isoformat(), (now - timedelta(days=2)).isoformat()
        st = post.empty_stats()
        st["account"]["history"] = [{"at": old, "followers_count": 1}, {"at": new, "followers_count": 9}]
        st["media"] = {"a": {"updated_at": old}, "b": {"updated_at": new}}
        st["daily"] = {"a": [{"d": "2026-10-01"}], "b": [{"d": "2026-10-20"}, {"d": "2026-12-30"}]}
        post.prune_stats(st, now, ROME)
        self.assertEqual(st["account"]["history"], [{"at": new, "followers_count": 9}])
        self.assertEqual(st["account"]["latest"]["followers_count"], 9)
        self.assertEqual(list(st["media"]), ["b"])
        self.assertEqual(st["daily"], {"b": [{"d": "2026-12-30"}]})

    def test_insight_values_shapes(self):
        body = {"data": [{"name": "views", "values": [{"value": 3}]}, {"name": "reach", "total_value": {"value": 2}},
                         {"name": "saved", "values": [{"value": True}]}, "junk"]}
        self.assertEqual(post.insight_values(body), {"views": 3, "reach": 2})


if __name__ == "__main__":
    unittest.main()


class NicheWordTuning(unittest.TestCase):
    """2026-11-12: everyday niche words in tech news ("AI", "prompt", "photo") must not reorder the plan."""

    def test_news_only_niche_words_score_zero_and_weak_scores_do_not_move(self):
        import post
        idx = post.trend_index([{"src": "news", "q": "iPhone", "t": "New AI prompt tricks for your photo app"},
                                {"src": "news", "q": "Android", "t": "AI photo prompt guide"}])
        it = post.Item(id="wd-1", date="2026-11-13", slot_time="18:00", caption="Point your camera and ask AI",
                       asset="wd-1.mp4", keyword="PHOTO") if hasattr(post, "Item") else None
        if it is None:
            self.skipTest("Item not constructible here")
        sc = post.score_item(it, idx, set())
        self.assertEqual(sc["trend"], 0, sc)
        self.assertEqual(post.move_points({"trend": 5, "performance": 0}), 0)
        self.assertEqual(post.move_points({"trend": 6, "performance": 0}), 6)
        self.assertEqual(post.move_points({"trend": 2, "performance": 4}), 4)
