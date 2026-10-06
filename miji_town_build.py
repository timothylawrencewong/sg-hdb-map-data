#!/usr/bin/env python3
"""
Miji town pages: build the "every sale in this town" data files behind the town pages
(for example miji.sg/towns/bukit-panjang).

What it does, for EVERY HDB town (or only the towns you name):
  1. HDB: every resale transaction in the town since Jan 2017 (block, street, flat type,
     storey range, floor area, lease start year, price, month). This is the same data.gov.sg
     file miji_index_build.py already downloads. It is read once and split by town.
  2. Condos and landed homes: every single-unit private sale URA gives us (about the last 5 years)
     for projects that physically sit in the town. URA's postal districts do not line up with HDB
     towns (District 23 covers Bukit Panjang AND Bukit Batok AND Choa Chu Kang), so a project is
     counted as "in the town" only if it is within MAX_PRIVATE_DISTANCE_M of one of the town's own
     HDB blocks. The log lists the projects counted and the near misses, so you can check the list
     and fix it with INCLUDE_PROJECTS / EXCLUDE_PROJECTS below.
  3. Writes town_data/<slug>.json for each town, plus town_data/index.json (the list of towns).

Neither HDB nor URA publish the exact unit or the exact day of sale. The pages only ever show what
they publish: the month, and a storey / floor RANGE.

One town failing never stops the others. The run only fails if fewer than MIN_TOWNS towns were built.

Needs, in the folder it runs from (the GitHub Actions job already has all of these):
  - miji_index_build.py        (imported, for the HDB download + helpers)
  - data/towns.json and data/<map slug>.json   (the HDB block list with coordinates, from sg_build.py)
  - private_data/d*.json       (from ura_build.py; if missing, the condo and landed tabs are left empty)

How to run:
    python3 miji_town_build.py                      # every town
    python3 miji_town_build.py "Bukit Panjang"      # just one
    python3 miji_town_build.py "Toa Payoh" "Bishan"
Standard library only.
"""

import json
import math
import os
import re
import sys
import time

import miji_index_build as mib

OUT_DIR = "town_data"
DATA_DIR = "data"
MAX_PRIVATE_DISTANCE_M = 350   # a private project this close to one of the town's HDB blocks counts as "in the town"
NEAR_MISS_DISTANCE_M = 800     # projects between the two distances are only logged, so you can review them
SQM_TO_SQFT = 10.7639
MIN_HDB_ROWS = 50              # a town with fewer sales than this is skipped (something is wrong with the data or name)
MIN_TOWNS = 20                 # the run fails if fewer towns than this were built

# Manual fixes, by town, after you have looked at the log. Project names exactly as the log prints them.
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
_PRIVATE_CACHE = {}


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


def map_towns():
    """{lower-case town name: map slug} from the HDB map data, if it is there."""
    out = {}
    path = os.path.join(DATA_DIR, "towns.json")
    if os.path.exists(path):
        try:
            for t in load_json(path).get("towns", []):
                nm = (t.get("name") or "").strip().lower()
                if nm and t.get("slug"):
                    out[nm] = t["slug"]
        except Exception as e:
            say("   NOTE: could not read %s (%s: %s)" % (path, type(e).__name__, e))
    return out


def town_blocks(town, map_slugs):
    """That town's HDB block coordinates (lat, lon), from the map data."""
    slug = map_slugs.get(town.lower()) or slugify(town)
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
    return blocks


# ------------------------------------------------------------------ HDB
def ensure_hdb_csv(tries=40, wait=15):
    """data.gov.sg does not hand over the file straight away: it has to prepare the download first
    (status STARTING), then gives the link. A job that nobody has asked data.gov.sg to prepare a file
    for can sit on STARTING, so this first asks it to START preparing (initiate-download), then
    checks every few seconds until the link arrives, then saves the file where
    miji_index_build.py's own downloader looks for it, so the normal download call right after this
    just uses the saved copy."""
    path = mib.hdb_cache_path()
    if os.path.exists(path) and (time.time() - os.path.getmtime(path)) < mib.CACHE_HOURS * 3600:
        say("Using the HDB resale copy saved earlier today (%s)" % path)
        return
    os.makedirs(mib.CACHE_DIR, exist_ok=True)
    poll_url = mib.API_BASE % mib.HDB_DATASET_ID
    start_url = poll_url.replace("poll-download", "initiate-download")
    try:
        d0 = mib.get_json(start_url, tries=2, wait=5, label="HDB dataset initiate-download")
        say("   asked data.gov.sg to start preparing the HDB file: %s" % json.dumps(d0)[:200])
    except Exception as e:
        say("   NOTE: could not send the start request (%s: %s). Carrying on with checking." % (type(e).__name__, e))
    url = ""
    for i in range(1, tries + 1):
        d = mib.get_json(poll_url, label="HDB dataset poll-download")
        url = ((d.get("data") or {}).get("url") or "")
        if url:
            break
        if i % 4 == 1:
            say("   data.gov.sg says: %s" % json.dumps(d)[:200])
        say("   still preparing the HDB file. Waiting %ds, check %d of %d..." % (wait, i, tries))
        time.sleep(wait)
    if not url:
        raise SystemExit("data.gov.sg still had not prepared the HDB resale file after %d checks (about %d minutes). "
                         "Last reply: %s" % (tries, tries * wait // 60, json.dumps(d)[:300]))
    say("Downloading the full HDB resale history...")
    raw = mib.http_get(url, timeout=300)
    with open(path, "wb") as f:
        f.write(raw)
    say("   saved %.1f MB" % (len(raw) / 1e6))


def read_hdb_all():
    """Reads the HDB file once. Returns ({TOWN UPPER: [raw rows]}, {TOWN UPPER: display name})."""
    ensure_hdb_csv()
    path = mib.download_hdb_csv()
    say("Reading every HDB resale sale and splitting it by town...")
    by_town, names = {}, {}
    rows_seen = 0
    for row in mib.parse_csv_rows(path):
        rows_seen += 1
        town_raw = (row.get("town") or "").strip()
        if not town_raw:
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
        if not blk or not st:
            continue
        key = town_raw.upper()
        names.setdefault(key, town_raw.title())
        by_town.setdefault(key, []).append((ym, blk, st, ft, (row.get("storey_range") or "").strip(), round(sqm, 1), lease, price))
    if rows_seen < 100000:
        raise SystemExit("STOP: only read %d rows from the HDB file (expected several hundred thousand)." % rows_seen)
    say("   %d rows read, %d towns found" % (rows_seen, len(by_town)))
    return by_town, names


def pack_hdb(raw_rows):
    streets, street_ix = [], {}
    types, type_ix = [], {}
    storeys, storey_ix = [], {}
    rows = []
    for ym, blk, st, ft, sr, sqm, lease, price in raw_rows:
        for lst, ix, v in ((streets, street_ix, st), (types, type_ix, ft), (storeys, storey_ix, sr)):
            if v not in ix:
                ix[v] = len(lst)
                lst.append(v)
        rows.append([ym, blk, street_ix[st], type_ix[ft], storey_ix[sr], sqm, lease, price])
    rows.sort(key=lambda r: (-r[0], r[1]))
    # Keep flat types in a sensible order, and re-point the type index to match.
    ordered = [t for t in TYPE_ORDER if t in type_ix] + [t for t in types if t not in TYPE_ORDER]
    remap = {type_ix[t]: i for i, t in enumerate(ordered)}
    for r in rows:
        r[3] = remap[r[3]]
    return {
        "cols": ["ym", "blk", "street", "type", "storey", "sqm", "lease", "price"],
        "streets": streets, "types": ordered, "storeys": storeys,
        "rows": rows,
    }


# ------------------------------------------------------------------ private (condo + landed)
def private_files():
    """Every private_data/d##.json, read once and kept for all towns."""
    if "files" in _PRIVATE_CACHE:
        return _PRIVATE_CACHE["files"]
    files = []
    if os.path.isdir(mib.PRIVATE_DIR):
        for name in sorted(os.listdir(mib.PRIVATE_DIR)):
            if not (name.startswith("d") and name.endswith(".json")) or name == "index.json":
                continue
            try:
                files.append((name, load_json(os.path.join(mib.PRIVATE_DIR, name))))
            except Exception as e:
                say("   NOTE: skipped %s (%s: %s)" % (name, type(e).__name__, e))
    _PRIVATE_CACHE["files"] = files
    return files


def build_private(town, blocks, verbose):
    empty = {"cols": [], "projects": [], "types": [], "tenures": [], "rows": [], "status": "none"}
    if not blocks:
        say("   Private: no HDB block coordinates for %s, so condo and landed were left empty." % town)
        empty["status"] = "no-block-coordinates"
        return empty
    files = private_files()
    if not files:
        say("   Private: no %s data found, so condo and landed were left empty." % mib.PRIVATE_DIR)
        empty["status"] = "no-private-data"
        return empty

    inc = set(INCLUDE_PROJECTS.get(town, []))
    exc = set(EXCLUDE_PROJECTS.get(town, []))
    pad = 0.02   # coarse bounding box first, so we do not measure every project against every block
    lat_lo = min(b[0] for b in blocks) - pad
    lat_hi = max(b[0] for b in blocks) + pad
    lon_lo = min(b[1] for b in blocks) - pad
    lon_hi = max(b[1] for b in blocks) + pad

    projects, proj_ix = [], {}
    types, type_ix = [], {}
    tenures, tenure_ix = [], {}
    rows = []
    included, near, ungeocoded = [], [], 0
    for name, data in files:
        d_types = data.get("types", [])
        d_tenures = data.get("tenures", [])
        for p in data.get("P", []):
            pname = (p.get("n") or "").strip()
            pstreet = (p.get("s") or "").strip()
            label = pname or pstreet
            la, lo = p.get("la"), p.get("ln")
            if not isinstance(la, (int, float)) or not isinstance(lo, (int, float)) or not la or not lo:
                dist = None
                if label not in inc:
                    ungeocoded += 1
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
    say("   Private: %d single-unit sales across %d projects within %dm of the town's HDB blocks" %
        (len(rows), len(projects), MAX_PRIVATE_DISTANCE_M))
    inc_sorted = sorted(set(included))
    near_sorted = sorted(set(near), key=lambda z: z[2])
    if verbose:
        say("   --- REVIEW: projects counted as inside %s (name | street | metres from nearest HDB block | district file) ---" % town)
        for x in inc_sorted:
            say("      IN   %s | %s | %dm | %s" % x)
        say("   --- REVIEW: near misses, NOT counted (add to INCLUDE_PROJECTS if they really are in %s) ---" % town)
        for x in near_sorted[:60]:
            say("      OUT  %s | %s | %dm | %s" % x)
    else:
        say("   IN (%d): %s" % (len(inc_sorted), "; ".join("%s (%dm)" % (x[0], x[2]) for x in inc_sorted[:40]) or "none"))
        if near_sorted:
            say("   OUT near misses (%d): %s" % (len(near_sorted), "; ".join("%s (%dm)" % (x[0], x[2]) for x in near_sorted[:12])))
    return {
        "cols": ["ym", "project", "type", "tenure", "sale", "floor", "sqm", "price"],
        "projects": projects, "types": types, "tenures": tenures,
        "sale_labels": {str(k): v for k, v in SALE_LABEL.items()},
        "rows": rows, "status": "ok",
    }


# ------------------------------------------------------------------ main
def write_json(path, payload):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, path)


def build_one(town, raw_rows, map_slugs, verbose):
    say("")
    say("== %s ==" % town)
    if len(raw_rows) < MIN_HDB_ROWS:
        raise RuntimeError("only %d HDB sales found for %s" % (len(raw_rows), town))
    slug = slugify(town)
    blocks = town_blocks(town, map_slugs)
    hdb = pack_hdb(raw_rows)
    say("   %d HDB sales, %s to %s, %d blocks with map coordinates" %
        (len(hdb["rows"]), min(r[0] for r in hdb["rows"]), max(r[0] for r in hdb["rows"]), len(blocks)))
    private = build_private(town, blocks, verbose)
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
    out_path = os.path.join(OUT_DIR, slug + ".json")
    write_json(out_path, payload)
    say("   wrote %s (%.0f KB)" % (out_path, os.path.getsize(out_path) / 1024.0))
    return {
        "name": town, "slug": slug,
        "hdb_sales": len(hdb["rows"]), "hdb_through_ym": payload["hdb_through_ym"],
        "private_sales": len(private["rows"]), "private_status": private["status"],
    }


def main():
    say("Miji town page data builder | %s" % time.strftime("%Y-%m-%d %H:%M"))
    by_town, names = read_hdb_all()
    wanted = sys.argv[1:]
    if wanted:
        keys = []
        for w in wanted:
            k = w.strip().upper()
            if k not in by_town:
                say("NOTE: %r was not found in the HDB data, so it was skipped." % w)
                continue
            keys.append(k)
    else:
        keys = sorted(by_town)
    os.makedirs(OUT_DIR, exist_ok=True)
    map_slugs = map_towns()
    verbose = len(keys) == 1
    built, failed = [], []
    for k in keys:
        town = names[k]
        try:
            built.append(build_one(town, by_town[k], map_slugs, verbose))
        except Exception as e:
            failed.append(town)
            say("   SKIPPED %s: %s: %s" % (town, type(e).__name__, e))
    if built and not wanted:
        write_json(os.path.join(OUT_DIR, "index.json"), {
            "built": time.strftime("%Y-%m-%d"),
            "towns": sorted(built, key=lambda t: t["name"]),
        })
    say("")
    say("Done: %d towns built%s" % (len(built), (", %d skipped: %s" % (len(failed), ", ".join(failed))) if failed else ""))
    if not wanted and len(built) < MIN_TOWNS:
        raise SystemExit("STOP: only %d towns were built (expected %d or more)." % (len(built), MIN_TOWNS))
    if wanted and not built:
        raise SystemExit("STOP: none of the requested towns could be built.")


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
