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


def get_json(path, params, retries=3):
    url = f"{API}/{path}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": "snotel-guru-maps/1.0"})
    last = None
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.load(r)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
            last = e
            time.sleep(2 * (attempt + 1))
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


def fetch_latest(triplets, days=5):
    """Return {triplet: {"SNWD": (date, value), "WTEQ": (date, value)}}."""
    end = date.today()
    begin = end - timedelta(days=days)
    latest = {}
    triplets = list(triplets)
    for i in range(0, len(triplets), 50):
        chunk = triplets[i:i + 50]
        data = get_json("data", {
            "stationTriplets": ",".join(chunk),
            "elements": "SNWD,WTEQ",
            "duration": "DAILY",
            "beginDate": begin.isoformat(),
            "endDate": end.isoformat(),
        })
        for st in data:
            trip = st.get("stationTriplet")
            for d in st.get("data", []):
                code = d.get("stationElement", {}).get("elementCode")
                vals = [v for v in d.get("values", []) if v.get("value") is not None]
                if code and vals:
                    v = vals[-1]
                    latest.setdefault(trip, {})[code] = (v.get("date"), v["value"])
    return latest


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
    latest = fetch_latest(stations.keys())
    gj = build_geojson(stations, latest)
    with_depth = sum(1 for f in gj["features"] if "depth_in" in f["properties"])
    if with_depth == 0:
        sys.exit("No depth readings returned; not overwriting output.")
    with open(a.out, "w", encoding="utf-8") as f:
        json.dump(gj, f, separators=(",", ":"))
    print(f"Wrote {a.out}: {len(gj['features'])} stations, {with_depth} with depth", file=sys.stderr)
    if a.ms_embedded:
        write_embedded_ms(a.ms_embedded, gj)
        print(f"Wrote {a.ms_embedded}", file=sys.stderr)


if __name__ == "__main__":
    main()
