#!/usr/bin/env python3
"""
Miji Weekly Market Update: works out what's genuinely NEW in the HDB and private-property data
since last week's run, and writes index_data/weekly_highlights.json - the numbers behind the
/market-update page and the weekly buyer email.

Why this is its own script, separate from miji_index_build.py's monthly run:
  Your main data pipeline (sg_build.py -> ura_build.py -> miji_index_build.py) only runs once a
  month (see refresh-data.yml). A weekly feature hooked into that would show the exact same
  numbers for weeks at a stretch, which reads as broken, not "weekly." So this is a second, much
  lighter job (see weekly-highlights.yml) that runs every week on its own. It does a fresh HDB +
  URA pull but skips OneMap geocoding entirely - this only needs prices and counts, not map
  positions - so it's fast enough to run weekly.

Why "new" isn't simply "the last 7 days":
  Neither data source tags a sale with the day it happened. HDB's dataset only says which MONTH a
  resale was registered in; URA's private-property feed only says which MONTH a caveat/sale was
  lodged in (its "contractDate" field is "MMYY", month + year only). So there is no exact-date
  field to filter by. What both sources DO give you is new rows appearing in the data over time -
  HDB updates daily, URA twice a week (Tuesday and Friday evenings, per URA's own coverage notes).
  So this script keeps a small fingerprint of every transaction it has already counted
  (weekly_state/weekly_seen.json) and, each run, works out which fingerprints are showing up for
  the FIRST time - genuinely new to the data this week, even if the underlying sale was registered
  a little earlier. That's also why the page/email should say "newly added to the data this
  week," not "sold this week" - it's the honest framing for what this data actually is.
  Sources: data.gov.sg's page for the HDB resale dataset (updated daily, organised by
  registration date) and URA's REALIS "Coverage and Methodology" page (caveats transmitted twice
  weekly; new-sale/developer data released every Friday).

Reuses miji_index_build.py's HDB download/CSV parsing and ura_build.py's URA download/tidy-up -
so this never re-implements those, and never drifts from how the rest of the site defines a flat
type, a "condo" vs "landed" split, or a town's spelling. Both files need to sit right next to this
one (same repo checkout) - the GitHub Action already does that.

FIRST RUN NOTE: the very first time this runs, there is no "last week" to compare against, so
everything currently in the data would look "new" - which would be a wrong, inflated first email.
Instead, the first run only saves today's fingerprints as the starting point and reports zero
highlights (with a clear note in the output). The following run is the first with real numbers.

How to run (Terminal), from the folder that holds this file, miji_index_build.py and
ura_build.py:
    URA_ACCESS_KEY=... python3 miji_weekly_highlights.py
Standard library only, nothing to install.
"""

import json
import os
import sys
import time
from collections import Counter

import miji_index_build as midx
import ura_build as ura

OUT_DIR = "index_data"
STATE_DIR = "weekly_state"
SEEN_PATH = os.path.join(STATE_DIR, "weekly_seen.json")
LOOKBACK_MONTHS = 6   # how far back a transaction can be and still count as "new" the first time
                       # it's seen - generous enough to cover HDB's registration lag and any
                       # late-appearing URA caveat, without keeping fingerprints forever
TOP_N = 5
MIN_HDB_ROWS = 500     # a sane floor for "6 months of nationwide HDB resales" - well under the
                       # real number, just enough to catch a badly broken/partial download
MIN_PRIVATE_ROWS = 150  # same idea for URA - lower bar since private volumes are smaller

SQM_TO_SQFT = midx.SQM_TO_SQFT
say = midx.say


def month_cutoff_ym():
    now = time.gmtime()
    y, m = midx.add_months(now.tm_year, now.tm_mon, -(LOOKBACK_MONTHS - 1))
    return y * 100 + m


def load_seen():
    if not os.path.exists(SEEN_PATH):
        return None   # None (not {}) specifically means "no baseline yet - this is the first run"
    try:
        with open(SEEN_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def save_seen(counts):
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(SEEN_PATH, "w", encoding="utf-8") as f:
        json.dump(counts, f, separators=(",", ":"))


def psf(price, sqm):
    if not price or not sqm:
        return None
    return round(price / (sqm * SQM_TO_SQFT), 1)


# ------------------------------------------------------------------ HDB
def hdb_rows(cutoff_ym):
    path = midx.download_hdb_csv()
    say("Reading HDB resale rows for the weekly update...")
    rows = []
    for row in midx.parse_csv_rows(path):
        ft = midx.HDB_FLAT_TYPE_MAP.get((row.get("flat_type") or "").strip().upper())
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
        blk = (row.get("block") or "").strip()
        st = (row.get("street_name") or "").strip()
        rows.append({
            "key": "H|%s|%s|%s|%s|%d|%d|%.1f" % (town, blk, st.upper(), ft, ym, int(price), area),
            "town": town,
            "type": ft,
            "blk": blk,
            "st": st.title(),
            "storey": (row.get("storey_range") or "").strip(),
            "sqm": round(area, 1),
            "lease": (row.get("remaining_lease") or "").strip(),
            "price": int(price),
            "ym": ym,
        })
    say("   %d HDB rows in the last %d months" % (len(rows), LOOKBACK_MONTHS))
    return rows


# ------------------------------------------------------------------ private (URA)
def private_rows(cutoff_ym):
    say("Downloading private-property transactions from URA (no geocoding needed for this)...")
    batches = ura.download_all(refresh=True)
    projects, seen_tx = ura.tidy(batches)
    say("   %d projects, %d transactions from URA" % (len(projects), seen_tx))
    rows = []
    for p in projects:
        name = p.get("name") or ""
        street = p.get("street") or ""
        seg = (p.get("seg") or "").upper()
        for t in p.get("tx") or []:
            ym = t.get("ym")
            if not ym or ym < cutoff_ym:
                continue
            if (t.get("units") or 1) != 1:
                continue  # bulk sale - not a fair "highest sale" comparison
            price = t.get("price")
            area = t.get("area")
            if not price or price <= 0 or not area or area <= 0:
                continue
            ptype = t.get("ptype") or ""
            floor = t.get("floor") or ""
            rows.append({
                "key": "P|%s|%s|%d|%d|%.1f|%s|%s" % (
                    name.upper(), street.upper(), ym, int(price), area, floor, ptype.upper(),
                ),
                "project": name,
                "street": street,
                "seg": seg if seg in ("CCR", "RCR", "OCR") else "",
                "type": midx.group_of(ptype),   # "condo" (incl. EC) or "landed" - matches the rest of the site
                "ptype": ptype,
                "tenure": t.get("tenure") or "",
                "floor": floor,
                "sqm": round(area, 1),
                "price": int(price),
                "ym": ym,
                "psf": psf(price, area),
            })
    say("   %d private-property rows in the last %d months" % (len(rows), LOOKBACK_MONTHS))
    if len(rows) < MIN_PRIVATE_ROWS:
        raise RuntimeError("only %d private-property rows came back (expected a few hundred) - "
                            "treating this as a bad/partial pull" % len(rows))
    return rows


# ------------------------------------------------------------------ diff against last week
def new_only(rows, seen):
    """Counts how many of each fingerprint have shown up before, and only keeps rows in excess of
    that - so if 3 identical-looking sales (same block, same price, same month - it happens) were
    already counted before and a 4th one appears this week, only that 4th one counts as new.
    `seen` is a plain dict {key: count} of the LAST saved baseline (or {} if there is one but this
    key wasn't in it yet)."""
    counts = Counter(r["key"] for r in rows)
    new = []
    remaining = dict(counts)
    for r in rows:
        key = r["key"]
        if remaining[key] > seen.get(key, 0):
            new.append(r)
            remaining[key] -= 1   # so identical-key rows are only "claimed" once each
    return new, counts


def top_by_price(rows, n=TOP_N):
    """Top N by price, with the internal dedup "key" fingerprint stripped out - that's plumbing
    for this script's own week-to-week comparison, not something the page or email should show."""
    top = sorted(rows, key=lambda r: -r["price"])[:n]
    return [{k: v for k, v in r.items() if k != "key"} for r in top]


def week_label_now():
    week_end = time.gmtime()
    week_start = time.gmtime(time.mktime(week_end) - 6 * 86400)
    return "%s – %s" % (time.strftime("%d %b", week_start), time.strftime("%d %b %Y", week_end))


def main():
    say("Weekly market update builder | Python %s | %s" % (sys.version.split()[0], time.strftime("%Y-%m-%d %H:%M")))
    os.makedirs(OUT_DIR, exist_ok=True)
    cutoff_ym = month_cutoff_ym()
    seen = load_seen()
    first_run = seen is None
    if first_run:
        say("NOTE: no saved baseline from a previous run (weekly_state/weekly_seen.json doesn't "
            "exist yet) - this is the FIRST run. Saving today's data as the starting point only; "
            "everything currently in the data is NOT reported as 'new this week' (it isn't - we "
            "have nothing earlier to compare it to). Next week's run will be the first with real "
            "highlights.")
        seen = {}

    hdb = hdb_rows(cutoff_ym)
    if len(hdb) < MIN_HDB_ROWS:
        raise SystemExit("STOP: only %d HDB rows in the last %d months (expected several thousand). "
                          "The HDB download may be incomplete - nothing was changed." % (len(hdb), LOOKBACK_MONTHS))
    new_hdb, hdb_counts = new_only(hdb, seen)
    say("   %d HDB rows are new since last run" % len(new_hdb))

    priv, priv_counts, new_priv = [], {}, []
    try:
        priv = private_rows(cutoff_ym)
        new_priv, priv_counts = new_only(priv, seen)
        say("   %d private-property rows are new since last run" % len(new_priv))
    except SystemExit as e:
        say("NOTE: private-property side skipped this run (%s) - HDB highlights are unaffected." % e)
    except Exception as e:
        say("NOTE: private-property side skipped this run (%s: %s) - HDB highlights are unaffected." % (type(e).__name__, e))

    if first_run:
        new_hdb, new_priv = [], []

    town_counts = Counter(r["town"] for r in new_hdb)
    busiest_town = None
    if town_counts:
        town, count = town_counts.most_common(1)[0]
        busiest_town = {"town": town, "count": count}

    new_condo = [r for r in new_priv if r["type"] == "condo"]
    new_landed = [r for r in new_priv if r["type"] == "landed"]

    out = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "week_label": week_label_now(),
        "first_run": first_run,
        "hdb": {
            "new_count": len(new_hdb),
            "top": top_by_price(new_hdb),
            "busiest_town": busiest_town,
        },
        "private": {
            "new_count": len(new_priv),
            "top_condo": top_by_price(new_condo),
            "top_landed": top_by_price(new_landed),
        },
    }

    path = os.path.join(OUT_DIR, "weekly_highlights.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))
    say("Wrote %s" % path)

    # Always save the FULL current-run counts as the new baseline (not just the new ones) - next
    # week's diff is simply "this run's counts minus this saved baseline". Rows that age out of the
    # LOOKBACK_MONTHS window naturally drop out of next run's counts too, so this file never grows
    # without bound.
    combined_counts = dict(hdb_counts)
    for k, v in priv_counts.items():
        combined_counts[k] = combined_counts.get(k, 0) + v
    save_seen(combined_counts)
    say("Saved %d fingerprints for next week's comparison" % len(combined_counts))


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
        with open("miji_weekly_highlights_log.txt", "w", encoding="utf-8") as f:
            f.write("\n".join(midx.LOG))
    except Exception:
        pass
    sys.exit(code)
