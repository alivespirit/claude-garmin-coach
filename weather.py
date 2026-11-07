#!/usr/bin/env python3
"""Hourly weather for a Garmin activity, from Open-Meteo (free, no API key).

Usage: weather.py ACTIVITY_ID

Location is the activity's GPS start point, rounded to ~1 km. Activities without
GPS (treadmill, strength) fall back to HOME_LATITUDE / HOME_LONGITUDE if set.
"""
import json
import os
import sys
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta

import bot  # noqa: F401  (importing it loads config.env, e.g. HOME_LATITUDE/HOME_LONGITUDE)

HOURLY = (
    "temperature_2m,apparent_temperature,relative_humidity_2m,dew_point_2m,"
    "precipitation,wind_speed_10m,wind_gusts_10m,wind_direction_10m,cloud_cover,weather_code"
)
CODES = {
    0: "clear", 1: "mainly clear", 2: "partly cloudy", 3: "overcast", 45: "fog", 48: "rime fog",
    51: "light drizzle", 53: "drizzle", 55: "dense drizzle", 56: "freezing drizzle",
    57: "freezing drizzle", 61: "light rain", 63: "rain", 65: "heavy rain", 66: "freezing rain",
    67: "freezing rain", 71: "light snow", 73: "snow", 75: "heavy snow", 77: "snow grains",
    80: "light showers", 81: "showers", 82: "violent showers", 85: "snow showers",
    86: "heavy snow showers", 95: "thunderstorm", 96: "thunderstorm with hail",
    99: "thunderstorm with hail",
}


def garmin_summary(activity_id):
    from garminconnect import Garmin

    g = Garmin()
    g.login(os.path.expanduser(os.environ.get("GARMINTOKENS") or "~/.garminconnect"))
    return g.get_activity(activity_id).get("summaryDTO") or {}


def main():
    if len(sys.argv) != 2 or not sys.argv[1].isdigit():
        sys.exit("Usage: weather.py ACTIVITY_ID")
    activity_id = int(sys.argv[1])
    s = garmin_summary(activity_id)

    lat, lon, source = s.get("startLatitude"), s.get("startLongitude"), "activity GPS start"
    if lat is None or lon is None:
        lat, lon = os.environ.get("HOME_LATITUDE"), os.environ.get("HOME_LONGITUDE")
        source = "home location (activity has no GPS)"
        if not lat or not lon:
            print(json.dumps({"error": "Activity has no GPS; HOME_LATITUDE/HOME_LONGITUDE not set"}))
            return
    lat, lon = round(float(lat), 2), round(float(lon), 2)

    start = datetime.fromisoformat(s["startTimeLocal"][:19])
    end = start + timedelta(seconds=s.get("elapsedDuration") or s.get("duration") or 0)
    # The forecast API serves the recent past; older dates need the archive API.
    old = (date.today() - start.date()).days > 60
    base = "https://archive-api.open-meteo.com/v1/archive" if old else "https://api.open-meteo.com/v1/forecast"
    query = urllib.parse.urlencode({
        "latitude": lat, "longitude": lon, "hourly": HOURLY, "timezone": "auto",
        "start_date": start.date().isoformat(), "end_date": end.date().isoformat(),
    })
    with urllib.request.urlopen(f"{base}?{query}", timeout=20) as r:
        data = json.load(r)

    hourly = data["hourly"]
    first_hour = start.replace(minute=0, second=0, microsecond=0)
    rows = []
    for i, t in enumerate(hourly["time"]):
        if first_hour <= datetime.fromisoformat(t) <= end:
            row = {"time": t}
            row.update({k: v[i] for k, v in hourly.items() if k not in ("time", "weather_code")})
            row["conditions"] = CODES.get(hourly["weather_code"][i], str(hourly["weather_code"][i]))
            rows.append(row)

    units = {k: v for k, v in data.get("hourly_units", {}).items() if k not in ("time", "weather_code")}
    print(json.dumps({
        "activity_id": activity_id,
        "location": {"lat": lat, "lon": lon, "source": source},
        "start_local": start.isoformat(), "end_local": end.isoformat(),
        "units": units, "hourly": rows,
    }, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
