"""Experiment: does training on the full HDB history (Jan 2017 onwards) beat the
12-month model? Same test set for every variant: the latest 3 months of sales.
Writes ml/report_full.json. Needs internet (data.gov.sg), so it runs in GitHub Actions.
"""
import json, os, sys
import numpy as np, pandas as pd, lightgbm as lgb

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, REPO); sys.path.insert(0, HERE)
import miji_index_build as mib          # reuses its data.gov.sg download + row cleaning
import train                            # reuses the feature code

full = pd.DataFrame(mib.build_recent_hdb_transactions(recent_months=12 * 11))
print("full history rows:", len(full), full["ym"].min(), "->", full["ym"].max())
full = full[full["type"].isin(["2-room", "3-room", "4-room", "5-room", "Executive"])].reset_index(drop=True)

months = sorted(full["ym"].unique())
test_from = months[-3]
last12_from = months[-12]

X = train.features(full)
y = np.log(full["price"].astype(float))
X["month"] = (full["ym"] // 100 - 2017) * 12 + full["ym"] % 100   # months since Jan 2017
for c in ["town", "type"]:
    X[c] = X[c].astype("category")
te = (full["ym"] >= test_from).values
act = full.loc[te, "price"].values

def params():
    return dict(objective="regression", learning_rate=0.05, num_leaves=127, min_data_in_leaf=20,
                feature_fraction=0.9, bagging_fraction=0.9, bagging_freq=1, verbose=-1, seed=42,
                monotone_constraints=[1 if c in ("sqm", "storey", "lease_left") else 0 for c in X.columns],
                monotone_constraints_method="advanced")

def score(name, tr_mask, weight=None, rounds=2000):
    ds = lgb.Dataset(X[tr_mask], y[tr_mask], weight=None if weight is None else weight[tr_mask])
    m = lgb.train(params(), ds, num_boost_round=rounds)
    pred = np.exp(m.predict(X[te]))
    pct = np.abs(pred - act) / act
    r = {"variant": name, "train_rows": int(tr_mask.sum()),
         "median_ape_pct": round(float(np.median(pct) * 100), 2),
         "mape_pct": round(float(pct.mean() * 100), 2),
         "within_5pct": round(float((pct <= .05).mean() * 100), 1),
         "within_10pct": round(float((pct <= .10).mean() * 100), 1),
         "mae_sgd": round(float(np.abs(pred - act).mean()))}
    ex = full.loc[te].assign(pct=pct)
    r["exec_median_ape_pct"] = round(float(ex.loc[ex["type"] == "Executive", "pct"].median() * 100), 2)
    print(json.dumps(r)); return r

age = (full["ym"].max() // 100 * 12 + full["ym"].max() % 100) - (full["ym"] // 100 * 12 + full["ym"] % 100)
results = [
    score("last 12 months (current)", ((full["ym"] >= last12_from) & ~te).values),
    score("full history since 2017", ~te),
    score("full history, recent sales weighted (half-life 24 months)", ~te, weight=(0.5 ** (age / 24)).values),
    score("full history, recent sales weighted (half-life 12 months)", ~te, weight=(0.5 ** (age / 12)).values),
    score("last 36 months", ((full["ym"] >= months[-36]) & ~te).values),
]
json.dump({"test_months": [int(m) for m in months[-3:]], "test_rows": int(te.sum()), "results": results},
          open(os.path.join(HERE, "report_full.json"), "w"), indent=1)
