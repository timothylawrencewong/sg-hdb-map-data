"""Miji HDB resale price model (v1).

Trains LightGBM on the last 12 months of HDB resale transactions in
sg-hdb-map-data, tests on the most recent 3 months (time holdout), and
writes the model plus an accuracy report.
"""
import json, math, re, sys, os
import numpy as np, pandas as pd, lightgbm as lgb

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.dirname(os.path.abspath(__file__))

TYPES = ["2-room", "3-room", "4-room", "5-room", "Executive"]
if os.environ.get("FULL_HISTORY") == "1":
    # full HDB resale history (Jan 2017 onwards) from data.gov.sg - needs internet, so GitHub Actions only
    sys.path.insert(0, REPO)
    import miji_index_build as mib
    hist_all = pd.DataFrame(mib.build_recent_hdb_transactions(recent_months=12 * 11))
    hist_all = hist_all[hist_all["type"].isin(TYPES)].reset_index(drop=True)
else:
    hist_all = pd.DataFrame(json.load(open(f"{REPO}/index_data/recent_transactions.json"))["hdb"])
_m = sorted(hist_all["ym"].unique())
tx = hist_all[hist_all["ym"] >= _m[max(0, len(_m) - 12)]].reset_index(drop=True)  # last 12 months: blocks to estimate
coords = json.load(open(f"{REPO}/index_data/address_coordinates.json"))["addresses"]
mrt = [m for m in json.load(open(f"{REPO}/index_data/nearby_amenities.json"))["categories"]["mrt"]
       if m.get("name") and m["name"] != "<Null>"]
mrt_xy = np.array([[m["lat"], m["lng"]] for m in mrt])


bh = json.load(open(f"{REPO}/index_data/block_history.json"))["hdb"]
_rows = []
for e in bh:
    for yr, v in e["years"].items():
        if 2019 <= int(yr) <= 2024:
            _rows.append((e["town"], e["blk"], e["st"], e["type"], int(yr), v["med"], v["n"]))
_bh = pd.DataFrame(_rows, columns=["town", "blk", "st", "type", "yr", "med", "n"])
_tt = _bh.groupby(["town", "type", "yr"]).apply(lambda g: np.average(g["med"], weights=g["n"])).rename("tmed").reset_index()
_bh = _bh.merge(_tt, on=["town", "type", "yr"])
_bh["ratio"] = np.log(_bh["med"] / _bh["tmed"])
BLOCK_PREM = (_bh.groupby(["blk", "st", "type"]).apply(lambda g: pd.Series({"prem": np.average(g["ratio"], weights=g["n"]), "prem_n": g["n"].sum()})))
BLOCK_PREM_ANY = _bh.groupby(["blk", "st"]).apply(lambda g: np.average(g["ratio"], weights=g["n"]))


# ---- extra location features (all from files already in this repo)
import glob as _glob
_town_files = [json.load(open(p)) for p in sorted(_glob.glob(f"{REPO}/data/*.json")) if isinstance(json.load(open(p)), dict) and "B" in json.load(open(p))]
def _uniq(pts):
    return np.array(sorted({(round(a, 5), round(b, 5)) for a, b in pts}))
_cat = json.load(open(f"{REPO}/index_data/nearby_amenities.json"))["categories"]
AMEN = {
    "prim": _uniq((k[2], k[3]) for t in _town_files for k in t.get("K", []) if k[1] in ("P", "M")),
    "sec": _uniq((k[2], k[3]) for t in _town_files for k in t.get("K", []) if k[1] in ("S", "M")),
    "hawker": _uniq((h["lat"], h["lng"]) for h in _cat["hawker"]),
    "park": _uniq((h["lat"], h["lng"]) for h in _cat["park"]),
    "superm": _uniq((x[1], x[2]) for t in _town_files for x in t.get("SM", [])),
    "lrt": _uniq((m["lat"], m["lng"]) for m in _cat["mrt"] if m.get("rail_type") == "LRT"),
}
BLOCK_INFO = {}
for t in _town_files:
    for b in t["B"]:
        if b.get("p"):
            BLOCK_INFO[str(b["p"])] = (b.get("fl"), b.get("un"), b.get("y"))
CBD = (1.2840, 103.8515)

# fallback coordinates from the town files, for blocks the geocode cache doesn't have (e.g. "Everton Pk")
_ABBR = {"avenue": "ave", "street": "st", "road": "rd", "park": "pk", "drive": "dr", "crescent": "cres",
         "central": "ctrl", "north": "nth", "south": "sth", "upper": "upp", "lorong": "lor", "jalan": "jln",
         "close": "cl", "place": "pl", "bukit": "bt", "kampong": "kg", "terrace": "ter", "heights": "hts",
         "garden": "gdn", "gardens": "gdns", "commonwealth": "c'wealth", "market": "mkt", "tanjong": "tg"}
def _norm_st(st):
    return " ".join(_ABBR.get(w, w) for w in re.sub(r"[^a-z0-9' ]", " ", str(st).lower()).split())
FALLBACK_XY = {}
for t in _town_files:
    for b in t["B"]:
        FALLBACK_XY[(str(b["b"]).upper(), _norm_st(b["st"]))] = (b["a"], b["o"], str(b.get("p", "")))

def km(lat1, lng1, lat2, lng2):
    p = math.pi / 180
    a = (np.sin((lat2 - lat1) * p / 2) ** 2 + np.cos(lat1 * p) * np.cos(lat2 * p) * np.sin((lng2 - lng1) * p / 2) ** 2)
    return 12742 * np.arcsin(np.sqrt(a))

def lease_years(s):
    y = re.search(r"(\d+)\s*year", s or ""); m = re.search(r"(\d+)\s*month", s or "")
    return (int(y.group(1)) if y else np.nan) + (int(m.group(1)) / 12 if m else 0)

def features(df):
    f = pd.DataFrame(index=df.index)
    f["town"] = df["town"].astype("category")
    f["type"] = df["type"].astype("category")
    f["sqm"] = df["sqm"].astype(float)
    f["storey"] = df["storey"].str.slice(0, 2).astype(float) + 1  # middle of "04 TO 06"
    f["lease_left"] = df["lease"].map(lease_years)
    f["month"] = (df["ym"] // 100 - 2017) * 12 + df["ym"] % 100  # months since Jan 2017
    key = df["blk"].astype(str) + "|" + df["st"].astype(str)
    def _xy(blk, st):
        c = coords.get(f"{blk}|{st}")
        if c:
            return c["lat"], c["lng"], str(c.get("postal", ""))
        return FALLBACK_XY.get((str(blk).upper(), _norm_st(st)), (np.nan, np.nan, ""))
    xy = [_xy(b, st) for b, st in zip(df["blk"], df["st"])]
    f["lat"] = [v[0] for v in xy]
    f["lng"] = [v[1] for v in xy]
    d = np.full(len(df), np.nan)
    ok = f["lat"].notna().values
    if ok.any():
        la, ln = f["lat"].values[ok][:, None], f["lng"].values[ok][:, None]
        d[ok] = km(la, ln, mrt_xy[:, 0][None, :], mrt_xy[:, 1][None, :]).min(axis=1)
    f["mrt_km"] = d
    if os.environ.get("EXTRA_FEATURES", "1") == "1":
        ok2 = f["lat"].notna().values
        la, ln = f["lat"].values[ok2][:, None], f["lng"].values[ok2][:, None]
        for name, pts in AMEN.items():
            if not len(pts):
                continue
            dd = km(la, ln, pts[:, 0][None, :], pts[:, 1][None, :])
            v = np.full(len(df), np.nan); v[ok2] = dd.min(axis=1); f[name + "_km"] = v
            if name in ("prim", "hawker", "superm"):
                c = np.full(len(df), np.nan); c[ok2] = (dd <= 1.0).sum(axis=1); f[name + "_1km"] = c
        dm = km(la, ln, mrt_xy[:, 0][None, :], mrt_xy[:, 1][None, :])
        c = np.full(len(df), np.nan); c[ok2] = (dm <= 1.0).sum(axis=1); f["mrt_1km"] = c
        v = np.full(len(df), np.nan); v[ok2] = km(la[:, 0], ln[:, 0], CBD[0], CBD[1]); f["cbd_km"] = v
        post = pd.Series([v[2] for v in xy], index=df.index)
        info = post.map(lambda p: BLOCK_INFO.get(p, (None, None, None)))
        f["blk_floors"] = info.map(lambda x: x[0]).astype(float)
        f["blk_units"] = info.map(lambda x: x[1]).astype(float)
        f["blk_year"] = info.map(lambda x: x[2]).astype(float)
    if "model" in df.columns and os.environ.get("FLAT_MODEL", "1") == "1":
        f["flat_model"] = df["model"].fillna("").astype(str).replace("", "Unknown").astype("category")
        f["lease_start"] = df["lc"].replace(0, np.nan).astype(float)
    k3 = list(zip(df["blk"], df["st"], df["type"]))
    f["blk_prem"] = [BLOCK_PREM["prem"].get(k, np.nan) for k in k3]
    f["blk_prem_n"] = [BLOCK_PREM["prem_n"].get(k, 0) for k in k3]
    f["blk_prem_any"] = [BLOCK_PREM_ANY.get((b, st), np.nan) for b, st, _ in k3]
    return f

if __name__ == "__main__":
    X = features(hist_all); y = np.log(hist_all["price"].astype(float))
    cats = {c: X[c].cat.categories for c in X.columns if str(X[c].dtype) == "category"}

    # time holdout: train on older months, test on the latest 3
    cut = sorted(hist_all["ym"].unique())[-3]
    tr, te = (hist_all["ym"] < cut).values, (hist_all["ym"] >= cut).values
    params = dict(objective="regression", learning_rate=0.05, num_leaves=127, min_data_in_leaf=20,
                  feature_fraction=0.9, bagging_fraction=0.9, bagging_freq=1, verbose=-1, seed=42,
                  # price can only go up with floor area, storey and remaining lease
                  monotone_constraints=[1 if c in ("sqm", "storey", "lease_left") else 0 for c in X.columns],
                  monotone_constraints_method="advanced")
    m = lgb.train(params, lgb.Dataset(X[tr], y[tr]), num_boost_round=2000)
    pred = np.exp(m.predict(X[te])); act = hist_all.loc[te, "price"].values
    err = np.abs(pred - act); pct = err / act
    # residual spread on held-out data -> width of the range we show
    resid = np.log(act / pred)
    lo_q, hi_q = np.quantile(resid, [0.1, 0.9])

    report = {
        "trained_on": int(tr.sum()), "tested_on": int(te.sum()),
        "test_months": sorted(int(v) for v in hist_all.loc[te, "ym"].unique()), "history_from": int(hist_all["ym"].min()),
        "mae_sgd": round(float(err.mean())), "median_abs_err_sgd": round(float(np.median(err))),
        "mape_pct": round(float(pct.mean() * 100), 2), "median_ape_pct": round(float(np.median(pct) * 100), 2),
        "within_5pct": round(float((pct <= .05).mean() * 100), 1), "within_10pct": round(float((pct <= .10).mean() * 100), 1),
        "range_band_log": [round(float(lo_q), 4), round(float(hi_q), 4)],
        "range_coverage_pct": round(float(((resid >= lo_q) & (resid <= hi_q)).mean() * 100), 1),
        "missing_coords_pct": round(float(X["lat"].isna().mean() * 100), 2),
    }
    by_type = (pd.DataFrame({"type": hist_all.loc[te, "type"].values, "pct": pct}).groupby("type")["pct"]
               .agg(["count", "median"]).assign(median=lambda d: (d["median"] * 100).round(2)))
    report["median_ape_by_type"] = by_type.reset_index().to_dict("records")

    # final model on all 12 months for publishing
    final = lgb.train(params, lgb.Dataset(X, y), num_boost_round=2000)
    final.save_model(f"{OUT}/miji_price_model.txt")
    json.dump({"cats": {k: list(v) for k, v in cats.items()}, "range_band_log": report["range_band_log"],
               "features": list(X.columns), "trained_through": int(hist_all["ym"].max())},
              open(f"{OUT}/miji_price_meta.json", "w"), indent=1)
    imp = dict(zip(X.columns, final.feature_importance("gain").round()))
    report["feature_importance_gain"] = {k: float(v) for k, v in sorted(imp.items(), key=lambda t: -t[1])}
    json.dump(report, open(f"{OUT}/report.json", "w"), indent=1)
    print(json.dumps(report, indent=1))
