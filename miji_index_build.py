#!/usr/bin/env python3
"""
Miji Index: build the "price growth over time" data behind the Miji Index screener/charts
(HDB resale by town + flat type, and private property by town, back to 2019).

What it does:
  1. Downloads the FULL HDB resale transaction history (Jan 2017 onwards, data.gov.sg, updated
     daily) and works out the median resale price per town, per flat type, per YEAR.
     This is separate from sg_build.py's map data on purpose: sg_build.py only keeps the last
     12 months (that's all the block map needs); the Miji Index needs the full multi-year trend,
     which is a much smaller amount of data (26 towns x 5 flat types x ~8 years of numbers,
     not every individual block).
  2. Reads the private_data/d*.json files that ura_build.py already produced earlier in the same
     run, and works out a median price + $psf per YEAR for each of URA's three market segments
     (CCR / RCR / OCR). URA's own Data Service only gives about the last 5 years of transactions,
     so private-property years before that are backward-projected from the earliest real growth
     rate available and marked as such in the output - never presented as an actual sale.
  3. Every HDB town is assigned to the market segment it actually sits in (see TOWN_SEGMENT below)
     so "Private (Condo)" and "Landed" numbers for that town use that segment's real trend, scaled
     to that segment's own $psf level - not one Singapore-wide average.
  4. Writes index_data/miji_index.json in the shape the Miji Index page expects: one entry per
     town, one array per flat type, one number per year.

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

YEARS = [2019, 2020, 2021, 2022, 2023, 2024, 2025, 2026]
FLAT_TYPES = ["2-room", "3-room", "4-room", "5-room", "Executive"]
HDB_FLAT_TYPE_MAP = {
    "2 ROOM": "2-room", "3 ROOM": "3-room", "4 ROOM": "4-room",
    "5 ROOM": "5-room", "EXECUTIVE": "Executive",
    # 1 ROOM and MULTI-GENERATION exist in the raw data but aren't in the Miji Index screener
}

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


def say(line=""):
    print(str(line), flush=True)
    LOG.append(str(line))


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


def median(vals):
    return statistics.median(vals) if vals else None


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


def build_hdb_medians():
    """Returns {town_title_case: {flat_type: {year: median_price}}}"""
    path = download_hdb_csv()
    say("Aggregating HDB resale prices by town, flat type and year...")
    buckets = defaultdict(list)  # (town, flat_type, year) -> [prices]
    rows_seen = 0
    for row in parse_csv_rows(path):
        rows_seen += 1
        ft = HDB_FLAT_TYPE_MAP.get((row.get("flat_type") or "").strip().upper())
        if not ft:
            continue
        month = (row.get("month") or "").strip()  # "YYYY-MM"
        if len(month) < 4 or not month[:4].isdigit():
            continue
        year = int(month[:4])
        if year not in YEARS:
            continue
        try:
            price = float(row.get("resale_price") or 0)
        except ValueError:
            continue
        if price <= 0:
            continue
        town = (row.get("town") or "").strip().title().replace("Hdb", "HDB")
        # Fix a couple of data.gov.sg's town spellings to match the site's display names
        town = {"Kallang/Whampoa": "Kallang/Whampoa", "Central Area": "Central Area"}.get(town, town)
        buckets[(town, ft, year)].append(price)
    if rows_seen < 100000:
        raise SystemExit("STOP: only read %d rows from the HDB dataset (expected several hundred thousand). "
                          "The download may be incomplete - existing index data was left untouched." % rows_seen)
    say("   read %d transaction rows" % rows_seen)

    out = defaultdict(lambda: defaultdict(dict))
    for (town, ft, year), prices in buckets.items():
        if len(prices) >= 5:  # too few sales in a town/type/year to call anything "typical"
            out[town][ft][year] = int(round(median(prices)))
    return out, sorted(set(t for t, _, _ in buckets))


# ------------------------------------------------------------------ 2. Private property, from ura_build.py's own output
def group_of(ptype):
    t = str(ptype).lower()
    if "detached" in t or "terrace" in t:
        return "landed"
    return "condo"  # includes executive condominium - Miji Index doesn't split EC out separately


def build_private_medians():
    """Reads private_data/d*.json (built earlier in the same run by ura_build.py) and returns
    {segment: {"Private (Condo)": {year: median_price}, "Landed": {year: median_price}}}
    for segment in CCR/RCR/OCR. Returns {} (not an error) if private_data isn't there - the HDB
    side of the Miji Index still works on its own."""
    if not os.path.isdir(PRIVATE_DIR):
        say("No %s folder found (ura_build.py hasn't run yet in this job) - "
            "Miji Index will only have HDB data this time." % PRIVATE_DIR)
        return {}
    say("Reading private-property transactions already downloaded by ura_build.py...")
    buckets = defaultdict(lambda: defaultdict(list))  # segment -> group -> [(year, price)]
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
                    ptype_idx = t[5]
                    ptype = types[ptype_idx] if 0 <= ptype_idx < len(types) else ""
                    year = ym // 100
                    if year not in YEARS:
                        continue
                    buckets[seg][group_of(ptype)][year].append(price)
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
    for seg, groups in buckets.items():
        out[seg] = {}
        for group, by_year in groups.items():
            label = "Private (Condo)" if group == "condo" else "Landed"
            real_years = {y: int(round(median(p))) for y, p in by_year.items() if len(p) >= 5}
            if not real_years:
                continue
            # URA's Data Service only gives ~5 years back, AND the current year often doesn't have
            # enough sales yet to clear the >=5-per-year bar above. Project both directions using
            # the earliest year-over-year growth rate we do have, rather than inventing an
            # unrelated number. Marked in the output as "est".
            #
            # Bug fix: this used to only fill backward (older years than the real window). If the
            # real data didn't reach all the way to the latest year in YEARS (very likely for the
            # current, still-in-progress year - see above), that year was left out of `filled`
            # entirely, main() below requires every year in YEARS to be present before it will use
            # this segment at all, and so EVERY segment failed that check - which is why Condo and
            # Landed came out empty for every single town, not just some.
            filled, est_years = dict(real_years), []
            known = sorted(real_years)
            if len(known) >= 2:
                growth = real_years[known[1]] / real_years[known[0]] if real_years[known[0]] else 1.0
            else:
                growth = 1.0
            growth = max(growth, 0.5)  # guard against a wild or negative rate from thin data
            for y in sorted(YEARS, reverse=True):  # backward: older than the real window
                if y in filled:
                    continue
                nxt = y + 1
                if nxt in filled:
                    filled[y] = int(round(filled[nxt] / growth))
                    est_years.append(y)
            for y in sorted(YEARS):  # forward: newer than the real window (e.g. the current year)
                if y in filled:
                    continue
                prv = y - 1
                if prv in filled:
                    filled[y] = int(round(filled[prv] * growth))
                    est_years.append(y)
            out[seg][label] = {"values": filled, "estimated_years": sorted(est_years)}
    return out


# ------------------------------------------------------------------ 3. combine + write
def main():
    say("Miji Index data builder | %s" % time.strftime("%Y-%m-%d %H:%M"))
    hdb, towns_seen = build_hdb_medians()
    private = build_private_medians()

    unmapped = [t for t in towns_seen if t not in TOWN_SEGMENT]
    if unmapped:
        say("   NOTE: these towns appeared in the HDB data but aren't in TOWN_SEGMENT yet, "
            "so they'll have no private-property numbers until added: %s" % ", ".join(unmapped))

    result_towns = {}
    for town, by_type in hdb.items():
        entry = {}
        for ft in FLAT_TYPES:
            years = by_type.get(ft, {})
            if len(years) >= 4:  # need a reasonable span of years to call it a trend
                entry[ft] = [years.get(y) for y in YEARS]
        if not entry:
            continue
        seg = TOWN_SEGMENT.get(town)
        if seg and seg in private:
            for label, d in private[seg].items():
                vals = d["values"]
                if all(y in vals for y in YEARS):
                    entry[label] = [vals[y] for y in YEARS]
        result_towns[town] = entry

    if len(result_towns) < 20:
        raise SystemExit("STOP: only %d towns came out with usable data (expected 20+). "
                          "Existing index data was left untouched." % len(result_towns))

    os.makedirs(OUT_DIR, exist_ok=True)
    out_path = os.path.join(OUT_DIR, "miji_index.json")
    payload = {
        "built": time.strftime("%Y-%m-%d"),
        "years": YEARS,
        "sources": {
            "hdb": "Housing & Development Board (HDB), Resale Flat Prices, via data.gov.sg. "
                   "Every registered resale transaction, median per town/flat type/year. "
                   "Singapore Open Data Licence - free for personal or commercial use.",
            "private": "Urban Redevelopment Authority (URA), Private Residential Property "
                       "Transactions, via the URA Data Service API. Grouped by URA market segment "
                       "(CCR/RCR/OCR) and applied to each town in that segment - an estimate, not "
                       "a per-town transaction median. Years older than URA's ~5-year window are "
                       "backward-projected from the earliest available growth rate and marked in "
                       "'estimated_years' below.",
        },
        "private_estimated_years": {
            seg: {label: d["estimated_years"] for label, d in groups.items()}
            for seg, groups in private.items()
        },
        "towns": result_towns,
    }
    tmp_path = out_path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp_path, out_path)
    say("")
    say("Done. %d towns written to %s" % (len(result_towns), out_path))


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
