#!/usr/bin/env python3
"""
Miji Weekly Market Update: publishes this MONTH's HDB and private-property highlights - the
highest sales, which town had the most resales, how many transactions came through - refreshed
every Monday, and writes index_data/weekly_highlights.json (the numbers behind the
/market-update page).

Why "weekly" but the content is a month, not a week-over-week diff:
  An earlier version of this script tried to work out what was genuinely NEW in the data since
  last Monday's run (keeping a fingerprint of every transaction already counted, comparing
  against it each week). In practice that produced a page that was empty or near-empty most
  weeks: HDB's dataset only says which MONTH a resale was registered in (no exact date), URA's
  private-property feed is the same (its "contractDate" field is "MMYY"), and registration itself
  lags the actual sale - so a genuine week-to-week diff on this data just doesn't move much,
  which read as broken, not "quiet." Simpler and more honest: every Monday, ask "what does the
  CURRENT month's data look like" and republish that - always something real to show, always
  clearly labelled with the month it's from, no diffing, no state to keep between runs.
  Sources: data.gov.sg's page for the HDB resale dataset (updated daily, organised by
  registration date) and URA's REALIS "Coverage and Methodology" page (caveats transmitted twice
  weekly; new-sale/developer data released every Friday).

Why no median price section:
  A single median swings with whatever mix of flat types/sizes happened to transact that month -
  not a controlled price index, and different houses have different values, so a week-to-week (or
  month-to-month) median comparison reads as more rigorous than the data supports. For genuine
  price-trend tracking, Miji Index is the right tool (a proper longer-run view) - this page only
  ever claims to be a snapshot of the current month's activity.

Reuses miji_index_build.py's HDB download/CSV parsing and ura_build.py's URA download/tidy-up -
so this never re-implements those, and never drifts from how the rest of the site defines a flat
type, a "condo" vs "landed" split, or a town's spelling. Both files need to sit right next to this
one (same repo checkout) - the GitHub Action already does that.

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

# URA's own typeOfSale codes (confirmed on URA's PMI_Resi_Transaction API reference: 1=New Sale,
# 2=Sub Sale, 3=Resale - not something this codebase mapped before, so this is the first place it's
# spelled out).
SALE_TYPE_MAP = {1: "New Sale", 2: "Sub Sale", 3: "Resale"}
LOOKBACK_MONTHS = 6   # how far back to pull, so there's a safety margin of prior months in case the
                       # very latest one is still thin - the script always shows just the single
                       # most-recent month present, this is only how far back it looks for that
TOP_N = 5
MIN_HDB_ROWS = 500     # a sane floor for "6 months of nationwide HDB resales" - well under the
                       # real number, just enough to catch a badly broken/partial download
MIN_PRIVATE_ROWS = 150  # same idea for URA - lower bar since private volumes are smaller
MILLION = 1_000_000
NEW_MONTH_MIN_SHARE = 0.0   # 0 = OFF: the weekly page always shows the current (latest) month, even in its
                             # first days. Last month's final numbers come from the separate monthly job
                             # (monthly_final/YYYY-MM.json). Set to e.g. 0.2 to make a new month wait until
                             # it has 20% of last month's HDB count before replacing it on the page.
REGION_ORDER = ["CCR", "RCR", "OCR"]   # fixed display order (URA's own convention), not sorted by
                                        # count - so the region mini-grid doesn't reshuffle week to week

SQM_TO_SQFT = midx.SQM_TO_SQFT
say = midx.say


def month_cutoff_ym():
    now = time.gmtime()
    y, m = midx.add_months(now.tm_year, now.tm_mon, -(LOOKBACK_MONTHS - 1))
    return y * 100 + m


def breakdown(rows, field, labels=None):
    """{value: count} over `rows`, as a list of {label, count, pct} sorted by count descending -
    used for both the HDB flat-type mix and the private new-sale/sub-sale/resale mix. `labels`, if
    given, renames a raw value (e.g. "condo" -> "Condo") - rows without a usable value are skipped
    rather than forced into a bucket."""
    total = len(rows)
    if not total:
        return []
    counts = Counter(r.get(field) for r in rows if r.get(field))
    out = []
    for value, count in counts.most_common():
        label = (labels or {}).get(value, value)
        out.append({"label": label, "count": count, "pct": round(count / total * 100, 1)})
    return out


def psf(price, sqm):
    if not price or not sqm:
        return None
    return round(price / (sqm * SQM_TO_SQFT), 1)


def median_or_none(vals):
    """Still used for the region mini-grid's per-region median $psf (a per-region breakdown, not a
    single overall figure claiming to represent the whole market - that's the part that was removed)."""
    vals = [v for v in vals if v]
    if not vals:
        return None
    vals = sorted(vals)
    n = len(vals)
    mid = n // 2
    return round(vals[mid] if n % 2 else (vals[mid - 1] + vals[mid]) / 2, 1)


def region_breakdown(rows):
    """Median $psf and count per CCR/RCR/OCR, for NON-LANDED private rows only (a "seg" tag doesn't
    apply the same way to landed) - skips a region entirely if there were no qualifying rows,
    rather than showing it as a hollow $0 card."""
    out = []
    for seg in REGION_ORDER:
        seg_rows = [r for r in rows if r.get("seg") == seg]
        if not seg_rows:
            continue
        out.append({
            "seg": seg,
            "count": len(seg_rows),
            "median_psf": median_or_none([r["psf"] for r in seg_rows if r.get("psf")]),
        })
    return out


def build_signals(month_hdb, above_1m_count, type_bd, sale_bd, region_bd, hdb_month_label, priv_month_label):
    """A handful of short, auto-written one-liners that call out whatever's actually notable in
    the current month's numbers - not an opinion on whether that's good or bad, just what stands
    out. Skips any signal whose underlying data is empty, rather than forcing a hollow line."""
    if not month_hdb:
        return []
    signals = []
    if above_1m_count:
        signals.append("%d HDB resale flat%s crossed $1M in %s." % (
            above_1m_count, "" if above_1m_count == 1 else "s", hdb_month_label))
    if type_bd:
        top = type_bd[0]
        signals.append("%s flats made up %s%% of %s's HDB resales." % (top["label"], top["pct"], hdb_month_label))
    if sale_bd and priv_month_label:
        top = sale_bd[0]
        signals.append("%s made up %s%% of %s's private transactions." % (top["label"], top["pct"], priv_month_label))
    if region_bd and len(region_bd) > 1 and priv_month_label:
        top = max(region_bd, key=lambda r: r["count"])
        total = sum(r["count"] for r in region_bd)
        if total:
            signals.append("%s accounted for %s%% of %s's non-landed private transactions." % (
                top["seg"], round(top["count"] / total * 100, 1), priv_month_label))
    return signals[:4]


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
            "town": town,
            "type": ft,
            "blk": blk,
            "st": st.title(),
            "storey": (row.get("storey_range") or "").strip(),
            "sqm": round(area, 1),
            "lease": (row.get("remaining_lease") or "").strip(),
            "price": int(price),
            "ym": ym,
            "psf": psf(price, area),
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
            try:
                sale_code = int(t.get("sale") or 0)
            except (TypeError, ValueError):
                sale_code = 0
            rows.append({
                "project": name,
                "street": street,
                "seg": seg if seg in ("CCR", "RCR", "OCR") else "",
                "type": midx.group_of(ptype),   # "condo" (incl. EC) or "landed" - matches the rest of the site
                "ptype": ptype,
                "sale_type": SALE_TYPE_MAP.get(sale_code, ""),   # "New Sale" / "Sub Sale" / "Resale" / "" if unknown
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


def top_by_price(rows, n=TOP_N):
    return sorted(rows, key=lambda r: -r["price"])[:n]


def cheapest_by_price(rows):
    """The single lowest-priced row, or None - Miji's own "affordability first" angle: every other
    stat here leads with "highest," this is the one that speaks to a budget-conscious buyer."""
    if not rows:
        return None
    return min(rows, key=lambda r: r["price"])


def top_by_psf(rows, n=TOP_N):
    """Same idea as top_by_price but ranked by $ per square foot - a small unit in a hot area can
    have a striking $psf even when its total price isn't the month's biggest number, which is
    genuinely different information from "highest price." Rows with no psf (shouldn't happen, but
    defensive) are left out rather than sorting as if they were $0/sqft."""
    ranked = [r for r in rows if r.get("psf")]
    return sorted(ranked, key=lambda r: -r["psf"])[:n]


_MONTH_NAMES = ["", "Jan", "Feb", "Mar", "Apr", "May", "Jun",
                "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def ym_label(ym):
    """220509 -> "Sep 2022"-style label for a YYYYMM int. None in, None out."""
    if not ym:
        return None
    year, mon = divmod(ym, 100)
    if mon < 1 or mon > 12:
        return None
    return "%s %d" % (_MONTH_NAMES[mon], year)


def latest_month_rows(rows):
    """The core selection this whole script runs on: out of everything pulled, keep only the
    single most-recent month actually present. Returns (rows_for_latest_month, ym) - ([], None) if
    `rows` is empty."""
    if not rows:
        return [], None
    latest_ym = max(r["ym"] for r in rows)
    return [r for r in rows if r["ym"] == latest_ym], latest_ym


def parse_args(argv):
    """Optional: --month YYYY-MM  builds that specific month (used by the monthly "final numbers"
    job) instead of whatever the latest month in the data is. Returns that month as the same
    year*100+month number the rows carry in r["ym"], or None for the normal weekly behaviour."""
    for i, a in enumerate(argv):
        if a == "--month":
            if i + 1 >= len(argv):
                raise SystemExit("STOP: --month needs a value like 2026-09.")
            val = argv[i + 1].strip()
            if len(val) != 7 or val[4] != "-" or not (val[:4] + val[5:]).isdigit() or not (1 <= int(val[5:]) <= 12):
                raise SystemExit("STOP: --month must look like 2026-09, got %r." % val)
            return int(val[:4]) * 100 + int(val[5:])
    return None


def month_rows(rows, target_ym):
    """target_ym None -> the latest month present (weekly behaviour, unchanged). Otherwise exactly
    that month, and a clear stop if it isn't in the data (so a monthly run never silently publishes
    a different month under the wrong name)."""
    if target_ym is None:
        return latest_month_rows(rows)
    picked = [r for r in rows if r["ym"] == target_ym]
    if not picked:
        raise SystemExit("STOP: no rows for %s in the data pulled." % ym_label(target_ym))
    return picked, target_ym


def main():
    say("Weekly market update builder | Python %s | %s" % (sys.version.split()[0], time.strftime("%Y-%m-%d %H:%M")))
    os.makedirs(OUT_DIR, exist_ok=True)
    target_ym = parse_args(sys.argv[1:])
    if target_ym:
        say("   Monthly final mode: building %s" % ym_label(target_ym))
    cutoff_ym = month_cutoff_ym()

    hdb_all = hdb_rows(cutoff_ym)
    if len(hdb_all) < MIN_HDB_ROWS:
        raise SystemExit("STOP: only %d HDB rows in the last %d months (expected several thousand). "
                          "The HDB download may be incomplete - nothing was changed." % (len(hdb_all), LOOKBACK_MONTHS))
    month_hdb, hdb_ym = month_rows(hdb_all, target_ym)
    month_note = None
    if target_ym is None and month_hdb:
        py, pm = midx.add_months(hdb_ym // 100, hdb_ym % 100, -1)
        prev_ym = py * 100 + pm
        prev_rows = [r for r in hdb_all if r["ym"] == prev_ym]
        if prev_rows and len(month_hdb) < NEW_MONTH_MIN_SHARE * len(prev_rows):
            month_note = ("%s has only %d HDB resales registered so far (%s had %d), so this page shows %s "
                          "until %s fills in." % (ym_label(hdb_ym), len(month_hdb), ym_label(prev_ym),
                                                   len(prev_rows), ym_label(prev_ym), ym_label(hdb_ym)))
            say("   " + month_note)
            month_hdb, hdb_ym = prev_rows, prev_ym
    hdb_month_label = ym_label(hdb_ym)
    say("   %d HDB rows for %s" % (len(month_hdb), hdb_month_label))

    priv_all, month_priv, priv_ym = [], [], None
    priv_month_label = None
    try:
        priv_all = private_rows(cutoff_ym)
        month_priv, priv_ym = month_rows(priv_all, target_ym)
        priv_month_label = ym_label(priv_ym)
        say("   %d private-property rows for %s" % (len(month_priv), priv_month_label))
    except SystemExit as e:
        say("NOTE: private-property side skipped this run (%s) - HDB highlights are unaffected." % e)
    except Exception as e:
        say("NOTE: private-property side skipped this run (%s: %s) - HDB highlights are unaffected." % (type(e).__name__, e))

    town_counts = Counter(r["town"] for r in month_hdb)
    busiest_town = None
    if town_counts:
        town, count = town_counts.most_common(1)[0]
        busiest_town = {"town": town, "count": count}

    month_condo = [r for r in month_priv if r["type"] == "condo"]
    month_landed = [r for r in month_priv if r["type"] == "landed"]
    above_1m_count = sum(1 for r in month_hdb if r["price"] >= MILLION)
    hdb_type_bd = breakdown(month_hdb, "type")
    priv_sale_bd = breakdown(month_priv, "sale_type")
    priv_region_bd = region_breakdown(month_condo)

    signals = build_signals(month_hdb, above_1m_count, hdb_type_bd, priv_sale_bd, priv_region_bd,
                             hdb_month_label, priv_month_label)

    out = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "hdb_month_label": hdb_month_label,
        "priv_month_label": priv_month_label,
        "signals": signals,
        "hdb": {
            "count": len(month_hdb),
            "top": top_by_price(month_hdb),
            "top_psf": top_by_psf(month_hdb),
            "cheapest": cheapest_by_price(month_hdb),
            "busiest_town": busiest_town,
            "type_breakdown": hdb_type_bd,
            "above_1m_count": above_1m_count,
        },
        "private": {
            "count": len(month_priv),
            "top_condo": top_by_price(month_condo),
            "top_landed": top_by_price(month_landed),
            "sale_type_breakdown": priv_sale_bd,
            "region_breakdown": priv_region_bd,
        },
    }

    if month_note:
        out["month_note"] = month_note
    if target_ym:
        ym_str = "%04d-%02d" % (target_ym // 100, target_ym % 100)
        out["kind"] = "monthly_final"
        out["registered_month"] = ym_str
        out["note"] = ("Counts are by the month HDB/URA registered the transaction, as published at generated_at. "
                       "Re-built on a schedule, so later runs can pick up transactions published after earlier ones.")
        final_dir = os.path.join(OUT_DIR, "monthly_final")
        os.makedirs(final_dir, exist_ok=True)
        paths = [os.path.join(final_dir, ym_str + ".json"), os.path.join(OUT_DIR, "monthly_final_latest.json")]
    else:
        paths = [os.path.join(OUT_DIR, "weekly_highlights.json")]
    for path in paths:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, separators=(",", ":"))
        say("Wrote %s" % path)


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
