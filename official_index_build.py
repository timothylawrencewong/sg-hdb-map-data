#!/usr/bin/env python3
"""
Miji Index (official): collect the government's OWN published price figures into one file.

Rule for this file: Miji does not calculate any median, average or index here. Every number
written out is copied as published by HDB or URA. The only things this script does are download,
check, and reshape.

Sources (all free, Singapore Open Data Licence / SingStat terms):
  1. HDB Resale Price Index, quarterly, 1Q2009 = 100        SingStat Table Builder M212161
  2. URA Private Residential Price Index, quarterly,           data.gov.sg d_97f8a2e995022d311c6c68cfda6d034c
     All Residential / Landed / Non-Landed, 1Q2009 = 100
  3. URA Price Index of Non-landed Properties by Locality      data.gov.sg d_f65e490a8ad430f60a9a3d9df2bff2a0
     (CCR / RCR / OCR), quarterly, 1Q2009 = 100
  4. HDB Median Resale Prices for Registered Applications,     data.gov.sg d_b51323a474ba789fb4cc3db58a3116d4
     by Town and Flat Type, quarterly (HDB's own medians)

Output: index_data/official_index.json

How to run (Terminal or GitHub Actions):
    python3 official_index_build.py
Standard library only, nothing to install.
"""

import csv
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

OUT_DIR = "index_data"
OUT_FILE = os.path.join(OUT_DIR, "official_index.json")

DGS_POLL = "https://api-open.data.gov.sg/v1/public/api/datasets/%s/poll-download"
SINGSTAT = "https://tablebuilder.singstat.gov.sg/api/table/tabledata/%s?offset=%d&limit=%d"

HDB_RPI_TABLE = "M212161"
URA_PPI_DATASET = "d_97f8a2e995022d311c6c68cfda6d034c"
URA_SEGMENT_DATASET = "d_f65e490a8ad430f60a9a3d9df2bff2a0"
HDB_MEDIAN_DATASET = "d_b51323a474ba789fb4cc3db58a3116d4"

FLAT_TYPES = ["1-room", "2-room", "3-room", "4-room", "5-room", "Executive"]

UA = {"User-Agent": "Mozilla/5.0 (compatible; MijiOfficialIndex/1.0; +https://miji.sg)",
      "Accept": "application/json, text/csv, */*"}


def say(line=""):
    print(line, flush=True)


# ------------------------------------------------------------------ downloading
def http_get(url, timeout=120, tries=4, wait=15):
    last = None
    for attempt in range(1, tries + 1):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            last = e
            code = getattr(e, "code", None)
            if code in (400, 401, 403, 404):
                break
            say("   attempt %d/%d failed (%s), waiting %ds..." % (attempt, tries, e, wait * attempt))
            time.sleep(wait * attempt)
    raise SystemExit("STOP: could not download %s (%s)" % (url, last))


def dgs_csv_rows(dataset_id, label):
    """data.gov.sg: ask for the download link, then read the CSV into a list of dicts."""
    say("Asking data.gov.sg for the %s download link..." % label)
    meta = json.loads(http_get(DGS_POLL % dataset_id))
    url = ((meta.get("data") or {}).get("url") or "")
    if not url:
        raise SystemExit("STOP: data.gov.sg gave no download link for %s: %r" % (label, meta))
    raw = http_get(url, timeout=180).decode("utf-8-sig")
    rows = list(csv.DictReader(io.StringIO(raw)))
    say("   %s: %d rows, columns %s" % (label, len(rows), list(rows[0].keys()) if rows else []))
    if not rows:
        raise SystemExit("STOP: %s came back empty." % label)
    return rows


def singstat_series(table_id, label):
    """SingStat Table Builder: returns {row title: [(quarter key, value string), ...]}."""
    say("Downloading %s from SingStat (%s)..." % (label, table_id))
    out, offset, page = {}, 0, 2000
    while True:
        d = json.loads(http_get(SINGSTAT % (table_id, offset, page)))
        rows = ((d.get("Data") or {}).get("row")) or []
        got = 0
        for r in rows:
            cols = r.get("columns") or []
            got = max(got, len(cols))
            out.setdefault(r.get("rowText", "").strip(), []).extend(
                (c.get("key", ""), c.get("value", "")) for c in cols)
        if got < page:
            break
        offset += page
    if not out:
        raise SystemExit("STOP: SingStat %s came back empty." % table_id)
    return out


# ------------------------------------------------------------------ tidying (no maths)
def quarter_key(text):
    """'2026 2Q', '2026-Q2', '2Q 2026', '2026Q2' -> '2026-Q2'."""
    t = str(text).strip().upper()
    m = re.search(r"(\d{4})\D*([1-4])Q", t) or re.search(r"(\d{4})\D*Q([1-4])", t)
    if m:
        return "%s-Q%s" % (m.group(1), m.group(2))
    m = re.search(r"([1-4])Q\D*(\d{4})", t) or re.search(r"Q([1-4])\D*(\d{4})", t)
    if m:
        return "%s-Q%s" % (m.group(2), m.group(1))
    raise ValueError("Unrecognised quarter %r" % text)


def qnum(q):
    y, n = q.split("-Q")
    return int(y) * 4 + int(n) - 1


def to_number(v):
    """Published value as a number, or None when the agency left it blank / 'na' / '-'."""
    s = str(v).strip().replace(",", "").replace("$", "")
    try:
        return float(s)
    except ValueError:
        return None


def as_series(pairs, label):
    """[(quarter, value)] -> {"start": "YYYY-Qn", "values": [...]} with one slot per quarter.
    Stops loudly on gaps or duplicates instead of filling them."""
    clean = {}
    for q, v in pairs:
        num = to_number(v)
        if num is None:
            continue
        k = quarter_key(q)
        if k in clean and clean[k] != num:
            raise SystemExit("STOP: %s has two different values for %s." % (label, k))
        clean[k] = num
    if not clean:
        raise SystemExit("STOP: %s has no numbers." % label)
    keys = sorted(clean, key=qnum)
    first, last = qnum(keys[0]), qnum(keys[-1])
    if last - first + 1 != len(keys):
        missing = [q for q in range(first, last + 1) if "%d-Q%d" % (q // 4, q % 4 + 1) not in clean]
        raise SystemExit("STOP: %s is missing %d quarter(s), e.g. %s." % (
            label, len(missing), ", ".join("%d-Q%d" % (q // 4, q % 4 + 1) for q in missing[:3])))
    base = clean.get("2009-Q1")
    if base is not None and abs(base - 100) > 0.05:
        raise SystemExit("STOP: %s is not on the 1Q2009 = 100 base (got %s)." % (label, base))
    return {"start": keys[0], "end": keys[-1], "values": [clean[k] for k in keys]}


def pick(row, *needles):
    """Find a column by part of its name, since data.gov.sg headers vary between datasets."""
    for k in row:
        lk = k.lower()
        if all(n in lk for n in needles):
            return k
    return None


def norm_flat(t):
    t = str(t).strip().lower().replace(" ", "-")
    for f in FLAT_TYPES:
        if t == f.lower():
            return f
    if t.startswith("exec"):
        return "Executive"
    return None


# ------------------------------------------------------------------ builders
def build_hdb_rpi():
    rows = singstat_series(HDB_RPI_TABLE, "HDB Resale Price Index")
    title = next((k for k in rows if "resale" in k.lower()), next(iter(rows)))
    return as_series(rows[title], "HDB Resale Price Index")


def build_private():
    rows = dgs_csv_rows(URA_PPI_DATASET, "URA Private Residential Price Index")
    qc, tc, vc = pick(rows[0], "quarter"), pick(rows[0], "type"), pick(rows[0], "index")
    if not (qc and tc and vc):
        raise SystemExit("STOP: unexpected columns in the URA price index: %s" % list(rows[0]))
    groups = {"all": [], "landed": [], "non_landed": []}
    for r in rows:
        t = r[tc].strip().lower()
        key = "non_landed" if "non" in t else "landed" if "landed" in t else "all" if ("all" in t or "residential" in t) else None
        if key:
            groups[key].append((r[qc], r[vc]))
    return {k: as_series(v, "URA private index (%s)" % k) for k, v in groups.items()}


def build_segments():
    rows = dgs_csv_rows(URA_SEGMENT_DATASET, "URA non-landed index by market segment")
    qc = pick(rows[0], "quarter")
    vc = pick(rows[0], "index")
    sc = next((k for k in rows[0] if k not in (qc, vc)), None)
    if not (qc and vc and sc):
        raise SystemExit("STOP: unexpected columns in the market segment index: %s" % list(rows[0]))
    groups = {"ccr": [], "rcr": [], "ocr": []}
    for r in rows:
        s = r[sc].strip().lower()
        key = "ccr" if s.startswith("core") else "rcr" if s.startswith("rest") else "ocr" if s.startswith("outside") else None
        if key:
            groups[key].append((r[qc], r[vc]))
    return {k: as_series(v, "URA %s index" % k.upper()) for k, v in groups.items()}


def build_hdb_medians():
    rows = dgs_csv_rows(HDB_MEDIAN_DATASET, "HDB median resale price by town and flat type")
    qc, tc, fc, pc = pick(rows[0], "quarter"), pick(rows[0], "town"), pick(rows[0], "flat"), pick(rows[0], "price")
    if not (qc and tc and fc and pc):
        raise SystemExit("STOP: unexpected columns in the HDB medians: %s" % list(rows[0]))
    quarters = sorted({quarter_key(r[qc]) for r in rows}, key=qnum)
    pos = {q: i for i, q in enumerate(quarters)}
    towns = {}
    for r in rows:
        f = norm_flat(r[fc])
        if not f:
            continue
        town = r[tc].strip().title()
        cell = towns.setdefault(town, {ft: [None] * len(quarters) for ft in FLAT_TYPES})
        num = to_number(r[pc])
        # Keep HDB's own reason when there is no median: "-" (no sales), "na"/"*" (not published).
        cell[f][pos[quarter_key(r[qc])]] = int(num) if num is not None else (r[pc].strip() or None)
    return {"start": quarters[0], "end": quarters[-1], "quarters": quarters, "towns": towns}


# ------------------------------------------------------------------ main
def main():
    built = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    out = {
        "built": built,
        "rule": "Every figure is copied as published by HDB or URA. Miji calculates no medians or indices.",
        "base": "1Q2009 = 100",
        "sources": {
            "hdb_rpi": {"publisher": "HDB", "via": "SingStat Table Builder", "id": HDB_RPI_TABLE,
                        "url": "https://tablebuilder.singstat.gov.sg/table/TS/" + HDB_RPI_TABLE},
            "private": {"publisher": "URA", "via": "data.gov.sg", "id": URA_PPI_DATASET,
                        "url": "https://data.gov.sg/datasets/%s/view" % URA_PPI_DATASET},
            "segments": {"publisher": "URA", "via": "data.gov.sg", "id": URA_SEGMENT_DATASET,
                         "url": "https://data.gov.sg/datasets/%s/view" % URA_SEGMENT_DATASET},
            "hdb_median": {"publisher": "HDB", "via": "data.gov.sg", "id": HDB_MEDIAN_DATASET,
                           "url": "https://data.gov.sg/datasets/%s/view" % HDB_MEDIAN_DATASET},
        },
        "hdb_rpi": build_hdb_rpi(),
        "private": build_private(),
        "segments": build_segments(),
        "hdb_median": build_hdb_medians(),
    }

    # Sanity checks against the file already on the site: never publish something that shrank.
    if os.path.exists(OUT_FILE):
        try:
            with open(OUT_FILE, encoding="utf-8") as f:
                old = json.load(f)
            for key in ("hdb_rpi",):
                if len(out[key]["values"]) < len(old[key]["values"]):
                    raise SystemExit("STOP: %s got shorter than last time. Live file left unchanged." % key)
            if len(out["hdb_median"]["towns"]) < len(old["hdb_median"]["towns"]) - 1:
                raise SystemExit("STOP: fewer towns than last time. Live file left unchanged.")
        except (ValueError, KeyError):
            pass
    if len(out["hdb_median"]["towns"]) < 20:
        raise SystemExit("STOP: only %d towns in the HDB medians." % len(out["hdb_median"]["towns"]))

    os.makedirs(OUT_DIR, exist_ok=True)
    tmp = OUT_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(out, f, separators=(",", ":"), ensure_ascii=False)
    os.replace(tmp, OUT_FILE)
    say("OK: HDB RPI to %s, private to %s, segments to %s, HDB medians to %s (%d towns). Wrote %s" % (
        out["hdb_rpi"]["end"], out["private"]["all"]["end"], out["segments"]["ccr"]["end"],
        out["hdb_median"]["end"], len(out["hdb_median"]["towns"]), OUT_FILE))


if __name__ == "__main__":
    main()
