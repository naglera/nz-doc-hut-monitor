"""Regression tests for nz_doc_monitor.

Exercises the full check_track() path (fetch -> find -> format) for every
rule, plus main() for every enabled track, with the DOC API and ntfy stubbed
out. Guards against crashes in alert handling, e.g. the missing
format_full_chain_message definition that crashed real Kepler hits.

Run: python -m unittest -v test_nz_doc_monitor
"""

import json
import sys
import types
import unittest
from datetime import datetime, timedelta

# Stub cloudscraper so tests need no network or third-party deps.
_cs = types.ModuleType("cloudscraper")
_cs.CloudScraper = object
_cs.create_scraper = lambda **kwargs: None
sys.modules.setdefault("cloudscraper", _cs)

import nz_doc_monitor as mon  # noqa: E402


def _slice(date_str, bunks):
  return {
      "ArrivalDate": f"{date_str}T00:00:00",
      "IsSeasonAvailable": True,
      "IsAvailable": True,
      "TotalAvailable": bunks,
  }


class FakeResponse:

  def __init__(self, payload, status=200):
    self._payload = payload
    self.status_code = status

  def raise_for_status(self):
    pass

  def json(self):
    return self._payload


class FakeScraper:
  """Serves a canned availability payload per placeId; records ntfy posts."""

  def __init__(self, payloads):
    self.payloads = payloads
    self.notifications = []

  def get(self, url, **kwargs):
    return FakeResponse({})

  def post(self, url, data=None, headers=None, **kwargs):
    if "ntfy.sh" in url:
      self.notifications.append(
          {"title": headers.get("Title"), "body": data.decode("utf-8")})
      return FakeResponse({})
    place_id = json.loads(data)["placeId"]
    return FakeResponse(
        self.payloads.get(place_id, {"GreatWalkFacilityData": []}))


def _payload_all_huts_free(track, bunks=5):
  """Every hut in the track has `bunks` free on every night of the window."""
  begin = datetime.strptime(mon.START_WINDOW_BEGIN, "%Y-%m-%d")
  end = datetime.strptime(mon.START_WINDOW_END, "%Y-%m-%d")
  days = (end - begin).days + track["nights"] + 1
  dates = [(begin + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(days)]
  return {
      "GreatWalkFacilityHeaderData": [],
      "GreatWalkFacilityData": [
          {
              "FacilityId": hut["facility_id"],
              "FacilityName": hut["name"],
              "GreatWalkFacilityDateData": [_slice(d, bunks) for d in dates],
          }
          for hut in track["ordered_huts"]
      ],
  }


# One synthetic track per rule, independent of which real tracks are enabled.
RULE_TRACKS = [
    {"key": "t_chain", "name": "Chain Track", "place_id": 1, "nights": 3,
     "rule": "full_chain",
     "ordered_huts": [{"facility_id": 11, "name": "A Hut"},
                      {"facility_id": 12, "name": "B Hut"},
                      {"facility_id": 13, "name": "C Hut"}]},
    {"key": "t_single", "name": "Single Track", "place_id": 2, "nights": 1,
     "rule": "any_single_night",
     "ordered_huts": [{"facility_id": 21, "name": "D Hut"}]},
    {"key": "t_two", "name": "Two Track", "place_id": 3, "nights": 2,
     "rule": "two_huts_consecutive",
     "ordered_huts": [{"facility_id": 31, "name": "E Hut"},
                      {"facility_id": 32, "name": "F Hut"}]},
]


class _TopicSetTest(unittest.TestCase):

  def setUp(self):
    self._topic = mon.NTFY_TOPIC
    mon.NTFY_TOPIC = "test-topic"

  def tearDown(self):
    mon.NTFY_TOPIC = self._topic


class CheckTrackEveryRuleTest(_TopicSetTest):

  def test_check_track_formats_alert_for_every_rule(self):
    for track in RULE_TRACKS:
      with self.subTest(rule=track["rule"]):
        scraper = FakeScraper({track["place_id"]: _payload_all_huts_free(track)})
        result = mon.check_track(scraper, track)
        self.assertIsNotNone(result, "expected an opening to be detected")
        title, message = result
        self.assertIn(track["name"].upper(), title)
        for hut in track["ordered_huts"]:
          self.assertIn(hut["name"], message)

  def test_check_track_returns_none_when_nothing_free(self):
    for track in RULE_TRACKS:
      with self.subTest(rule=track["rule"]):
        scraper = FakeScraper({})
        self.assertIsNone(mon.check_track(scraper, track))

  def test_every_configured_track_uses_a_known_rule(self):
    known = {"full_chain", "any_single_night", "two_huts_consecutive"}
    for track in mon.TRACKS:
      self.assertIn(track["rule"], known, track["key"])


class MainEndToEndTest(_TopicSetTest):

  def test_main_sends_one_alert_per_enabled_track(self):
    scraper = FakeScraper(
        {t["place_id"]: _payload_all_huts_free(t) for t in mon.TRACKS})
    original = mon.cloudscraper.create_scraper
    mon.cloudscraper.create_scraper = lambda **kwargs: scraper
    try:
      mon.main()
    finally:
      mon.cloudscraper.create_scraper = original
    titles = [n["title"] for n in scraper.notifications]
    self.assertEqual(len(titles), len(mon.TRACKS), titles)
    for track in mon.TRACKS:
      self.assertTrue(any(track["name"].upper() in t for t in titles),
                      f"no alert for {track['name']}: {titles}")

  def test_main_exits_when_topic_missing(self):
    mon.NTFY_TOPIC = ""
    with self.assertRaises(SystemExit) as ctx:
      mon.main()
    self.assertNotEqual(ctx.exception.code, 0)


if __name__ == "__main__":
  unittest.main()
