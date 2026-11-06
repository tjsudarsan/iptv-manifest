import json
import tempfile
import unittest
from pathlib import Path

from generator import core, generate, net

CONFIG = generate.load_config()
LAYOUT = core.Layout(CONFIG)

TA = """#EXTM3U
#EXTINF:-1 tvg-id="SunTV.in@HD" tvg-logo="https://i.imgur.com/sun.png" group-title="Entertainment",Sun TV HD (1080p)
https://example.com/sun.m3u8
#EXTINF:-1 tvg-id="IsaiAruvi.in@SD" group-title="Music",Isai Aruvi (396p)
https://example.com/isai.m3u8
#EXTINF:-1 tvg-id="NewMusic.in@SD" group-title="Music",A New Music (576p)
https://example.com/newmusic.m3u8
#EXTINF:-1 tvg-id="SaiTV.in@SD" group-title="Undefined",Sai TV (576p)
https://example.com/sai.m3u8
#EXTINF:-1 tvg-id="MoonTV.in@SD" group-title="Entertainment",Moon TV
https://example.com/moon.m3u8
#EXTINF:-1 tvg-id="Church.in@SD" group-title="Religious",Church TV
https://example.com/church.m3u8
#EXTINF:-1 tvg-id="Header.in@SD" http-referrer="https://x.example/" group-title="News",Header News
#EXTVLCOPT:http-referrer=https://x.example/
https://example.com/header.m3u8
"""
EN = """#EXTM3U
#EXTINF:-1 tvg-id="AFV.us@SD" group-title="Comedy",AFV (720p)
https://example.com/afv.m3u8
#EXTINF:-1 tvg-id="NotListed.us@SD" group-title="Comedy",Not Listed
https://example.com/nope.m3u8
"""
META = [{"id": i, "is_nsfw": False, "categories": c} for i, c in [
    ("SunTV.in", ["entertainment"]), ("IsaiAruvi.in", ["music"]), ("NewMusic.in", ["music"]),
    ("SaiTV.in", []), ("MoonTV.in", []), ("Church.in", ["religious"]), ("Header.in", ["news"]), ("AFV.us", [])]]


def sources(tmp: Path) -> generate.Sources:
    (tmp / "ta.m3u").write_text(TA)
    (tmp / "en.m3u").write_text(EN)
    (tmp / "channels.json").write_text(json.dumps(META))
    return generate.Sources(CONFIG, tmp)


def empty_state():
    return {"version": 0, "content_hash": "", "service_ids": {}, "streams": {}, "source_counts": {}}


class ParserTest(unittest.TestCase):
    def test_parses_ids_groups_and_flags_header_entries(self):
        items = core.parse_m3u(TA)
        self.assertEqual("SunTV.in@HD", items[0].id)
        self.assertEqual("Entertainment", items[0].group)
        self.assertTrue(next(i for i in items if i.id == "Header.in@SD").headers)
        self.assertFalse(items[0].headers)

    def test_rejects_non_playlists(self):
        with self.assertRaises(ValueError):
            core.parse_m3u("<html>")


class PolicyTest(unittest.TestCase):
    def test_owner_exclusions_adult_and_non_hindu_religious(self):
        p = CONFIG["policy"]
        ok = core.Classification(False, False)
        self.assertIn("owner", core.exclusion_reason("MoonTV.in@HD", "Moon", "Entertainment", ok, p))
        self.assertIn("adult", core.exclusion_reason("X.in", "Late XXX", "", ok, p))
        self.assertIn("adult", core.exclusion_reason("X.in", "X", "", core.Classification(True, False), p))
        self.assertIn("devotional", core.exclusion_reason("Church.in", "C", "", core.Classification(False, True), p))
        self.assertIn("classification", core.exclusion_reason("Unknown.in", "U", "Music", None, p))
        self.assertEqual("", core.exclusion_reason("SaiTV.in@SD", "Sai", "Undefined", ok, p))

    def test_url_policy(self):
        hosts = frozenset({"sivantv.livebox.co.in"})
        self.assertTrue(core.stream_allowed("https://cdn.example.com/a.m3u8"))
        self.assertFalse(core.stream_allowed("http://cdn.example.com/a.m3u8", hosts))
        self.assertTrue(core.stream_allowed("http://sivantv.livebox.co.in/a.m3u8", hosts))
        self.assertFalse(core.stream_allowed("https://192.168.0.5/a.m3u8"))
        self.assertFalse(core.stream_allowed("https://cdn.example.com/a.m3u8?token=1"))


class OrderTest(unittest.TestCase):
    def cat(self, cid, group):
        return LAYOUT.categorise("ta", cid, group)

    def test_categories_match_tv_rules(self):
        self.assertEqual("ta-music", self.cat("New", " Entertainment; Music "))
        self.assertEqual("ta-entertainment", self.cat("New", "Movies"))
        self.assertEqual("ta-other", self.cat("New", "Kids;Animation"))
        self.assertEqual("ta-devotional", self.cat("SaiTV.in@HD", "Undefined"))
        self.assertEqual("ta-devotional", self.cat("SairamTV.in@SD", "Music"))
        self.assertEqual("ta-news", self.cat("KalaignarSeithigal.in@SD", "News"))
        self.assertEqual("en-sports", LAYOUT.categorise("en", "DDSports.in", "Sports"))

    def test_pinned_channels_lead_then_names(self):
        rows = [("UnknownMusic", "Music"), ("AaryaaTV.in@SD", "Music"), ("7SMusic.in@SD", "Music"),
                ("IsaiAruvi.in@SD", "Music"), ("RajMusixTamil.in@SD", "Music")]
        rows.sort(key=lambda r: LAYOUT.sort_key("ta", self.cat(*r), r[0], r[0]))
        self.assertEqual(["IsaiAruvi.in@SD", "RajMusixTamil.in@SD", "7SMusic.in@SD", "AaryaaTV.in@SD", "UnknownMusic"],
                         [r[0] for r in rows])

    def test_service_ids_are_stable_and_new_ids_continue(self):
        ids = core.assign_service_ids(["A", "B", "C"], {"B": 9600}, 9502)
        self.assertEqual({"A": 9601, "B": 9600, "C": 9602}, ids)


class GenerateTest(unittest.TestCase):
    def run_once(self, state, probe_ok=True):
        with tempfile.TemporaryDirectory() as d:
            src = sources(Path(d))
            original = net.probe
            net.probe = (lambda url, **kw: None) if probe_ok else self.fail_probe
            try:
                return generate.run(CONFIG, state, src, now="2026-01-01T00:00:00Z")
            finally:
                net.probe = original

    @staticmethod
    def fail_probe(url, **kw):
        raise net.ProbeError("HTTP 403")

    def test_manifest_is_ordered_numbered_and_filtered(self):
        manifest, state, rows, changed = self.run_once(empty_state())
        self.assertTrue(changed)
        self.assertEqual(1, manifest["version"])
        ids = [c["id"] for c in manifest["channels"]]
        self.assertEqual(["IsaiAruvi.in@SD", "NewMusic.in@SD", "SunTV.in@HD", "SaiTV.in@SD", "AFV.us"], ids)
        self.assertEqual(["1", "2", "3", "4", "5"], [c["number"] for c in manifest["channels"]])
        self.assertNotIn("MoonTV.in@SD", json.dumps(manifest))
        self.assertNotIn("Church.in@SD", json.dumps(manifest))
        self.assertEqual(["Header.in@SD"], [u["id"] for u in manifest["unavailable"]])
        self.assertEqual("https://i.imgur.com/sun.png", manifest["channels"][2]["logo"])
        # Category list always contains every configured row, in TV row order.
        self.assertEqual([c["id"] for c in CONFIG["categories"]], [c["id"] for c in manifest["categories"]])

    def test_shortcuts_are_published_and_validated(self):
        manifest, _, _, _ = self.run_once(empty_state())
        ktv = next(s for s in manifest["shortcuts"] if s["id"] == "sunnxt.KTV.in")
        self.assertEqual(("ta", "ta-sun", "com.suntv.sunnxt"), (ktv["language"], ktv["category"], ktv["app"]))
        self.assertIn("ta-sun", [c["id"] for c in manifest["categories"]])
        import copy
        bad = copy.deepcopy(CONFIG)
        bad["shortcuts"][0]["uri"] = "intent://evil#Intent;end"
        with self.assertRaises(ValueError):
            core.build_manifest(bad, LAYOUT, [core.Resolved("A", "A", "ta", "ta-music", "", "https://x.example/a.m3u8", True, "")],
                                {"A": 1}, 1, "now")

    def test_rerun_without_changes_keeps_version(self):
        manifest, state, _, _ = self.run_once(empty_state())
        again, state2, _, changed = self.run_once(state)
        self.assertFalse(changed)
        self.assertEqual(manifest["version"], again["version"])
        self.assertEqual(state["service_ids"], state2["service_ids"])

    def test_failed_probe_retains_previous_working_stream(self):
        manifest, state, _, _ = self.run_once(empty_state())
        state["streams"]["SunTV.in@HD"]["url"] = "https://example.com/old-sun.m3u8"
        again, _, _, _ = self.run_once(state, probe_ok=False)
        sun = next(c for c in again["channels"] if c["id"] == "SunTV.in@HD")
        self.assertEqual("https://example.com/old-sun.m3u8", sun["url"])

    def test_truncated_source_is_rejected(self):
        state = empty_state()
        state["source_counts"] = {"ta": 100}
        with self.assertRaises(ValueError):
            self.run_once(state)


if __name__ == "__main__":
    unittest.main()
