from datetime import datetime, timedelta
import json
import os
import sys
import cloudscraper

# --- CONFIGURATION ---
TRACK_ID = "routeburn"  # DOC track resource ID
PARTY_SIZE = 2
START_WINDOW_BEGIN = "2026-12-15"
START_WINDOW_END = "2027-01-15"

# Huts in exact travel order (Routeburn Shelter -> The Divide)
ORDERED_HUTS = [
    {"id": "falls_hut", "name": "Routeburn Falls Hut"},
    {"id": "mackenzie_hut", "name": "Lake Mackenzie Hut"},
]

NTFY_TOPIC = os.getenv("NTFY_TOPIC", "REDACTED_TOPIC")


def send_push_notification(
    title: str, message: str, booking_url: str, scraper: cloudscraper.CloudScraper
):
  """Sends push notification via ntfy.sh."""
  endpoint = f"https://ntfy.sh/{NTFY_TOPIC}"
  try:
    response = scraper.post(
        endpoint,
        data=message.encode("utf-8"),
        headers={
            "Title": title,
            "Priority": "high",
            "Tags": "rotating_light,mountain",
            "Click": booking_url,
        },
        timeout=10,
    )
    if response.status_code == 200:
      print("📲 Push notification delivered!")
    else:
      print(f"⚠️ Notification delivery failed: {response.status_code}")
  except Exception as e:
    print(f"⚠️ Notification error: {e}")


def check_doc_availability():
  # Initialize cloudscraper to bypass Cloudflare anti-bot challenges
  scraper = cloudscraper.create_scraper(
      browser={"browser": "chrome", "platform": "linux", "desktop": True}
  )

  api_url = "https://bookings.doc.govt.nz/Web/api/availability"

  trip_length = len(ORDERED_HUTS)
  fetch_end = (
      datetime.strptime(START_WINDOW_END, "%Y-%m-%d")
      + timedelta(days=trip_length + 2)
  ).strftime("%Y-%m-%d")

  params = {
      "resourceId": TRACK_ID,
      "startDate": START_WINDOW_BEGIN,
      "endDate": fetch_end,
      "guests": PARTY_SIZE,
  }

  print(f"📡 Querying DOC API via Cloudscraper for track '{TRACK_ID}'...")

  try:
    res = scraper.get(api_url, params=params, timeout=20)
    print(f"HTTP Status: {res.status_code}")
    res.raise_for_status()
    raw_data = res.json()
  except Exception as e:
    print(f"❌ Failed to retrieve DOC data: {e}")
    sys.exit(0)

  # Robust Fallback Parsing for DOC JSON Structures
  availability = {}
  entries = []

  if isinstance(raw_data, dict):
    entries = (
        raw_data.get("availability")
        or raw_data.get("resources")
        or raw_data.get("items")
        or []
    )
  elif isinstance(raw_data, list):
    entries = raw_data

  for entry in entries:
    hut_id = str(
        entry.get("hut_id") or entry.get("resourceId") or entry.get("id") or ""
    )
    date_str = str(
        entry.get("date") or entry.get("startDate") or entry.get("time") or ""
    )
    cap = int(
        entry.get("available_capacity")
        or entry.get("capacity")
        or entry.get("freeBunks")
        or 0
    )

    if hut_id and date_str:
      availability[(hut_id, date_str)] = cap

  print(f"📊 Processed {len(availability)} hut-date records.")

  # Contiguous Chain Verification Engine
  curr_start = datetime.strptime(START_WINDOW_BEGIN, "%Y-%m-%d")
  latest_start = datetime.strptime(START_WINDOW_END, "%Y-%m-%d")
  valid_itineraries = []

  while curr_start <= latest_start:
    is_valid_chain = True
    schedule = []

    for day_idx, hut in enumerate(ORDERED_HUTS):
      night_date = (curr_start + timedelta(days=day_idx)).strftime("%Y-%m-%d")
      free_bunks = availability.get((hut["id"], night_date), 0)

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
      valid_itineraries.append({
          "start_date": curr_start.strftime("%Y-%m-%d"),
          "schedule": schedule,
      })

    curr_start += timedelta(days=1)

  booking_url = (
      "https://bookings.doc.govt.nz/Web/Default.aspx#!greatwalk-result"
  )

  if valid_itineraries:
    print(
        f"🚨 FOUND {len(valid_itineraries)} VALID CONTIGUOUS ITINERARIES! Sending"
        " alert..."
    )

    lines = [f"Found {len(valid_itineraries)} route openings for {PARTY_SIZE}p:"]
    for trip in valid_itineraries[:2]:
      lines.append(f"\nTrip Starting {trip['start_date']}:")
      for night in trip["schedule"]:
        lines.append(
            f"  • Night {night['night']} ({night['date']}): {night['hut']} ({night['bunks']} free)"
        )

    msg_body = "\n".join(lines)
    send_push_notification(
        f"🚨 DOC HUT OPENING FOUND! ({TRACK_ID.upper()})",
        msg_body,
        booking_url,
        scraper,
    )
  else:
    print("ℹ️ Check finished: No contiguous hut openings available.")


if __name__ == "__main__":
  check_doc_availability()
    
