"""Performance dashboard for the live signal record.

Reads the service's own database - every alert it sent and how each settled -
and writes a self-contained HTML page plus a terminal summary. No server, no
network, no external assets: open the file, or re-run to refresh it.

The point is to answer, at a glance, the question that decides automation:
*is the live record behaving like the backtest?* So every panel pairs what
happened with how uncertain it still is, and the sample-size warning stays up
until the record is large enough to mean anything.
"""

import argparse
import json
import math
import sqlite3
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

from .store import ACCOUNTED_SQL, position_pnl
from .validation import contracts_for_budget, kalshi_fee_charged, wilson_lower

# Diverging pair: blue for gains, red for losses, validated for colour-vision
# deficiency in both modes. Green/red - the obvious choice for money - fails:
# deutan separation between them is dE 4.1, far under the 8 floor.
POSITIVE = "#2a78d6"
NEGATIVE = "#e34948"


def load(db_path: str, stake: float) -> dict:
    db = sqlite3.connect(db_path)
    columns = {row[1] for row in db.execute("PRAGMA table_info(predictions)")}
    price = "contract_price" if "contract_price" in columns else "NULL"
    qualified = "qualified" if "qualified" in columns else "0"
    rows = db.execute(
        f"SELECT window_open, side, contract_ticker, {price}, {qualified}, won, "
        "raw_probability, target, entry_price "
        "FROM predictions WHERE won IS NOT NULL ORDER BY window_open"
    ).fetchall()

    # What was actually traded, keyed by window. Telegram and this page must
    # not price the same rows two different ways; they previously disagreed
    # ($10 of max payout vs $10 of cash) and neither matched the account.
    traded = {}
    try:
        for row in db.execute(
            "SELECT window_open, status, count, COALESCE(fill_price, entry_limit), "
            "fee_paid, exit_price, exit_count FROM trade_proposals "
            f"WHERE strategy='primary' AND status IN {ACCOUNTED_SQL}"
        ):
            traded[row[0]] = row[1:]
    except sqlite3.OperationalError:
        pass  # an older database without the execution columns

    signals = []
    equity = real_equity = 0.0
    for open_ms, side, ticker, contract_price, is_qualified, won, raw, target, spot in rows:
        pnl = None
        if contract_price:
            # Size it the way the bot sizes an order, so this paper figure is
            # comparable with the account. Kalshi does support fractional
            # contracts - a real fill of 13.5 exists in this account's history -
            # but contracts_for_budget rounds down to whole ones, so $10/0.98
            # buys 10, not 10.2. Pricing the paper row at 10.2 would describe
            # an order this system would never place.
            contracts = contracts_for_budget(stake, contract_price)
            gross = contracts * ((1.0 if won else 0.0) - contract_price)
            pnl = round(gross - kalshi_fee_charged(contract_price, contracts), 4)
            equity += pnl

        # The account's own P&L, through the same function the daily loss floor
        # and the Telegram record use. Three private copies disagreed about the
        # same trade, and a partially-sold position fell through all of them.
        real = None
        real_paid = real_size = None
        order = traded.get(open_ms)
        if order:
            _status, count, paid, fee, exit_price, exit_count = order
            real_paid, real_size = paid, count
            pnl_real = position_pnl(
                paid=paid,
                count=count,
                entry_fee=fee,
                exit_price=exit_price,
                exit_count=exit_count,
                won=bool(won),
            )
            if pnl_real is not None:
                real = round(pnl_real, 4)
                real_equity += real
        signals.append(
            {
                "open_ms": open_ms,
                "time": datetime.fromtimestamp(open_ms / 1000, UTC).strftime("%Y-%m-%d %H:%M"),
                "day": datetime.fromtimestamp(open_ms / 1000, UTC).strftime("%Y-%m-%d"),
                "side": side,
                "ticker": ticker or "",
                "price": contract_price,
                "qualified": bool(is_qualified),
                "won": bool(won),
                "model": raw,
                "target": target,
                "spot": spot,
                "pnl": pnl,
                "real": real,
                "paid": real_paid,
                "size": real_size,
                "traded": order is not None,
                "equity": round(equity, 4),
                "real_equity": round(real_equity, 4),
            }
        )

    pending = db.execute(
        "SELECT COUNT(*) FROM predictions WHERE won IS NULL"
    ).fetchone()[0]
    # The broker's own account record, when the mirror has been synced. Read
    # defensively: an older database has no `settlements` table at all.
    try:
        broker = db.execute(
            "SELECT COUNT(*), SUM(pnl > 0), COALESCE(SUM(pnl), 0) FROM settlements"
        ).fetchone()
    except sqlite3.OperationalError:
        broker = None
    return {
        "signals": signals, "pending": pending, "stake": stake, "broker": broker,
    }


def summarise(data: dict) -> dict:
    signals = data["signals"]
    scored = [s for s in signals if s["pnl"] is not None]
    # The signal record covers every settled signal; the P&L can only cover the
    # ones that carry a price. Those sets differ, so the difference is reported
    # rather than left implicit - pairing a 16-signal win rate with a 9-signal
    # P&L read as 16 trades staking $90.
    wins = sum(1 for s in signals if s["won"])
    total = len(signals)
    unscored = total - len(scored)
    net = round(sum(s["pnl"] for s in scored), 2)
    staked = round(data["stake"] * len(scored), 2)

    # The account, as distinct from the paper record above.
    #
    # PREFER THE BROKER. The per-signal reconstruction below is kept because it
    # is what draws the per-day chart, but its TOTAL must not be allowed to
    # disagree with the one Telegram prints: two surfaces quoting different P&L
    # for the same account is exactly the failure this whole path was rewritten
    # to end. Where the settlements mirror has rows, it wins.
    real_rows = [s for s in signals if s["real"] is not None]
    real_net = round(sum(s["real"] for s in real_rows), 4)
    real_wins = sum(1 for s in real_rows if s["real"] > 0)
    broker = data.get("broker")
    if broker and broker[0]:
        _markets, real_wins, real_net = broker[0], broker[1], round(broker[2], 4)
    # Cost is price x contracts. Summing the price alone reported $0.84 spent
    # on a ten-contract position that cost $8.40, and made the ROI ten times too
    # large in the denominator's favour.
    real_staked = round(sum((s["paid"] or 0.0) * (s["size"] or 0.0) for s in real_rows), 4)

    daily: dict[str, float] = defaultdict(float)
    for s in scored:
        daily[s["day"]] += s["pnl"]

    peak = drawdown = running = 0.0
    for s in scored:
        running += s["pnl"]
        peak = max(peak, running)
        drawdown = max(drawdown, peak - running)

    def slice_stats(key, value):
        subset = [s for s in signals if s[key] == value]
        scored_subset = [s for s in subset if s["pnl"] is not None]
        return {
            "n": len(subset),
            "wins": sum(1 for s in subset if s["won"]),
            "win_rate": (sum(1 for s in subset if s["won"]) / len(subset)) if subset else 0.0,
            "net": round(sum(s["pnl"] for s in scored_subset), 2),
            # The money covers only the priced rows, so the count it is divided
            # by is reported separately from the record's count.
            "priced": len(scored_subset),
        }

    bands = {}
    # Labels state the range they actually filter on. "0.85-0.99" over a
    # 0.85 <= p < 1.0 filter attributed 99c+ trades to a band that excluded them.
    for label, low, high in (("0.85-1.00", 0.85, 1.0), ("0.50-0.85", 0.50, 0.85),
                             ("below 0.50", 0.0, 0.50)):
        subset = [s for s in scored if s["price"] and low <= s["price"] < high]
        if subset:
            bands[label] = {
                "n": len(subset),
                "wins": sum(1 for s in subset if s["won"]),
                "win_rate": sum(1 for s in subset if s["won"]) / len(subset),
                "avg_price": round(sum(s["price"] for s in subset) / len(subset), 4),
                "net": round(sum(s["pnl"] for s in subset), 2),
            }

    # How many settled signals before a +1% edge could be told from zero.
    avg_price = (sum(s["price"] for s in scored) / len(scored)) if scored else None
    # For the sample-size guide, fall back to the band the strategy targets.
    reference = avg_price if avg_price else 0.85
    variance = reference * (1 - reference)
    needed = int((1.96 * math.sqrt(variance) / 0.01) ** 2) if variance else 0

    return {
        "total": total,
        "unscored": unscored,
        "wins": wins,
        "losses": total - wins,
        "win_rate": (wins / total) if total else 0.0,
        "win_rate_lower": wilson_lower(wins, total),
        "scored": len(scored),
        "net": net,
        "staked": staked,
        "roi": (net / staked) if staked else 0.0,
        # The account, kept separate from the paper figures above: these are
        # orders that actually filled, at the size filled and the fee charged.
        "real_trades": len(real_rows),
        "real_wins": real_wins,
        "real_net": real_net,
        "real_staked": real_staked,
        "real_roi": (real_net / real_staked) if real_staked else 0.0,
        "avg_price": round(avg_price, 4) if avg_price else None,
        "max_drawdown": round(drawdown, 2),
        "pending": data["pending"],
        "daily": dict(sorted(daily.items())),
        "positive_days": sum(1 for v in daily.values() if v > 0),
        "days": len(daily),
        "by_side": {side: slice_stats("side", side) for side in ("UP", "DOWN")},
        "by_qualified": {
            "rule qualified": slice_stats("qualified", True),
            "paper only": slice_stats("qualified", False),
        },
        "by_band": bands,
        "trades_needed": needed,
        "stake": data["stake"],
    }


def _sparkline(points: list[float], width: int = 640, height: int = 160) -> str:
    """Equity curve as inline SVG, zero-line anchored, no external library."""
    if len(points) < 2:
        return '<p class="empty">Not enough settled signals yet to draw a curve.</p>'
    low, high = min(min(points), 0.0), max(max(points), 0.0)
    span = (high - low) or 1.0
    pad = 8

    def x(index):
        return pad + index * (width - 2 * pad) / (len(points) - 1)

    def y(value):
        return height - pad - (value - low) / span * (height - 2 * pad)

    line = " ".join(f"{x(i):.1f},{y(v):.1f}" for i, v in enumerate(points))
    zero = y(0.0)
    ends = points[-1]
    colour = POSITIVE if ends >= 0 else NEGATIVE
    return f"""<svg viewBox="0 0 {width} {height}" role="img"
   aria-label="Cumulative profit and loss across {len(points)} settled signals"
   preserveAspectRatio="none" class="spark">
  <line x1="{pad}" x2="{width - pad}" y1="{zero:.1f}" y2="{zero:.1f}" class="zero"/>
  <polyline points="{line}" fill="none" stroke="{colour}" stroke-width="2"
     stroke-linejoin="round" stroke-linecap="round"/>
</svg>"""


def _day_bars(daily: dict[str, float], width: int = 640, height: int = 140) -> str:
    if not daily:
        return '<p class="empty">No settled days yet.</p>'
    items = list(daily.items())
    top = max((abs(v) for _, v in items), default=1.0) or 1.0
    pad = 8
    slot = (width - 2 * pad) / len(items)
    bar = max(2.0, min(28.0, slot - 2))  # 2px surface gap between bars
    mid = height / 2
    marks = []
    for index, (day, value) in enumerate(items):
        centre = pad + slot * (index + 0.5)
        tall = abs(value) / top * (mid - pad)
        top_y = mid - tall if value >= 0 else mid
        colour = POSITIVE if value >= 0 else NEGATIVE
        marks.append(
            f'<rect x="{centre - bar / 2:.1f}" y="{top_y:.1f}" width="{bar:.1f}" '
            f'height="{max(tall, 1):.1f}" rx="4" fill="{colour}">'
            f"<title>{day}: {value:+.2f}</title></rect>"
        )
    return f"""<svg viewBox="0 0 {width} {height}" role="img"
   aria-label="Profit and loss for each of {len(items)} settled days" class="bars">
  <line x1="{pad}" x2="{width - pad}" y1="{mid}" y2="{mid}" class="zero"/>
  {"".join(marks)}
</svg>"""


def render(stats: dict, signals: list[dict]) -> str:
    money = lambda v: f"{v:+,.2f}"  # noqa: E731
    thin = stats["total"] < 100
    equity = [s["equity"] for s in signals if s["pnl"] is not None]
    avg_entry = (
        f"avg entry {stats['avg_price']:.2f}" if stats["scored"] else "no priced signals yet"
    )
    recent = list(reversed(signals[-25:]))
    untraded_n = stats["unscored"] + stats["scored"] - stats["real_trades"]
    real_note = "no orders filled yet" if not stats["real_trades"] else "realised, after fees"
    unscored_note = (
        f" · {stats['unscored']} unpriced, excluded from P&amp;L" if stats["unscored"] else ""
    )
    record_note = (
        f"{stats['win_rate']:.0%} · floor {stats['win_rate_lower']:.0%}{unscored_note}"
    )

    warning = ""
    if thin:
        warning = f"""<div class="warn">
  <strong>{stats['total']} settled signals.</strong> At an average entry of
  {stats['avg_price'] or 0.85:.2f} a fair market wins about
  {(stats['avg_price'] or 0.85) * 100:.0f}% of the time anyway, so a streak here
  proves nothing. Roughly <strong>{stats['trades_needed']:,}</strong> settled
  signals are needed before a 1%-per-trade edge separates from zero.
</div>"""

    def table(title, rows, first):
        body = "".join(
            f"<tr><td>{name}</td><td class='num'>{d['n']}</td>"
            f"<td class='num'>{d['wins']}</td>"
            f"<td class='num'>{d['win_rate']:.0%}</td>"
            f"<td class='num {'pos' if d['net'] >= 0 else 'neg'}'>{money(d['net'])}</td></tr>"
            for name, d in rows.items()
            if d["n"]
        )
        if not body:
            return ""
        return f"""<section><h2>{title}</h2><table>
<thead><tr><th>{first}</th><th class="num">n</th><th class="num">wins</th>
<th class="num">win rate</th><th class="num">net</th></tr></thead>
<tbody>{body}</tbody></table></section>"""

    def signal_row(s: dict) -> str:
        entry = f"{s['price']:.0%}" if s["price"] else "&mdash;"
        # An unknown P&L gets no colour. Painting a missing value in the gain
        # hue reads as a small win at a glance, which is worse than blank.
        if s["pnl"] is None:
            pnl, tone = "&mdash;", "dim"
        else:
            pnl = money(s["pnl"])
            tone = "pos" if s["pnl"] >= 0 else "neg"
        source = "rule" if s["qualified"] else "paper"
        outcome = "WIN" if s["won"] else "LOSS"
        return (
            f"<tr><td>{s['time']}</td><td>{s['side']}</td>"
            f"<td class='num'>{entry}</td><td>{outcome}</td>"
            f"<td class='num {tone}'>{pnl}</td><td>{source}</td></tr>"
        )

    rows = "".join(signal_row(s) for s in recent)

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>BTC15 Performance</title>
<style>
  :root {{
    color-scheme: light;
    --surface: #fcfcfb; --panel: #ffffff; --line: #e4e3df;
    --ink: #0b0b0b; --ink-2: #52514e; --ink-3: #7a7873;
    --pos: {POSITIVE}; --neg: {NEGATIVE};
  }}
  @media (prefers-color-scheme: dark) {{
    :root:not([data-theme="light"]) {{
      color-scheme: dark;
      --surface: #1a1a19; --panel: #232322; --line: #383835;
      --ink: #ffffff; --ink-2: #c3c2b7; --ink-3: #8f8e86;
      --pos: #3987e5; --neg: #e66767;
    }}
  }}
  :root[data-theme="dark"] {{
    color-scheme: dark;
    --surface: #1a1a19; --panel: #232322; --line: #383835;
    --ink: #ffffff; --ink-2: #c3c2b7; --ink-3: #8f8e86;
    --pos: #3987e5; --neg: #e66767;
  }}
  * {{ box-sizing: border-box; }}
  body {{ margin: 0; background: var(--surface); color: var(--ink);
    font: 14px/1.5 ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif;
    padding: 24px 16px 48px; }}
  .wrap {{ max-width: 860px; margin: 0 auto; }}
  h1 {{ font-size: 20px; margin: 0 0 2px; letter-spacing: -0.01em; }}
  .sub {{ color: var(--ink-3); margin: 0 0 20px; font-size: 13px; }}
  h2 {{ font-size: 13px; text-transform: uppercase; letter-spacing: 0.06em;
    color: var(--ink-3); margin: 0 0 10px; font-weight: 600; }}
  section {{ background: var(--panel); border: 1px solid var(--line);
    border-radius: 12px; padding: 16px; margin-bottom: 14px; }}
  .tiles {{ display: grid; gap: 10px; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
    margin-bottom: 14px; }}
  .tile {{ background: var(--panel); border: 1px solid var(--line);
    border-radius: 12px; padding: 14px 16px; }}
  .tile .k {{ font-size: 12px; color: var(--ink-3); margin-bottom: 4px; }}
  .tile .v {{ font-size: 26px; font-weight: 600; letter-spacing: -0.02em;
    font-variant-numeric: tabular-nums; }}
  .tile .n {{ font-size: 12px; color: var(--ink-3); margin-top: 2px; }}
  .pos {{ color: var(--pos); }} .neg {{ color: var(--neg); }}
  .dim {{ color: var(--ink-3); }}
  .warn {{ border: 1px solid var(--line); border-left: 3px solid var(--neg);
    background: var(--panel); border-radius: 8px; padding: 12px 14px;
    color: var(--ink-2); font-size: 13px; margin-bottom: 14px; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 13px;
    font-variant-numeric: tabular-nums; }}
  th {{ text-align: left; color: var(--ink-3); font-weight: 600; font-size: 12px;
    padding: 6px 8px; border-bottom: 1px solid var(--line); }}
  td {{ padding: 6px 8px; border-bottom: 1px solid var(--line); color: var(--ink-2); }}
  tbody tr:last-child td {{ border-bottom: 0; }}
  .num {{ text-align: right; }}
  svg {{ width: 100%; height: auto; display: block; }}
  .zero {{ stroke: var(--line); stroke-width: 1; }}
  .empty {{ color: var(--ink-3); font-size: 13px; margin: 0; }}
  .scroll {{ overflow-x: auto; }}
  footer {{ color: var(--ink-3); font-size: 12px; margin-top: 18px; }}
</style></head><body>
<div class="wrap">
  <h1>BTC15 Performance</h1>
  <p class="sub">Generated {datetime.now(UTC).strftime('%Y-%m-%d %H:%M')} UTC</p>

  {warning}

  <h2>Account</h2>
  <p class="sub">Orders that actually filled — the size filled, the price paid,
     the fee charged.</p>
  <div class="tiles">
    <div class="tile"><div class="k">Realised P&amp;L</div>
      <div class="v {'pos' if stats['real_net'] >= 0 else 'neg'}">{money(stats['real_net'])}</div>
      <div class="n">on ${stats['real_staked']:,.2f} actually spent</div></div>
    <div class="tile"><div class="k">Trades placed</div>
      <div class="v">{stats['real_wins']}&hairsp;/&hairsp;{stats['real_trades']}</div>
      <div class="n">{untraded_n} signals not traded</div></div>
    <div class="tile"><div class="k">Return on spend</div>
      <div class="v {'pos' if stats['real_roi'] >= 0 else 'neg'}">{stats['real_roi']:+.2%}</div>
      <div class="n">{real_note}</div></div>
  </div>

  <h2>Signals (paper)</h2>
  <p class="sub">Every settled signal priced at ${stats['stake']:,.2f} per signal,
     whether or not an order was placed. Not money.</p>
  <div class="tiles">
    <div class="tile"><div class="k">Paper P&amp;L</div>
      <div class="v {'pos' if stats['net'] >= 0 else 'neg'}">{money(stats['net'])}</div>
      <div class="n">on ${stats['staked']:,.2f} notional · {stats['scored']} priced</div></div>
    <div class="tile"><div class="k">Record</div>
      <div class="v">{stats['wins']}&hairsp;/&hairsp;{stats['total']}</div>
      <div class="n">{record_note}</div></div>
    <div class="tile"><div class="k">Return on notional</div>
      <div class="v {'pos' if stats['roi'] >= 0 else 'neg'}">{stats['roi']:+.2%}</div>
      <div class="n">{avg_entry}</div></div>
    <div class="tile"><div class="k">Max drawdown</div>
      <div class="v">${stats['max_drawdown']:,.2f}</div>
      <div class="n">paper, at ${stats['stake']:,.2f}/signal</div></div>
  </div>

  <section><h2>Cumulative P&amp;L</h2>{_sparkline(equity)}</section>
  <section><h2>P&amp;L by day</h2>{_day_bars(stats['daily'])}</section>

  {table("By side", stats["by_side"], "side")}
  {table("By entry price", stats["by_band"], "band")}
  {table("Rule qualified vs paper", stats["by_qualified"], "source")}

  <section><h2>Recent settled signals</h2><div class="scroll"><table>
  <thead><tr><th>window (UTC)</th><th>side</th><th class="num">entry</th>
  <th>result</th><th class="num">P&amp;L</th><th>source</th></tr></thead>
  <tbody>{rows or '<tr><td colspan="6">No settled signals yet.</td></tr>'}</tbody>
  </table></div></section>

  <footer>{stats['pending']} signal(s) awaiting settlement.
  P&amp;L assumes ${stats['stake']:.0f} per signal at the quoted ask, net of Kalshi's
  per-order fee. Win/loss is stated in text, never by colour alone.</footer>
</div></body></html>
"""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Render the BTC15 performance dashboard")
    parser.add_argument("--db", default="btc15.db")
    parser.add_argument("--stake", type=float, default=1.0)
    parser.add_argument("--output", default="reports/dashboard.html")
    parser.add_argument("--json", default=None, help="also write the stats as JSON")
    return parser.parse_args()


def run() -> None:
    args = parse_args()
    if not Path(args.db).exists():
        raise SystemExit(f"no database at {args.db}")
    data = load(args.db, args.stake)
    stats = summarise(data)

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(render(stats, data["signals"]), encoding="utf-8")
    if args.json:
        Path(args.json).write_text(json.dumps(stats, indent=2, default=str) + "\n")

    print("\nBTC15 PERFORMANCE")
    print("  ACCOUNT (orders that actually filled)")
    if stats["real_trades"]:
        print(f"    realised   {stats['real_net']:+,.4f} on "
              f"{stats['real_staked']:,.2f} spent ({stats['real_roi']:+.2%})")
        print(f"    trades     {stats['real_wins']}/{stats['real_trades']}")
    else:
        print("    no orders have filled yet")
    print(f"\n  SIGNALS (paper, ${args.stake:,.2f} per signal - not money)")
    print(f"    settled    {stats['total']}  ({stats['pending']} pending, "
          f"{stats['scored']} priced)")
    print(f"    record     {stats['wins']}/{stats['total']} = {stats['win_rate']:.1%}"
          f"   95% floor {stats['win_rate_lower']:.1%}")
    if stats["scored"]:
        print(f"    paper P&L  {stats['net']:+,.2f} on {stats['staked']:,.2f} notional "
              f"({stats['roi']:+.2%})")
        print(f"    drawdown   {stats['max_drawdown']:,.2f}")
    if stats["total"] < 100:
        print(f"  NOTE: {stats['total']} signals proves nothing; "
              f"~{stats['trades_needed']:,} needed to detect a 1%/trade edge.")
    print(f"\n  {output}")


if __name__ == "__main__":
    run()
