#!/usr/bin/env python3
"""
Build a SNOTEL snow-depth GeoJSON for Guru Maps.

Pulls active SNOTEL stations and their latest daily snow depth (SNWD) and
snow water equivalent (WTEQ) from the NRCS AWDB REST API, then writes:

  snotel.geojson   - point per station; label = depth in inches
  (optional) --ms-embedded FILE : a self-contained .ms with the data baked in

Standard library only. Usage:
  python3 snotel_geojson.py                       # Utah only
  python3 snotel_geojson.py --states UT,CO,ID,WY  # several states
  python3 snotel_geojson.py --ms-embedded SNOTEL-Snow-Embedded.ms
"""
import argparse
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone

API = "https://wcc.sc.egov.usda.gov/awdbRestApi/services/v1"

# MapCSS used both by the hosted .ms template and the embedded .ms
STYLE = """node {
    icon-image: "poi_circle_small.svg";
    icon-tint: #6E7B8B;
    icon-scale: 1.4;
    image-allow-overlap: true;
    text: eval(tag("label"));
    font-size: 12;
    font-weight: bold;
    text-color: #0B3C5D;
    font-stroke-width: 2px;
    font-stroke-color: #FFFFFFD9;
    details-text: eval(tag("name"));
    details-description: eval(tag("details"));
    [depth_in>=1] { icon-tint: #5DADE2; }
    [depth_in>=12] { icon-tint: #2E86C1; }
    [depth_in>=36] { icon-tint: #1B4F72; }
}
"""


def get_json(path, params, retries=5):
    url = f"{API}/{path}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": "snotel-guru-maps/1.1"})
    last = None
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=45) as r:
                return json.load(r)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
            last = e
            wait = 5 * (2 ** attempt)  # 5, 10, 20, 40, 80 s
            print(f"  attempt {attempt + 1}/{retries} failed ({e}); "
                  f"{'retrying in ' + str(wait) + 's' if attempt < retries - 1 else 'giving up'}",
                  file=sys.stderr)
            if attempt < retries - 1:
                time.sleep(wait)
    raise RuntimeError(f"AWDB request failed: {url}\n{last}")


def fetch_stations(states):
    out = {}
    for st in states:
        data = get_json("stations", {
            "stationTriplets": f"*:{st}:SNTL",
            "activeOnly": "true",
        })
        for s in data:
            if s.get("latitude") is None or s.get("longitude") is None:
                continue
            out[s["stationTriplet"]] = s
        print(f"{st}: {sum(1 for t in out if t.endswith(f':{st}:SNTL'))} stations", file=sys.stderr)
    return out


def _latest_from(data, latest, overwrite):
    for st in data:
        trip = st.get("stationTriplet")
        for d in st.get("data", []):
            code = d.get("stationElement", {}).get("elementCode")
            vals = [v for v in d.get("values", []) if v.get("value") is not None]
            if not (code and vals):
                continue
            v = vals[-1]
            slot = latest.setdefault(trip, {})
            if overwrite or code not in slot:
                slot[code] = (v.get("date"), v["value"])


def fetch_latest(triplets, days=5):
    """Return ({triplet: {"SNWD": (stamp, value), "WTEQ": (stamp, value)}}, failed).

    Daily values are fetched first as a baseline, then hourly values replace
    them where available so the map shows the most recent reading. A chunk
    that fails does not abort the run: its stations are returned in `failed`
    so the caller can keep their previous values.
    """
    today = date.today()
    latest, failed = {}, set()
    triplets = list(triplets)
    for i in range(0, len(triplets), 25):
        group = triplets[i:i + 25]
        chunk = ",".join(group)
        got_any = False
        try:
            data = get_json("data", {
                "stationTriplets": chunk,
                "elements": "SNWD,WTEQ",
                "duration": "DAILY",
                "beginDate": (today - timedelta(days=days)).isoformat(),
                "endDate": today.isoformat(),
            })
            _latest_from(data, latest, overwrite=False)
            got_any = True
        except RuntimeError as e:
            print(f"Daily fetch failed for a chunk: {e}", file=sys.stderr)
        try:
            hourly = get_json("data", {
                "stationTriplets": chunk,
                "elements": "SNWD,WTEQ",
                "duration": "HOURLY",
                "beginDate": f"{(today - timedelta(days=1)).isoformat()} 00:00",
                "endDate": f"{(today + timedelta(days=1)).isoformat()} 00:00",
            })
            _latest_from(hourly, latest, overwrite=True)
            got_any = True
        except RuntimeError as e:
            print(f"Hourly fetch failed for a chunk: {e}", file=sys.stderr)
        if not got_any:
            failed.update(group)
    return latest, failed


def fmt(v):
    return f"{v:.0f}" if abs(v - round(v)) < 0.05 else f"{v:.1f}"


def build_geojson(stations, latest):
    feats = []
    for trip, s in sorted(stations.items(), key=lambda kv: kv[1].get("name", "")):
        snwd = latest.get(trip, {}).get("SNWD")
        wteq = latest.get(trip, {}).get("WTEQ")
        if snwd:
            label = f'{fmt(snwd[1])}"'
            depth = fmt(snwd[1])
        else:
            label, depth = "-", ""
        lines = []
        lines.append(f"Snow depth: {fmt(snwd[1])} in ({snwd[0]})" if snwd else "Snow depth: no report")
        if wteq:
            lines.append(f"Snow water equivalent: {fmt(wteq[1])} in ({wteq[0]})")
        if s.get("elevation") is not None:
            lines.append(f"Elevation: {s['elevation']:,.0f} ft")
        lines.append(f"Station {trip}")
        props = {
            "name": f"SNOTEL: {s.get('name', trip)}",
            "label": label,
            "details": "\n".join(lines),
            "station": trip,
        }
        if depth != "":
            props["depth_in"] = depth
        feats.append({
            "type": "Feature",
            "properties": props,
            "geometry": {"type": "Point",
                         "coordinates": [round(s["longitude"], 5), round(s["latitude"], 5)]},
        })
    return {
        "type": "FeatureCollection",
        "updated": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%MZ"),
        "features": feats,
    }


def write_embedded_ms(path, gj):
    ms = f"""<?xml version="1.0" encoding="UTF-8"?>
<customMapSource overlay="true">
<name>SNOTEL Snow Depth (snapshot {gj['updated'][:10]})</name>
<minZoom>7</minZoom>
<geojson><![CDATA[
{json.dumps(gj, separators=(',', ':'))}
]]></geojson>
<style><![CDATA[
{STYLE}]]></style>
</customMapSource>
"""
    with open(path, "w", encoding="utf-8") as f:
        f.write(ms)


def load_previous(path):
    try:
        with open(path, encoding="utf-8") as f:
            gj = json.load(f)
        return {ft["properties"].get("station"): ft for ft in gj.get("features", [])}
    except (OSError, ValueError, KeyError):
        return {}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--states", default="UT", help="comma-separated state codes (default UT)")
    ap.add_argument("--out", default="snotel.geojson")
    ap.add_argument("--ms-embedded", metavar="FILE", help="also write a self-contained .ms snapshot")
    a = ap.parse_args()

    states = [s.strip().upper() for s in a.states.split(",") if s.strip()]
    stations = fetch_stations(states)
    if not stations:
        sys.exit("No stations returned; not overwriting output.")
    latest, failed = fetch_latest(stations.keys())
    gj = build_geojson(stations, latest)

    if failed:
        prev = load_previous(a.out)
        kept = 0
        for i, ft in enumerate(gj["features"]):
            trip = ft["properties"].get("station")
            if trip in failed and trip in prev:
                gj["features"][i] = prev[trip]
                kept += 1
        print(f"{len(failed)} stations could not be refreshed; kept previous values for {kept}.",
              file=sys.stderr)

    fresh = sum(1 for f in gj["features"]
                if "depth_in" in f["properties"] and f["properties"].get("station") not in failed)
    if fresh == 0:
        sys.exit("No fresh depth readings returned; not overwriting output.")
    with open(a.out, "w", encoding="utf-8") as f:
        json.dump(gj, f, separators=(",", ":"))
    print(f"Wrote {a.out}: {len(gj['features'])} stations, {fresh} freshly updated", file=sys.stderr)
    if a.ms_embedded:
        write_embedded_ms(a.ms_embedded, gj)
        print(f"Wrote {a.ms_embedded}", file=sys.stderr)


if __name__ == "__main__":
    main()
