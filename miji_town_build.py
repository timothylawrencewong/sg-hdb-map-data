#!/usr/bin/env python3
"""
Miji town pages: build the "every sale in this town" data file behind a town page
(for example miji.sg/towns/bukit-panjang).

What it does, for each town it is asked to build (default: Bukit Panjang):
  1. HDB: every resale transaction in that town since Jan 2017 (block, street, flat type,
     storey range, floor area, lease start year, price, month). This is the same data.gov.sg
     file miji_index_build.py already downloads, so it reuses that script's saved copy
     (no second download).
  2. Condos and landed homes: every single-unit private sale URA gives us (about the last 5 years)
     for projects that physically sit in that town. URA's postal districts do not line up with HDB
     towns (District 23 covers Bukit Panjang AND Bukit Batok AND Choa Chu Kang), so a project is
     counted as "in the town" only if it is within MAX_PRIVATE_DISTANCE_M of one of the town's own
     HDB blocks. The log lists every project that was included and every near miss, so you can check
     the list once and fix it with INCLUDE_PROJECTS / EXCLUDE_PROJECTS below.
  3. Writes town_data/<slug>.json, in a compact shape the Miji town page reads.

Neither HDB nor URA publish the exact unit or the exact day of sale. The page only ever shows what
they publish: the month, and a storey / floor RANGE.

Needs, in the folder it runs from (the GitHub Actions job already has all of these by then):
  - miji_index_build.py        (imported, for the HDB download + helpers)
  - data/towns.json and data/<slug>.json   (the HDB block list with coordinates, from sg_build.py)
  - private_data/d*.json       (from ura_build.py; if missing, the private tabs are just left empty)

How to run:
    python3 miji_town_build.py                  # Bukit Panjang only
    python3 miji_town_build.py "Bukit Panjang"  # same, explicit
    python3 miji_town_build.py "Toa Payoh" "Bishan"
Standard library only.
"""

import json
import math
import os
import re
import sys
import time
from collections import defaultdict

import miji_index_build as mib

OUT_DIR = "town_data"
DATA_DIR = "data"
MAX_PRIVATE_DISTANCE_M = 350   # a private project this close to one of the town's HDB blocks counts as "in the town"
NEAR_MISS_DISTANCE_M = 800     # projects between the two distances are only logged, so you can review them
SQM_TO_SQFT = 10.7639
MIN_HDB_ROWS = 1500            # safety net: a real town has thousands; fewer means the download or filter went wrong

# Manual fixes, by town, after you have looked at the log once. Project names exactly as the log prints them.
INCLUDE_PROJECTS = {
    # "Bukit Panjang": ["Some Project Name"],
}
EXCLUDE_PROJECTS = {
    # "Bukit Panjang": ["Some Project Name"],
}

HDB_TYPE_MAP = {
    "1 ROOM": "1-room", "2 ROOM": "2-room", "3 ROOM": "3-room", "4 ROOM": "4-room",
    "5 ROOM": "5-room", "EXECUTIVE": "Executive", "MULTI-GENERATION": "Multi-gen", "MULTI GENERATION": "Multi-gen",
}
TYPE_ORDER = ["1-room", "2-room", "3-room", "4-room", "5-room", "Executive", "Multi-gen"]
SALE_LABEL = {1: "New sale", 2: "Sub sale", 3: "Resale"}

say = mib.say


def slugify(name):
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def haversine_m(lat1, lon1, lat2, lon2):
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def load_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def town_slug_and_blocks(town):
    """The slug used by the HDB map data, and that town's block coordinates (lat, lon)."""
    slug = slugify(town)
    towns_path = os.path.join(DATA_DIR, "towns.json")
    if os.path.exists(towns_path):
        try:
            for t in load_json(towns_path).get("towns", []):
                if (t.get("name") or "").strip().lower() == town.lower():
                    slug = t.get("slug") or slug
                    break
        except Exception as e:
            say("   NOTE: could not read %s (%s: %s); using slug %r" % (towns_path, type(e).__name__, e, slug))
    blocks = []
    path = os.path.join(DATA_DIR, slug + ".json")
    if os.path.exists(path):
        try:
            for b in load_json(path).get("B", []):
                la, lo = b.get("a"), b.get("o")
                if isinstance(la, (int, float)) and isinstance(lo, (int, float)) and la and lo:
                    blocks.append((la, lo))
        except Exception as e:
            say("   NOTE: could not read %s (%s: %s)" % (path, type(e).__name__, e))
    return slug, blocks


# ------------------------------------------------------------------ HDB
def build_hdb(town):
    path = mib.download_hdb_csv()
    want = town.strip().upper()
    streets, street_ix = [], {}
    types, type_ix = [], {}
    storeys, storey_ix = [], {}
    rows = []
    rows_seen = 0
    for row in mib.parse_csv_rows(path):
        rows_seen += 1
        if (row.get("town") or "").strip().upper() != want:
            continue
        ft = HDB_TYPE_MAP.get((row.get("flat_type") or "").strip().upper())
        if not ft:
            continue
        month_s = (row.get("month") or "").strip()
        if len(month_s) < 7 or not month_s[:4].isdigit() or not month_s[5:7].isdigit():
            continue
        ym = int(month_s[:4]) * 100 + int(month_s[5:7])
        try:
            price = int(float(row.get("resale_price") or 0))
            sqm = float(row.get("floor_area_sqm") or 0)
        except ValueError:
            continue
        if price <= 0 or sqm <= 0:
            continue
        try:
            lease = int(float(row.get("lease_commence_date") or 0))
        except ValueError:
            lease = 0
        blk = (row.get("block") or "").strip()
        st = (row.get("street_name") or "").strip().title()
        sr = (row.get("storey_range") or "").strip()
        if not blk or not st:
            continue
        for lst, ix, v in ((streets, street_ix, st), (types, type_ix, ft), (storeys, storey_ix, sr)):
            if v not in ix:
                ix[v] = len(lst)
                lst.append(v)
        rows.append([ym, blk, street_ix[st], type_ix[ft], storey_ix[sr], round(sqm, 1), lease, price])
    if rows_seen < 100000:
        raise SystemExit("STOP: only read %d rows from the HDB file (expected several hundred thousand)." % rows_seen)
    if len(rows) < MIN_HDB_ROWS:
        raise SystemExit("STOP: only %d HDB sales found for %s (expected thousands). Check the town name." % (len(rows), town))
    rows.sort(key=lambda r: (-r[0], r[1]))
    # Keep flat types in a sensible order, and re-point the type index to match.
    ordered = [t for t in TYPE_ORDER if t in type_ix] + [t for t in types if t not in TYPE_ORDER]
    remap = {type_ix[t]: i for i, t in enumerate(ordered)}
    for r in rows:
        r[3] = remap[r[3]]
    say("   HDB: %d sales in %s, %s to %s" % (len(rows), town, min(r[0] for r in rows), max(r[0] for r in rows)))
    return {
        "cols": ["ym", "blk", "street", "type", "storey", "sqm", "lease", "price"],
        "streets": streets, "types": ordered, "storeys": storeys,
        "rows": rows,
    }


# ------------------------------------------------------------------ private (condo + landed)
def build_private(town, blocks):
    empty = {"cols": [], "projects": [], "types": [], "tenures": [], "rows": [], "status": "none"}
    if not blocks:
        say("   Private: no HDB block coordinates for %s (data/<slug>.json missing), so condo and landed were left empty." % town)
        empty["status"] = "no-block-coordinates"
        return empty
    if not os.path.isdir(mib.PRIVATE_DIR):
        say("   Private: no %s folder, so condo and landed were left empty." % mib.PRIVATE_DIR)
        empty["status"] = "no-private-data"
        return empty

    inc = set(INCLUDE_PROJECTS.get(town, []))
    exc = set(EXCLUDE_PROJECTS.get(town, []))
    # a coarse bounding box first, so we do not measure every project against every block
    pad = 0.02
    lat_lo = min(b[0] for b in blocks) - pad
    lat_hi = max(b[0] for b in blocks) + pad
    lon_lo = min(b[1] for b in blocks) - pad
    lon_hi = max(b[1] for b in blocks) + pad

    projects, proj_ix = [], {}
    types, type_ix = [], {}
    tenures, tenure_ix = [], {}
    rows = []
    included, near, ungeocoded = [], [], 0
    for name in sorted(os.listdir(mib.PRIVATE_DIR)):
        if not (name.startswith("d") and name.endswith(".json")) or name == "index.json":
            continue
        try:
            data = load_json(os.path.join(mib.PRIVATE_DIR, name))
        except Exception as e:
            say("   NOTE: skipped %s (%s: %s)" % (name, type(e).__name__, e))
            continue
        d_types = data.get("types", [])
        d_tenures = data.get("tenures", [])
        for p in data.get("P", []):
            pname = (p.get("n") or "").strip()
            pstreet = (p.get("s") or "").strip()
            label = pname or pstreet
            la, lo = p.get("la"), p.get("ln")
            if not isinstance(la, (int, float)) or not isinstance(lo, (int, float)) or not la or not lo:
                ungeocoded += 1
                dist = None
            elif not (lat_lo <= la <= lat_hi and lon_lo <= lo <= lon_hi):
                continue
            else:
                dist = min(haversine_m(la, lo, b[0], b[1]) for b in blocks)
            in_town = (dist is not None and dist <= MAX_PRIVATE_DISTANCE_M) or label in inc
            if label in exc:
                in_town = False
            if not in_town:
                if dist is not None and dist <= NEAR_MISS_DISTANCE_M:
                    near.append((label, pstreet, int(dist), name[:3]))
                continue
            included.append((label, pstreet, int(dist) if dist is not None else -1, name[:3]))
            for t in p.get("T", []):
                # [ym, price, sqm, floor, sale, type_idx, tenure_idx, units, areaType_idx]
                if len(t) < 8 or t[7] != 1:
                    continue
                ym, price, sqm, floor, sale = t[0], t[1], t[2], t[3], t[4]
                if not price or price <= 0 or not sqm or sqm <= 0:
                    continue
                ptype = d_types[t[5]] if 0 <= t[5] < len(d_types) else ""
                tenure = d_tenures[t[6]] if 0 <= t[6] < len(d_tenures) else ""
                key = (label, pstreet)
                if key not in proj_ix:
                    proj_ix[key] = len(projects)
                    projects.append({"n": label, "s": pstreet})
                if ptype not in type_ix:
                    type_ix[ptype] = len(types)
                    types.append({"n": ptype, "g": mib.group_of(ptype)})
                if tenure not in tenure_ix:
                    tenure_ix[tenure] = len(tenures)
                    tenures.append(tenure)
                rows.append([ym, proj_ix[key], type_ix[ptype], tenure_ix[tenure], int(sale or 0),
                             floor or "", round(sqm, 1), int(price)])
    rows.sort(key=lambda r: -r[0])
    say("   Private: %d single-unit sales across %d projects within %dm of %s's HDB blocks" %
        (len(rows), len(projects), MAX_PRIVATE_DISTANCE_M, town))
    if ungeocoded:
        say("      (%d projects had no map position and were skipped unless listed in INCLUDE_PROJECTS)" % ungeocoded)
    say("   --- REVIEW: projects counted as inside %s (name | street | metres from nearest HDB block | district file) ---" % town)
    for x in sorted(set(included)):
        say("      IN   %s | %s | %dm | %s" % x)
    say("   --- REVIEW: near misses, NOT counted (add to INCLUDE_PROJECTS if they really are in %s) ---" % town)
    for x in sorted(set(near), key=lambda z: z[2])[:60]:
        say("      OUT  %s | %s | %dm | %s" % x)
    return {
        "cols": ["ym", "project", "type", "tenure", "sale", "floor", "sqm", "price"],
        "projects": projects, "types": types, "tenures": tenures,
        "sale_labels": {str(k): v for k, v in SALE_LABEL.items()},
        "rows": rows, "status": "ok",
    }


# ------------------------------------------------------------------ main
def build_one(town):
    say("")
    say("== %s ==" % town)
    slug, blocks = town_slug_and_blocks(town)
    say("   slug %s, %d HDB blocks with coordinates" % (slug, len(blocks)))
    hdb = build_hdb(town)
    private = build_private(town, blocks)
    payload = {
        "v": 1,
        "town": town,
        "slug": slug,
        "built": time.strftime("%Y-%m-%d"),
        "hdb_through_ym": max(r[0] for r in hdb["rows"]),
        "n_blocks": len(blocks),
        "hdb": hdb,
        "private": private,
        "sources": {
            "hdb": "Housing & Development Board (HDB), Resale flat prices, via data.gov.sg (Open Data Licence). "
                   "Every registered resale transaction since Jan 2017. HDB publishes the month of registration "
                   "and a storey range, not the exact day or unit.",
            "private": "Urban Redevelopment Authority (URA), Private Residential Property Transactions, via the URA "
                       "Data Service. Single-unit sales for roughly the last 5 years, for projects within %dm of the "
                       "town's HDB blocks. URA publishes the month of sale and a floor range, not the exact unit. "
                       "Area for landed homes is usually land area." % MAX_PRIVATE_DISTANCE_M,
        },
    }
    os.makedirs(OUT_DIR, exist_ok=True)
    out_path = os.path.join(OUT_DIR, slug + ".json")
    tmp = out_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, out_path)
    say("   wrote %s (%.0f KB)" % (out_path, os.path.getsize(out_path) / 1024.0))
    return slug


def main():
    towns = sys.argv[1:] or ["Bukit Panjang"]
    say("Miji town page data builder | %s" % time.strftime("%Y-%m-%d %H:%M"))
    done = []
    for t in towns:
        done.append(build_one(t))
    say("")
    say("Done: %s" % ", ".join(done))


if __name__ == "__main__":
    code = 0
    try:
        main()
    except SystemExit as e:
        say(str(e))
        code = 1
    except Exception as e:
        say("STOPPED WITH AN ERROR: %s: %s" % (type(e).__name__, e))
        code = 1
    try:
        with open("miji_town_build_log.txt", "w", encoding="utf-8") as f:
            f.write("\n".join(mib.LOG))
    except Exception:
        pass
    sys.exit(code)
