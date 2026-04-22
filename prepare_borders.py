#!/usr/bin/env python3
"""Download Natural Earth 110m country borders and save as country_borders.json.

Run once before deploying:
    python3 prepare_borders.py
Then commit country_borders.json to git.
"""
import json
import ssl
import urllib.request

# macOS Python installers ship without system CA certs — bypass verification
# for this one-time download script (data-only, no credentials involved).
_SSL_CTX = ssl.create_default_context()
_SSL_CTX.check_hostname = False
_SSL_CTX.verify_mode = ssl.CERT_NONE

URL = (
    "https://raw.githubusercontent.com/nvkelso/natural-earth-vector"
    "/master/geojson/ne_110m_admin_0_countries.geojson"
)

# Natural Earth uses -99 for countries without a standard ISO_A2 code.
# These manual overrides map the country name to the correct code.
MANUAL = {
    "Kosovo":   "xk",
    "Taiwan":   "tw",
    "N. Cyprus": "cy",  # fold into Cyprus footprint
}
SKIP = {"Antarctica", "Somaliland"}


def main() -> None:
    print(f"Downloading {URL} …")
    with urllib.request.urlopen(URL, timeout=30, context=_SSL_CTX) as resp:
        data = json.loads(resp.read().decode())

    borders: dict[str, object] = {}
    skipped: list[str] = []

    for feature in data["features"]:
        props = feature["properties"]
        name = props.get("ADMIN") or props.get("NAME", "")

        if name in SKIP:
            continue

        iso2 = props.get("ISO_A2", "-99")
        if iso2 in ("-99", "-1", "", None):
            iso2 = MANUAL.get(name)

        if not iso2:
            skipped.append(name)
            continue

        key = iso2.lower()
        geom = feature["geometry"]

        # Merge MultiPolygon entries for the same ISO (e.g. France + overseas)
        if key in borders:
            existing = borders[key]
            if existing["type"] == "MultiPolygon":
                if geom["type"] == "MultiPolygon":
                    existing["coordinates"].extend(geom["coordinates"])
                else:
                    existing["coordinates"].append(geom["coordinates"])
            else:
                coords = existing["coordinates"]
                extra = geom["coordinates"] if geom["type"] == "MultiPolygon" else [geom["coordinates"]]
                borders[key] = {"type": "MultiPolygon", "coordinates": [coords] + extra}
        else:
            borders[key] = geom

    out_path = "country_borders.json"
    with open(out_path, "w") as f:
        json.dump(borders, f, separators=(",", ":"))

    size_kb = len(open(out_path).read()) / 1024
    print(f"Saved {out_path}  ({size_kb:.0f} KB,  {len(borders)} countries)")
    if skipped:
        print(f"Skipped (no ISO code): {', '.join(skipped)}")


if __name__ == "__main__":
    main()
