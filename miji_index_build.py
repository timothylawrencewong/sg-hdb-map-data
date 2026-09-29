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
     run - one file per URA postal district (d01.json ... d28.json) - and works out median $psf
     (not total price - condo/landed unit sizes vary too much within a type for a total-price
     median to mean much) per YEAR for each district, again with percentiles + sale count. URA's
     own Data Service only gives about the last 5 years of transactions, so private-property years
     before that are backward-projected from the earliest real growth rate available and marked as
     such in the output - never presented as an actual sale.
  3. Every HDB town is assigned to the URA postal district(s) it actually sits in (see
     TOWN_DISTRICT below), so "Private (Condo)" and "Landed" numbers for that town are pooled from
     real transactions in its own district(s) - not from a Singapore-wide average, and not lumped
     in with every other town in the same broad CCR/RCR/OCR segment either. A town whose district
     is shared with other towns (there are only 28 districts for 26 towns, and a couple of towns
     genuinely straddle two districts - see TOWN_DISTRICT) will still share numbers with those
     specific towns, since that's the real geography, not an approximation shortcut.
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
side is a district-level estimate (URA postal-district median, pooled across a town's assigned
district(s) - not an exact per-town transaction count, since landed/condo sales in any one HDB
town are usually too thin on their own), and the output says so explicitly so the front end can
show the same "these are estimates" language it already uses for the methodology box.

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
HDB_PROPERTY_INFO_DATASET_ID = "d_17f5382f26140b1fdae0ba2ef6239d2f"  # "HDB Property Information" -
# EVERY HDB block that exists (~13.4K rows, updated quarterly), not just ones with a recent
# resale - feeds the agent listing form's complete address directory, see build_hdb_directory().
API_BASE = "https://api-open.data.gov.sg/v1/public/api/datasets/%s/poll-download"
OUT_DIR = "index_data"
PRIVATE_DIR = "private_data"
CACHE_DIR = "index_cache"
CACHE_HOURS = 20   # the HDB dataset updates daily; no need to re-download more than once a day
PROPERTY_INFO_CACHE_HOURS = 24 * 30   # this one only updates quarterly - no need to refetch often
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

# Every HDB town, assigned to the URA postal district(s) it actually sits in - district codes
# match the private_data/d##.json filenames ura_build.py already writes (e.g. "20" -> d20.json).
# This replaces an earlier, coarser version of this mapping that only used the three CCR/RCR/OCR
# market segments - with just 3 segments, every town in the same segment showed IDENTICAL
# Condo/Landed numbers (e.g. Ang Mo Kio, Tampines and Punggol are all "OCR"), which looked like a
# bug even though it wasn't one. URA's own postal districts (28 of them) are the standard, publicly
# documented finer split - most towns get their own district and a real, differentiated trend;
# a handful of towns still share a district with a neighbour (there are only 28 districts for 26
# towns, and HDB town boundaries don't line up exactly with URA's), which is the genuine geography,
# not a shortcut. A town listed with two districts (e.g. Bukit Merah) has its real transactions
# pooled across both before the median is taken, since its planning area genuinely spans both.
# Source: URA's published postal-district reference, cross-checked against two independent SG
# property-district guides (see the note on build_private_medians for specifics).
TOWN_DISTRICT = {
    "Ang Mo Kio": ["20"], "Bedok": ["16"], "Bishan": ["20"], "Bukit Batok": ["23"],
    "Bukit Merah": ["03", "04"], "Bukit Panjang": ["23"], "Bukit Timah": ["10"],
    "Central Area": ["01", "02", "06", "07"], "Choa Chu Kang": ["23"], "Clementi": ["05"],
    "Geylang": ["14"], "Hougang": ["19"], "Jurong East": ["22"], "Jurong West": ["22"],
    "Kallang/Whampoa": ["12"], "Marine Parade": ["15"], "Pasir Ris": ["18"],
    "Punggol": ["19"], "Queenstown": ["03"], "Sembawang": ["27"], "Sengkang": ["19"],
    "Serangoon": ["19"], "Tampines": ["18"], "Toa Payoh": ["12"], "Woodlands": ["25"],
    "Yishun": ["27"], "Central": ["01", "02", "06", "07"],  # both spellings seen across earlier mockups
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


def property_info_cache_path():
    return os.path.join(CACHE_DIR, "hdb_property_info.csv")


def download_hdb_property_info_csv():
    os.makedirs(CACHE_DIR, exist_ok=True)
    path = property_info_cache_path()
    if os.path.exists(path) and (time.time() - os.path.getmtime(path)) < PROPERTY_INFO_CACHE_HOURS * 3600:
        say("Using the HDB Property Information copy saved earlier (%s)" % path)
        return path
    say("Asking data.gov.sg for the HDB Property Information download link...")
    d = get_json(API_BASE % HDB_PROPERTY_INFO_DATASET_ID, label="HDB Property Information poll-download")
    url = ((d.get("data") or {}).get("url") or "")
    if not url:
        raise SystemExit("data.gov.sg did not give a download link for HDB Property Information: %r" % d)
    say("Downloading the complete HDB block directory (every block, updated quarterly by HDB)...")
    raw = http_get(url, timeout=120)
    with open(path, "wb") as f:
        f.write(raw)
    say("   saved %.1f MB" % (len(raw) / 1e6))
    return path


def build_hdb_directory():
    """EVERY residential HDB block that currently exists - not filtered to recent resale activity
    the way recent_transactions.json (and the address suggestions built from it) used to be. Feeds
    the agent listing form's address directory (hdb_directory.json) so a block that just hasn't
    sold in the last several months (e.g. 542 Jelapang Rd) is still searchable and geocodable -
    previously it would silently be missing from suggestions with no way to tell why. Non-
    residential rows (standalone multistorey carparks, market/hawker buildings, etc. - this
    dataset includes those too) are skipped; an agent is never listing one of those."""
    path = download_hdb_property_info_csv()
    say("Building the complete HDB block directory...")
    seen = {}
    out = []
    skipped_non_residential = 0
    for row in parse_csv_rows(path):
        if (row.get("residential") or "").strip().upper() != "Y":
            skipped_non_residential += 1
            continue
        blk = (row.get("blk_no") or "").strip()
        st = (row.get("street") or "").strip().title()
        town = (row.get("bldg_contract_town") or "").strip().title().replace("Hdb", "HDB")
        if not blk or not st:
            continue
        key = (blk + "|" + st).lower()
        if key in seen:
            continue
        seen[key] = True
        out.append({"blk": blk, "st": st, "town": town})
    say("   %d residential HDB blocks in the directory (%d non-residential rows skipped)"
        % (len(out), skipped_non_residential))
    return out


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
    """Reads private_data/d*.json (built earlier in the same run by ura_build.py - one file per
    URA postal district, e.g. d20.json) and returns
    {town: {"Private (Condo)": {"values": {year_or_CURRENT_SLOT: stats_dict}, "estimated_slots": [...]}, "Landed": {...}}}
    for every town in TOWN_DISTRICT. Raw $psf sales are bucketed by district first (the real,
    file-level granularity), then pooled across whichever district(s) TOWN_DISTRICT assigns to
    each town BEFORE taking a median - a town spanning two districts (see TOWN_DISTRICT) gets one
    combined pool of real sales, not an average of two separately-computed medians, so the
    percentile/sale-count figures stay meaningful. The published number is median $psf, not total
    price - condo and landed unit sizes vary too much within a type for a total-price median to
    mean much; $psf is what's actually comparable across projects. Returns {} (not an error) if
    private_data isn't there - the HDB side of the Miji Index still works on its own."""
    if not os.path.isdir(PRIVATE_DIR):
        say("No %s folder found (ura_build.py hasn't run yet in this job) - "
            "Miji Index will only have HDB data this time." % PRIVATE_DIR)
        return {}
    say("Reading private-property transactions already downloaded by ura_build.py...")
    # district ("01".."28", from the d##.json filename) -> group -> year_or_CURRENT_SLOT -> [psf values]
    cal_buckets = defaultdict(lambda: defaultdict(lambda: defaultdict(list)))
    ttm_buckets = defaultdict(lambda: defaultdict(list))
    files_read = 0
    skipped_files = []
    for name in sorted(os.listdir(PRIVATE_DIR)):
        if not (name.startswith("d") and name.endswith(".json")):
            continue
        district = name[1:-5]  # "d20.json" -> "20" - matches TOWN_DISTRICT's codes directly
        try:
            with open(os.path.join(PRIVATE_DIR, name), "r", encoding="utf-8") as f:
                data = json.load(f)
            types = data.get("types", [])
            for proj in data.get("P", []):
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
                        cal_buckets[district][g][year].append(psf)
                    if (year, mon) in ttm_months:
                        ttm_buckets[district][g].append(psf)
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

    out = {}
    for town, districts in TOWN_DISTRICT.items():
        out[town] = {}
        for group, label in (("condo", "Private (Condo)"), ("landed", "Landed")):
            real = {}
            for y in CALENDAR_YEARS:
                # Pool the RAW sale psf's across every district this town is assigned to before
                # taking the median - not an average of pre-computed per-district medians, so a
                # two-district town's percentiles/sale-count still describe one real sample.
                vals = []
                for d in districts:
                    vals.extend(cal_buckets.get(d, {}).get(group, {}).get(y, []))
                s = stats_for(vals) if vals else None
                if s:
                    real[y] = s
            ttm_vals = []
            for d in districts:
                ttm_vals.extend(ttm_buckets.get(d, {}).get(group, []))
            ttm_stats = stats_for(ttm_vals) if ttm_vals else None
            if ttm_stats:
                real[CURRENT_SLOT] = ttm_stats
            if not real:
                continue
            # URA's Data Service only gives ~5 years back, AND the current window can still be
            # thin for a slow district/group. Project both directions using the earliest
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
            out[town][label] = {"values": filled, "estimated_slots": sorted(est_slots)}
        if not out[town]:
            del out[town]
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


# ------------------------------------------------------------------ 2b. same-block / same-project price history (Check a Unit)
def build_hdb_block_history():
    """Median resale price per YEAR for every (town, block, street, flat type) combination -
    the block-level equivalent of build_hdb_medians() above, just keyed one level deeper. This is
    what lets Check a Unit show "how has THIS block's price moved over the years" (the same idea
    as PropertyGuru's same-block, multi-year comparison), as a separate, much smaller number
    (one median per block per year) rather than every individual transaction going back years,
    which would bloat recent_transactions.json 10x+ for comparatively little benefit. Reuses the
    same cached CSV download_hdb_csv() already saved (no second download). Isolated pass, on
    purpose - wrapped in its own try/except at the call site so a problem here can never affect
    miji_index.json or recent_transactions.json, both already live."""
    path = download_hdb_csv()
    say("Aggregating HDB resale prices by block, for the same-block price-history view...")
    buckets = defaultdict(lambda: defaultdict(list))  # (town, blk, st, ft) -> year -> [prices]
    for row in parse_csv_rows(path):
        ft = HDB_FLAT_TYPE_MAP.get((row.get("flat_type") or "").strip().upper())
        if not ft:
            continue
        month_s = (row.get("month") or "").strip()
        if len(month_s) < 7 or not month_s[:4].isdigit() or not month_s[5:7].isdigit():
            continue
        year = int(month_s[:4])
        if year not in YEARS:   # same rolling 8-year window as the Miji Index above
            continue
        try:
            price = float(row.get("resale_price") or 0)
        except ValueError:
            continue
        if price <= 0:
            continue
        blk = (row.get("block") or "").strip()
        st = (row.get("street_name") or "").strip().title()
        if not blk or not st:
            continue
        town = (row.get("town") or "").strip().title().replace("Hdb", "HDB")
        town = {"Kallang/Whampoa": "Kallang/Whampoa", "Central Area": "Central Area"}.get(town, town)
        buckets[(town, blk, st, ft)][year].append(price)

    out = []
    for (town, blk, st, ft), by_year in buckets.items():
        years_out = {}
        for y, prices in by_year.items():
            m = imed(prices)
            if m is not None:
                years_out[str(y)] = {"med": m, "n": len(prices)}
        if years_out:
            out.append({"town": town, "blk": blk, "st": st, "type": ft, "years": years_out})
    say("   built price history for %d block + flat-type combinations" % len(out))
    return out


def build_private_project_history():
    """Median $psf per YEAR for every private project (+ property type within it) - the
    project-level equivalent of build_private_medians(). Reuses the private_data/d*.json files
    ura_build.py already wrote earlier in this run (no new URA API call). URA's Data Service only
    gives ~5 real years, same ceiling as the Index above - but unlike the Index, this does NOT
    backward-project older years, since a fabricated point on one specific project's chart would
    be misleading in a way a townwide trend estimate isn't. A project's history here simply stops
    where the real data does."""
    if not os.path.isdir(PRIVATE_DIR):
        return []
    say("Aggregating private-property prices by project, for the same-project price-history view...")
    buckets = defaultdict(lambda: defaultdict(list))  # (project, street, seg, group) -> year -> [psf]
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
                pname = proj.get("n") or ""
                pstreet = proj.get("s") or ""
                for t in proj.get("T", []):
                    if len(t) < 8:
                        continue
                    ym, price, sqm = t[0], t[1], t[2]
                    ptype_idx, units = t[5], t[7]
                    if units != 1 or not sqm or sqm <= 0 or not price or price <= 0:
                        continue
                    year = ym // 100
                    if year not in YEARS:
                        continue
                    ptype = types[ptype_idx] if 0 <= ptype_idx < len(types) else ""
                    psf = price / (sqm * SQM_TO_SQFT)
                    buckets[(pname, pstreet, seg, group_of(ptype))][year].append(psf)
        except Exception as e:
            skipped_files.append("%s (%s: %s)" % (name, type(e).__name__, e))
            continue
    if skipped_files:
        say("   NOTE: skipped %d district file(s) that didn't parse as expected: %s" %
            (len(skipped_files), "; ".join(skipped_files)))

    out = []
    for (pname, pstreet, seg, group), by_year in buckets.items():
        years_out = {}
        for y, psfs in by_year.items():
            m = imed(psfs)
            if m is not None:
                years_out[str(y)] = {"med_psf": m, "n": len(psfs)}
        if years_out:
            out.append({"project": pname, "street": pstreet, "seg": seg, "type": group, "years": years_out})
    say("   built price history for %d private projects" % len(out))
    return out


# ------------------------------------------------------------------ nearby amenities (data.gov.sg)
# Feeds the "X min walk to Y" lines on a listing's detail page - not just MRT, but the other
# things buyers actually ask about (hawker centre, park). Each category is computed here, once,
# as a small static lookup table - NOT a live per-listing API call - because (a) these locations
# barely ever change, so re-fetching them on every visitor's page load would be wasted work for
# data that's stable for months at a time, and (b) it keeps the listing page down to a couple of
# calls (geocode the address, then plain arithmetic against these files) instead of depending on
# OneMap's newer routing/nearby-amenity endpoints, which - unlike the plain address search this
# site already uses - require an OneMap account and an auth token that would have to sit in
# public front-end code. A straight-line ("as the crow flies") distance is what's used, same
# convention URA/HDB themselves use for walking-distance estimates (roughly 80m per minute of
# walking) - it can be a little off from the actual walking route around a building, but it's
# the same honest approximation the rest of the industry uses for this exact purpose.
#
# Categories are limited, on purpose, to ones with a reliable government point-location dataset
# (name + lat/lng ready to use). Schools were considered and left out for now - MOE's directory
# has no coordinates, only postal codes, which would mean geocoding ~350 schools one at a time
# during every build. Malls aren't published as open government location data at all.
# Supermarkets ARE published (NEA's List of Supermarket Licences) but, like schools, only as an
# address/postal code - no lat/lng - so they can't go straight into a category here the way
# MRT/hawker/park do. build_supermarket_addresses() below just parses + dedupes that list into
# index_data/supermarket_addresses.json; miji_geocode_addresses.py (the OneMap-authenticated
# script, run right after this one) is what actually geocodes each address and adds the
# "supermarket" category to nearby_amenities.json - same reason HDB block geocoding lives in that
# separate script and not here: it needs an authenticated OneMap account, so isolating it means a
# OneMap problem can never stop this file's other outputs from updating.
MRT_STATION_DATASET_ID = "d_8d886e3a83934d7447acdf5bc6959999"    # URA Master Plan 2019 Rail Station layer
HAWKER_CENTRE_DATASET_ID = "d_4a086da0a5553be1d89383cd90d07ecd"  # NEA Hawker Centres
PARK_DATASET_ID = "d_0542d48f0991541706b58059381a6eca"           # NParks Parks
SUPERMARKET_DATASET_ID = "d_11edd0117280c5776651d7891114c88c"    # NEA List of Supermarket Licences
AMENITY_CACHE_HOURS = 24 * 30   # these barely move month to month - no need to refetch every run
SUPERMARKET_CACHE_HOURS = 24 * 30


def download_geojson_dataset(dataset_id, cache_name, label):
    os.makedirs(CACHE_DIR, exist_ok=True)
    path = os.path.join(CACHE_DIR, cache_name)
    if os.path.exists(path) and (time.time() - os.path.getmtime(path)) < AMENITY_CACHE_HOURS * 3600:
        say("Using the %s layer saved earlier (%s)" % (label, path))
        return path
    say("Asking data.gov.sg for the %s download link..." % label)
    d = get_json(API_BASE % dataset_id, label="%s dataset poll-download" % label)
    url = ((d.get("data") or {}).get("url") or "")
    if not url:
        raise SystemExit("data.gov.sg did not give a download link for %s: %r" % (label, d))
    say("Downloading %s..." % label)
    raw = http_get(url, timeout=120)
    with open(path, "wb") as f:
        f.write(raw)
    say("   saved %.1f KB" % (len(raw) / 1e3))
    return path


def _polygon_centroid(coords):
    """Plain average-of-vertices centroid for one polygon ring. Not area-weighted, but these are
    small outline shapes, so the difference from a true centroid is at most a few metres - well
    within the margin of a straight-line walking-distance estimate anyway."""
    ring = coords[0]  # outer ring - these outlines don't have holes
    xs = [p[0] for p in ring]
    ys = [p[1] for p in ring]
    return sum(xs) / len(xs), sum(ys) / len(ys)


def build_named_points(dataset_id, cache_name, label, extra_field=None, extra_key=None, extra_default=""):
    """Returns [{name, lat, lng[, extra_key]}, ...] - one point per named feature. A name can
    appear as several polygons/points in the source layer (interchange platforms, multiple
    entrances) - these are merged into one point per NAME by averaging that name's positions, so
    a bigger site doesn't quietly get more "pull" just because it has more shapes. Field names
    are the ones data.gov.sg's own dataset page documents (NAME, plus RAIL_TYPE for the MRT
    layer); the fallbacks and the diagnostic line below are there so a schema change on their end
    shows up clearly in the run log instead of silently producing an empty file."""
    path = download_geojson_dataset(dataset_id, cache_name, label)
    with open(path, "r", encoding="utf-8") as f:
        raw = json.load(f)

    features = raw.get("features") or []
    by_name = defaultdict(list)  # name -> [(lng, lat, extra_val), ...]
    for feat in features:
        props = feat.get("properties") or {}
        name = (props.get("NAME") or props.get("Name") or props.get("name")
                or props.get("STN_NAME") or props.get("Station") or "").strip()
        extra_val = extra_default
        if extra_field:
            extra_val = (props.get(extra_field) or extra_default or "").strip() or extra_default
        geom = feat.get("geometry") or {}
        if not name or not geom:
            continue
        gtype = geom.get("type")
        if gtype == "Point":
            coords = geom.get("coordinates") or []
            if len(coords) >= 2:
                by_name[name].append((coords[0], coords[1], extra_val))
            continue
        polys = []
        if gtype == "Polygon":
            polys = [geom.get("coordinates")]
        elif gtype == "MultiPolygon":
            polys = geom.get("coordinates") or []
        for poly in polys:
            try:
                lng, lat = _polygon_centroid(poly)
                by_name[name].append((lng, lat, extra_val))
            except (IndexError, ZeroDivisionError, TypeError):
                continue

    if not by_name and features:
        say("   NOTE: %s - found %d features but none had a usable NAME/geometry - the source "
            "dataset's field names may have changed. First feature's properties: %r" %
            (label, len(features), (features[0].get("properties") or {})))

    points = []
    for name, pts in sorted(by_name.items()):
        lngs = [p[0] for p in pts]
        lats = [p[1] for p in pts]
        entry = {
            "name": name.title() if name.isupper() else name,
            "lat": round(sum(lats) / len(lats), 6),
            "lng": round(sum(lngs) / len(lngs), 6),
        }
        if extra_field:
            extras = sorted(set(p[2] for p in pts if p[2]))
            entry[extra_key] = "/".join(extras) if extras else extra_default
        points.append(entry)
    return points


def build_mrt_stations():
    return build_named_points(
        MRT_STATION_DATASET_ID, "mrt_stations_raw.geojson", "MRT/LRT station layer",
        extra_field="RAIL_TYPE", extra_key="rail_type", extra_default="MRT",
    )


def build_hawker_centres():
    return build_named_points(
        HAWKER_CENTRE_DATASET_ID, "hawker_centres_raw.geojson", "Hawker Centres layer",
    )


def build_parks():
    return build_named_points(
        PARK_DATASET_ID, "parks_raw.geojson", "Parks layer",
    )


def supermarket_cache_path():
    return os.path.join(CACHE_DIR, "supermarket_licences.csv")


def download_supermarket_csv():
    """NEA's List of Supermarket Licences - a CSV, unlike the GEOJSON layers above (see the NOTE
    above SUPERMARKET_DATASET_ID for why this only gets as far as an address list here)."""
    os.makedirs(CACHE_DIR, exist_ok=True)
    path = supermarket_cache_path()
    if os.path.exists(path) and (time.time() - os.path.getmtime(path)) < SUPERMARKET_CACHE_HOURS * 3600:
        say("Using the supermarket licences copy saved earlier (%s)" % path)
        return path
    say("Asking data.gov.sg for the supermarket licences download link...")
    d = get_json(API_BASE % SUPERMARKET_DATASET_ID, label="Supermarket licences poll-download")
    url = ((d.get("data") or {}).get("url") or "")
    if not url:
        raise SystemExit("data.gov.sg did not give a download link for supermarket licences: %r" % d)
    say("Downloading the NEA supermarket licence list...")
    raw = http_get(url, timeout=60)
    with open(path, "wb") as f:
        f.write(raw)
    say("   saved %.1f KB" % (len(raw) / 1e3))
    return path


def _first_field(row_lower, candidates):
    """Tries several plausible column-name spellings, case/spacing-insensitively - protects
    against data.gov.sg publishing this CSV with slightly different headers than expected (same
    defensive idea as the NAME-field fallbacks in build_named_points above)."""
    for c in candidates:
        v = row_lower.get(c)
        if v:
            return v.strip()
    return ""


def build_supermarket_addresses():
    """One row per licensed supermarket premises - {name, address, postal} - deduped by address,
    since a single unit sometimes has more than one licence record. This is as far as this file
    takes it (see the NOTE above SUPERMARKET_DATASET_ID) - miji_geocode_addresses.py turns this
    into actual coordinates."""
    path = download_supermarket_csv()
    say("Building the supermarket address list...")
    seen = {}
    out = []
    skipped_missing = 0
    skipped_dupe = 0
    first_row_keys = None
    for row in parse_csv_rows(path):
        row_lower = {(k or "").strip().lower().replace(" ", "_"): (v or "") for k, v in row.items()}
        if first_row_keys is None:
            first_row_keys = list(row.keys())
        name = _first_field(row_lower, ["licensee_name", "licensee", "name"])
        building = _first_field(row_lower, ["building_name", "building"])
        block = _first_field(row_lower, ["block_house_number", "block_house_no", "block_no", "house_blk_no", "blk_no"])
        street = _first_field(row_lower, ["street_name", "street"])
        postal = _first_field(row_lower, ["postal_code", "postal"])
        if not street or not postal:
            skipped_missing += 1
            continue
        address = " ".join(x for x in [block, street] if x).strip()
        key = (address.lower(), postal)
        if key in seen:
            skipped_dupe += 1
            continue
        seen[key] = True
        out.append({
            "name": building or name or "Supermarket",
            "address": address,
            "postal": postal,
        })

    if not out:
        raise RuntimeError(
            "parsed 0 usable supermarket addresses (skipped %d rows with no street/postal) - the "
            "source dataset's column names may have changed. First row's columns: %r"
            % (skipped_missing, first_row_keys)
        )
    say("   %d unique supermarket addresses found (%d duplicate rows, %d rows missing "
        "street/postal)" % (len(out), skipped_dupe, skipped_missing))
    return out


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

    unmapped = [t for t in towns_seen if t not in TOWN_DISTRICT]
    if unmapped:
        say("   NOTE: these towns appeared in the HDB data but aren't in TOWN_DISTRICT yet, "
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
        if town in private:
            for label, d in private[town].items():
                vals = d["values"]
                if all(y in vals for y in YEARS):
                    entry[label] = slots_to_entry(vals)
                    if town not in private_estimated:
                        private_estimated[town] = {}
                    private_estimated[town][label] = d["estimated_slots"]
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
                       "pooled from real transactions in the URA postal district(s) each town sits "
                       "in - the standard 28-district split, finer than URA's own 3-tier CCR/RCR/OCR "
                       "segments, though a handful of towns that share a district (or genuinely span "
                       "two) will still show the same or pooled numbers. An estimate, not an exact "
                       "per-town transaction median. Years older than URA's ~5-year window are "
                       "backward-projected from the earliest available growth rate and marked in "
                       "'private_estimated_slots' below.",
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

    # ------------------------------------------------------------------ 5. same-block / same-project price history, for Check a Unit
    # Deliberately AFTER and isolated from both files above - same reasoning as the
    # recent_transactions.json block: a problem here should never take down anything already live.
    try:
        block_hist_hdb = build_hdb_block_history()
        block_hist_private = build_private_project_history()
        block_hist_path = os.path.join(OUT_DIR, "block_history.json")
        block_hist_payload = {
            "built": time.strftime("%Y-%m-%d"),
            "years": YEARS,
            "hdb": block_hist_hdb,
            "private": block_hist_private,
            "sources": {
                "hdb": "Housing & Development Board (HDB), Resale Flat Prices, via data.gov.sg. "
                       "Median resale price per block, flat type and year, over the same rolling "
                       "%d-year window as the Miji Index above - for Check a Unit's same-block "
                       "price-history view, not individual transactions." % len(YEARS),
                "private": "Urban Redevelopment Authority (URA), Private Residential Property "
                           "Transactions, via the URA Data Service API. Median $ per square foot "
                           "per project, property type and year. URA's Data Service only gives "
                           "about the last 5 years of real transactions - unlike the Miji Index, "
                           "older years are left blank here rather than projected, since an "
                           "estimated point on one specific project's own chart would be "
                           "misleading in a way it isn't on a townwide trend.",
            },
        }
        block_hist_tmp = block_hist_path + ".tmp"
        with open(block_hist_tmp, "w", encoding="utf-8") as f:
            json.dump(block_hist_payload, f, ensure_ascii=False, separators=(",", ":"))
        os.replace(block_hist_tmp, block_hist_path)
        say("Done. %d HDB block + %d private project histories written to %s" %
            (len(block_hist_hdb), len(block_hist_private), block_hist_path))
    except Exception as e:
        say("")
        say("NOTE: block_history.json was NOT updated this run (miji_index.json and "
            "recent_transactions.json above are unaffected) - %s: %s" % (type(e).__name__, e))

    # ------------------------------------------------------------------ 6. nearby amenities, for listing pages
    # Same isolation as the two blocks above - a problem here (e.g. data.gov.sg changing one of
    # these datasets' field names) should never take down anything already live. This one doesn't
    # depend on the HDB/URA data above at all, so it'll keep working even if those ever fail. Each
    # category is fetched independently too, so a problem with one (say, the parks layer) doesn't
    # cost the other two.
    try:
        categories = {}
        for key, builder, cat_label in [
            ("mrt", build_mrt_stations, "MRT/LRT stations"),
            ("hawker", build_hawker_centres, "hawker centres"),
            ("park", build_parks, "parks"),
        ]:
            try:
                categories[key] = builder()
            except Exception as e:
                say("   NOTE: %s were NOT included this run - %s: %s" % (cat_label, type(e).__name__, e))
                categories[key] = []

        if not any(categories.values()):
            raise RuntimeError("parsed 0 amenities across every category - see the NOTEs above for what the source data looked like")

        amenities_path = os.path.join(OUT_DIR, "nearby_amenities.json")
        amenities_payload = {
            "built": time.strftime("%Y-%m-%d"),
            "categories": categories,
            "sources": {
                "mrt": "Urban Redevelopment Authority (URA), Master Plan 2019 Rail Station layer, "
                       "via data.gov.sg. One point per MRT/LRT station (interchange platforms "
                       "merged into a single point per station name).",
                "hawker": "National Environment Agency (NEA), Hawker Centres, via data.gov.sg.",
                "park": "National Parks Board (NParks), Parks, via data.gov.sg.",
                "method": "Straight-line distance from a listing's geocoded address to the "
                          "nearest point in each category, converted to a walking-time estimate "
                          "at ~80m/min (the same convention URA/HDB use) - not an actual walking "
                          "route, and not shown at all beyond a reasonable walking distance.",
            },
        }
        amenities_tmp = amenities_path + ".tmp"
        with open(amenities_tmp, "w", encoding="utf-8") as f:
            json.dump(amenities_payload, f, ensure_ascii=False, separators=(",", ":"))
        os.replace(amenities_tmp, amenities_path)
        say("Done. %d MRT/LRT + %d hawker centres + %d parks written to %s" %
            (len(categories.get("mrt", [])), len(categories.get("hawker", [])), len(categories.get("park", [])), amenities_path))
    except Exception as e:
        say("")
        say("NOTE: nearby_amenities.json was NOT updated this run (everything else above is "
            "unaffected) - %s: %s" % (type(e).__name__, e))

    # ------------------------------------------------------------------ 7. complete HDB block directory, for the agent listing form's address suggestions
    # Same isolation as the blocks above. Deliberately a SEPARATE source from recent_transactions.json
    # (built above) - that file only has blocks with a recent resale, which was fine for Check a
    # Unit (it only needs recently-comparable blocks) but meant a block that hadn't sold in a
    # while was silently missing from the agent's address suggestions with no way to tell why.
    # This is the complete list instead, independent of transaction history.
    try:
        directory = build_hdb_directory()
        if len(directory) < 5000:
            raise SystemExit("STOP: only %d residential blocks came out of HDB Property "
                              "Information (expected 8000+) - the download may be incomplete or "
                              "the dataset's columns may have changed. hdb_directory.json was left "
                              "untouched." % len(directory))
        directory_path = os.path.join(OUT_DIR, "hdb_directory.json")
        directory_payload = {
            "built": time.strftime("%Y-%m-%d"),
            "blocks": directory,
            "sources": {
                "hdb": "Housing & Development Board (HDB), HDB Property Information, via "
                       "data.gov.sg. Every residential HDB block that currently exists (updated "
                       "quarterly by HDB) - not filtered to recent transactions, unlike "
                       "recent_transactions.json above. Feeds the agent listing form's address "
                       "suggestions so every real HDB block is searchable, whether or not it's "
                       "sold recently.",
            },
        }
        directory_tmp = directory_path + ".tmp"
        with open(directory_tmp, "w", encoding="utf-8") as f:
            json.dump(directory_payload, f, ensure_ascii=False, separators=(",", ":"))
        os.replace(directory_tmp, directory_path)
        say("Done. %d HDB blocks written to %s" % (len(directory), directory_path))
    except SystemExit as e:
        say("")
        say(str(e))
    except Exception as e:
        say("")
        say("NOTE: hdb_directory.json was NOT updated this run (everything else above is "
            "unaffected) - %s: %s" % (type(e).__name__, e))

    # ------------------------------------------------------------------ 8. supermarket address list, for miji_geocode_addresses.py to geocode
    # Same isolation as the blocks above. Just the address list - see the NOTE above
    # SUPERMARKET_DATASET_ID for why the coordinates themselves come from a later, separate,
    # OneMap-authenticated step (miji_geocode_addresses.py), not here.
    try:
        supermarkets = build_supermarket_addresses()
        supermarkets_path = os.path.join(OUT_DIR, "supermarket_addresses.json")
        supermarkets_payload = {
            "built": time.strftime("%Y-%m-%d"),
            "addresses": supermarkets,
            "source": "National Environment Agency (NEA), List of Supermarket Licences, via "
                       "data.gov.sg. Address/postal code only, as published - no coordinates yet "
                       "(see index_data/nearby_amenities.json's supermarket category, added by "
                       "miji_geocode_addresses.py, for the geocoded version this feeds).",
        }
        supermarkets_tmp = supermarkets_path + ".tmp"
        with open(supermarkets_tmp, "w", encoding="utf-8") as f:
            json.dump(supermarkets_payload, f, ensure_ascii=False, separators=(",", ":"))
        os.replace(supermarkets_tmp, supermarkets_path)
        say("Done. %d supermarket addresses written to %s" % (len(supermarkets), supermarkets_path))
    except Exception as e:
        say("")
        say("NOTE: supermarket_addresses.json was NOT updated this run (everything else above is "
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
