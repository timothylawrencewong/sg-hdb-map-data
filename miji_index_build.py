#!/usr/bin/env python3
"""
Miji Index: build the "price growth over time" data behind the Miji Index screener/charts
(HDB resale by town + flat type, and private property by town, over a rolling 8-year window).

What it does:
  1. Downloads the FULL HDB resale transaction history (Jan 2017 onwards, data.gov.sg, updated
     daily) and works out the median resale price per town, per flat type, per YEAR - plus the
     25th/75th percentile and the sale count, so a thin year can be shown honestly instead of
     hidden. This is separate from sg_build.py's map data on purpose: sg_build.py only keeps the
     last 12 months (that's all the block map needs); the Miji Index needs the full multi-year
     trend, which is a much smaller amount of data (26 towns x 7 types x ~8 years of numbers, not
     every individual block).
  2. Reads the private_data/d*.json files that ura_build.py already produced earlier in the same
     run, and works out median $psf (not total price - condo/landed unit sizes vary too much
     within a type for a total-price median to mean much) per YEAR for each of URA's three market
     segments (CCR / RCR / OCR), again with percentiles + sale count. URA's own Data Service only
     gives about the last 5 years of transactions, so private-property years before that are
     backward-projected from the earliest real growth rate available and marked as such in the
     output - never presented as an actual sale.
  3. Every HDB town is assigned to the market segment it actually sits in (see TOWN_SEGMENT below)
     so "Private (Condo)" and "Landed" numbers for that town use that segment's real $psf trend,
     not one Singapore-wide average.
  4. The most recent slot in every series is NOT "this calendar year so far" (which would be an
     unfair, partial-year number sitting next to seven full calendar years) - it's a genuine
     trailing-12-month window ending at build time. Everything else - the 7 years before that -
     is a clean, complete calendar year, and the whole 8-year window shifts forward by one year
     every time this runs in a new year (YEARS is computed from today's date, not hardcoded), so
     it never needs to be manually bumped.
  5. Writes index_data/miji_index.json in the shape the Miji Index page expects: one entry per
     town, one object per flat type with {vals, n, p25, p75, low} arrays - one slot per year/TTM
     window.

This is real, sourced data - HDB side is exact (every registered resale transaction, from HDB
via data.gov.sg, Open Data Licence, free for personal or commercial use). The private-property
side is an estimate (URA market-segment index/median, not an individual per-town figure), and the
output says so explicitly so the front end can show the same "these are estimates" language it
already uses for the methodology box.

How to run (Terminal), from the folder that holds this file and (for private data) next to a
private_data folder already built by ura_build.py:
    python3 miji_index_build.py
Standard library only, nothing to install.
"""

import json
import os
import statistics
import sys
import time
import urllib.error
import urllib.request
from collections import defaultdict

# ------------------------------------------------------------------ settings
HDB_DATASET_ID = "d_8b84c4ee58e3cfc0ece0d773c8ca6abc"  # "Resale flat prices ... from Jan-2017 onwards"
API_BASE = "https://api-open.data.gov.sg/v1/public/api/datasets/%s/poll-download"
OUT_DIR = "index_data"
PRIVATE_DIR = "private_data"
CACHE_DIR = "index_cache"
CACHE_HOURS = 20   # the HDB dataset updates daily; no need to re-download more than once a day
SQM_TO_SQFT = 10.7639  # matches ura_build.py

_NOW_YEAR = time.gmtime().tm_year
YEARS = list(range(_NOW_YEAR - 7, _NOW_YEAR + 1))  # a rolling 8-year window ending at THIS year -
# computed fresh every run, not a fixed list. Without this, the chart would freeze at whatever
# years it was first written with and never advance as real time passes.
CALENDAR_YEARS = YEARS[:-1]   # the 7 most recent complete calendar years
CURRENT_SLOT = YEARS[-1]      # last slot: a trailing-12-month window (see build_ttm_window below),
                               # not "this year so far" - so it's never compared unfairly against a full year

LOW_SAMPLE_N = 5   # fewer sales than this in a year/window and the point is flagged, not hidden
MIN_YEARS_PRESENT = 2   # need at least 2 data points (any confidence) to call it a usable trend

RECENT_MONTHS = 8   # how far back "Check a Unit" comparables look - matches the site copy
                     # ("sold in the past 8 months"). Independent of YEARS/CURRENT_SLOT above -
                     # this feeds a different output file (recent_transactions.json), not the Index.

FLAT_TYPES = ["2-room", "3-room", "4-room", "5-room", "Executive"]
HDB_FLAT_TYPE_MAP = {
    "2 ROOM": "2-room", "3 ROOM": "3-room", "4 ROOM": "4-room",
    "5 ROOM": "5-room", "EXECUTIVE": "Executive",
    # 1 ROOM and MULTI-GENERATION exist in the raw data but aren't in the Miji Index screener
}
PRIVATE_TYPES = ["Private (Condo)", "Landed"]  # shown as $psf, not total price - see build_private_medians

# Every HDB town, assigned to the URA market segment (CCR / RCR / OCR) it actually sits in.
# This is the standard three-tier split URA itself publishes its private price index by - it is
# the least-arbitrary way to give each town a private-property trend without pretending we have
# per-town private transactions (we don't; see the docstring above).
TOWN_SEGMENT = {
    "Ang Mo Kio": "OCR", "Bedok": "OCR", "Bishan": "RCR", "Bukit Batok": "OCR",
    "Bukit Merah": "RCR", "Bukit Panjang": "OCR", "Bukit Timah": "CCR",
    "Central Area": "CCR", "Choa Chu Kang": "OCR", "Clementi": "OCR",
    "Geylang": "RCR", "Hougang": "OCR", "Jurong East": "OCR", "Jurong West": "OCR",
    "Kallang/Whampoa": "RCR", "Marine Parade": "RCR", "Pasir Ris": "OCR",
    "Punggol": "OCR", "Queenstown": "RCR", "Sembawang": "OCR", "Sengkang": "OCR",
    "Serangoon": "RCR", "Tampines": "OCR", "Toa Payoh": "RCR", "Woodlands": "OCR",
    "Yishun": "OCR", "Central": "RCR",  # both spellings seen across earlier mockups
}

UA = {"User-Agent": "Mozilla/5.0 (compatible; MijiIndexBuilder/1.0; +https://miji.sg)", "Accept": "application/json"}
LOG = []
MONTH_ABBR = ["", "Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def say(line=""):
    print(str(line), flush=True)
    LOG.append(str(line))


# ------------------------------------------------------------------ small stats helpers
def median(vals):
    return statistics.median(vals) if vals else None


def imed(vals):
    m = median(vals)
    return int(round(m)) if m is not None else None


def ipctl(vals):
    """(p25, p75) for a list of numbers - or (v, v) when there's only one, since a percentile
    needs at least 2 points to mean anything. Never crashes on a thin sample."""
    if not vals:
        return (None, None)
    if len(vals) == 1:
        v = int(round(vals[0]))
        return (v, v)
    q = statistics.quantiles(sorted(vals), n=4, method="inclusive")
    return (int(round(q[0])), int(round(q[2])))


def stats_for(vals):
    """One year/window's worth of raw numbers -> {n, med, p25, p75}. None if there's nothing."""
    if not vals:
        return None
    p25, p75 = ipctl(vals)
    return {"n": len(vals), "med": imed(vals), "p25": p25, "p75": p75}


# ------------------------------------------------------------------ trailing-12-month window
def add_months(y, m, delta):
    idx = (y * 12 + (m - 1)) + delta
    return idx // 12, idx % 12 + 1


def build_ttm_window():
    """The trailing-12-month window used for the CURRENT_SLOT, anchored to build time (UTC).
    Returns (set of (year, month) tuples in the window, a human label like 'Oct 2025 - Sep 2026')."""
    now = time.gmtime()
    anchor_y, anchor_m = now.tm_year, now.tm_mon
    months = set(add_months(anchor_y, anchor_m, -i) for i in range(12))
    start_y, start_m = add_months(anchor_y, anchor_m, -11)
    label = "%s %d - %s %d" % (MONTH_ABBR[start_m], start_y, MONTH_ABBR[anchor_m], anchor_y)
    return months, label


# ------------------------------------------------------------------ network helpers (same pattern as ura_build.py)
def http_get(url, timeout=180):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def get_json(url, tries=4, wait=15, label=""):
    last = None
    for attempt in range(tries):
        try:
            return json.loads(http_get(url))
        except urllib.error.HTTPError as e:
            last = e
            if e.code == 429 or e.code >= 500:
                say("   (%s: server asked us to slow down, waiting %ds, try %d of %d)" % (label, wait, attempt + 1, tries))
                time.sleep(wait)
                continue
            raise
        except Exception as e:
            last = e
            time.sleep(5)
    raise RuntimeError("Gave up on %s: %s" % (label or "a request", last))


# ------------------------------------------------------------------ 1. HDB resale history (data.gov.sg)
def hdb_cache_path():
    return os.path.join(CACHE_DIR, "hdb_resale.csv")


def download_hdb_csv():
    os.makedirs(CACHE_DIR, exist_ok=True)
    path = hdb_cache_path()
    if os.path.exists(path) and (time.time() - os.path.getmtime(path)) < CACHE_HOURS * 3600:
        say("Using the HDB resale copy saved earlier today (%s)" % path)
        return path
    say("Asking data.gov.sg for today's HDB resale download link...")
    d = get_json(API_BASE % HDB_DATASET_ID, label="HDB dataset poll-download")
    url = ((d.get("data") or {}).get("url") or "")
    if not url:
        raise SystemExit("data.gov.sg did not give a download link for the HDB resale dataset: %r" % d)
    say("Downloading the full HDB resale history (Jan 2017 onwards, updated daily by HDB)...")
    raw = http_get(url, timeout=300)
    with open(path, "wb") as f:
        f.write(raw)
    say("   saved %.1f MB" % (len(raw) / 1e6))
    return path


def parse_csv_rows(path):
    """Small dependency-free CSV reader - good enough for this dataset (no embedded newlines,
    only the street/block fields ever contain a comma, and those are quoted by the source)."""
    import csv
    with open(path, "r", encoding="utf-8", newline="") as f:
        yield from csv.DictReader(f)


def build_hdb_medians(ttm_months):
    """Returns {town_title_case: {flat_type: {year_or_CURRENT_SLOT: stats_dict}}}.
    Every year with >=1 real sale is included (flagged as 'low' downstream if it's a thin sample)
    - years are never hidden just because sales were few, only when there were truly none."""
    path = download_hdb_csv()
    say("Aggregating HDB resale prices by town, flat type and year...")
    cal_buckets = defaultdict(list)   # (town, ft, year) -> [prices], year in CALENDAR_YEARS
    ttm_buckets = defaultdict(list)   # (town, ft) -> [prices] inside the trailing-12-month window
    rows_seen = 0
    for row in parse_csv_rows(path):
        rows_seen += 1
        ft = HDB_FLAT_TYPE_MAP.get((row.get("flat_type") or "").strip().upper())
        if not ft:
            continue
        month_s = (row.get("month") or "").strip()  # "YYYY-MM"
        if len(month_s) < 7 or not month_s[:4].isdigit() or not month_s[5:7].isdigit():
            continue
        year = int(month_s[:4])
        mon = int(month_s[5:7])
        try:
            price = float(row.get("resale_price") or 0)
        except ValueError:
            continue
        if price <= 0:
            continue
        town = (row.get("town") or "").strip().title().replace("Hdb", "HDB")
        # Fix a couple of data.gov.sg's town spellings to match the site's display names
        town = {"Kallang/Whampoa": "Kallang/Whampoa", "Central Area": "Central Area"}.get(town, town)
        if year in CALENDAR_YEARS:
            cal_buckets[(town, ft, year)].append(price)
        if (year, mon) in ttm_months:
            ttm_buckets[(town, ft)].append(price)
    if rows_seen < 100000:
        raise SystemExit("STOP: only read %d rows from the HDB dataset (expected several hundred thousand). "
                          "The download may be incomplete - existing index data was left untouched." % rows_seen)
    say("   read %d transaction rows" % rows_seen)

    out = defaultdict(lambda: defaultdict(dict))
    for (town, ft, year), prices in cal_buckets.items():
        s = stats_for(prices)
        if s:
            out[town][ft][year] = s
    for (town, ft), prices in ttm_buckets.items():
        s = stats_for(prices)
        if s:
            out[town][ft][CURRENT_SLOT] = s
    return out, sorted(set(t for t, _, _ in cal_buckets) | set(t for t, _ in ttm_buckets))


def build_recent_hdb_transactions(recent_months=RECENT_MONTHS):
    """Individual HDB resale transactions from the last `recent_months` months - the raw material
    for the Check a Unit comparables tool. Reuses the same cached CSV build_hdb_medians() already
    downloaded (download_hdb_csv() caches for CACHE_HOURS, so this never triggers a second
    download) but keeps the per-transaction fields the median-building throws away: block, street,
    storey range and remaining lease. Every row here is a real transaction - nothing estimated or
    backward-projected (that only happens on the private-property side, in
    build_recent_private_transactions). Runs as its own pass over the CSV rather than being folded
    into build_hdb_medians(), on purpose - so a bug here can never affect the Index numbers that
    are already live."""
    path = download_hdb_csv()
    say("Extracting individual HDB transactions from the last %d months (for Check a Unit)..." % recent_months)
    now = time.gmtime()
    cutoff_y, cutoff_m = add_months(now.tm_year, now.tm_mon, -(recent_months - 1))
    cutoff_ym = cutoff_y * 100 + cutoff_m
    rows = []
    for row in parse_csv_rows(path):
        ft = HDB_FLAT_TYPE_MAP.get((row.get("flat_type") or "").strip().upper())
        if not ft:
            continue
        month_s = (row.get("month") or "").strip()
        if len(month_s) < 7 or not month_s[:4].isdigit() or not month_s[5:7].isdigit():
            continue
        year, mon = int(month_s[:4]), int(month_s[5:7])
        ym = year * 100 + mon
        if ym < cutoff_ym:
            continue
        try:
            price = float(row.get("resale_price") or 0)
            area = float(row.get("floor_area_sqm") or 0)
        except ValueError:
            continue
        if price <= 0 or area <= 0:
            continue
        town = (row.get("town") or "").strip().title().replace("Hdb", "HDB")
        town = {"Kallang/Whampoa": "Kallang/Whampoa", "Central Area": "Central Area"}.get(town, town)
        rows.append({
            "town": town,
            "type": ft,
            "blk": (row.get("block") or "").strip(),
            "st": (row.get("street_name") or "").strip().title(),
            "storey": (row.get("storey_range") or "").strip(),    # e.g. "10 TO 12" - a band; HDB
                                                                     # never publishes an exact floor
            "sqm": round(area, 1),
            "lease": (row.get("remaining_lease") or "").strip(),  # e.g. "67 years 04 months"
            "price": int(price),
            "ym": ym,
        })
    say("   kept %d individual transactions from the last %d months" % (len(rows), recent_months))
    return rows


# ------------------------------------------------------------------ 2. Private property, from ura_build.py's own output
def group_of(ptype):
    t = str(ptype).lower()
    if "detached" in t or "terrace" in t:
        return "landed"
    return "condo"  # includes executive condominium - Miji Index doesn't split EC out separately


def build_private_medians(ttm_months):
    """Reads private_data/d*.json (built earlier in the same run by ura_build.py) and returns
    {segment: {"Private (Condo)": {"values": {year_or_CURRENT_SLOT: stats_dict}, "estimated_years": [...]}, "Landed": {...}}}
    for segment in CCR/RCR/OCR. The published number is median $psf, not total price - condo and
    landed unit sizes vary too much within a type for a total-price median to mean much; $psf is
    what's actually comparable across projects. Returns {} (not an error) if private_data isn't
    there - the HDB side of the Miji Index still works on its own."""
    if not os.path.isdir(PRIVATE_DIR):
        say("No %s folder found (ura_build.py hasn't run yet in this job) - "
            "Miji Index will only have HDB data this time." % PRIVATE_DIR)
        return {}
    say("Reading private-property transactions already downloaded by ura_build.py...")
    # seg -> group -> year_or_CURRENT_SLOT -> [psf values]
    cal_buckets = defaultdict(lambda: defaultdict(lambda: defaultdict(list)))
    ttm_buckets = defaultdict(lambda: defaultdict(list))
    files_read = 0
    skipped_files = []
    for name in sorted(os.listdir(PRIVATE_DIR)):
        if not (name.startswith("d") and name.endswith(".json")):
            continue
        try:
            with open(os.path.join(PRIVATE_DIR, name), "r", encoding="utf-8") as f:
                data = json.load(f)
            types = data.get("types", [])
            for proj in data.get("P", []):
                seg = (proj.get("seg") or "").upper()
                if seg not in ("CCR", "RCR", "OCR"):
                    continue
                for t in proj.get("T", []):
                    if len(t) < 6:
                        continue  # a row from an older/shorter format - skip rather than crash
                    ym, price = t[0], t[1]
                    sqm = t[2] if len(t) > 2 else None
                    ptype_idx = t[5]
                    ptype = types[ptype_idx] if 0 <= ptype_idx < len(types) else ""
                    if not sqm or sqm <= 0 or not price or price <= 0:
                        continue  # can't get a $psf out of this row - skip it, don't fake a size
                    year, mon = ym // 100, ym % 100
                    psf = price / (sqm * SQM_TO_SQFT)
                    g = group_of(ptype)
                    if year in CALENDAR_YEARS:
                        cal_buckets[seg][g][year].append(psf)
                    if (year, mon) in ttm_months:
                        ttm_buckets[seg][g].append(psf)
        except Exception as e:
            # One oddly-shaped district file shouldn't take down the whole build - skip it and
            # carry on with the rest, which is far better than losing all the private data.
            skipped_files.append("%s (%s: %s)" % (name, type(e).__name__, e))
            continue
        files_read += 1
    if skipped_files:
        say("   NOTE: skipped %d district file(s) that didn't parse as expected: %s" %
            (len(skipped_files), "; ".join(skipped_files)))
    if not files_read:
        say("   %s existed but had no district files in it - skipping private data." % PRIVATE_DIR)
        return {}
    say("   read %d district files" % files_read)

    segs = set(list(cal_buckets.keys()) + list(ttm_buckets.keys()))
    out = {}
    for seg in segs:
        out[seg] = {}
        for group, label in (("condo", "Private (Condo)"), ("landed", "Landed")):
            real = {}
            for y in CALENDAR_YEARS:
                vals = cal_buckets.get(seg, {}).get(group, {}).get(y)
                s = stats_for(vals) if vals else None
                if s:
                    real[y] = s
            ttm_vals = ttm_buckets.get(seg, {}).get(group)
            ttm_stats = stats_for(ttm_vals) if ttm_vals else None
            if ttm_stats:
                real[CURRENT_SLOT] = ttm_stats
            if not real:
                continue
            # URA's Data Service only gives ~5 years back, AND the current window can still be
            # thin for a slow segment/group. Project both directions using the earliest
            # year-over-year $psf growth rate we do have, rather than inventing an unrelated
            # number. Filled slots are marked in the output as estimated - never shown as a real sale.
            filled, est_slots = dict(real), []
            known = sorted(y for y in real if y != CURRENT_SLOT) or sorted(real)
            if len(known) >= 2:
                growth = real[known[1]]["med"] / real[known[0]]["med"] if real[known[0]]["med"] else 1.0
            else:
                growth = 1.0
            growth = max(growth, 0.5)  # guard against a wild or negative rate from thin data
            for y in sorted(CALENDAR_YEARS, reverse=True):  # backward: older than the real window
                if y in filled:
                    continue
                nxt = y + 1 if y + 1 in filled else None
                if nxt is not None:
                    filled[y] = {"n": 0, "med": int(round(filled[nxt]["med"] / growth)), "p25": None, "p75": None}
                    est_slots.append(y)
            fill_order = sorted(CALENDAR_YEARS) + [CURRENT_SLOT]
            for y in fill_order:  # forward: newer than the real window (incl. the TTM slot)
                if y in filled:
                    continue
                prv = y - 1 if y != CURRENT_SLOT else CALENDAR_YEARS[-1]
                if prv in filled:
                    filled[y] = {"n": 0, "med": int(round(filled[prv]["med"] * growth)), "p25": None, "p75": None}
                    est_slots.append(y)
            out[seg][label] = {"values": filled, "estimated_slots": sorted(est_slots)}
    return out


def build_recent_private_transactions(recent_months=RECENT_MONTHS):
    """Individual private-property transactions from the last `recent_months` months, read from
    the same private_data/d*.json files build_private_medians() already reads (written earlier in
    the same run by ura_build.py) - no second URA API call, no new use of your AccessKey. Only
    single-unit sales are kept (a bulk/multi-unit sale distorts a per-unit comparison - the same
    rule ura_build.py's own compact_project() already applies for its psf summaries). Returns []
    if private_data isn't there, same as build_private_medians(). Runs as its own pass for the same
    reason as build_recent_hdb_transactions() - isolated from the numbers already live on the Index."""
    if not os.path.isdir(PRIVATE_DIR):
        return []
    say("Extracting individual private-property transactions from the last %d months..." % recent_months)
    now = time.gmtime()
    cutoff_y, cutoff_m = add_months(now.tm_year, now.tm_mon, -(recent_months - 1))
    cutoff_ym = cutoff_y * 100 + cutoff_m
    rows = []
    skipped_files = []
    for name in sorted(os.listdir(PRIVATE_DIR)):
        if not (name.startswith("d") and name.endswith(".json")):
            continue
        try:
            with open(os.path.join(PRIVATE_DIR, name), "r", encoding="utf-8") as f:
                data = json.load(f)
            types = data.get("types", [])
            tenures = data.get("tenures", [])
            for proj in data.get("P", []):
                seg = (proj.get("seg") or "").upper()
                if seg not in ("CCR", "RCR", "OCR"):
                    continue
                pname = proj.get("n") or ""
                pstreet = proj.get("s") or ""
                for t in proj.get("T", []):
                    # columns, per ura_build.py's compact_project(): [ym, price, sqm, floor, sale,
                    # type_idx, tenure_idx, units, areaType_idx]
                    if len(t) < 8:
                        continue  # a row from an older/shorter format - skip rather than crash
                    ym, price, sqm, floor = t[0], t[1], t[2], t[3]
                    ptype_idx, tenure_idx, units = t[5], t[6], t[7]
                    if units != 1:
                        continue  # bulk sale - not a fair per-unit comparison
                    if ym < cutoff_ym or not sqm or sqm <= 0 or not price or price <= 0:
                        continue
                    ptype = types[ptype_idx] if 0 <= ptype_idx < len(types) else ""
                    tenure = tenures[tenure_idx] if 0 <= tenure_idx < len(tenures) else ""
                    rows.append({
                        "project": pname,
                        "street": pstreet,
                        "seg": seg,
                        "type": group_of(ptype),   # "condo" or "landed" - matches build_private_medians' own grouping
                        "ptype": ptype,             # URA's raw property type, e.g. "Apartment", "Terrace"
                        "tenure": tenure,
                        "floor": floor,             # URA's floorRange band, e.g. "10 TO 12" - not an exact floor
                        "sqm": round(sqm, 1),
                        "price": int(price),
                        "ym": ym,
                    })
        except Exception as e:
            skipped_files.append("%s (%s: %s)" % (name, type(e).__name__, e))
            continue
    if skipped_files:
        say("   NOTE: skipped %d district file(s) that didn't parse as expected: %s" %
            (len(skipped_files), "; ".join(skipped_files)))
    say("   kept %d individual private-property transactions from the last %d months" % (len(rows), recent_months))
    return rows


# ------------------------------------------------------------------ 3. combine + write
def slots_to_entry(stats_by_slot):
    """{year_or_CURRENT_SLOT: stats_dict} for all 8 YEARS slots -> {vals, n, p25, p75, low}
    ready to publish. A slot that's missing entirely stays null across the board - never faked."""
    vals, ns, p25s, p75s, lows = [], [], [], [], []
    for y in YEARS:
        s = stats_by_slot.get(y)
        if not s or s.get("med") is None:
            vals.append(None); ns.append(None); p25s.append(None); p75s.append(None); lows.append(False)
        else:
            vals.append(s["med"]); ns.append(s.get("n")); p25s.append(s.get("p25")); p75s.append(s.get("p75"))
            n = s.get("n") or 0
            lows.append(n < LOW_SAMPLE_N)
    return {"vals": vals, "n": ns, "p25": p25s, "p75": p75s, "low": lows}


def main():
    say("Miji Index data builder | %s" % time.strftime("%Y-%m-%d %H:%M"))
    ttm_months, ttm_label = build_ttm_window()
    say("   trailing-12-month window for the latest slot: %s" % ttm_label)
    hdb, towns_seen = build_hdb_medians(ttm_months)
    private = build_private_medians(ttm_months)

    unmapped = [t for t in towns_seen if t not in TOWN_SEGMENT]
    if unmapped:
        say("   NOTE: these towns appeared in the HDB data but aren't in TOWN_SEGMENT yet, "
            "so they'll have no private-property numbers until added: %s" % ", ".join(unmapped))

    private_estimated = {}
    result_towns = {}
    borderline = []  # (town, type, n_years, sale_years) - had *some* real data but got dropped anyway
    for town, by_type in hdb.items():
        entry = {}
        for ft in FLAT_TYPES:
            slots = by_type.get(ft, {})
            if len(slots) >= MIN_YEARS_PRESENT:
                entry[ft] = slots_to_entry(slots)
            elif slots:
                # There WAS at least one real sale somewhere in the YEARS window, just not in enough
                # different years to draw a trend line (need >=2). Not the same as zero sales ever -
                # logged here so it's visible instead of silently looking identical to "no data at all".
                borderline.append((town, ft, len(slots), sorted(slots.keys())))
        seg = TOWN_SEGMENT.get(town)
        if seg and seg in private:
            for label, d in private[seg].items():
                vals = d["values"]
                if all(y in vals for y in YEARS):
                    entry[label] = slots_to_entry(vals)
                    if seg not in private_estimated:
                        private_estimated[seg] = {}
                    private_estimated[seg][label] = d["estimated_slots"]
        if entry:
            result_towns[town] = entry

    if borderline:
        say("   NOTE: %d town/type combo(s) had at least one real sale in %d-%d but not in "
            "enough different years to draw a trend (need data in >=%d years) - shown as blank "
            "on the site, not the same as zero sales ever:" % (len(borderline), YEARS[0], YEARS[-1], MIN_YEARS_PRESENT))
        for town, ft, n_years, sale_years in sorted(borderline):
            say("      %s / %s: real data in %s only" % (town, ft, sale_years))
    else:
        say("   Every town/type combo with any %d-%d sales cleared the >=%d-year bar - "
            "every remaining blank is a genuine zero sales, not a filtered borderline case." % (YEARS[0], YEARS[-1], MIN_YEARS_PRESENT))

    if len(result_towns) < 20:
        raise SystemExit("STOP: only %d towns came out with usable data (expected 20+). "
                          "Existing index data was left untouched." % len(result_towns))

    os.makedirs(OUT_DIR, exist_ok=True)
    out_path = os.path.join(OUT_DIR, "miji_index.json")
    payload = {
        "built": time.strftime("%Y-%m-%d"),
        "years": YEARS,
        "current_slot_label": ttm_label,   # what the last "year" tick actually means - see docstring
        "low_sample_n": LOW_SAMPLE_N,
        "sources": {
            "hdb": "Housing & Development Board (HDB), Resale Flat Prices, via data.gov.sg. "
                   "Every registered resale transaction, median per town/flat type/year (25th-75th "
                   "percentile and sale count included). Singapore Open Data Licence - free for "
                   "personal or commercial use.",
            "private": "Urban Redevelopment Authority (URA), Private Residential Property "
                       "Transactions, via the URA Data Service API. Median $ per square foot, "
                       "grouped by URA market segment (CCR/RCR/OCR) and applied to each town in "
                       "that segment - an estimate, not a per-town transaction median. Years older "
                       "than URA's ~5-year window are backward-projected from the earliest "
                       "available growth rate and marked in 'private_estimated_slots' below.",
            "current_slot": "The most recent point in every series is a trailing 12-month window "
                             "(%s), not a partial calendar year - so it's never compared unfairly "
                             "against a full year of data." % ttm_label,
        },
        "private_estimated_slots": private_estimated,
        "towns": result_towns,
    }
    tmp_path = out_path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp_path, out_path)
    say("")
    say("Done. %d towns written to %s" % (len(result_towns), out_path))

    # ------------------------------------------------------------------ 4. recent individual transactions, for Check a Unit
    # Deliberately AFTER the Index above is safely written, and deliberately wrapped so any problem
    # here - a bad row, a changed column name, a format ura_build.py hasn't produced yet - can never
    # stop miji_index.json (which the live site already depends on) from being written. Worst case
    # if this block fails: the Index updates as normal and recent_transactions.json just doesn't
    # refresh this run, logged below rather than silently swallowed.
    try:
        recent_hdb = build_recent_hdb_transactions()
        recent_private = build_recent_private_transactions()
        recent_path = os.path.join(OUT_DIR, "recent_transactions.json")
        recent_payload = {
            "built": time.strftime("%Y-%m-%d"),
            "recent_months": RECENT_MONTHS,
            "hdb": recent_hdb,
            "private": recent_private,
            "sources": {
                "hdb": "Housing & Development Board (HDB), Resale Flat Prices, via data.gov.sg. "
                       "Individual transactions from the last %d months (block, street, storey "
                       "range, floor area, remaining lease, price) - for the Check a Unit "
                       "comparables tool, not the Index above." % RECENT_MONTHS,
                "private": "Urban Redevelopment Authority (URA), Private Residential Property "
                           "Transactions, via the URA Data Service API. Individual single-unit "
                           "sales only from the last %d months." % RECENT_MONTHS,
            },
        }
        recent_tmp = recent_path + ".tmp"
        with open(recent_tmp, "w", encoding="utf-8") as f:
            json.dump(recent_payload, f, ensure_ascii=False, separators=(",", ":"))
        os.replace(recent_tmp, recent_path)
        say("Done. %d HDB + %d private transactions written to %s" %
            (len(recent_hdb), len(recent_private), recent_path))
    except Exception as e:
        say("")
        say("NOTE: recent_transactions.json was NOT updated this run (miji_index.json above is "
            "unaffected) - %s: %s" % (type(e).__name__, e))


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
        with open("miji_index_build_log.txt", "w", encoding="utf-8") as f:
            f.write("\n".join(LOG))
    except Exception:
        pass
    sys.exit(code)
