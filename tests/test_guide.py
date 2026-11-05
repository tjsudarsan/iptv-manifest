import gzip
import unittest
from unittest import mock

from generator import guide


class GuideTest(unittest.TestCase):
    def test_normalise_clips_sorts_and_removes_overlaps(self):
        progs = [(50, 150, "B", "x" * 300), (0, 60, "A", ""), (140, 200, "C", ""), (300, 400, "Late", ""), (90, 95, " ", "")]
        out = guide.normalise(progs, 10, 250, 80, 20)
        self.assertEqual([[10, 60, "A", ""], [60, 150, "B", "x" * 19 + "…"], [150, 200, "C", ""]], out)

    def test_sources_prefer_same_feed_airtel_then_known_files(self):
        index = [
            {"channel": "SunTV.in", "feed": "SD", "site": "airtelxstream.in", "site_id": "SUN"},
            {"channel": "SunTV.in", "feed": "HD", "site": "airtelxstream.in", "site_id": "SUN_HD"},
            {"channel": "AFV.us", "feed": "SD", "site": "i.mjh.nz", "site_id": "Plex/gb#x-1"},
            {"channel": "AFV.us", "feed": "SD", "site": "i.mjh.nz", "site_id": "Plex/us#x-2"},
            {"channel": "AFV.us", "feed": "SD", "site": "tataplay.com", "site_id": "9"},
        ]
        manifest = {"channels": [{"id": "SunTV.in@HD"}, {"id": "AFV.us"}, {"id": "None.in@SD"}]}
        chosen = guide.choose_sources(manifest, index, {})
        self.assertEqual(("airtelxstream.in", "SUN_HD"), chosen["SunTV.in@HD"][0])
        self.assertEqual([("i.mjh.nz", "Plex/us#x-2"), ("i.mjh.nz", "Plex/gb#x-1")], chosen["AFV.us"])
        self.assertNotIn("None.in@SD", chosen)
        self.assertEqual([("i.mjh.nz", "Foxtel/epg#A")],
                         guide.choose_sources(manifest, index, {"AFV.us": "i.mjh.nz:Foxtel/epg#A"})["AFV.us"])

    def test_xmltv_matches_plex_ids_with_changed_prefix(self):
        xml = b"""<tv><channel id="new-abc"/><programme channel="new-abc" start="20260105100000 +0000" stop="20260105110000 +0000">
        <title>Show</title><desc>About</desc></programme><programme channel="other" start="20260105100000 +0000"
        stop="20260105110000 +0000"><title>X</title></programme></tv>"""
        with mock.patch.object(guide, "get", return_value=gzip.compress(xml)):
            parsed = guide.mjh("Plex/us", {"old-abc"})
        self.assertEqual([(1767607200, 1767610800, "Show", "About")], parsed["old-abc"])


if __name__ == "__main__":
    unittest.main()
