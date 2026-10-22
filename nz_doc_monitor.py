"""DOC Great Walk hut-availability monitor (Milford + Routeburn).

Polls the New Zealand Department of Conservation (DOC) booking platform
(Tyler Technologies "RDR" reservation system behind https://bookings.doc.govt.nz)
and sends a push notification via ntfy.sh when a target trip becomes bookable.

Two tracks are monitored with different rules:

  * Milford Track  - a *fixed* 3-night Great Walk. All three huts must be
    available on consecutive nights, in travel order:
        Clinton Hut -> Mintaro Hut -> Dumpling Hut
  * Routeburn Track - a single night in either Routeburn hut is enough.

Shared search window: arrival between 2026-12-28 and 2027-01-10 (inclusive).

----------------------------------------------------------------------------
API contract (verified against the live site by capturing the real request the
booking SPA sends via its "Search" button):

  POST {API_BASE}search/greatwalkplacefacility
  Content-Type: application/json
  Body: {
      "accomodation": "",                 # MUST be empty string -> returns all
                                          #   huts/campsites for the place.
                                          #   A numeric id yields an empty result.
      "placeId": <PlaceId>,               # the Great Walk place id
      "customerClassificationId": 0,      # 0 = public / default
      "arrivalDate": "YYYY-MM-DD",        # first day of the returned grid
      "nights": <int>                     # width of the returned grid (days)
  }
  ->  {
        "GreatWalkFacilityHeaderData": [ {"FullDate": "...T00:00:00", ...}, ... ],
        "GreatWalkFacilityData": [
          {
            "FacilityId": <int>,
            "FacilityName": <str>,
            "OrderBy": <int>,
            "GreatWalkFacilityDateData": [
              {
                "ArrivalDate": "YYYY-MM-DDT00:00:00",
                "IsSeasonAvailable": <bool>,
                "IsAvailable": <bool>,
                "TotalAvailable": <int>   # bunks free that night
              }, ...
            ]
          }, ...
        ]
      }

A night is bookable when, exactly as the website decides:
    IsSeasonAvailable and IsAvailable and TotalAvailable >= party_size
"""

from datetime import datetime, timedelta
import json
import os

import cloudscraper

# --- CONFIGURATION ---------------------------------------------------------

API_BASE = "https://prod-nz-rdr.recreation-management.tylerapp.com/nzrdr/rdr/"
CUSTOMER_CLASSIFICATION_ID = 0  # public / default pricing class

PARTY_SIZE = 2

# Shared arrival window for the trip start (inclusive).
START_WINDOW_BEGIN = "2026-12-28"
START_WINDOW_END = "2027-01-10"

# Send a "still watching" heartbeat notification. On ephemeral CI runners a
# local state file does not persist between runs, so cadence is controlled by
# *when* this runs: the workflow enables it on a once-a-day schedule only.
# Set env SEND_HEARTBEAT=1 to send a heartbeat this run, 0 to skip.
SEND_DAILY_HEARTBEAT = os.getenv("SEND_HEARTBEAT", "0") == "1"

# Track definitions. Identifiers discovered from the live DOC booking platform.
#   place_id     : Great Walk place id
#   nights       : consecutive nights the trip needs
#   rule         : how to decide an "opening":
#       "full_chain"            -> every hut in `ordered_huts` must be free on
#                                  consecutive nights, in order (fixed Milford)
#       "any_single_night"      -> any single available hut-night qualifies
#                                  (Routeburn: one night is enough)
#       "two_huts_consecutive"  -> both huts in `ordered_huts` free on two
#                                  consecutive nights, in EITHER order
#                                  (Kepler: Luxmore + Iris Burn, direction free)
#   ordered_huts : huts (FacilityId + display name); order is the travel order
#                  for "full_chain" and ignored for the other rules.
TRACKS = [
    {
        "key": "milford",
        "name": "Milford Track",
        "place_id": 873,
        "nights": 3,
        "rule": "full_chain",
        "ordered_huts": [
            {"facility_id": 2857, "name": "Clinton Hut"},
            {"facility_id": 2859, "name": "Mintaro Hut"},
            {"facility_id": 2858, "name": "Dumpling Hut"},
        ],
    },
    {
        "key": "routeburn",
        "name": "Routeburn Track",
        "place_id": 874,
        "nights": 1,
        "rule": "any_single_night",
        "ordered_huts": [
            {"facility_id": 2862, "name": "Routeburn Falls Hut"},
            {"facility_id": 2861, "name": "Lake Mackenzie Hut"},
        ],
    },
    {
        "key": "kepler",
        "name": "Kepler Track",
        "place_id": 872,
        "nights": 2,
        "rule": "two_huts_consecutive",
        "ordered_huts": [
            {"facility_id": 2855, "name": "Luxmore Hut"},
            {"facility_id": 2854, "name": "Iris Burn Hut"},
        ],
    },
]

NTFY_TOPIC = os.getenv("NTFY_TOPIC", "REDACTED_TOPIC")

BOOKING_URL = "https://bookings.doc.govt.nz/Web/Default.aspx#!greatwalk-result"

REQUEST_HEADERS = {
    "Accept": "application/json",
    "Content-Type": "application/json",
    "Origin": "https://bookings.doc.govt.nz",
    "Referer": "https://bookings.doc.govt.nz/",
}


def _ascii_header(value: str) -> str:
  """HTTP header values must be latin-1 safe; drop any non-latin-1 chars.

  ntfy renders emoji from the `Tags` field, so the human-facing title stays
  readable without needing emoji in the (latin-1-only) Title header.
  """
  return value.encode("latin-1", "ignore").decode("latin-1").strip() or "DOC monitor"


def send_push_notification(
    title: str, message: str, scraper: "cloudscraper.CloudScraper",
    priority: str = "high", tags: str = "rotating_light,mountain"
) -> None:
  """Sends a push notification via ntfy.sh.

  Title/Priority/Tags/Click are HTTP headers (latin-1 only); the message body
  is sent as UTF-8 so emoji there render fine.
  """
  endpoint = f"https://ntfy.sh/{NTFY_TOPIC}"
  try:
    response = scraper.post(
        endpoint,
        data=message.encode("utf-8"),
        headers={
            "Title": _ascii_header(title),
            "Priority": priority,
            "Tags": tags,
            "Click": BOOKING_URL,
        },
        timeout=10,
    )
    if response.status_code == 200:
      print(f"📲 Notification delivered: {title}")
    else:
      print(f"⚠ Notification delivery failed: {response.status_code}")
  except Exception as e:  # noqa: BLE001 - never let notification errors crash the run
    print(f"⚠ Notification error: {e}")


def fetch_track_availability(
    scraper: "cloudscraper.CloudScraper", track: dict, request_nights: int
) -> dict:
  """Fetches bookable (facility_id, 'YYYY-MM-DD') -> free-bunk map for a track.

  Note: `accomodation` MUST be an empty string; a numeric id makes the API
  return an empty result set (verified against the live booking site).
  """
  url = f"{API_BASE}search/greatwalkplacefacility"
  body = {
      "accomodation": "",
      "placeId": track["place_id"],
      "customerClassificationId": CUSTOMER_CLASSIFICATION_ID,
      "arrivalDate": START_WINDOW_BEGIN,
      "nights": request_nights,
  }

  print(
      f"📡 {track['name']}: querying place {track['place_id']} "
      f"({request_nights}-day grid from {START_WINDOW_BEGIN})..."
  )

  try:
    res = scraper.post(
        url, data=json.dumps(body), headers=REQUEST_HEADERS, timeout=30
    )
    print(f"   HTTP Status: {res.status_code}")
    res.raise_for_status()
    data = res.json()
  except Exception as e:  # noqa: BLE001 - treat any fetch error as "no data this run"
    print(f"   ❌ Failed to retrieve data: {e}")
    return {}

  facilities = data.get("GreatWalkFacilityData") or []
  availability: dict = {}

  for facility in facilities:
    facility_id = facility.get("FacilityId")
    for slice_ in facility.get("GreatWalkFacilityDateData") or []:
      arrival = str(slice_.get("ArrivalDate") or "")
      date_str = arrival.split("T", 1)[0]  # "2026-12-28T00:00:00" -> "2026-12-28"
      is_season = bool(slice_.get("IsSeasonAvailable"))
      is_available = bool(slice_.get("IsAvailable"))
      total_available = int(slice_.get("TotalAvailable") or 0)

      if date_str and is_season and is_available:
        availability[(facility_id, date_str)] = total_available

  print(f"   📊 {len(availability)} bookable hut-date records.")
  return availability


def find_full_chain_itineraries(track: dict, availability: dict) -> list:
  """Finds contiguous full-chain openings (all huts, in order)."""
  curr_start = datetime.strptime(START_WINDOW_BEGIN, "%Y-%m-%d")
  latest_start = datetime.strptime(START_WINDOW_END, "%Y-%m-%d")
  itineraries = []

  while curr_start <= latest_start:
    schedule = []
    is_valid_chain = True

    for day_idx, hut in enumerate(track["ordered_huts"]):
      night_date = (curr_start + timedelta(days=day_idx)).strftime("%Y-%m-%d")
      free_bunks = availability.get((hut["facility_id"], night_date), 0)
      if free_bunks < PARTY_SIZE:
        is_valid_chain = False
        break
      schedule.append({
          "night": day_idx + 1,
          "hut": hut["name"],
          "date": night_date,
          "bunks": free_bunks,
      })

    if is_valid_chain:
      itineraries.append({
          "start_date": curr_start.strftime("%Y-%m-%d"),
          "schedule": schedule,
      })

    curr_start += timedelta(days=1)

  return itineraries


def find_single_night_openings(track: dict, availability: dict) -> list:
  """Finds any single hut-night opening within the window (one night suffices)."""
  begin = datetime.strptime(START_WINDOW_BEGIN, "%Y-%m-%d")
  end = datetime.strptime(START_WINDOW_END, "%Y-%m-%d")
  openings = []

  for hut in track["ordered_huts"]:
    for (fac_id, date_str), bunks in availability.items():
      if fac_id != hut["facility_id"]:
        continue
      night = datetime.strptime(date_str, "%Y-%m-%d")
      if begin <= night <= end and bunks >= PARTY_SIZE:
        openings.append({"date": date_str, "hut": hut["name"], "bunks": bunks})

  openings.sort(key=lambda o: (o["date"], o["hut"]))
  return openings


def find_two_huts_consecutive(track: dict, availability: dict) -> list:
  """Finds openings where BOTH huts are free on two consecutive nights,
  in EITHER order (direction doesn't matter).

  For each candidate start date D in the window, checks both orderings:
    hutA on D + hutB on D+1, and hutB on D + hutA on D+1.
  """
  hut_a, hut_b = track["ordered_huts"][0], track["ordered_huts"][1]
  begin = datetime.strptime(START_WINDOW_BEGIN, "%Y-%m-%d")
  end = datetime.strptime(START_WINDOW_END, "%Y-%m-%d")
  itineraries = []

  curr = begin
  while curr <= end:
    d1 = curr.strftime("%Y-%m-%d")
    d2 = (curr + timedelta(days=1)).strftime("%Y-%m-%d")
    for first, second in ((hut_a, hut_b), (hut_b, hut_a)):
      b1 = availability.get((first["facility_id"], d1), 0)
      b2 = availability.get((second["facility_id"], d2), 0)
      if b1 >= PARTY_SIZE and b2 >= PARTY_SIZE:
        itineraries.append({
            "start_date": d1,
            "schedule": [
                {"night": 1, "hut": first["name"], "date": d1, "bunks": b1},
                {"night": 2, "hut": second["name"], "date": d2, "bunks": b2},
            ],
        })
    curr += timedelta(days=1)

  return itineraries



  lines = [
      f"{track['name']}: found {len(itineraries)} full "
      f"{track['nights']}-night opening(s) for {PARTY_SIZE}p:"
  ]
  for trip in itineraries[:3]:
    lines.append(f"\nTrip starting {trip['start_date']}:")
    for night in trip["schedule"]:
      lines.append(
          f"  • Night {night['night']} ({night['date']}): "
          f"{night['hut']} ({night['bunks']} free)"
      )
  return "\n".join(lines)


def format_single_night_message(track: dict, openings: list) -> str:
  lines = [
      f"{track['name']}: found {len(openings)} hut-night opening(s) "
      f"for {PARTY_SIZE}p (1 night is enough):"
  ]
  for o in openings[:8]:
    lines.append(f"  • {o['date']}: {o['hut']} ({o['bunks']} free)")
  if len(openings) > 8:
    lines.append(f"  ... and {len(openings) - 8} more")
  return "\n".join(lines)


def check_track(scraper: "cloudscraper.CloudScraper", track: dict):
  """Returns (title, message) if an opening is found, else None."""
  window_days = (
      datetime.strptime(START_WINDOW_END, "%Y-%m-%d")
      - datetime.strptime(START_WINDOW_BEGIN, "%Y-%m-%d")
  ).days
  # Request enough days to cover the whole window plus one trip length.
  request_nights = window_days + track["nights"] + 1

  availability = fetch_track_availability(scraper, track, request_nights)

  rule = track["rule"]
  title = f"🚨 {track['name'].upper()} OPENING FOUND!"

  if rule == "full_chain":
    itineraries = find_full_chain_itineraries(track, availability)
    if itineraries:
      print(f"   🚨 {track['name']}: {len(itineraries)} full-chain itinerary(ies)!")
      return (title, format_full_chain_message(track, itineraries))
  elif rule == "two_huts_consecutive":
    itineraries = find_two_huts_consecutive(track, availability)
    if itineraries:
      print(f"   🚨 {track['name']}: {len(itineraries)} two-hut opening(s)!")
      return (title, format_full_chain_message(track, itineraries))
  elif rule == "any_single_night":
    openings = find_single_night_openings(track, availability)
    if openings:
      print(f"   🚨 {track['name']}: {len(openings)} single-night opening(s)!")
      return (title, format_single_night_message(track, openings))
  else:
    print(f"   ⚠ {track['name']}: unknown rule {rule!r}; skipping.")

  print(f"   ℹ {track['name']}: no qualifying openings.")
  return None


def maybe_send_daily_heartbeat(scraper, any_hits: bool) -> None:
  """Sends a 'still watching' heartbeat when SEND_HEARTBEAT=1.

  Cadence is controlled by the caller/scheduler (the workflow enables this on a
  once-a-day cron), so no local state file is needed — which is important on
  ephemeral CI runners where files do not persist between runs.
  """
  if not SEND_DAILY_HEARTBEAT:
    return

  status = "openings found this run" if any_hits else "no openings yet"
  msg = (
      f"DOC monitor is alive ✅\n"
      f"Daily check OK — {status}.\n"
      f"Window {START_WINDOW_BEGIN} to {START_WINDOW_END}, party of {PARTY_SIZE}.\n"
      f"Tracks: Milford (3-night chain), Routeburn (1 night), "
      f"Kepler (Luxmore + Iris Burn, 2 consecutive nights)."
  )
  send_push_notification(
      "DOC monitor daily check", msg, scraper,
      priority="low", tags="white_check_mark,calendar",
  )


def main() -> None:
  scraper = cloudscraper.create_scraper(
      browser={"browser": "chrome", "platform": "linux", "desktop": True}
  )
  # Prime cookies by touching the booking site first.
  try:
    scraper.get("https://bookings.doc.govt.nz/Web/Default.aspx", timeout=30)
  except Exception:  # noqa: BLE001 - priming is best-effort
    pass

  print(f"Window: {START_WINDOW_BEGIN} to {START_WINDOW_END}, party of {PARTY_SIZE}\n")

  hits = []
  for track in TRACKS:
    result = check_track(scraper, track)
    if result:
      hits.append(result)

  if hits:
    for title, message in hits:
      send_push_notification(title, message, scraper)
  else:
    print("\nℹ Check finished: no openings for any monitored track.")

  maybe_send_daily_heartbeat(scraper, any_hits=bool(hits))


if __name__ == "__main__":
  main()
