"""Build per-town ML price estimate files for publishing.

For every block + flat type with a resale in the last 12 months, estimate today's
price for each storey band seen in that block, using the block's typical floor area
and its remaining lease as of the latest data month. Output: estimates/<town-slug>.json
"""
import json, os, re, sys
import numpy as np, pandas as pd, lightgbm as lgb
from train import features, tx, OUT, REPO  # same feature code and data

meta = json.load(open(f"{OUT}/miji_price_meta.json"))
model = lgb.Booster(model_file=f"{OUT}/miji_price_model.txt")
lo_b, hi_b = meta["range_band_log"]
latest = int(tx["ym"].max())

def months_between(a, b):
    return (b // 100 - a // 100) * 12 + (b % 100 - a % 100)

top = tx.assign(t=tx["storey"].str.slice(-2).astype(int)).groupby(["blk", "st"])["t"].max()
rows = []
has_model = "model" in tx.columns
if not has_model:
    tx = tx.assign(model="", lc=0)
for (town, blk, st, typ, fm), g in tx.groupby(["town", "blk", "st", "type", "model"]):
    g = g.sort_values("ym")
    last = g.iloc[-1]
    sqm = float(g["sqm"].median())
    # remaining lease today = last recorded lease minus months elapsed since that sale
    y = re.search(r"(\d+)\s*year", last["lease"]); m = re.search(r"(\d+)\s*month", last["lease"])
    left = (int(y.group(1)) + (int(m.group(1)) / 12 if m else 0)) - months_between(int(last["ym"]), latest) / 12
    lm = int(round(left * 12))
    lease_txt = f"{lm // 12} years {lm % 12:02d} months"
    bands = [f"{a:02d} TO {a + 2:02d}" for a in range(1, int(top[(blk, st)]) + 1, 3)]
    for band in bands:
        rows.append(dict(town=town, blk=blk, st=st, type=typ, model=fm, lc=int(g["lc"].max()), storey=band, sqm=sqm,
                         lease=lease_txt, ym=latest, n=int(len(g))))

grid = pd.DataFrame(rows)
X = features(grid)
if not has_model:
    X = X.drop(columns=[c for c in ("flat_model", "lease_start") if c in X.columns])
for c, cats in meta["cats"].items():
    X[c] = pd.Categorical(X[c].astype(str), categories=cats)
X = X[meta["features"]]
p = np.exp(model.predict(X))
grid["est"] = (np.round(p / 1000) * 1000).astype(int)
grid["lo"] = (np.round(p * np.exp(lo_b) / 1000) * 1000).astype(int)
grid["hi"] = (np.round(p * np.exp(hi_b) / 1000) * 1000).astype(int)

out_dir = f"{REPO}/estimates"; os.makedirs(out_dir, exist_ok=True)
slug = lambda t: re.sub(r"[^a-z0-9]+", "-", t.lower()).strip("-")
note = ("Machine-learning price estimate, not a valuation. Estimated from HDB resale transactions "
        "in the last 12 months (data.gov.sg) using town, flat type, floor area, storey, remaining lease, "
        "location and distance to MRT. Actual prices depend on condition, renovation, facing and the market.")
for town, g in grid.groupby("town"):
    blocks = {}
    for r in g.itertuples():
        k = f"{r.blk}|{r.st}|{r.type}|{r.model}"
        b = blocks.setdefault(k, {"blk": r.blk, "st": r.st, "type": r.type, "model": r.model or None, "sqm": r.sqm,
                                  "lease": r.lease, "sales_12m": r.n, "storeys": {}})
        b["storeys"][r.storey] = [int(r.lo), int(r.est), int(r.hi)]
    json.dump({"town": town, "as_of": latest, "model": "LightGBM v1", "note": note,
               "blocks": list(blocks.values())},
              open(f"{out_dir}/{slug(town)}.json", "w"), separators=(",", ":"))
index = {}
for r in grid.drop_duplicates(["blk", "st"]).itertuples():
    index[f"{r.blk}|{r.st}"] = slug(r.town)
json.dump({"as_of": latest, "blocks": index}, open(f"{out_dir}/index.json", "w"), separators=(",", ":"))
print(len(grid), "estimates across", grid["town"].nunique(), "towns;",
      sum(os.path.getsize(f"{out_dir}/{f}") for f in os.listdir(out_dir)) // 1024, "KB")

