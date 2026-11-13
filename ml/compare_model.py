"""Experiment: does HDB's flat model (Maisonette, DBSS, Premium Apartment...) plus lease
start year lower the error? Full history, test = latest 3 months. Writes ml/report_model.json."""
import json, os, sys
import numpy as np, pandas as pd, lightgbm as lgb
os.environ["FULL_HISTORY"] = "1"
HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, HERE)
import train
h = train.hist_all; X_all = train.features(h); y = np.log(h["price"].astype(float)).values
months = sorted(h["ym"].unique()); te = (h["ym"] >= months[-3]).values; tr = ~te; act = h["price"].values[te]
def run(name, cols):
    p = dict(objective="regression", learning_rate=0.05, num_leaves=127, min_data_in_leaf=20, feature_fraction=0.9,
             bagging_fraction=0.9, bagging_freq=1, verbose=-1, seed=42,
             monotone_constraints=[1 if c in ("sqm", "storey", "lease_left") else 0 for c in cols], monotone_constraints_method="advanced")
    m = lgb.train(p, lgb.Dataset(X_all[cols][tr], y[tr]), num_boost_round=2000)
    pred = np.exp(m.predict(X_all[cols][te])); pct = np.abs(pred - act) / act
    d = h.loc[te].assign(pct=pct)
    r = {"variant": name, "median_ape_pct": round(float(np.median(pct) * 100), 2), "within_5pct": round(float((pct <= .05).mean() * 100), 1),
         "within_10pct": round(float((pct <= .1).mean() * 100), 1), "mae_sgd": round(float(np.abs(pred - act).mean())),
         "by_flat_model": {k: [int(len(g)), round(float(g["pct"].median() * 100), 2)] for k, g in d.groupby("model") if len(g) >= 10}}
    print(json.dumps(r)); return r
base = [c for c in X_all.columns if c not in ("flat_model", "lease_start")]
res = [run("current (v3) + coordinate fix", base), run("+ flat model + lease start year", base + ["flat_model", "lease_start"])]
json.dump({"test_rows": int(te.sum()), "missing_coords": int(X_all["lat"].isna().sum()), "results": res},
          open(os.path.join(HERE, "report_model.json"), "w"), indent=1)
