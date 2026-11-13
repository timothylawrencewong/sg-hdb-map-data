"""Miji condo / apartment / EC price model (LightGBM).

Trains on every URA private residential transaction in private_data/d*.json (about 5 years),
tests on the latest 3 months it has never seen, writes ml/condo_report.json, and with
--publish writes estimates/condo/<district>.json + estimates/condo/index.json.
Landed homes are left out on purpose: too few sales and every house is different.
"""
import glob, json, math, os, re, sys
import numpy as np, pandas as pd, lightgbm as lgb

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
CONDO_TYPES = {"Apartment", "Condominium", "Executive Condominium"}
CBD = (1.2840, 103.8515)


def km(lat1, lng1, lat2, lng2):
    p = math.pi / 180
    a = np.sin((lat2 - lat1) * p / 2) ** 2 + np.cos(lat1 * p) * np.cos(lat2 * p) * np.sin((lng2 - lng1) * p / 2) ** 2
    return 12742 * np.arcsin(np.sqrt(a))


def load():
    rows = []
    for f in sorted(glob.glob(f"{REPO}/private_data/d*.json")):
        d = json.load(open(f))
        types, tenures = d["types"], d["tenures"]
        for p in d["P"]:
            if p.get("seg") not in ("CCR", "RCR", "OCR") or p.get("la") is None:
                continue
            mrt_m = (p.get("mr") or [None, np.nan])[1]
            for t in p["T"]:
                ym, price, sqm, floor, sale, ti, te, units = t[:8]
                ptype = types[ti] if 0 <= ti < len(types) else ""
                if units != 1 or ptype not in CONDO_TYPES or not price or not sqm:
                    continue
                tenure = tenures[te] if 0 <= te < len(tenures) else ""
                rows.append((d["district"], p["n"], p["s"], p["seg"], ptype, tenure, floor, float(sqm),
                             int(sale), int(ym), float(price), p["la"], p["ln"], mrt_m))
    return pd.DataFrame(rows, columns=["district", "project", "street", "seg", "ptype", "tenure", "floor", "sqm",
                                       "sale", "ym", "price", "lat", "lng", "mrt_m"])


def tenure_feats(s):
    if not s or s.lower().startswith("freehold"):
        return 9999.0, np.nan
    y = re.search(r"(\d+)\s*yrs?", s); c = re.search(r"from\s*(\d{4})", s)
    return (float(y.group(1)) if y else np.nan), (float(c.group(1)) if c else np.nan)


def features(df):
    f = pd.DataFrame(index=df.index)
    for c in ["district", "project", "seg", "ptype"]:
        f[c] = df[c].astype("category")
    f["sqm"] = df["sqm"]
    lo = pd.to_numeric(df["floor"].astype(str).str.extract(r"^B?(\d+)")[0], errors="coerce")
    hi = pd.to_numeric(df["floor"].astype(str).str.extract(r"(\d+)$")[0], errors="coerce")
    f["floor"] = (lo + hi) / 2
    tf = df["tenure"].map(tenure_feats)
    f["lease_years"] = [a for a, _ in tf]
    start = np.array([b for _, b in tf])
    yr = (df["ym"] // 100 + (df["ym"] % 100) / 12).values
    f["lease_left"] = np.where(np.isnan(start), 9999.0, f["lease_years"].values - (yr - start))
    f["sale"] = df["sale"].astype("category")  # 1 new sale, 2 sub-sale, 3 resale
    f["month"] = (df["ym"] // 100 - 2017) * 12 + df["ym"] % 100
    f["lat"], f["lng"] = df["lat"], df["lng"]
    f["mrt_km"] = df["mrt_m"] / 1000
    f["cbd_km"] = km(df["lat"].values, df["lng"].values, CBD[0], CBD[1])
    return f


PARAMS = dict(objective="regression", learning_rate=0.05, num_leaves=127, min_data_in_leaf=20,
              feature_fraction=0.9, bagging_fraction=0.9, bagging_freq=1, verbose=-1, seed=42,
              max_cat_to_onehot=4, cat_smooth=10)


def mono(cols):
    return [1 if c in ("sqm", "floor", "lease_left") else 0 for c in cols]


if __name__ == "__main__":
    df = load()
    X = features(df); y = np.log(df["price"]).values
    months = sorted(df["ym"].unique()); te = (df["ym"] >= months[-3]).values; tr = ~te
    p = dict(PARAMS, monotone_constraints=mono(X.columns), monotone_constraints_method="advanced")
    m = lgb.train(p, lgb.Dataset(X[tr], y[tr]), num_boost_round=2000)
    pred = np.exp(m.predict(X[te])); act = df["price"].values[te]; pct = np.abs(pred - act) / act
    resid = np.log(act / pred)
    is_resale = (df.loc[te, "sale"] == 3).values  # published estimates are for resale, so the range comes from resales
    lo_q, hi_q = np.quantile(resid[is_resale], [0.1, 0.9])
    d = df.loc[te].assign(pct=pct)
    report = {
        "history_from": int(df["ym"].min()), "trained_on": int(tr.sum()), "tested_on": int(te.sum()),
        "test_months": [int(x) for x in months[-3:]],
        "median_ape_pct": round(float(np.median(pct) * 100), 2), "mape_pct": round(float(pct.mean() * 100), 2),
        "within_5pct": round(float((pct <= .05).mean() * 100), 1), "within_10pct": round(float((pct <= .10).mean() * 100), 1),
        "mae_sgd": round(float(np.abs(pred - act).mean())),
        "range_band_log": [round(float(lo_q), 4), round(float(hi_q), 4)],
        "resale_median_ape_pct": round(float(np.median(pct[is_resale]) * 100), 2),
        "resale_within_5pct": round(float((pct[is_resale] <= .05).mean() * 100), 1),
        "resale_within_10pct": round(float((pct[is_resale] <= .10).mean() * 100), 1),
        "by_type": {k: [int(len(g)), round(float(g["pct"].median() * 100), 2)] for k, g in d.groupby("ptype")},
        "by_sale": {{1: "new sale", 2: "sub-sale", 3: "resale"}[k]: [int(len(g)), round(float(g["pct"].median() * 100), 2)] for k, g in d.groupby("sale")},
        "by_segment": {k: [int(len(g)), round(float(g["pct"].median() * 100), 2)] for k, g in d.groupby("seg")},
    }
    imp = dict(zip(X.columns, m.feature_importance("gain"))); tot = sum(imp.values())
    report["importance_pct"] = {k: round(v / tot * 100, 1) for k, v in sorted(imp.items(), key=lambda t: -t[1])}
    json.dump(report, open(f"{HERE}/condo_report.json", "w"), indent=1)
    print(json.dumps(report, indent=1))

    if "--publish" in sys.argv:
        final = lgb.train(p, lgb.Dataset(X, y), num_boost_round=2000)
        latest = int(df["ym"].max())
        recent = df[df["ym"] >= months[-12]]
        grid = []
        for (dist, proj, street, ptype, tenure), g in recent.groupby(["district", "project", "street", "ptype", "tenure"]):
            g0 = g.iloc[-1]
            tops = pd.to_numeric(g["floor"].astype(str).str.extract(r"-(\d+)$")[0], errors="coerce").dropna()
            if tops.empty:
                continue
            bands = [f"{a:02d}-{a + 4:02d}" for a in range(1, int(tops.max()) + 1, 5)]
            for sz in sorted({round(float(s)) for s in g["sqm"].quantile([0.1, 0.5, 0.9])}):
                for b in bands:
                    grid.append(dict(district=dist, project=proj, street=street, seg=g0["seg"], ptype=ptype, tenure=tenure,
                                     floor=b, sqm=float(sz), sale=3, ym=latest, price=1.0, lat=g0["lat"], lng=g0["lng"],
                                     mrt_m=g0["mrt_m"], sales_12m=len(g)))
        grid = pd.DataFrame(grid)
        Xg = features(grid)
        for c in ["district", "project", "seg", "ptype", "sale"]:
            Xg[c] = pd.Categorical(Xg[c].astype(str) if c != "sale" else Xg[c].astype(int), categories=X[c].cat.categories)
        pg = np.exp(final.predict(Xg[X.columns]))
        grid["est"] = (np.round(pg / 1000) * 1000).astype(int)
        grid["lo"] = (np.round(pg * np.exp(lo_q) / 1000) * 1000).astype(int)
        grid["hi"] = (np.round(pg * np.exp(hi_q) / 1000) * 1000).astype(int)
        out = f"{REPO}/estimates/condo"; os.makedirs(out, exist_ok=True)
        index = {}
        for dist, g in grid.groupby("district"):
            projects = {}
            for r in g.itertuples():
                k = f"{r.project}|{r.ptype}|{r.tenure}"
                pj = projects.setdefault(k, {"project": r.project, "street": r.street, "type": r.ptype, "tenure": r.tenure,
                                             "sales_12m": int(r.sales_12m), "sizes": {}})
                pj["sizes"].setdefault(str(int(r.sqm)), {})[r.floor] = [int(r.lo), int(r.est), int(r.hi)]
                index[r.project.upper()] = dist
            json.dump({"district": dist, "as_of": latest, "model": "LightGBM condo v1", "projects": list(projects.values())},
                      open(f"{out}/d{dist}.json", "w"), separators=(",", ":"))
        json.dump({"as_of": latest, "projects": index}, open(f"{out}/index.json", "w"), separators=(",", ":"))
        print(len(grid), "condo estimates across", grid["project"].nunique(), "projects")
