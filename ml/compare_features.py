"""Experiment: do schools, hawkers, parks, supermarkets, LRT, distance to CBD and
block details (height, units, year built) lower the error? Full HDB history,
same test set (latest 3 months) for every variant. Writes ml/report_features.json.
"""
import json, os, sys
import numpy as np, pandas as pd, lightgbm as lgb

os.environ["FULL_HISTORY"] = "1"
HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, HERE)
import train

full = train.hist_all
X_all = train.features(full)
y = np.log(full["price"].astype(float)).values
months = sorted(full["ym"].unique()); te = (full["ym"] >= months[-3]).values; tr = ~te
act = full.loc[te, "price"].values

BASE = ["town", "type", "sqm", "storey", "lease_left", "month", "lat", "lng", "mrt_km", "blk_prem", "blk_prem_n", "blk_prem_any"]
SCHOOLS = ["prim_km", "prim_1km", "sec_km"]
FOOD_SHOPS = ["hawker_km", "hawker_1km", "superm_km", "superm_1km"]
GREEN_TRANSPORT = ["park_km", "lrt_km", "mrt_1km", "cbd_km"]
BLOCK = ["blk_floors", "blk_units", "blk_year"]

def run(name, cols):
    X = X_all[cols]
    p = dict(objective="regression", learning_rate=0.05, num_leaves=127, min_data_in_leaf=20,
             feature_fraction=0.9, bagging_fraction=0.9, bagging_freq=1, verbose=-1, seed=42,
             monotone_constraints=[1 if c in ("sqm", "storey", "lease_left") else 0 for c in cols],
             monotone_constraints_method="advanced")
    m = lgb.train(p, lgb.Dataset(X[tr], y[tr]), num_boost_round=2000)
    pred = np.exp(m.predict(X[te])); pct = np.abs(pred - act) / act
    r = {"variant": name, "features": len(cols),
         "median_ape_pct": round(float(np.median(pct) * 100), 2), "mape_pct": round(float(pct.mean() * 100), 2),
         "within_5pct": round(float((pct <= .05).mean() * 100), 1), "within_10pct": round(float((pct <= .10).mean() * 100), 1),
         "mae_sgd": round(float(np.abs(pred - act).mean()))}
    if name.startswith("everything"):
        imp = dict(zip(cols, m.feature_importance("gain")))
        tot = sum(imp.values())
        r["importance_pct"] = {k: round(v / tot * 100, 1) for k, v in sorted(imp.items(), key=lambda t: -t[1])}
    print(json.dumps(r)); return r

res = [
    run("current (v2)", BASE),
    run("+ schools", BASE + SCHOOLS),
    run("+ hawkers and supermarkets", BASE + FOOD_SHOPS),
    run("+ parks, LRT, MRT count, distance to CBD", BASE + GREEN_TRANSPORT),
    run("+ block height, units, year built", BASE + BLOCK),
    run("everything", BASE + SCHOOLS + FOOD_SHOPS + GREEN_TRANSPORT + BLOCK),
]
json.dump({"test_months": [int(m) for m in months[-3:]], "test_rows": int(te.sum()), "results": res},
          open(os.path.join(HERE, "report_features.json"), "w"), indent=1)
