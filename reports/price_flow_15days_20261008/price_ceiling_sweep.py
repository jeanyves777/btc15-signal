"""Observed-quote sweep for a strict maximum IOC entry price."""
import json

import run


signals, polls = run.load()
out = {}
for ceiling in (.85, .86, .87, .88, .89, .90, .91, .92, .93, .99):
    days, trades = run.replay(
        signals, polls, "wait_85_either_side", slippage=0, target=True, maximum=ceiling)
    out[f"{ceiling:.2f}"] = run.summary(days, trades)
print(json.dumps(out, indent=2))
(run.OUT / "price_ceiling_sweep.json").write_text(json.dumps(out, indent=2), encoding="utf-8")
