#!/usr/bin/env python3
"""
Miji: build the private-home (condo, EC, landed) data files from URA's Data Service.

What it does:
  1. Asks URA for today's token, using your AccessKey (the key stays on this Mac)
  2. Downloads the private residential transactions (URA gives the latest 5 years) in 4 batches
  3. Turns URA's map coordinates (SVY21) into latitude and longitude
  4. Groups the sales by project, works out price per sq ft, and saves one small file per
     postal district (private_data/d01.json ... d28.json) plus an index (private_data/index.json)

Your AccessKey is read from, in this order:
  a) the environment variable URA_ACCESS_KEY
  b) a file called ura_key.txt sitting next to this script (one line, just the key)
  c) it asks you to paste it (nothing is shown on screen and nothing is saved)
It is never printed, never written to the log, and never put in the output folder.
ONLY upload the private_data folder to GitHub. Never upload ura_key.txt or ura_cache.

Everything URA sends is saved in ura_cache (kept on this Mac only), so re-running within 3 days
does not call URA again. Use --refresh to force a new download. URA updates these sales on
Tuesday and Friday evenings, so once a month is plenty.

How to run (Terminal), from the folder that holds this file:
    python3 ura_build.py
    python3 ura_build.py --refresh       <- ignore the saved copy and download again
    python3 ura_build.py --no-lookup     <- skip the OneMap lookups for projects URA gives no position for
    python3 ura_build.py --no-nearby     <- skip the nearby MRT / school / hawker / supermarket information
Standard Python only, nothing to install.
"""

import getpass
import json
import math
import os
import re
import ssl
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict

# ------------------------------------------------------------------ settings
BASE = os.environ.get("URA_BASE_URL", "https://eservice.ura.gov.sg/uraDataService/")
CACHE_DIR = "ura_cache"
OUT_DIR = "private_data"
CACHE_DAYS = 3
ONEMAP_SEARCH = os.environ.get("ONEMAP_URL", "https://www.onemap.gov.sg/api/common/elastic/search")
PAUSE_ONEMAP = 0.7
RETRY_LOOKUP_DAYS = 30
# Nearby MRT, schools, hawker centres and supermarkets come from your HDB map data (already public)
POI_LOCAL = ("data", "sg_data")
POI_URL = "https://timothylawrencewong.github.io/sg-hdb-map-data/data/"
FAR_MIN = 30              # farther than this many minutes' walk, an MRT, hawker centre or supermarket is not listed
WALK_DETOUR = 1.3
WALK_M_PER_MIN = 80
MARGIN_KM = 3.0
SCHOOL_NEAR_1 = 1000
SCHOOL_NEAR_2 = 2000
BATCHES = (1, 2, 3, 4)
SQM_TO_SQFT = 10.7639
RECENT_MONTHS = 12
# Safety checks before the new files replace the old ones
MIN_PROJECTS = 500
MIN_TRANSACTIONS = 20000
MIN_WITH_COORDS = 0.90
# Singapore, with a little room
LAT_RANGE = (1.15, 1.50)
LON_RANGE = (103.55, 104.10)

UA = {"User-Agent": "Mozilla/5.0 (compatible; MijiBuilder/1.0; +https://miji.sg)", "Accept": "application/json"}
LOG = []
SECRETS = []


def say(line=""):
    line = str(line)
    for sec in SECRETS:
        if sec:
            line = line.replace(sec, "***")
    print(line, flush=True)
    LOG.append(line)


# ------------------------------------------------------------------ SVY21 -> latitude, longitude
_A = 6378137.0
_F = 1 / 298.257223563
_B = _A * (1 - _F)
_E2 = 2 * _F - _F * _F
_E4 = _E2 * _E2
_E6 = _E4 * _E2
_A0 = 1 - _E2 / 4 - 3 * _E4 / 64 - 5 * _E6 / 256
_A2 = 3.0 / 8 * (_E2 + _E4 / 4 + 15 * _E6 / 128)
_A4 = 15.0 / 256 * (_E4 + 3 * _E6 / 4)
_A6 = 35 * _E6 / 3072
_O_LAT = math.radians(1.366666)
_O_LON = math.radians(103.833333)
_O_N = 38744.572
_O_E = 28001.642
_K = 1.0


def _meridian_arc(lat):
    return _A * (_A0 * lat - _A2 * math.sin(2 * lat) + _A4 * math.sin(4 * lat) - _A6 * math.sin(6 * lat))


def svy21_to_latlon(x_east, y_north):
    """URA gives x = easting, y = northing (SVY21, EPSG:3414). Returns (lat, lon) in degrees."""
    n_prime = y_north - _O_N
    m_prime = _meridian_arc(_O_LAT) + n_prime / _K
    n = (_A - _B) / (_A + _B)
    n2, n3, n4 = n * n, n ** 3, n ** 4
    g = _A * (1 - n) * (1 - n2) * (1 + 9 * n2 / 4 + 225 * n4 / 64) * math.pi / 180
    sigma = m_prime * math.pi / (180 * g)
    lat_p = (sigma
             + (3 * n / 2 - 27 * n3 / 32) * math.sin(2 * sigma)
             + (21 * n2 / 16 - 55 * n4 / 32) * math.sin(4 * sigma)
             + (151 * n3 / 96) * math.sin(6 * sigma)
             + (1097 * n4 / 512) * math.sin(8 * sigma))
    sin_l = math.sin(lat_p)
    rho = _A * (1 - _E2) / (1 - _E2 * sin_l * sin_l) ** 1.5
    v = _A / math.sqrt(1 - _E2 * sin_l * sin_l)
    psi = v / rho
    psi2, psi3, psi4 = psi ** 2, psi ** 3, psi ** 4
    t = math.tan(lat_p)
    t2, t4, t6 = t ** 2, t ** 4, t ** 6
    e_prime = x_east - _O_E
    x = e_prime / (_K * v)
    x3, x5, x7 = x ** 3, x ** 5, x ** 7
    lat_factor = t / (_K * rho)
    lat = (lat_p
           - lat_factor * (e_prime * x / 2)
           + lat_factor * (e_prime * x3 / 24) * (-4 * psi2 + 9 * psi * (1 - t2) + 12 * t2)
           - lat_factor * (e_prime * x5 / 720) * (8 * psi4 * (11 - 24 * t2) - 12 * psi3 * (21 - 71 * t2)
                                                   + 15 * psi2 * (15 - 98 * t2 + 15 * t4)
                                                   + 180 * psi * (5 * t2 - 3 * t4) + 360 * t4)
           + lat_factor * (e_prime * x7 / 40320) * (1385 + 3633 * t2 + 4095 * t4 + 1575 * t6))
    sec = 1 / math.cos(lat_p)
    lon = (_O_LON
           + x * sec
           - (x3 / 6) * sec * (psi + 2 * t2)
           + (x5 / 120) * sec * (-4 * psi3 * (1 - 6 * t2) + psi2 * (9 - 68 * t2) + 72 * psi * t2 + 24 * t4)
           - (x7 / 5040) * sec * (61 + 662 * t2 + 1320 * t4 + 720 * t6))
    return math.degrees(lat), math.degrees(lon)


# ------------------------------------------------------------------ network
class HttpError(Exception):
    def __init__(self, code, body="", retry_after=None):
        Exception.__init__(self, "HTTP %s | %s" % (code, body[:200]))
        self.code = code
        self.retry_after = retry_after


def http_get(url, headers, timeout=180):
    h = dict(UA)
    h.update(headers)
    req = urllib.request.Request(url, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.headers.get("Content-Type", ""), resp.read().decode("utf-8", errors="replace")
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
                "SSL certificate problem. In Finder open Applications > Python 3.x and "
                "double-click 'Install Certificates.command', then run this again.")
        raise RuntimeError("Network error: %s" % e.reason)


def get_json(url, headers, tries=4, wait=20, label=""):
    last = None
    for attempt in range(tries):
        try:
            status, ctype, text = http_get(url, headers)
        except HttpError as e:
            last = e
            if e.code == 429 or e.code >= 500:
                w = e.retry_after or wait
                say("   (the server asked us to slow down, waiting %ds, try %d of %d)" % (w, attempt + 1, tries))
                time.sleep(w)
                continue
            raise
        except RuntimeError as e:
            last = e
            time.sleep(5)
            continue
        try:
            return json.loads(text)
        except ValueError:
            snippet = " ".join(text.split())[:300]
            raise RuntimeError("%s: URA answered (HTTP %s, %s) but not with data. It said: %r" % (
                label or "request", status, ctype or "no content type", snippet))
    raise RuntimeError("Gave up on %s: %s" % (label or "a request", last))


# ------------------------------------------------------------------ key and token
def read_key():
    key = os.environ.get("URA_ACCESS_KEY", "").strip()
    if not key:
        here = os.path.dirname(os.path.abspath(__file__))
        for p in (os.path.join(here, "ura_key.txt"), "ura_key.txt"):
            if os.path.exists(p):
                with open(p, "r", encoding="utf-8") as f:
                    key = f.readline().strip()
                if key:
                    break
    if not key:
        try:
            key = getpass.getpass("Paste your URA AccessKey (nothing will show as you paste), then press Enter: ").strip()
        except (EOFError, KeyboardInterrupt):
            key = ""
    if not key:
        raise SystemExit("No AccessKey found. Put it in a file called ura_key.txt next to this script (one line) and run again.")
    SECRETS.append(key)
    return key


def get_token(key):
    say("Asking URA for today's token...")
    try:
        d = get_json(BASE + "insertNewToken/v1", {"AccessKey": key}, label="the token request")
    except HttpError as e:
        if e.code in (401, 403):
            raise SystemExit("URA refused the AccessKey (HTTP %d). Check that it is copied exactly, with no spaces, and that the account is activated." % e.code)
        raise
    if d.get("Result"):
        SECRETS.append(str(d["Result"]))
    if str(d.get("Status", "")).lower() != "success" or not d.get("Result"):
        raise SystemExit("URA did not give a token: Status=%s Message=%s" % (d.get("Status"), d.get("Message")))
    return d["Result"]


# ------------------------------------------------------------------ download
def cache_file(batch):
    return os.path.join(CACHE_DIR, "PMI_Resi_Transaction_batch%d.json" % batch)


def load_cached(batch):
    p = cache_file(batch)
    if not os.path.exists(p):
        return None
    if (time.time() - os.path.getmtime(p)) > CACHE_DAYS * 86400:
        return None
    try:
        with open(p, "r", encoding="utf-8") as f:
            recs = json.load(f)
        return recs if recs else None
    except Exception:
        return None


def download_all(refresh):
    """Returns {batch: list of project records}."""
    os.makedirs(CACHE_DIR, exist_ok=True)
    out = {}
    key = token = None
    for b in BATCHES:
        recs = None if refresh else load_cached(b)
        if recs is not None:
            say("Batch %d: using the copy saved in %s (%d projects)" % (b, CACHE_DIR, len(recs)))
            out[b] = recs
            continue
        if token is None:
            key = read_key()
            token = get_token(key)
        say("Batch %d: downloading from URA..." % b)
        url = BASE + "invokeUraDS/v1?service=PMI_Resi_Transaction&batch=%d" % b
        d = get_json(url, {"AccessKey": key, "Token": token}, label="batch %d" % b)
        if str(d.get("Status", "")).lower() != "success" or not isinstance(d.get("Result"), list):
            raise SystemExit("Batch %d did not come back properly: Status=%s Message=%s" % (b, d.get("Status"), d.get("Message")))
        recs = d["Result"]
        with open(cache_file(b), "w", encoding="utf-8") as f:
            json.dump(recs, f, ensure_ascii=False)
        say("   got %d projects" % len(recs))
        out[b] = recs
        time.sleep(2)
    return out


# ------------------------------------------------------------------ tidy up
def num(v):
    try:
        return float(str(v).replace(",", "").strip())
    except Exception:
        return None


def parse_ym(s):
    """URA gives MMYY ('0715' = July 2015). Returns YYYYMM as an int, or None."""
    s = str(s).strip()
    if len(s) == 3:
        s = "0" + s
    if len(s) != 4 or not s.isdigit():
        return None
    mm, yy = int(s[:2]), int(s[2:])
    if not 1 <= mm <= 12:
        return None
    return (2000 + yy) * 100 + mm


def title(s):
    small = {"OF", "AND", "THE", "AT", "ON", "BY", "FOR"}
    roman = {"II", "III", "IV", "VI", "VII", "VIII", "IX", "XI", "XII"}
    out = []
    for i, w in enumerate(str(s).strip().split()):
        if w.upper() in roman:
            out.append(w.upper())
        elif i and w.upper() in small:
            out.append(w.lower())
        elif any(ch.isdigit() for ch in w):
            out.append(w.upper())
        else:
            t = "-".join(p.capitalize() for p in w.split("-"))
            t = re.sub(r"\b([A-Za-z])'([a-z])", lambda m: m.group(1) + "'" + m.group(2).upper(), t)
            out.append(t)
    return " ".join(out)


def r1(x):
    return round(x, 1)


def r5(x):
    return round(x, 5)


def group_of(ptype):
    """condo (condominium, apartment), ec (executive condominium) or landed (detached, semi-detached, terrace)."""
    t = str(ptype).lower()
    if "executive" in t:
        return "ec"
    if "detached" in t or "terrace" in t:
        return "landed"
    return "condo"


def tidy(batches):
    """Merge batches into a list of projects, each with cleaned transactions."""
    projects = {}
    seen_tx = 0
    for b, recs in batches.items():
        for r in recs:
            name = str(r.get("project") or "").strip()
            street = str(r.get("street") or "").strip()
            x, y = num(r.get("x")), num(r.get("y"))
            for t in (r.get("transaction") or []):
                ym = parse_ym(t.get("contractDate"))
                price = num(t.get("price"))
                area = num(t.get("area"))
                if not ym or not price or price <= 0 or not area or area <= 0:
                    continue
                ptype = str(t.get("propertyType") or "").strip()
                # Landed sales are grouped per street AND house type, so a median is never a mix of
                # detached and terrace houses. Condos and ECs stay one record per project.
                key = (name, street, x, y, ptype if group_of(ptype) == "landed" else "")
                p = projects.get(key)
                if p is None:
                    p = {"name": name, "street": street, "x": x, "y": y,
                         "seg": str(r.get("marketSegment") or "").strip().upper(), "tx": []}
                    projects[key] = p
                units = int(num(t.get("noOfUnits")) or 1)
                p["tx"].append({
                    "ym": ym, "price": int(round(price)), "area": area,
                    "floor": str(t.get("floorRange") or "").strip(),
                    "sale": int(num(t.get("typeOfSale")) or 0),
                    "ptype": ptype,
                    "tenure": str(t.get("tenure") or "").strip(),
                    "units": units,
                    "atype": str(t.get("typeOfArea") or "").strip(),
                    "district": str(t.get("district") or "").strip().zfill(2),
                })
                seen_tx += 1
    return list(projects.values()), seen_tx


# ------------------------------------------------------------------ nearby MRT, schools, hawker centres, supermarkets
def haversine_m(lat1, lon1, lat2, lon2):
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def walk_info(straight_m):
    wm = straight_m * WALK_DETOUR
    return int(round(wm / 10.0) * 10), max(1, int(round(wm / WALK_M_PER_MIN)))


def nearest(items, lat, lon, k=1):
    scored = sorted(((haversine_m(lat, lon, it["lat"], it["lon"]), it) for it in items), key=lambda x: x[0])
    return scored[:k]


def load_pois():
    """The MRT/LRT, school, hawker and supermarket lists already sit inside your HDB map data (one file per town).
    Read them from the data folder next to this script if it is there, otherwise from your public GitHub Pages."""
    def read_local(folder, name):
        with open(os.path.join(folder, name), "r", encoding="utf-8") as f:
            return json.load(f)
    folder = next((f for f in POI_LOCAL if os.path.exists(os.path.join(f, "towns.json"))), None)
    try:
        if folder:
            say("Reading MRT, school, hawker and supermarket lists from your %s folder..." % folder)
            get = lambda name: read_local(folder, name)
        else:
            say("Reading MRT, school, hawker and supermarket lists from your GitHub Pages...")
            get = lambda name: get_json(POI_URL + name, {}, label=name)
        idx = get("towns.json")
        stations, schools, hawkers, supers = {}, {}, {}, {}
        for t in idx.get("towns", []):
            D = get(t["slug"] + ".json")
            for a in D.get("A", []):
                stations[(a[0], a[1])] = {"name": a[0], "type": a[1], "lat": a[2], "lon": a[3]}
            for k in D.get("K", []):
                lvl = k[1]
                schools[k[0]] = {"name": k[0], "lvl": lvl, "lat": k[2], "lon": k[3],
                                 "primary": lvl in ("P", "M"), "secondary": lvl in ("S", "M")}
            for h in D.get("H", []):
                hawkers[(h[0], round(h[1], 4), round(h[2], 4))] = {"name": h[0], "lat": h[1], "lon": h[2]}
            for x in D.get("SM", []):
                supers[(x[0], round(x[1], 4), round(x[2], 4))] = {"name": x[0], "lat": x[1], "lon": x[2]}
        out = {"built": idx.get("built"), "stations": list(stations.values()), "schools": list(schools.values()),
               "hawkers": list(hawkers.values()), "supers": list(supers.values())}
        say("   %d stations, %d schools, %d hawker centres, %d supermarkets" % (
            len(out["stations"]), len(out["schools"]), len(out["hawkers"]), len(out["supers"])))
        if not out["stations"] or not out["schools"]:
            raise RuntimeError("the lists came back empty")
        return out
    except SystemExit:
        raise
    except Exception as e:
        say("   Could not read the nearby lists (%s). Private files will be built without the nearby information." % e)
        return None


def near_pois(plist, pois):
    """For one district: the nearby lists (for the map layers) and, for every project, its nearest MRT/LRT,
    hawker centres, supermarkets and schools, in the same shape as the HDB files."""
    lats = [p["lat"] for p in plist]
    lons = [p["lon"] for p in plist]
    lat0 = sum(lats) / len(lats)
    lat_pad = MARGIN_KM / 111.0
    lon_pad = MARGIN_KM / (111.0 * math.cos(math.radians(lat0)))
    box = (min(lats) - lat_pad, max(lats) + lat_pad, min(lons) - lon_pad, max(lons) + lon_pad)
    inb = lambda x: box[0] <= x["lat"] <= box[1] and box[2] <= x["lon"] <= box[3]
    stations = [x for x in pois["stations"] if inb(x)]
    hawkers = [x for x in pois["hawkers"] if inb(x)]
    supers = [x for x in pois["supers"] if inb(x)]
    schools = [x for x in pois["schools"] if inb(x)]
    primaries = [x for x in schools if x["primary"]]
    secondaries = [x for x in schools if x["secondary"]]
    K, kidx = [], {}

    def sidx(sc):
        if sc["name"] not in kidx:
            kidx[sc["name"]] = len(K)
            K.append([sc["name"], sc["lvl"], r5(sc["lat"]), r5(sc["lon"])])
        return kidx[sc["name"]]

    for sc in schools:
        sidx(sc)
    extras = {}
    for p in plist:
        e = {}
        for kind, key in (("MRT", "mr"), ("LRT", "lr")):
            pool = [x for x in stations if x["type"] == kind]
            near = nearest(pool, p["lat"], p["lon"], 1)
            if near:
                m, mins = walk_info(near[0][0])
                if mins <= FAR_MIN:
                    e[key] = [near[0][1]["name"], m, mins]
        for pool, key in ((hawkers, "h"), (supers, "sm")):
            lst = []
            for d, x in nearest(pool, p["lat"], p["lon"], 2):
                m, mins = walk_info(d)
                if mins <= FAR_MIN:
                    lst.append([x["name"], m, mins])
            if lst:
                e[key] = lst
        pr = nearest(primaries, p["lat"], p["lon"], len(primaries))
        p1 = [sidx(x) for d, x in pr if d <= SCHOOL_NEAR_1]
        p2 = [sidx(x) for d, x in pr if SCHOOL_NEAR_1 < d <= SCHOOL_NEAR_2]
        if p1:
            e["p1"] = p1
        if p2:
            e["p2"] = p2
        sc2 = [[sidx(x), int(round(d / 10.0) * 10)] for d, x in nearest(secondaries, p["lat"], p["lon"], 2)]
        if sc2:
            e["sc"] = sc2
        extras[id(p)] = e
    lists = {"A": [[x["name"], x["type"], r5(x["lat"]), r5(x["lon"])] for x in stations],
             "K": K,
             "H": [[x["name"], r5(x["lat"]), r5(x["lon"])] for x in hawkers],
             "SM": [[x["name"], r5(x["lat"]), r5(x["lon"])] for x in supers]}
    return lists, extras


def tx_count_of(projects):
    return sum(len(p["tx"]) for p in projects)


def median(vals):
    return statistics.median(vals) if vals else None


def compact_project(p, latest_ym, idx, extra=None):
    """Small, map-friendly version of one project. idx holds shared lookup lists."""
    def look(lst, table, v):
        if v not in table:
            table[v] = len(lst)
            lst.append(v)
        return table[v]

    cut = _months_back(latest_ym, RECENT_MONTHS)
    rows = []
    recent = []
    for t in sorted(p["tx"], key=lambda t: t["ym"]):
        rows.append([
            t["ym"], t["price"], r1(t["area"]), t["floor"], t["sale"],
            look(idx["types"], idx["types_t"], t["ptype"]),
            look(idx["tenures"], idx["tenures_t"], t["tenure"]),
            t["units"],
            look(idx["areas"], idx["areas_t"], t["atype"]),
        ])
        # Price per sq ft is only fair for a single unit sold on its own
        if t["ym"] > cut and t["units"] == 1:
            psf = t["price"] / (t["area"] * SQM_TO_SQFT)
            recent.append((t["price"], psf))
    ptypes = Counter(t["ptype"] for t in p["tx"])
    top_type = ptypes.most_common(1)[0][0] if ptypes else ""
    tenures = Counter(t["tenure"] for t in p["tx"])
    top_tenure = tenures.most_common(1)[0][0] if tenures else ""
    out = {
        "n": title(p["name"]) if p["name"] else "",
        "s": title(p["street"]),
        "seg": p["seg"],
        "ty": top_type,
        "te": top_tenure,
        "la": p["lat"], "ln": p["lon"],
        "T": rows,
    }
    if recent:
        out["m"] = {"n12": len(recent),
                    "psf12": int(round(median([x[1] for x in recent]))),
                    "med12": int(round(median([x[0] for x in recent])))}
    if p.get("ap"):
        out["ap"] = 1
    if extra:
        out.update(extra)
    out["last"] = max(t["ym"] for t in p["tx"])
    return out


def district_summary(d, plist, rows, nt, latest_ym):
    """One line of the index: where the district sits on the map and its typical prices, so the
    map can draw all 28 districts without opening every file."""
    cut = _months_back(latest_ym, RECENT_MONTHS)
    g = {}
    for p in plist:
        for t in p["tx"]:
            if t["ym"] <= cut or t["units"] != 1:
                continue
            k = group_of(t["ptype"])
            e = g.setdefault(k, {"psf": [], "price": []})
            e["psf"].append(t["price"] / (t["area"] * SQM_TO_SQFT))
            e["price"].append(t["price"])
    out = {}
    for k, e in g.items():
        if len(e["price"]) >= 3:          # too few sales to call anything "typical"
            out[k] = {"n": len(e["price"]), "med": int(round(median(e["price"])))}
            if k != "landed":
                out[k]["psf"] = int(round(median(e["psf"])))
    return {"d": d, "projects": len(rows), "sales": nt, "file": "d%s.json" % d,
            "la": r5(median([p["lat"] for p in plist])), "ln": r5(median([p["lon"] for p in plist])),
            "g": out}


def _months_back(ym, n):
    y, m = divmod(ym, 100)
    m -= n
    while m <= 0:
        m += 12
        y -= 1
    return y * 100 + m


# ------------------------------------------------------------------ positions for projects URA gives none for
# URA leaves out the coordinates of many new launches and some landed streets. For those we ask OneMap
# (free, no key): first for the project by name (exact spot), else for its street (approximate spot).
ABBREV = {
    "RD": "ROAD", "AVE": "AVENUE", "ST": "STREET", "DR": "DRIVE", "CRES": "CRESCENT",
    "CTRL": "CENTRAL", "NTH": "NORTH", "STH": "SOUTH", "BT": "BUKIT", "JLN": "JALAN",
    "LOR": "LORONG", "TER": "TERRACE", "PL": "PLACE", "CL": "CLOSE", "GDNS": "GARDENS",
    "HTS": "HEIGHTS", "TG": "TANJONG", "UPP": "UPPER", "PK": "PARK", "LK": "LINK",
}
GEO_FILE = "geocode.json"


def norm(s):
    return "".join(ch for ch in str(s).upper() if ch.isalnum())


def expand_street(street):
    return " ".join(ABBREV.get(w, w) for w in street.upper().split())


def in_sg(la, lo):
    return LAT_RANGE[0] <= la <= LAT_RANGE[1] and LON_RANGE[0] <= lo <= LON_RANGE[1]


def onemap(q):
    url = ONEMAP_SEARCH + "?" + urllib.parse.urlencode(
        {"searchVal": q, "returnGeom": "Y", "getAddrDetails": "Y", "pageNum": "1"})
    d = get_json(url, {}, tries=5, wait=15, label="OneMap")
    time.sleep(PAUSE_ONEMAP)
    return d.get("results") or []


def result_ll(r):
    try:
        la, lo = float(r["LATITUDE"]), float(r["LONGITUDE"])
    except Exception:
        return None
    return (la, lo) if in_sg(la, lo) else None


def find_by_name(name):
    n = norm(name)
    if len(n) < 4:
        return None
    for r in onemap(name):
        if n in norm("%s %s %s" % (r.get("SEARCHVAL", ""), r.get("BUILDING", ""), r.get("ADDRESS", ""))):
            ll = result_ll(r)
            if ll:
                return ll
    return None


def find_by_street(street):
    words = [w for w in expand_street(street).split() if len(w) > 1]
    pts = []
    for r in onemap(expand_street(street)):
        road = str(r.get("ROAD_NAME", "")).upper()
        ll = result_ll(r)
        if ll and words and all(w in road for w in words[:3]):
            pts.append(ll)
    if not pts:
        return None
    pts = pts[:10]
    return (statistics.median(p[0] for p in pts), statistics.median(p[1] for p in pts))


def is_generic_name(name):
    n = norm(name)
    return (not n) or "LANDED" in n or n in ("NA", "NIL")


def fill_from_onemap(projects, skip):
    need = [p for p in projects if p["x"] is None or p["y"] is None]
    if not need:
        return
    if skip:
        say("   Skipping the OneMap lookups (--no-lookup).")
        return
    gc = {}
    gpath = os.path.join(CACHE_DIR, GEO_FILE)
    if os.path.exists(gpath):
        try:
            with open(gpath, "r", encoding="utf-8") as f:
                gc = json.load(f)
        except Exception:
            gc = {}
    now = time.time()

    def fresh(e):
        # a found exact spot is kept for good; misses and street-only guesses are retried monthly
        if e is None:
            return False
        if e.get("lat") is not None and not e.get("ap"):
            return True
        return (now - e.get("t", 0)) < RETRY_LOOKUP_DAYS * 86400

    todo = []
    for p in need:
        key = "P|%s|%s" % (p["name"], p["street"])
        p["_gkey"] = key
        if not fresh(gc.get(key)):
            todo.append(p)
    if todo:
        say("   Looking up %d projects on OneMap (about %d minutes the first time, and remembered after that)..." % (
            len(todo), max(1, int(len(todo) * 1.6 * PAUSE_ONEMAP / 60) + 1)))
    done = 0
    try:
        for p in todo:
            name, street = p["name"], p["street"]
            hit = None
            if not is_generic_name(name):
                ll = find_by_name(name)
                if ll:
                    hit = {"lat": ll[0], "lon": ll[1], "ap": 0}
            if not hit and street:
                ll = find_by_street(street)
                if ll:
                    hit = {"lat": ll[0], "lon": ll[1], "ap": 1}
            gc[p["_gkey"]] = dict(hit or {"lat": None, "lon": None, "ap": 0}, t=now)
            done += 1
            if done % 25 == 0:
                say("   ...%d of %d looked up" % (done, len(todo)))
                _save_geo(gc, gpath)
    except HttpError as e:
        say("   OneMap refused the lookups (HTTP %s), so those projects stay off the map for now." % e.code)
    except RuntimeError as e:
        say("   OneMap lookups stopped: %s" % e)
    _save_geo(gc, gpath)
    for p in need:
        e = gc.get(p["_gkey"])
        if e and e.get("lat") is not None:
            p["lat"], p["lon"] = r5(e["lat"]), r5(e["lon"])
            p["ap"] = 1 if e.get("ap") else 0


def _save_geo(gc, gpath):
    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(gpath, "w", encoding="utf-8") as f:
        json.dump(gc, f, ensure_ascii=False)


# ------------------------------------------------------------------ build
def build(refresh):
    batches = download_all(refresh)
    say("Tidying up...")
    projects, tx_count = tidy(batches)
    say("   %d projects, %d sales" % (len(projects), tx_count))

    no_xy, out_of_range = [], []
    for p in projects:
        p["lat"] = p["lon"] = None
        if p["x"] is None or p["y"] is None:
            no_xy.append(p)
            continue
        try:
            la, lo = svy21_to_latlon(p["x"], p["y"])
        except Exception:
            out_of_range.append(p)
            continue
        if LAT_RANGE[0] <= la <= LAT_RANGE[1] and LON_RANGE[0] <= lo <= LON_RANGE[1]:
            p["lat"], p["lon"] = r5(la), r5(lo)
        else:
            out_of_range.append(p)
    projects = [p for p in projects if p["tx"]]
    fill_from_onemap(projects, "--no-lookup" in sys.argv[1:])
    located = [p for p in projects if p["lat"] is not None]
    missing = [p for p in projects if p["lat"] is None]
    coord_share = len(located) / max(1, len(projects))
    sales_share = sum(len(p["tx"]) for p in located) / max(1, tx_count_of(projects))
    approx = len([p for p in located if p.get("ap")])
    if approx:
        say("   %d projects are placed by their street only (marked approximate)" % approx)
    say("   %.0f%% of projects, holding %.0f%% of the sales, have a map position" % (coord_share * 100, sales_share * 100))
    if missing:
        say("   Left without a position: %d projects (%d gave no coordinates, %d had coordinates outside Singapore)" % (
            len(missing), len([p for p in missing if p["x"] is None or p["y"] is None]),
            len([p for p in missing if p["x"] is not None and p["y"] is not None])))
        say("   The biggest ones (for checking):")
        for p in sorted(missing, key=lambda p: -len(p["tx"]))[:8]:
            types = ", ".join(sorted(set(t["ptype"] for t in p["tx"])))[:40]
            say("     - %s | %s | %s | %d sales | x=%s y=%s" % (p["name"][:30], p["street"][:30], types, len(p["tx"]), p["x"], p["y"]))

    # Safety checks: if anything looks wrong, leave the current files alone
    problems = []
    if len(projects) < MIN_PROJECTS:
        problems.append("only %d projects (expected at least %d)" % (len(projects), MIN_PROJECTS))
    if tx_count < MIN_TRANSACTIONS:
        problems.append("only %d sales (expected at least %d)" % (tx_count, MIN_TRANSACTIONS))
    if sales_share < MIN_WITH_COORDS:
        problems.append("only %.0f%% of the sales have a map position (expected %.0f%%)" % (sales_share * 100, MIN_WITH_COORDS * 100))
    if problems:
        raise SystemExit("STOPPED, the data does not look right: " + "; ".join(problems) +
                         ". The existing %s folder was not touched." % OUT_DIR)

    all_ym = [t["ym"] for p in projects for t in p["tx"]]
    latest_ym, first_ym = max(all_ym), min(all_ym)

    # Put every project in its postal district (the one most of its sales are in)
    by_district = defaultdict(list)
    dropped = 0
    for p in projects:
        if p["lat"] is None:
            dropped += 1
            continue
        d = Counter(t["district"] for t in p["tx"]).most_common(1)[0][0]
        by_district[d].append(p)
    if dropped:
        say("   %d projects had no usable position and are left out of the map files" % dropped)

    pois = None if "--no-nearby" in sys.argv[1:] else load_pois()
    if pois:
        say("Working out the nearest MRT, schools, hawker centres and supermarkets for each project...")

    tmp = OUT_DIR + "_new"
    if os.path.isdir(tmp):
        for f in os.listdir(tmp):
            os.remove(os.path.join(tmp, f))
    os.makedirs(tmp, exist_ok=True)

    built = time.strftime("%Y-%m-%d")
    index = []
    total_p = total_t = 0
    for d in sorted(by_district):
        idx = {"types": [], "types_t": {}, "tenures": [], "tenures_t": {}, "areas": [], "areas_t": {}}
        plist = sorted(by_district[d], key=lambda p: (p["name"], p["street"]))
        lists, extras = near_pois(plist, pois) if pois else ({}, {})
        rows = [compact_project(p, latest_ym, idx, extras.get(id(p))) for p in plist]
        data = {
            "district": d, "built": built,
            "cols": ["yyyymm", "price", "sqm", "floor", "sale(1 new,2 sub,3 resale)", "type", "tenure", "units", "areaType"],
            "types": idx["types"], "tenures": idx["tenures"], "areaTypes": idx["areas"],
            "P": rows,
        }
        data.update(lists)
        with open(os.path.join(tmp, "d%s.json" % d), "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
        nt = sum(len(r["T"]) for r in rows)
        total_p += len(rows)
        total_t += nt
        index.append(district_summary(d, plist, rows, nt, latest_ym))
    with open(os.path.join(tmp, "index.json"), "w", encoding="utf-8") as f:
        json.dump({
            "built": built,
            "from": first_ym, "to": latest_ym,
            "projects": total_p, "sales": total_t,
            "source": "Urban Redevelopment Authority (URA), Private Residential Property Transactions, via the URA Data Service API. Contains information licensed under the Singapore Open Data Licence.",
            "poi": ({"built": pois["built"]} if pois else None),
            "districts": index,
        }, f, ensure_ascii=False, separators=(",", ":"))

    # Swap in the new folder only once everything was written
    if os.path.isdir(OUT_DIR):
        for f in os.listdir(OUT_DIR):
            os.remove(os.path.join(OUT_DIR, f))
        os.rmdir(OUT_DIR)
    os.rename(tmp, OUT_DIR)

    size = sum(os.path.getsize(os.path.join(OUT_DIR, f)) for f in os.listdir(OUT_DIR))
    say("")
    say("Done. %d districts, %d projects, %d sales (%d to %d), %.1f MB in the %s folder." % (
        len(index), total_p, total_t, first_ym, latest_ym, size / 1e6, OUT_DIR))
    say("Upload ONLY the %s folder to the GitHub repo. Do not upload %s or ura_key.txt." % (OUT_DIR, CACHE_DIR))


def main():
    refresh = "--refresh" in sys.argv[1:]
    say("Private-home data builder | Python %s | %s" % (sys.version.split()[0], time.strftime("%Y-%m-%d %H:%M")))
    code = 0
    try:
        build(refresh)
    except SystemExit as e:
        say(str(e))
        code = 1
    except KeyboardInterrupt:
        say("Stopped. Anything already downloaded is saved, so run it again to carry on.")
        code = 1
    except Exception as e:
        say("STOPPED WITH AN ERROR: %s: %s" % (type(e).__name__, e))
        say("Please copy everything above into the chat (it never contains your key).")
        code = 1
    try:
        with open("ura_build_log.txt", "w", encoding="utf-8") as f:
            f.write("\n".join(LOG))
    except Exception:
        pass
    sys.exit(code)


if __name__ == "__main__":
    main()
