"""DOC Great Walk hut-availability monitor (Milford + Routeburn).

Polls the New Zealand Department of Conservation (DOC) booking platform
(Tyler Technologies "RDR" reservation system that powers
https://bookings.doc.govt.nz) and sends a push notification via ntfy.sh when a
target trip becomes bookable.

Two tracks are monitored with different rules:

  * Milford Track  - a *fixed* 3-night Great Walk. All three huts must be
    available on consecutive nights, in travel order:
        Clinton Hut -> Mintaro Hut -> Dumpling Hut
  * Routeburn Track - a single night in any Routeburn hut is enough.

Shared search window: arrival between 2026-12-28 and 2027-01-10 (inclusive).

----------------------------------------------------------------------------
API contract (reverse-engineered from the live booking SPA):

  POST {API_BASE}search/greatwalkplacefacility
  Content-Type: application/json
  Body: {
      "accomodation": <AccomodationId>,   # "Hut" accommodation id for the track
      "placeId": <PlaceId>,               # the Great Walk place id
      "customerClassificationId": 0,      # 0 = public / default
      "arrivalDate": "YYYY-MM-DD",
      "nights": <int>
  }
  ->  {
        "GreatWalkFacilityHeaderData": {...} | null,
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

# Track definitions. Identifiers discovered from the live DOC booking platform.
#   place_id          : Great Walk place id
#   accommodation_id  : "Hut" accommodation id used by the availability POST
#   nights            : how many consecutive nights the trip needs
#   require_full_chain:
#       True  -> every hut in `ordered_huts` must be free on consecutive
#                nights, in order (used for the fixed Milford walk)
#       False -> any single available hut-night in the window qualifies
#                (used for Routeburn, where one night is enough)
#   ordered_huts      : huts in travel order (FacilityId + display name)
TRACKS = [
    {
        "key": "milford",
        "name": "Milford Track",
        "place_id": 873,
        "accommodation_id": 2857,
        "nights": 3,
        "require_full_chain": True,
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
        "accommodation_id": 2861,
        "nights": 1,
        "require_full_chain": False,
        "ordered_huts": [
            {"facility_id": 2862, "name": "Routeburn Falls Hut"},
            {"facility_id": 2861, "name": "Lake Mackenzie Hut"},
        ],
    },
]

NTFY_TOPIC = os.getenv("NTFY_TOPIC", "REDACTED_TOPIC")

BOOKING_URL = "https://bookings.doc.govt.nz/Web/Default.aspx#!greatwalk-result"

# Headers that make the request look like it came from the booking site.
REQUEST_HEADERS = {
    "Accept": "application/json",
    "Content-Type": "application/json",
    "Origin": "https://bookings.doc.govt.nz",
    "Referer": "https://bookings.doc.govt.nz/",
}


def send_push_notification(
    title: str, message: str, scraper: "cloudscraper.CloudScraper"
) -> None:
  """Sends a push notification via ntfy.sh."""
  endpoint = f"https://ntfy.sh/{NTFY_TOPIC}"
  try:
    response = scraper.post(
        endpoint,
        data=message.encode("utf-8"),
        headers={
            "Title": title,
            "Priority": "high",
            "Tags": "rotating_light,mountain",
            "Click": BOOKING_URL,
        },
        timeout=10,
    )
    if response.status_code == 200:
      print("📲 Push notification delivered!")
    else:
      print(f"⚠ Notification delivery failed: {response.status_code}")
  except Exception as e:  # noqa: BLE001 - never let notification errors crash the run
    print(f"⚠ Notification error: {e}")


def fetch_track_availability(
    scraper: "cloudscraper.CloudScraper", track: dict, request_nights: int
) -> dict:
  """Fetches bookable (facility_id, 'YYYY-MM-DD') -> free-bunk map for a track."""
  url = f"{API_BASE}search/greatwalkplacefacility"
  body = {
      "accomodation": track["accommodation_id"],
      "placeId": track["place_id"],
      "customerClassificationId": CUSTOMER_CLASSIFICATION_ID,
      "arrivalDate": START_WINDOW_BEGIN,
      "nights": request_nights,
  }

  print(
      f"📡 {track['name']}: querying place {track['place_id']} "
      f"(accommodation {track['accommodation_id']}, {request_nights} nights "
      f"from {START_WINDOW_BEGIN})..."
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
        openings.append({
            "date": date_str,
            "hut": hut["name"],
            "bunks": bunks,
        })

  openings.sort(key=lambda o: (o["date"], o["hut"]))
  return openings


def format_full_chain_message(track: dict, itineraries: list) -> str:
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
  # Request enough nights to cover the whole window plus one trip length.
  request_nights = window_days + track["nights"] + 1

  availability = fetch_track_availability(scraper, track, request_nights)

  if track["require_full_chain"]:
    itineraries = find_full_chain_itineraries(track, availability)
    if itineraries:
      print(
          f"   🚨 {track['name']}: {len(itineraries)} full-chain itinerary(ies)!"
      )
      title = f"🚨 {track['name'].upper()} OPENING FOUND!"
      return title, format_full_chain_message(track, itineraries)
  else:
    openings = find_single_night_openings(track, availability)
    if openings:
      print(f"   🚨 {track['name']}: {len(openings)} single-night opening(s)!")
      title = f"🚨 {track['name'].upper()} OPENING FOUND!"
      return title, format_single_night_message(track, openings)

  print(f"   ℹ {track['name']}: no qualifying openings.")
  return None


def main() -> None:
  scraper = cloudscraper.create_scraper(
      browser={"browser": "chrome", "platform": "linux", "desktop": True}
  )

  print(
      f"Window: {START_WINDOW_BEGIN} to {START_WINDOW_END}, party of {PARTY_SIZE}\n"
  )

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


if __name__ == "__main__":
  main()
