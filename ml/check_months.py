"""Check: full-history model vs Miji's Oct (monthly final) and Nov (weekly update) sales.
For each month, trains only on sales BEFORE it. Writes ml/report_months.json."""
import json, os, sys
import numpy as np, pandas as pd, lightgbm as lgb
os.environ["FULL_HISTORY"] = "1"
HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, HERE)
import train
h = train.hist_all; X = train.features(h); y = np.log(h["price"].astype(float)).values
cols = list(X.columns)
p = dict(objective="regression", learning_rate=0.05, num_leaves=127, min_data_in_leaf=20, feature_fraction=0.9,
         bagging_fraction=0.9, bagging_freq=1, verbose=-1, seed=42,
         monotone_constraints=[1 if c in ("sqm", "storey", "lease_left") else 0 for c in cols], monotone_constraints_method="advanced")
out = []
for ym, label, f in [(202610, "Oct 2026 (monthly final)", "monthly_final_latest.json"), (202611, "Nov 2026 so far (weekly update)", "weekly_highlights.json")]:
    tr = (h["ym"] < ym).values; te = (h["ym"] == ym).values
    m = lgb.train(p, lgb.Dataset(X[tr], y[tr]), num_boost_round=2000)
    pred = np.exp(m.predict(X[te])); act = h["price"].values[te]; pct = np.abs(pred - act) / act
    top = pd.DataFrame(json.load(open(os.path.join(train.REPO, "index_data", f)))["hdb"]["top"])[:6]
    tp = np.exp(m.predict(train.features(top)))
    r = {"month": label, "sales": int(te.sum()), "median_ape_pct": round(float(np.median(pct) * 100), 2),
         "within_5pct": round(float((pct <= .05).mean() * 100), 1), "within_10pct": round(float((pct <= .1).mean() * 100), 1),
         "median_sale": float(np.median(act)), "median_estimate": round(float(np.median(pred))),
         "top_sales": [{"sale": f"{t.town} {t.blk} {t.st}, {t.type}, {t.storey}", "sold": int(t.price), "model": round(float(e)),
                        "off_pct": round(float((e - t.price) / t.price * 100), 1)} for t, e in zip(top.itertuples(), tp)]}
    print(json.dumps(r)); out.append(r)
json.dump(out, open(os.path.join(HERE, "report_months.json"), "w"), indent=1)
