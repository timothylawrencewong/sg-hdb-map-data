#!/usr/bin/env python3
"""
Property map, whole Singapore: build one small data file per HDB town.

What it does (read-only, public data, no keys or logins):
  1. Downloads the last 12 months of HDB resale sales, all towns (data.gov.sg)
  2. Downloads HDB's own outlines of every block ("HDB Existing Building"), the block list
     ("HDB Property Information"), MRT/LRT exits (LTA), hawker centres (NEA), schools (MOE)
     and the supermarket list (SFA)
  3. Works out each block's position from HDB's outlines. It asks OneMap for ONE address per
     street (not per block) to link a street name to its outline data, so it needs about a
     tenth of the lookups the Bukit Panjang run needed
  4. For every block: nearest MRT and LRT, nearest hawker centres, primary schools within
     1 km and 2 km, nearest secondary schools, sold prices, storeys and units
  5. Saves one file per town in a folder called sg_data, plus an index, towns.json

Everything it downloads is saved in a folder called sg_cache, so re-running is quick.
Nothing is changed on any website. Standard Python only, nothing to install.

How to run (Terminal), from the folder that holds this file:
    python3 sg_build.py --limit 8        <- quick test on Bukit Panjang (a few minutes)
    python3 sg_build.py --town "TAMPINES"   <- one town
    caffeinate -i python3 sg_build.py    <- every town (first run can take an hour or more)
"""

import json
import math
import os
import re
import shutil
import ssl
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict

# ------------------------------------------------------------------ settings
SALES_MONTHS = 12
WALK_DETOUR = 1.3
WALK_M_PER_MIN = 80
SCHOOL_NEAR_1 = 1000
SCHOOL_NEAR_2 = 2000
MARGIN_KM = 3.0
MAX_STREET_MATCH_M = 150      # a street's outline must be within this of the address OneMap gives
PAUSE_ONEMAP = 1.0
CACHE_DAYS = 7
CACHE_DIR = "sg_cache"
OUT_DIR = "sg_data"

UA = {"User-Agent": "Mozilla/5.0 (property-map-builder)"}
DS_RESALE = "d_8b84c4ee58e3cfc0ece0d773c8ca6abc"
DS_SCHOOLS = "d_688b934f82c1059ed0a6993d2a829089"
DS_MRT_EXITS = "d_b39d3a0871985372d7e1637193335da5"
DS_HAWKERS = "d_4a086da0a5553be1d89383cd90d07ecd"
DS_BUILDINGS = "d_16b157c52ed637edd6ba1232e026258d"
DS_PROPINFO = "d_17f5382f26140b1fdae0ba2ef6239d2f"
DS_SUPERMARKETS = "d_1bf762ee1d6d7fb61192cb442fb2f5b4"
ONEMAP_SEARCH = "https://www.onemap.gov.sg/api/common/elastic/search"

BOX_SG = (1.15, 1.48, 103.60, 104.10)

ABBREV = {
    "RD": "ROAD", "AVE": "AVENUE", "ST": "STREET", "DR": "DRIVE", "CRES": "CRESCENT",
    "CTRL": "CENTRAL", "NTH": "NORTH", "STH": "SOUTH", "BT": "BUKIT", "JLN": "JALAN",
    "LOR": "LORONG", "TER": "TERRACE", "PL": "PLACE", "CL": "CLOSE", "GDNS": "GARDENS",
    "HTS": "HEIGHTS", "TG": "TANJONG", "UPP": "UPPER", "PK": "PARK", "LK": "LINK",
}

LOG = []


def say(line=""):
    print(line, flush=True)
    LOG.append(line)


# ------------------------------------------------------------------ network
class HttpError(Exception):
    def __init__(self, code, body="", retry_after=None):
        Exception.__init__(self, "HTTP %s | %s" % (code, body[:200]))
        self.code = code
        self.retry_after = retry_after


def http_get(url, timeout=180):
    req = urllib.request.Request(url, headers=UA)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", errors="replace")
        except Exception:
            pass
        ra = None
        try:
            ra = int(e.headers.get("Retry-After"))
        except Exception:
            pass
        raise HttpError(e.code, body, ra)
    except urllib.error.URLError as e:
        if isinstance(e.reason, ssl.SSLCertVerificationError):
            raise SystemExit(
                "SSL certificate problem. In Finder open Applications > Python 3.14 and "
                "double-click 'Install Certificates.command', then run this again.")
        raise RuntimeError("Network error: %s" % e.reason)


def get_json(url, tries=6, wait=15, label=""):
    last = None
    for attempt in range(tries):
        try:
            return json.loads(http_get(url))
        except HttpError as e:
            last = e
            if e.code == 429 or e.code >= 500:
                w = e.retry_after or wait
                say("   (%s asked us to slow down, waiting %ds, try %d of %d)" % (label or "server", w, attempt + 1, tries))
                time.sleep(w)
                continue
            raise
        except (RuntimeError, ValueError) as e:
            last = e
            time.sleep(3)
    raise RuntimeError("Gave up on %s: %s" % (label or url[:60], last))


# ------------------------------------------------------------------ cache
def cache_path(name):
    return os.path.join(CACHE_DIR, name)


def cache_load(name, max_age_days=None):
    p = cache_path(name)
    if not os.path.exists(p):
        return None
    if max_age_days is not None and (time.time() - os.path.getmtime(p)) > max_age_days * 86400:
        return None
    try:
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def cache_save(name, obj):
    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(cache_path(name), "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)


# ------------------------------------------------------------------ data.gov.sg
def recent_months(n):
    t = time.localtime()
    y, m, out = t.tm_year, t.tm_mon, []
    for _ in range(n):
        out.append("%d-%02d" % (y, m))
        m -= 1
        if m == 0:
            m, y = 12, y - 1
    return out


def datastore_all(resource_id, filters=None, page=1000, label="data.gov.sg"):
    records, offset = [], 0
    while True:
        params = {"resource_id": resource_id, "limit": str(page), "offset": str(offset)}
        if filters:
            params["filters"] = json.dumps(filters)
        url = "https://data.gov.sg/api/action/datastore_search?" + urllib.parse.urlencode(params)
        data = get_json(url, label=label)
        recs = (data.get("result") or {}).get("records") or []
        records.extend(recs)
        if len(recs) < page:
            return records
        offset += page
        time.sleep(3)


def download_geojson(dataset_id, label):
    url = "https://api-open.data.gov.sg/v1/public/api/datasets/%s/poll-download" % dataset_id
    for attempt in range(5):
        info = get_json(url, label=label)
        link = (info.get("data") or {}).get("url")
        if info.get("code") == 0 and link:
            return get_json(link, label=label)
        time.sleep(3)
    raise RuntimeError("No download link for " + label)


def get_resales(towns, refresh):
    name = "resales_all.json" if not towns else "resales_%s.json" % "_".join(sorted(slug(t) for t in towns))
    c = None if refresh else cache_load(name, CACHE_DAYS)
    if c is not None:
        say("   using saved sales (%d records)" % len(c))
        return c
    filters = {"month": recent_months(SALES_MONTHS + 1)}
    if towns:
        filters["town"] = list(towns)
    recs = datastore_all(DS_RESALE, filters)
    cache_save(name, recs)
    return recs


def get_geo(dataset_id, cache_name, label, refresh):
    c = None if refresh else cache_load(cache_name, CACHE_DAYS)
    if c is not None:
        say("   using saved %s" % label)
        return c
    time.sleep(12)
    gj = download_geojson(dataset_id, "data.gov.sg")
    cache_save(cache_name, gj)
    return gj


# ------------------------------------------------------------------ geometry
def haversine_m(lat1, lon1, lat2, lon2):
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def walk_info(straight_m):
    wm = straight_m * WALK_DETOUR
    return int(round(wm / 10.0) * 10), max(1, int(round(wm / WALK_M_PER_MIN)))


def in_box(lat, lon, box):
    return box[0] <= lat <= box[1] and box[2] <= lon <= box[3]


def title(s):
    out, new = [], True
    for ch in str(s).lower():
        out.append(ch.upper() if new and ch.isalpha() else ch)
        new = ch in " /-("
    return "".join(out).strip()


def slug(town):
    return re.sub(r"[^a-z0-9]+", "-", str(town).lower()).strip("-")


def centroid(geom):
    """Average of a building outline's corners: (lat, lon), or None."""
    try:
        t = geom.get("type")
        c = geom.get("coordinates")
        ring = c[0] if t == "Polygon" else (c[0][0] if t == "MultiPolygon" else None)
        if not ring:
            return None
        if len(ring) > 1 and ring[0] == ring[-1]:
            ring = ring[:-1]
        return (sum(p[1] for p in ring) / len(ring), sum(p[0] for p in ring) / len(ring))
    except Exception:
        return None


# ------------------------------------------------------------------ OneMap
PACE = {"pause": PAUSE_ONEMAP, "ok": 0, "slowdowns": 0}


def onemap_search(q):
    url = ONEMAP_SEARCH + "?" + urllib.parse.urlencode(
        {"searchVal": q, "returnGeom": "Y", "getAddrDetails": "Y", "pageNum": "1"})
    for attempt in range(10):
        try:
            data = json.loads(http_get(url))
        except HttpError as e:
            if e.code in (401, 403):
                raise SystemExit("OneMap now wants a login token (HTTP %s). Tell Claude and we will add it." % e.code)
            if e.code == 429 or e.code >= 500:
                PACE["slowdowns"] += 1
                PACE["pause"] = min(8.0, PACE["pause"] * 1.6)
                PACE["ok"] = 0
                wait = e.retry_after or 15
                if PACE["slowdowns"] <= 3 or PACE["slowdowns"] % 5 == 0:
                    say("   (OneMap says slow down: waiting %ds, now %.1fs between lookups)" % (wait, PACE["pause"]))
                time.sleep(wait)
                continue
            raise
        except ValueError:
            time.sleep(3)
            continue
        PACE["ok"] += 1
        if PACE["ok"] >= 25 and PACE["pause"] > 0.5:
            PACE["pause"] = max(0.5, PACE["pause"] * 0.85)
            PACE["ok"] = 0
        time.sleep(PACE["pause"])
        return data.get("results") or []
    raise RuntimeError("OneMap kept refusing requests. Wait 10 minutes and run this again; it carries on where it stopped.")


def expand_street(street):
    return " ".join(ABBREV.get(w, w) for w in street.upper().split())


def to_ll(r):
    try:
        return float(r["LATITUDE"]), float(r["LONGITUDE"])
    except Exception:
        return None


def result_block_no(r):
    b = str(r.get("BLK_NO", "") or "").strip()
    if not b:
        parts = str(r.get("ADDRESS", "") or "").strip().split(" ")
        b = parts[0] if parts else ""
    return b.upper()


def geocode_block(block, street, gc):
    key = "BLK|%s|%s" % (block, street)
    if key in gc:
        return gc[key]
    hit = None
    for q in (block + " " + expand_street(street), block + " " + street):
        for r in onemap_search(q):
            ll = to_ll(r)
            if ll and result_block_no(r) == block.upper() and in_box(ll[0], ll[1], BOX_SG):
                hit = (ll, r)
                break
        if hit:
            break
    gc[key] = {"lat": hit[0][0], "lon": hit[0][1], "postal": hit[1].get("POSTAL")} if hit else None
    return gc[key]


def geocode_school(postal, name, gc):
    key = "SCH|%s" % postal
    if key in gc:
        return gc[key]
    hit = None
    for q in (postal, name):
        for r in onemap_search(q):
            ll = to_ll(r)
            if ll and in_box(ll[0], ll[1], BOX_SG):
                hit = ll
                break
        if hit:
            break
    gc[key] = {"lat": hit[0], "lon": hit[1]} if hit else None
    return gc[key]


def geocode_postal(postal, gc):
    key = "POST|%s" % postal
    if key in gc:
        return gc[key]
    hit = None
    for r in onemap_search(postal):
        ll = to_ll(r)
        if ll and in_box(ll[0], ll[1], BOX_SG):
            hit = ll
            break
    gc[key] = {"lat": hit[0], "lon": hit[1]} if hit else None
    return gc[key]


# ------------------------------------------------------------------ supermarkets
def load_supermarkets(refresh):
    """[{name, postal}] from the official Singapore Food Agency list."""
    c = None if refresh else cache_load("supermarkets_v2.json", 30)
    if c is not None:
        say("   using saved supermarket list (%d)" % len(c))
        return c
    recs = datastore_all(DS_SUPERMARKETS, None)
    rows, seen = [], set()
    for r in recs:
        addr = str(r.get("premise_address", "")).strip()
        m = re.search(r"S\((\d{6})\)", addr)
        if not m:
            continue
        postal = m.group(1)
        brand = title(str(r.get("business_name", "")).strip()) or "Supermarket"
        pre = addr[:m.start()]
        clean = re.sub(r"#[0-9A-Za-z\-/]+", "", pre)              # remove unit numbers like #01-1755
        parts = [p.strip(" ,") for p in clean.split(",")]
        parts = [p for p in parts if p]
        building = title(parts[-1]) if len(parts) > 1 else ""
        street = title(parts[0]) if parts else ""
        label = building or street
        name = "%s (%s)" % (brand, label) if label else brand
        key = (name, postal)
        if key in seen:
            continue
        seen.add(key)
        rows.append({"name": name, "postal": postal})
    cache_save("supermarkets_v2.json", rows)
    return rows


# ------------------------------------------------------------------ HDB outlines
def load_outlines(refresh):
    """[[BLK, STREET_CODE, POSTAL, lat, lon], ...] for every HDB block, from HDB's outlines."""
    c = None if refresh else cache_load("outlines.json", CACHE_DAYS)
    if c is not None:
        say("   using saved block outlines (%d)" % len(c))
        return c
    time.sleep(12)
    gj = download_geojson(DS_BUILDINGS, "data.gov.sg")
    rows = []
    for f in gj.get("features", []):
        p = f.get("properties") or {}
        blk = str(p.get("BLK_NO", "")).strip().upper()
        code = str(p.get("ST_COD", "")).strip().upper()
        ll = centroid(f.get("geometry") or {})
        if blk and code and ll:
            rows.append([blk, code, str(p.get("POSTAL_COD", "")).strip(), round(ll[0], 6), round(ll[1], 6)])
    cache_save("outlines.json", rows)
    return rows


def load_propinfo(refresh):
    """(block, street) -> {floors, units}. Nice to have; the run carries on without it."""
    c = None if refresh else cache_load("propinfo.json", 30)
    if c is None:
        try:
            recs = datastore_all(DS_PROPINFO, None)
        except Exception as e:
            say("   (block details list not available, carrying on without it: %s)" % e)
            return {}
        c = {}
        for r in recs:
            k = "%s|%s" % (str(r.get("blk_no", "")).strip().upper(), str(r.get("street", "")).strip().upper())
            try:
                c[k] = [int(r.get("max_floor_lvl") or 0), int(r.get("total_dwelling_units") or 0)]
            except Exception:
                pass
        cache_save("propinfo.json", c)
    return c


def resolve_street_code(town, street, blocks, by_blk, gc, sc):
    """Find which HDB outline street code belongs to this street, using one address lookup."""
    key = "%s|%s" % (town, street)
    if key in sc:
        return sc[key]
    code = ""
    anchors = sorted(blocks, key=lambda b: len(by_blk.get(b, [])))
    tries = 0
    for b in anchors:
        polys = by_blk.get(b, [])
        if not polys:
            continue
        tries += 1
        if tries > 3:
            break
        g = geocode_block(b, street, gc)
        if not g:
            continue
        best = min(polys, key=lambda p: haversine_m(g["lat"], g["lon"], p[3], p[4]))
        if haversine_m(g["lat"], g["lon"], best[3], best[4]) <= MAX_STREET_MATCH_M:
            code = best[1]
            break
    sc[key] = code
    return code


# ------------------------------------------------------------------ build helpers
def summarise_sales(recs):
    prices = [float(r["resale_price"]) for r in recs]
    latest = sorted(recs, key=lambda r: (r["month"], float(r["resale_price"])), reverse=True)[0]
    return {
        "n": len(recs), "median": int(round(statistics.median(prices))),
        "min": int(min(prices)), "max": int(max(prices)),
        "latest": {
            "month": latest["month"], "price": int(float(latest["resale_price"])),
            "storey": latest.get("storey_range"),
            "sqm": float(latest["floor_area_sqm"]) if latest.get("floor_area_sqm") else None,
            "lease_left": latest.get("remaining_lease"),
        },
    }


def rail_from_exits(gj):
    st = defaultdict(list)
    for f in gj.get("features", []):
        nm = str((f.get("properties") or {}).get("STATION_NA", "")).strip()
        geom = f.get("geometry") or {}
        if not nm or geom.get("type") != "Point":
            continue
        lon, lat = geom["coordinates"][:2]
        st[nm].append((lat, lon))
    out = []
    for nm, pts in st.items():
        up = nm.upper()
        if up.endswith(" MRT STATION"):
            kind, base = "MRT", nm[:-len(" MRT STATION")]
        elif up.endswith(" LRT STATION"):
            kind, base = "LRT", nm[:-len(" LRT STATION")]
        else:
            continue
        out.append({"name": title(base), "type": kind, "exits": pts,
                    "lat": sum(p[0] for p in pts) / len(pts), "lon": sum(p[1] for p in pts) / len(pts)})
    return out


def hawkers_from_geo(gj):
    out = []
    for f in gj.get("features", []):
        p = f.get("properties") or {}
        geom = f.get("geometry") or {}
        if geom.get("type") != "Point":
            continue
        if not str(p.get("STATUS", "")).strip().lower().startswith("existing"):
            continue
        lon, lat = geom["coordinates"][:2]
        out.append({"name": str(p.get("NAME", "")).strip(), "lat": lat, "lon": lon})
    return out


def nearest(items, lat, lon, k=1, get_pts=None):
    scored = []
    for it in items:
        pts = get_pts(it) if get_pts else [(it["lat"], it["lon"])]
        d = min(haversine_m(lat, lon, p[0], p[1]) for p in pts)
        scored.append((d, it))
    scored.sort(key=lambda x: x[0])
    return scored[:k]


def r5(x):
    return round(x, 5)


def compact_town(town, pos_blocks, groups, propinfo, stations_all, hawk_all, schools_all, supers_all, built):
    """pos_blocks: [((block, street), (lat, lon, postal))]. Returns the small per-town data file."""
    lats = [p[0] for _, p in pos_blocks]
    lons = [p[1] for _, p in pos_blocks]
    lat0, lon0 = sum(lats) / len(lats), sum(lons) / len(lons)
    lat_pad = MARGIN_KM / 111.0
    lon_pad = MARGIN_KM / (111.0 * math.cos(math.radians(lat0)))
    box = (min(lats) - lat_pad, max(lats) + lat_pad, min(lons) - lon_pad, max(lons) + lon_pad)

    stations = [s for s in stations_all if in_box(s["lat"], s["lon"], box)]
    hawkers = [h for h in hawk_all if in_box(h["lat"], h["lon"], box)]
    supers = [s for s in supers_all if in_box(s["lat"], s["lon"], box)]
    schools = [s for s in schools_all if in_box(s["lat"], s["lon"], box)]
    primaries = [s for s in schools if s["primary"]]
    secondaries = [s for s in schools if s["secondary"]]

    K, kidx = [], {}

    def sidx(s):
        if s["name"] not in kidx:
            kidx[s["name"]] = len(K)
            K.append([s["name"], {"PRIMARY": "P", "SECONDARY": "S", "MIXED": "M"}[s["level"]], r5(s["lat"]), r5(s["lon"])])
        return kidx[s["name"]]

    for s in schools:
        sidx(s)

    B = []
    all_prices = defaultdict(list)
    for (blk, street), (lat, lon, postal) in pos_blocks:
        recs = groups[(blk, street)]
        by_type = defaultdict(list)
        for r in recs:
            by_type[r["flat_type"]].append(r)
            all_prices[r["flat_type"]].append(float(r["resale_price"]))
        lease_years = [r.get("lease_commence_date") for r in recs if str(r.get("lease_commence_date", "")).isdigit()]
        lease_start = int(Counter(lease_years).most_common(1)[0][0]) if lease_years else None
        f = {}
        for ft, rs in sorted(by_type.items()):
            v = summarise_sales(rs)
            L = v["latest"]
            f[ft] = [v["n"], v["median"], v["min"], v["max"], L["price"], L["month"], L.get("storey"), L.get("sqm") or 0, L.get("lease_left") or ""]
        item = {"i": "%s %s" % (blk, street), "b": blk, "st": title(expand_street(street)), "a": r5(lat), "o": r5(lon),
                "p": postal or "", "y": lease_start, "f": f}
        info = propinfo.get("%s|%s" % (blk.upper(), street.upper()))
        if info and info[0]:
            item["fl"] = info[0]
            item["un"] = info[1]
        for kind, key in (("MRT", "m"), ("LRT", "l")):
            pool = [s for s in stations if s["type"] == kind]
            near = nearest(pool, lat, lon, 1, get_pts=lambda s: s["exits"])
            if near:
                d, s = near[0]
                m, mins = walk_info(d)
                item[key] = [s["name"], m, mins]
            else:
                item[key] = None
        item["h"] = []
        for d, h in nearest(hawkers, lat, lon, 2):
            m, mins = walk_info(d)
            item["h"].append([h["name"], m, mins])
        item["sm"] = []
        for d, sm in nearest(supers, lat, lon, 2):
            m, mins = walk_info(d)
            item["sm"].append([sm["name"], m, mins])
        pr = nearest(primaries, lat, lon, len(primaries))
        item["p1"] = [sidx(s) for d, s in pr if d <= SCHOOL_NEAR_1]
        item["p2"] = [sidx(s) for d, s in pr if SCHOOL_NEAR_1 < d <= SCHOOL_NEAR_2]
        item["sc"] = [[sidx(s), int(round(d / 10.0) * 10)] for d, s in nearest(secondaries, lat, lon, 2)]
        B.append(item)

    data = {
        "town": title(town), "built": built, "months": SALES_MONTHS, "walk": "80 m a minute",
        "attr": [
            "HDB resale flat prices, HDB, via data.gov.sg (Singapore Open Data Licence)",
            "HDB Existing Building and HDB Property Information, HDB, via data.gov.sg (Singapore Open Data Licence)",
            "General information of schools, MOE, via data.gov.sg (Singapore Open Data Licence)",
            "MRT station exits, LTA, via data.gov.sg (Singapore Open Data Licence)",
            "Hawker centres, NEA, via data.gov.sg (Singapore Open Data Licence)",
            "Listing of Supermarkets, SFA, via data.gov.sg (Singapore Open Data Licence)",
            "Address positions from OneMap, Singapore Land Authority",
        ],
        "c": [r5(lat0), r5(lon0)],
        "K": K,
        "A": [[s["name"], s["type"], r5(s["lat"]), r5(s["lon"])] for s in stations],
        "H": [[h["name"], r5(h["lat"]), r5(h["lon"])] for h in hawkers],
        "SM": [[x["name"], r5(x["lat"]), r5(x["lon"])] for x in supers],
        "B": B,
    }
    medians = {ft: int(round(statistics.median(v))) for ft, v in all_prices.items()}
    return data, medians, (lat0, lon0), sum(len(v) for v in all_prices.values())


# ------------------------------------------------------------------ main build
def build(towns_wanted, limit, refresh):
    started = time.time()
    os.makedirs(CACHE_DIR, exist_ok=True)
    out_dir = OUT_DIR if not limit else OUT_DIR + "_test"
    os.makedirs(out_dir, exist_ok=True)

    # reuse work from the Bukit Panjang run if it is there
    old = os.path.join("bp_cache", "geocode.json")
    if os.path.exists(old) and not os.path.exists(cache_path("geocode.json")):
        shutil.copy(old, cache_path("geocode.json"))
        say("(reusing the address lookups already done for Bukit Panjang)")
    gc = cache_load("geocode.json") or {}
    sc = cache_load("streets.json") or {}

    say("1/6  HDB sales, last %d months" % SALES_MONTHS)
    resales = get_resales(towns_wanted, refresh)
    groups = defaultdict(list)
    block_town = {}
    for r in resales:
        try:
            float(r["resale_price"])
        except Exception:
            continue
        key = (str(r["block"]).strip().upper(), str(r["street_name"]).strip().upper())
        groups[key].append(r)
        block_town[key] = str(r["town"]).strip().upper()
    towns = sorted(set(block_town.values()))
    say("     %d sales, %d blocks, %d towns" % (len(resales), len(groups), len(towns)))
    if not resales:
        raise SystemExit("No sales came back. Please send this output to Claude.")

    say("2/6  HDB block outlines and details")
    outlines = load_outlines(refresh)
    by_blk = defaultdict(list)
    by_code_blk = {}
    for blk, code, postal, lat, lon in outlines:
        by_blk[blk].append((blk, code, postal, lat, lon))
        by_code_blk.setdefault((code, blk), (lat, lon, postal))
    propinfo = load_propinfo(refresh)
    say("     %d block outlines, %d block detail rows" % (len(outlines), len(propinfo)))

    say("3/6  MRT/LRT stations and hawker centres")
    stations_all = rail_from_exits(get_geo(DS_MRT_EXITS, "mrt_exits.json", "MRT/LRT exits", refresh))
    hawk_all = hawkers_from_geo(get_geo(DS_HAWKERS, "hawkers.json", "hawker centres", refresh))
    say("     %d stations, %d hawker centres" % (len(stations_all), len(hawk_all)))

    say("4/6  Working out block positions")
    # streets to resolve, grouped by town
    streets = defaultdict(list)
    for (blk, street), town in block_town.items():
        streets[(town, street)].append(blk)
    street_keys = sorted(streets.keys())
    if limit:
        street_keys = street_keys[:limit]
    say("     %d streets to match to HDB's outlines (one address lookup each, at most)" % len(street_keys))
    positions = {}
    from_outline = from_onemap = 0
    missing = []
    try:
        for i, (town, street) in enumerate(street_keys, 1):
            blocks = streets[(town, street)]
            code = resolve_street_code(town, street, blocks, by_blk, gc, sc)
            for b in blocks:
                pos = by_code_blk.get((code, b)) if code else None
                if pos:
                    positions[(b, street)] = (pos[0], pos[1], pos[2])
                    from_outline += 1
                else:
                    g = geocode_block(b, street, gc)
                    if g:
                        positions[(b, street)] = (g["lat"], g["lon"], g.get("postal"))
                        from_onemap += 1
                    else:
                        missing.append("%s %s" % (b, street))
            if i % 10 == 0 or i == len(street_keys):
                mins = (time.time() - started) / 60.0
                left = (len(street_keys) - i) * (mins / i) if i else 0
                say("     %d of %d streets done (%d min so far, about %d min to go)" % (i, len(street_keys), mins, left))
            if i % 10 == 0:
                cache_save("geocode.json", gc)
                cache_save("streets.json", sc)
    finally:
        cache_save("geocode.json", gc)
        cache_save("streets.json", sc)
    say("     positions from HDB outlines: %d | from address lookups: %d | not found: %d" % (from_outline, from_onemap, len(missing)))

    # cross-check against the OneMap points we already have for the same blocks
    diffs = []
    for (b, street), (lat, lon, postal) in positions.items():
        g = gc.get("BLK|%s|%s" % (b, street))
        if g:
            diffs.append(haversine_m(lat, lon, g["lat"], g["lon"]))
    if len(diffs) >= 10:
        diffs.sort()
        say("     check against OneMap on %d blocks: median gap %d m, 90%% within %d m, worst %d m" % (
            len(diffs), diffs[len(diffs) // 2], diffs[int(len(diffs) * 0.9)], diffs[-1]))

    say("5/6  Schools")
    schools_raw = datastore_all(DS_SCHOOLS, None, page=500)
    rows = []
    for s in schools_raw:
        level = str(s.get("mainlevel_code", "")).strip().upper()
        postal = str(s.get("postal_code", "")).strip()
        is_p = level == "PRIMARY" or "P1" in level
        is_s = level.startswith("SECONDARY") or "S1" in level or "-S" in level
        if (is_p or is_s) and postal.isdigit():
            rows.append({"name": title(s.get("school_name", "")), "postal": postal, "primary": is_p, "secondary": is_s,
                         "area": str(s.get("dgp_code", "")).strip().upper(),
                         "level": "MIXED" if (is_p and is_s) else ("PRIMARY" if is_p else "SECONDARY")})
    if limit:
        # quick test: try schools in and around the chosen towns first
        near = set(towns_wanted) | {"CHOA CHU KANG", "BUKIT BATOK", "BUKIT TIMAH"}
        rows = sorted(rows, key=lambda s: s["area"] not in near)[:limit]
    say("     finding positions for %d schools (OneMap)" % len(rows))
    schools_all, school_failed = [], []
    try:
        for i, s in enumerate(rows, 1):
            g = geocode_school(s["postal"], s["name"], gc)
            if g:
                s2 = dict(s)
                s2["lat"], s2["lon"] = g["lat"], g["lon"]
                schools_all.append(s2)
            else:
                school_failed.append(s["name"])
            if i % 25 == 0 or i == len(rows):
                say("     %d of %d schools done (%d min so far)" % (i, len(rows), (time.time() - started) // 60))
            if i % 10 == 0:
                cache_save("geocode.json", gc)
    finally:
        cache_save("geocode.json", gc)

    say("     supermarkets")
    sm_rows = load_supermarkets(refresh)
    if limit:
        # quick test: try supermarkets in the same postal sectors as the chosen blocks first
        sectors = set(str(v[2])[:2] for v in positions.values() if v[2])
        sm_rows = sorted(sm_rows, key=lambda r: r["postal"][:2] not in sectors)[:limit]
    supers_all, sm_failed = [], 0
    postals = sorted(set(r["postal"] for r in sm_rows))
    say("     finding positions for %d supermarket addresses (OneMap)" % len(postals))
    try:
        for i, pc in enumerate(postals, 1):
            g = geocode_postal(pc, gc)
            if not g:
                sm_failed += 1
            if i % 25 == 0 or i == len(postals):
                say("     %d of %d supermarket addresses done (%d min so far)" % (i, len(postals), (time.time() - started) // 60))
            if i % 10 == 0:
                cache_save("geocode.json", gc)
    finally:
        cache_save("geocode.json", gc)
    for r in sm_rows:
        g = gc.get("POST|%s" % r["postal"])
        if g:
            supers_all.append({"name": r["name"], "lat": g["lat"], "lon": g["lon"]})
    say("     %d supermarkets placed on the map, %d addresses not found" % (len(supers_all), sm_failed))

    say("6/6  Writing the data files")
    built = time.strftime("%Y-%m-%d")
    index = []
    for town in towns:
        pos_blocks = [((b, st), positions[(b, st)]) for (b, st), t in block_town.items()
                      if t == town and (b, st) in positions]
        if not pos_blocks:
            continue
        pos_blocks.sort(key=lambda x: (x[0][1], x[0][0]))
        data, medians, (lat0, lon0), nsales = compact_town(town, pos_blocks, groups, propinfo, stations_all, hawk_all, schools_all, supers_all, built)
        fname = slug(town) + ".json"
        with open(os.path.join(out_dir, fname), "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
        index.append({"slug": slug(town), "name": title(town), "n": len(pos_blocks), "sales": nsales,
                      "lat": r5(lat0), "lon": r5(lon0), "med": medians})
        say("     %-18s %4d blocks  %4d KB" % (title(town), len(pos_blocks), os.path.getsize(os.path.join(out_dir, fname)) // 1024))
    with open(os.path.join(out_dir, "towns.json"), "w", encoding="utf-8") as f:
        json.dump({"built": built, "towns": index}, f, ensure_ascii=False, separators=(",", ":"))

    say("")
    say("=" * 60)
    say("DONE in %d minutes. Files are in the folder: %s" % ((time.time() - started) // 60, out_dir))
    say("Towns written: %d | blocks with a position: %d of %d" % (len(index), len(positions), len(street_keys) and sum(len(streets[k]) for k in street_keys)))
    if missing:
        say("Blocks NOT found (%d): %s" % (len(missing), ", ".join(missing[:12]) + (" ..." if len(missing) > 12 else "")))
    if school_failed:
        say("Schools NOT found (%d): %s" % (len(school_failed), ", ".join(school_failed[:8])))
    if index:
        first = json.load(open(os.path.join(out_dir, index[0]["slug"] + ".json"), encoding="utf-8"))
        say("")
        say("One block as a check (%s):" % index[0]["name"])
        say(json.dumps(first["B"][0], ensure_ascii=False)[:1200])


def main():
    argv = sys.argv[1:]
    refresh = "--refresh" in argv
    limit = 0
    if "--limit" in argv:
        try:
            limit = int(argv[argv.index("--limit") + 1])
        except Exception:
            limit = 8
    towns = []
    if "--town" in argv:
        towns = [t.strip().upper() for t in argv[argv.index("--town") + 1].split(",") if t.strip()]
    if limit and not towns:
        towns = ["BUKIT PANJANG"]
    say("Whole-Singapore data builder | Python %s | %s" % (sys.version.split()[0], time.strftime("%Y-%m-%d %H:%M")))
    if limit:
        say("QUICK TEST: %s, only %d streets and %d schools" % (", ".join(towns), limit, limit))
    try:
        build(towns, limit, refresh)
    except SystemExit as e:
        say(str(e))
    except KeyboardInterrupt:
        say("Stopped. What was found so far is saved, so run it again to carry on.")
    except Exception as e:
        say("STOPPED WITH AN ERROR: %s: %s" % (type(e).__name__, e))
        say("Please copy everything above into the chat.")
    try:
        with open("sg_build_log.txt", "w", encoding="utf-8") as f:
            f.write("\n".join(LOG))
    except Exception:
        pass


if __name__ == "__main__":
    main()
