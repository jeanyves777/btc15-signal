"""The local model PROPOSES; the statistics DISPOSE. Run inside every learning run.

Moved here from scripts/lifecycle_hypotheses.py on 2026-09-28 so the service can
call it (operator: "do so the system learns and adapts"). It had run once, by
hand, for BTC only; now every instance runs it after each learning run.

WHY IT EXISTS. The statistical learner keys every decision on
distance x price x momentum, declared IN ADVANCE on purpose so it could not be
chosen on whichever partition happened to light up. The price of that
discipline is a blind spot: it can only learn WITHIN the keying. The trade
lifecycle is recorded - `observations` carries book depth, spread, trade
counts, session, time left - and this is what reads it for learning.

THE DIVISION, and it is the whole safety argument:

    the model PROPOSES a hypothesis as a machine-checkable filter over recorded
    columns. It never emits a number, a threshold it computed, or a decision.

    THIS MODULE disposes: every proposal is compiled, run over the recorded
    windows, and scored by a day-clustered bootstrap, with Benjamini-Hochberg
    across the batch.

    NOTHING IS ADOPTED. A survivor is a CANDIDATE for keying, reported to the
    operator; it still has to clear the ordinary promotion bar as a cell before
    it can affect an order.

A PROPOSAL IS A JSON OBJECT, and anything else is rejected without being run:

    {"name": "...", "why": "...",
     "where": [{"column": "spread_bps", "op": ">", "value": 200}, ...]}

Only columns in ALLOWED and operators in OPS are accepted, values must be
numeric or a plain string, and at most MAX_TERMS conjuncts. There is no eval
anywhere in this file: the filter is applied by comparing Python values.
"""

from __future__ import annotations

import json
import random
import sqlite3
import statistics
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

# COLUMNS THE MODEL MAY REASON ABOUT. Everything here is recorded at or before
# the decision instant. Nothing that is only known afterwards is listed, because
# a filter on an outcome column would select its own answer.
ALLOWED = {
    "spread_bps", "normalized_distance", "distance_bps", "momentum_5m_bps",
    "volatility_5m_bps", "our_ask", "yes_bid", "remaining_s",
    "book_yes_depth", "book_no_depth", "book_yes_share", "book_bid_size",
    "book_ask_size", "book_levels", "depth_bid_qty", "depth_ask_qty",
    "trade_count", "buy_volume", "sell_volume", "open_interest", "volume",
    "book_age_s", "elapsed_minutes", "session", "weekday", "hour_utc",
    "vol_regime", "side", "regime_weight", "confidence_adjustment",
}
# EXPLICITLY NOT ALLOWED, and named so the exclusion is legible rather than an
# accident of which columns got typed: `won`, `final_price`, `exit_price`,
# `exit_reason`, `realised_pnl`, `unrealised` are outcomes.
OPS = {">", ">=", "<", "<=", "==", "!="}
MAX_TERMS = 3
MAX_PROPOSALS = 8
# 700 cut SOL's reply off inside its 10th proposal on every observed run, and
# a cut-off JSON reply used to parse to nothing at all (FINDINGS 107). Fewer,
# shorter proposals and room to finish them; a cut-off reply is salvaged.
MAX_TOKENS = 1200

SYSTEM = (
    "You are a quantitative research assistant. You propose HYPOTHESES about "
    "when a 15-minute binary options setup is more or less likely to settle in "
    "the money. You never state a result, a win rate, or a number you computed "
    "- another system measures every hypothesis you give. Reply with JSON only."
)

# The control set: if a model's proposals do not beat these, it is
# contributing nothing a blunt sweep would not have found.
BASELINE = [
    {"name": "wide book", "why": "control",
     "terms": [("spread_bps", ">", 300)]},
    {"name": "thin trade count", "why": "control",
     "terms": [("trade_count", "<", 10)]},
    {"name": "stale book", "why": "control",
     "terms": [("book_age_s", ">", 20)]},
    {"name": "late entry", "why": "control",
     "terms": [("remaining_s", "<", 420)]},
]


def prompt_for(asset: str, summary: dict) -> str:
    cols = ", ".join(sorted(ALLOWED))
    return f"""Instrument: {asset}. Below is a summary of recorded trade
lifecycles. Each row was a live decision; `residual` is win rate minus the
price paid, so positive means the setups in that slice beat their price.

{json.dumps(summary, separators=(",", ":"))[:2000]}

Propose up to {MAX_PROPOSALS} hypotheses about WHICH RECORDED CONDITIONS
separate winning setups from losing ones. Look for conditions the summary hints
at but does not already key on - the existing system keys only on distance,
price and momentum, so those three are already covered and a hypothesis using
only them adds nothing.

You may use ONLY these columns: {cols}

Reply with JSON only, this exact shape, no prose around it. Give every
hypothesis a DIFFERENT name of at most six words, and keep "why" under
fifteen words:
{{"hypotheses": [
  {{"name": "short name",
    "why": "one short sentence of reasoning",
    "where": [{{"column": "spread_bps", "op": ">", "value": 200}}]}}
]}}

Each hypothesis may have at most {MAX_TERMS} conditions in "where".
Operators allowed: {sorted(OPS)}."""


def ask_model(url: str, model: str, prompt: str, timeout: float) -> str:
    body = json.dumps({
        "model": model,
        "messages": [{"role": "system", "content": SYSTEM},
                     {"role": "user", "content": prompt}],
        "temperature": 0.4, "max_tokens": MAX_TOKENS,
    }).encode()
    req = urllib.request.Request(
        url.rstrip("/") + "/chat/completions", data=body,
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        payload = json.loads(r.read().decode())
    choice = payload["choices"][0]
    message = choice.get("message") or {}
    # A REASONING MODEL PUTS ITS ANSWER SOMEWHERE ELSE, and will spend the whole
    # token budget before writing any of it: `content` empty, the answer in
    # `reasoning_content`, `finish_reason` "length". Both are read and the
    # finish reason is surfaced rather than swallowed.
    text = (message.get("content") or "").strip()
    if not text:
        text = (message.get("reasoning_content") or "").strip()
    if not text and choice.get("finish_reason") == "length":
        raise RuntimeError(
            "model spent its whole token budget without answering")
    return text


def _salvage(text: str) -> list:
    """The complete proposal objects of a reply cut off part-way."""
    at = text.find('"hypotheses"')
    at = text.find("[", at) if at >= 0 else -1
    if at < 0:
        return []
    decoder, items, i = json.JSONDecoder(), [], at + 1
    while True:
        while i < len(text) and text[i] in " \t\r\n,":
            i += 1
        try:
            item, i = decoder.raw_decode(text, i)
        except json.JSONDecodeError:
            return items
        items.append(item)


def parse_reply(text: str) -> tuple[list, str]:
    """(well-formed proposals, a note when the reply was not whole JSON).

    A reply cut off at the token limit used to parse to NOTHING and be
    recorded as a healthy model that proposed 0 - indistinguishable from one
    that found nothing. Its complete proposals are kept and the cut is said.
    """
    note = ""
    start, end = text.find("{"), text.rfind("}")
    try:
        if start < 0 or end <= start:
            raise json.JSONDecodeError("no object", text, 0)
        items = json.loads(text[start:end + 1]).get("hypotheses") or []
    except (json.JSONDecodeError, AttributeError):
        items = _salvage(text)
        note = (f"reply cut off; {len(items)} complete proposal(s) recovered"
                if items else "reply was not valid JSON")
    return _valid(items), note


def parse_proposals(text: str) -> list:
    """Accept only well-formed proposals over allowed columns. No eval, ever."""
    return parse_reply(text)[0]


def _valid(items) -> list:
    out = []
    for h in (items if isinstance(items, list) else [])[:MAX_PROPOSALS]:
        if not isinstance(h, dict):
            continue
        where = h.get("where")
        if not isinstance(where, list) or not 1 <= len(where) <= MAX_TERMS:
            continue
        terms = []
        for t in where:
            if not isinstance(t, dict):
                break
            col, op, val = t.get("column"), t.get("op"), t.get("value")
            if col not in ALLOWED or op not in OPS:
                break
            if not isinstance(val, (int, float, str)) or isinstance(val, bool):
                break
            # A STRING VALUE ONLY MEANS ANYTHING TO == AND !=. Column-to-column
            # comparison is unsupported, and a proposal that tries it is
            # REFUSED where it can be seen, not disguised as untested.
            if isinstance(val, str) and op not in ("==", "!="):
                break
            terms.append((col, op, val))
        if len(terms) != len(where):
            continue
        out.append({"name": str(h.get("name") or "unnamed")[:60],
                    "why": str(h.get("why") or "")[:200], "terms": terms})
    return out


def matches(row: dict, terms) -> bool:
    for col, op, val in terms:
        got = row.get(col)
        if got is None:
            return False
        if isinstance(val, str) or isinstance(got, str):
            a, b = str(got), str(val)
            if op == "==" and a != b:
                return False
            if op == "!=" and a == b:
                return False
            if op not in ("==", "!="):
                return False
            continue
        try:
            got = float(got)
            num = float(val)
        except (TypeError, ValueError):
            return False
        if op == ">" and not got > num:
            return False
        if op == ">=" and not got >= num:
            return False
        if op == "<" and not got < num:
            return False
        if op == "<=" and not got <= num:
            return False
        if op == "==" and got != num:
            return False
        if op == "!=" and got == num:
            return False
    return True


def load(db_path: str | Path) -> list:
    """One row per alerted window: the decision as sent, plus its outcome.
    Read-only."""
    path = Path(db_path)
    if not path.exists():
        return []
    con = sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True, timeout=5)
    con.row_factory = sqlite3.Row
    try:
        have = {x[1] for x in con.execute("PRAGMA table_info(observations)")}
        cols = sorted(ALLOWED & have) + ["window_open", "observed_ms", "won"]
        rows, seen = [], set()
        for r in con.execute(
                f"SELECT {','.join(cols)} FROM observations "
                " WHERE alerted = 1 AND won IS NOT NULL AND our_ask IS NOT NULL "
                " ORDER BY window_open, observed_ms"):
            w = int(r["window_open"])
            if w in seen:
                continue
            seen.add(w)
            d = dict(r)
            d["day"] = datetime.fromtimestamp(w / 1000, timezone.utc).date()
            d["residual"] = float(r["won"]) - float(r["our_ask"])
            rows.append(d)
    finally:
        con.close()
    return rows


def boot(vals, draws=3000, seed=11):
    if not vals:
        return (0.0, 0.0, 0.0, 1.0)
    byday = defaultdict(list)
    for d, v in vals:
        byday[d].append(v)
    days = list(byday)
    rng = random.Random(seed)
    means = []
    for _ in range(draws):
        pool = [v for d in (rng.choice(days) for _ in days) for v in byday[d]]
        if pool:
            means.append(statistics.fmean(pool))
    means.sort()
    obs = statistics.fmean([v for _, v in vals])
    hits = sum(1 for m in means
               if (m - obs) * (1 if obs > 0 else -1) <= -abs(obs))
    p = max((2.0 * hits) / max(len(means), 1), 1.0 / max(len(means), 1))
    return (obs, means[int(0.025 * len(means))],
            means[int(0.975 * len(means))], p)


def sign_flip_p(diffs) -> float:
    """Two-sided p that the mean day-difference is zero: flip each day's sign.

    THE DAY IS THE UNIT, and with D days no test can honestly claim more than
    one pattern in 2^(D-1). The day-clustered bootstrap claimed 1/3000
    whenever every day agreed in sign - which chance alone does a quarter of
    the time at 3 days - so a random filter "survived" in 27-98% of runs
    (FINDINGS 107). Exact over every sign pattern up to 14 days; sampled
    beyond. At 3 days the smallest p is 0.25, at 6 days 0.031, at 8 days
    0.0078: until there are enough days, nothing survives, which is true.
    """
    vals = [v for _, v in diffs]
    n = len(vals)
    if not n:
        return 1.0
    obs = abs(sum(vals)) - 1e-12
    if n <= 14:
        hits = 0
        for mask in range(1 << n):
            total = 0.0
            for i, v in enumerate(vals):
                total += v if (mask >> i) & 1 else -v
            hits += abs(total) >= obs
        return hits / (1 << n)
    rng = random.Random(13)
    draws = 20000
    hits = sum(
        abs(sum(v if rng.random() < 0.5 else -v for v in vals)) >= obs
        for _ in range(draws))
    return (hits + 1) / (draws + 1)


def benjamini_hochberg(pvalues: dict, alpha: float = 0.05) -> dict:
    ordered = sorted(pvalues.items(), key=lambda kv: kv[1])
    k = len(ordered)
    if not k:
        return {}
    cutoff = 0
    for rank, (_k, p) in enumerate(ordered, start=1):
        if p <= alpha * rank / k:
            cutoff = rank
    return {key: (rank <= cutoff)
            for rank, (key, _p) in enumerate(ordered, start=1)}


def summarise(rows: list) -> dict:
    """Finished numbers for the model to reason ABOUT. It computes none."""
    def slice_of(pred):
        v = [r for r in rows if pred(r)]
        if len(v) < 15:
            return None
        return {"n": len(v),
                "win_rate": round(statistics.fmean(
                    float(r["won"]) for r in v), 3),
                "mean_ask": round(statistics.fmean(
                    float(r["our_ask"]) for r in v), 3),
                "residual": round(statistics.fmean(
                    r["residual"] for r in v), 4)}
    out = {"overall": slice_of(lambda r: True), "by": {}}
    for col in ("session", "vol_regime", "side", "weekday"):
        if col not in rows[0]:
            continue
        vals = {str(r.get(col)) for r in rows if r.get(col) is not None}
        got = {}
        for v in sorted(vals)[:8]:
            s = slice_of(lambda r, c=col, x=v: str(r.get(c)) == x)
            if s:
                got[v] = s
        if got:
            out["by"][col] = got
    for col in ("spread_bps", "book_yes_share", "trade_count", "book_age_s",
                "volatility_5m_bps", "open_interest", "remaining_s"):
        vals = sorted(float(r[col]) for r in rows
                      if r.get(col) is not None)
        if len(vals) < 60:
            continue
        med = vals[len(vals) // 2]
        lo = slice_of(lambda r, c=col, m=med: r.get(c) is not None
                      and float(r[c]) <= m)
        hi = slice_of(lambda r, c=col, m=med: r.get(c) is not None
                      and float(r[c]) > m)
        if lo and hi:
            out["by"][col] = {f"<= {round(med, 3)}": lo,
                              f"> {round(med, 3)}": hi}
    return out


def score(rows: list, proposals: list, *, min_n: int = 25,
          min_days: int = 3) -> list:
    """Every proposal measured, day-clustered, BH across the batch."""
    scored = []
    for h in proposals:
        sel = [r for r in rows if matches(r, h["terms"])]
        ndays = len({r["day"] for r in sel})
        if len(sel) < min_n or ndays < min_days:
            scored.append({**h, "n": len(sel), "days": ndays,
                           "testable": False, "untestable": "selects too few"})
            continue
        # THE QUANTITY IS THE DIFFERENCE, not the slice's own residual: a
        # slice that merely reproduces the baseline has found nothing.
        inside = [(r["day"], r["residual"]) for r in sel]
        outside = [(r["day"], r["residual"]) for r in rows
                   if not matches(r, h["terms"])]
        if len(outside) < min_n:
            scored.append({**h, "n": len(sel), "days": ndays,
                           "testable": False,
                           "untestable": "selects nearly everything"})
            continue
        mu_in, _, _, _ = boot(inside)
        mu_out, _, _, _ = boot(outside)
        byday = defaultdict(lambda: [[], []])
        for d, v in inside:
            byday[d][0].append(v)
        for d, v in outside:
            byday[d][1].append(v)
        diffs = [(d, statistics.fmean(a) - statistics.fmean(b))
                 for d, (a, b) in byday.items() if a and b]
        dmu, dlo, dhi, _boot_p = boot(diffs)
        p = sign_flip_p(diffs)
        scored.append({**h, "n": len(sel), "days": ndays, "testable": True,
                       "inside": mu_in, "outside": mu_out,
                       "diff": dmu, "lo": dlo, "hi": dhi, "p": p})
    # KEYED BY POSITION, never by name: the model repeats names (it names a
    # proposal after its column), and a shared key gave two proposals one
    # verdict - a p=0.69 proposal was reported as a survivor.
    survives = benjamini_hochberg(
        {i: s["p"] for i, s in enumerate(scored) if s["testable"]})
    for i, s in enumerate(scored):
        s["survives"] = bool(survives.get(i)) if s["testable"] else False
    return scored


def run(db_path: str | Path, asset: str, *, url: str, model: str = "local",
        timeout: float = 600.0, min_n: int = 25, min_days: int = 3,
        offline: bool = False, ask=ask_model) -> dict:
    """One full propose-and-test cycle. Never raises; the result says what
    happened, including when the model could not be reached."""
    out = {"asset": asset, "windows": 0, "days": 0, "model_ok": False,
           "model_error": "", "model_note": "", "skipped": "", "proposed": 0,
           "scored": [], "survivors": []}
    try:
        rows = load(db_path)
    except sqlite3.Error as exc:
        out["model_error"] = f"store unreadable: {exc}"
        return out
    out["windows"] = len(rows)
    out["days"] = len({r["day"] for r in rows})
    if len(rows) < 60:
        out["skipped"] = f"only {len(rows)} alerted windows; too few to test"
        out["model_error"] = out["skipped"]
        return out
    proposals = list(BASELINE)
    if not offline:
        try:
            got, note = parse_reply(ask(url, model,
                                        prompt_for(asset, summarise(rows)),
                                        timeout))
            out["model_note"] = note
            if note and not got:
                out["model_error"] = note
            else:
                out["model_ok"] = True
            seen = {tuple(map(tuple, h["terms"])) for h in proposals}
            fresh = []
            for h in got:
                key = tuple(map(tuple, h["terms"]))
                if key not in seen:
                    seen.add(key)
                    fresh.append(h)
            out["proposed"] = len(fresh)
            proposals += fresh
        except Exception as exc:  # noqa: BLE001 - the controls still run
            out["model_error"] = f"{type(exc).__name__}: {str(exc)[:120]}"
    out["scored"] = score(rows, proposals, min_n=min_n, min_days=min_days)
    out["survivors"] = [s for s in out["scored"]
                        if s.get("survives") and s.get("why") != "control"]
    return out
