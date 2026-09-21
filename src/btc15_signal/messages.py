"""Telegram message composition.

Telegram has no text colour, so colour is carried by emoji chips - a green
circle for a live entry, red for an exit, white for a pass - and structure by
its HTML subset (bold, monospace). Every message opens with the same scoreboard
line so the running win rate is visible without scrolling back.

Kept apart from main.py so the wording and layout can be tested without a
network client or an event loop.
"""

from html import escape

BAR_FULL = "▰"  # ▰
BAR_EMPTY = "▱"  # ▱
RULE = "—" * 16


def bar(fraction: float, width: int = 10) -> str:
    filled = max(0, min(width, round(fraction * width)))
    return BAR_FULL * filled + BAR_EMPTY * (width - filled)


def scoreboard(
    settled: int,
    wins: int,
    net_dollars: float,
    basis: str = "per contract",
    live: tuple[int, int, float] | None = None,
) -> str:
    """The running record, pinned to the top of every message.

    Two records, never merged into one number, because they answer different
    questions and only one of them is money:

    * **Signals** - was the call right? Counts every settled signal, including
      the great majority nobody ever placed an order for. Its dollar figure is
      explicitly a per-contract paper figure.
    * **Live** - what did the account actually do? Real fills, real sizes, real
      fees, from `Store.realised_record`.

    Merging them is what produced a reported -$3.91 on a night whose real loss
    was $0.85: nine signals, eight of them never traded, priced at ten contracts
    each while the account had bought one.

    The win-rate bar stays on the signal line because a bare percentage over a
    handful of trades reads as far more solid than it is, and the sample count
    belongs beside it.
    """
    if not settled:
        head = "\U0001f4ca <b>No settled signals yet</b>"
        # Real money is still reported. Returning early here dropped the Live
        # line whenever nothing had settled yet - precisely the state in which a
        # first real trade is the only thing worth showing.
        if live and live[0]:
            trades, live_wins, dollars = live
            chip = "\U0001f4b0" if dollars >= 0 else "\U0001f4b8"
            head += (
                f"\n{chip} <b>Live: {dollars:+,.2f}</b> · {trades} trade"
                f"{'s' if trades != 1 else ''} ({live_wins}W-{trades - live_wins}L)"
            )
        return head
    rate = wins / settled
    lines = [
        f"\U0001f3af <b>{rate:.0%} win rate</b> · {wins}W-{settled - wins}L "
        f"· <i>{settled} signals</i>",
        f"<code>{bar(rate)}</code> <i>paper {net_dollars:+,.2f} on {escape(basis)}</i>",
    ]
    if live is None:
        return "\n".join(lines)
    trades, live_wins, dollars = live
    if not trades:
        lines.append("\U0001f4b0 <b>Live: no trades placed yet</b>")
    else:
        chip = "\U0001f4b0" if dollars >= 0 else "\U0001f4b8"
        lines.append(
            f"{chip} <b>Live: {dollars:+,.2f}</b> · {trades} trade"
            f"{'s' if trades != 1 else ''} ({live_wins}W-{trades - live_wins}L)"
        )
    return "\n".join(lines)


def _distance_bps(price: float, target: float) -> str:
    if not target:
        return ""
    bps = (price / target - 1) * 10_000
    return f"{bps:+.0f} bps"


def entry_alert(
    *,
    head: str,
    live: bool,
    side: str,
    ask: float,
    price: float,
    target: float,
    remaining: int,
    ticker: str,
    model: float,
    observed: float,
    samples: int,
    note: str = "",
    rule_ok: bool = True,
    rule_reason: str = "",
    missing: str = "",
    checks: list[tuple[str, bool, str]] | None = None,
) -> str:
    """An entry the operator can act on.

    The rule's verdict is shown but does not decide: a setup the rule rejects
    still gets a button, marked as an override, because the point of the manual
    stage is being able to take trades automation would not.
    """
    if live:
        chip, title = "\U0001f7e2", "ENTRY READY"
    elif rule_ok:
        chip, title = "\U0001f535", "MANUAL ENTRY"
    else:
        chip, title = "\U0001f7e0", "MANUAL — RULE SAYS NO"
    arrow = "\U0001f4c8" if side == "UP" else "\U0001f4c9"
    lines = [
        head,
        RULE,
        f"{chip} <b>{title}</b> · {escape(ticker)}",
        f"{arrow} <b>BTC {side}</b> at <b>{ask:.0%}</b>",
        f"\U0001f3af Target <code>${target:,.2f}</code> · now "
        f"<code>${price:,.2f}</code> ({_distance_bps(price, target)})",
        f"⏱ <b>{remaining // 60}m {remaining % 60:02d}s</b> to settle",
        f"\U0001f9ee Model {model:.0%} · observed {observed:.0%} ({samples} samples)",
    ]
    if checks:
        # Every gate with its numbers. "rule says no: contract price band" hides
        # whether it missed by a cent or by thirty, and hides which gates passed.
        lines.append("<b>Checks</b>")
        for name, passed, detail in checks:
            lines.append(f"  {'✅' if passed else '❌'} {name}: <code>{escape(detail)}</code>")
    elif rule_ok:
        lines.append("✅ <i>Rule: qualifies</i>")
    else:
        lines.append(f"\U0001f6ab <i>Rule says no: {escape(rule_reason or 'disabled')}</i>")
    if not live:
        lines.append("\U0001f446 <i>Not auto-validated — your press is the decision.</i>")
    if missing:
        # Name only what is actually missing. Listing settings that are already
        # correct sends you to check things that are fine.
        lines.append(
            f"⚙️ <i>A press will be refused — still needed: {escape(missing)}</i>"
        )
    if note:
        lines.append(f"⚠️ <i>{escape(note)}</i>")
    return "\n".join(lines)


def no_entry_alert(
    *,
    head: str,
    side: str,
    ask: float,
    price: float,
    target: float,
    remaining: int,
    reason: str,
    model: float,
    observed: float,
    samples: int,
) -> str:
    arrow = "\U0001f4c8" if side == "UP" else "\U0001f4c9"
    return "\n".join(
        [
            head,
            RULE,
            "⚪ <b>NO ENTRY</b> · paper",
            f"{arrow} BTC {side} at {ask:.0%} · {remaining // 60}m left",
            f"\U0001f3af Target <code>${target:,.2f}</code> · now "
            f"<code>${price:,.2f}</code> ({_distance_bps(price, target)})",
            f"\U0001f6ab <i>{escape(reason)}</i>",
            f"\U0001f9ee Model {model:.0%} · observed {observed:.0%} ({samples} samples)",
        ]
    )


def reversion_alert(
    *,
    head: str,
    live: bool,
    side: str,
    entry: float,
    take_profit: float,
    key_level: float,
    spike_bps: float,
    rejection_bps: float,
    remaining: int,
    note: str = "",
) -> str:
    chip = "\U0001f7e2" if live else "\U0001f535"
    lines = [
        head,
        RULE,
        f"{chip} <b>SPIKE REVERSION</b> · {'live' if live else 'paper'}",
        f"\U0001f504 Buy <b>{side}</b> at <b>{entry:.0%}</b> "
        f"→ take profit <b>{take_profit:.0%}</b>",
        f"\U0001f4cd Key level <code>${key_level:,.2f}</code>",
        f"\U0001f4ca Spike {spike_bps:.1f} bps · rejection {rejection_bps:.1f} bps",
        f"⏱ <b>{remaining // 60}m {remaining % 60:02d}s</b> to settle",
    ]
    if note:
        lines.append(f"⚠️ <i>{escape(note)}</i>")
    lines.append(
        "\U0001f4a1 <i>The entry price is what the contract costs, "
        "not a win probability.</i>"
    )
    return "\n".join(lines)


def exit_alert(
    *, head: str, ticker: str, side: str, price: float, target: float, remaining: int, bid: float
) -> str:
    return "\n".join(
        [
            head,
            RULE,
            f"\U0001f534 <b>EXIT · REVERSAL</b> · {escape(ticker)}",
            f"\U0001f4c9 Held <b>{side}</b>, BTC crossed back through "
            f"<code>${target:,.2f}</code>",
            f"\U0001f4b8 Now <code>${price:,.2f}</code> · best exit near <b>{bid:.0%}</b>",
            f"⏱ {remaining // 60}m {remaining % 60:02d}s left",
            "⚠️ <i>Thesis broken: this was a bet it would not happen.</i>",
        ]
    )


def settlement(
    *,
    head: str,
    ticker: str,
    side: str,
    winner: str,
    won: bool,
    target: float,
    contract_price: float | None,
    pnl: float | None,
    qualified: bool,
    basis: str = "1 contract",
    contracts: float | None = None,
    exited_at: float | None = None,
    paper: bool = False,
    exact: bool = True,
) -> str:
    """How a window closed, and what it did to the account.

    The headline verdict describes the TRADE, not the market, and those come
    apart for a position sold early: a trade closed at a loss stays a loss even
    if the contract later settled our way. Reporting it as a win was a real
    defect - the daily loss floor books such a trade at its exit price, so the
    two disagreed in sign about the same money.

    A signal nobody traded gets no money line at all. `pnl` is None there,
    because nothing was spent and any figure would be a claim about money that
    never moved.
    """
    # The headline is about MONEY, and only when there was money. A signal
    # nobody traded used to headline "✅ WIN" directly above "No order was
    # placed" - the tick meant the call was right, the reader saw a payday.
    # Three separate facts, each now stated in its own words:
    #   what the market did · what we said · what it did to the account.
    right = "right" if won else "wrong"
    if paper:
        chip, verdict = "\U0001f4dd", "SIGNAL ONLY · NOT TRADED"
    elif exited_at is not None:
        made_money = pnl is not None and pnl >= 0
        chip = "\U0001f4b0" if made_money else "\U0001f4b8"
        verdict = f"SOLD EARLY · {'PROFIT' if made_money else 'LOSS'}"
    else:
        made_money = pnl is not None and pnl >= 0
        chip = "\U0001f4b0" if made_money else "\U0001f4b8"
        verdict = "PROFIT" if made_money else "LOSS"
    lines = [
        head,
        RULE,
        f"{chip} <b>{verdict}</b> · {escape(ticker)}",
        f"\U0001f3c1 Market settled <b>{winner}</b> · target <code>${target:,.2f}</code>",
    ]
    priced = f" at {contract_price:.0%}" if contract_price else ""
    mark = "✅" if won else "❌"
    lines.append(
        f"\U0001f4dd We said <b>{side}</b>{priced} · {mark} <b>{right}</b>"
    )
    if paper:
        why = "the rule liked it, but no order was placed" if qualified else "paper only"
        lines.append(f"\U0001f4a4 <i>Not traded - {why}.</i>")
    if exited_at is not None:
        # Said plainly, because the verdict above now describes the sale rather
        # than the settlement and the two can point opposite ways.
        outcome = "would have won" if won else "would have lost"
        lines.append(
            f"\U0001f504 <i>Sold at {exited_at:.0%} before expiry · "
            f"holding {outcome}</i>"
        )
    if pnl is not None:
        money = "\U0001f4b0" if pnl >= 0 else "\U0001f4b8"
        # Cost, not max payout, is the money at risk - on a loss it IS the loss.
        cost = (
            f" · cost ${contracts * contract_price:,.2f}"
            if contracts and contract_price
            else ""
        )
        lines.append(f"{money} <b>{pnl:+,.2f}</b> on {basis}{cost} after fees")
        if exited_at is not None:
            # The running total in the header does not move here, and that is
            # correct: this money was counted the moment the sale happened.
            # Without saying so, a second message carrying the same total reads
            # as a total that has stopped updating.
            lines.append(
                "🧾 <i>Already counted when it sold - this is the recap, "
                "not a second gain.</i>"
            )
        if not exact:
            # The fill was never read back, so this is priced at the posted
            # limit. Only the order confirmation said so before; the settlement
            # report presented the same estimate as settled fact.
            lines.append(
                "⚠️ <i>Priced at the posted limit · the fill was never "
                "confirmed, so the real cost was this or lower.</i>"
            )
    elif paper:
        lines.append("\U0001f4b5 <i>Nothing at risk, nothing made.</i>")
    return "\n".join(lines)


def order_result(
    *,
    status: str,
    side: str,
    count: float,
    note: str,
    order_id: str | None,
    price: float | None = None,
    limit: float | None = None,
    fee: float | None = None,
    exact: bool = True,
) -> str:
    """Confirmation for an order placed by pressing the button.

    Carries the money, which it previously did not: the only figures were a
    status and a contract count, so pressing Execute committed real cash and
    the reply never said how much.
    """
    chip = "✅" if status.lower() in {"filled", "ok", "success", "protected"} else "⚠️"
    lines = [
        f"{chip} <b>ORDER {escape(status.upper())}</b>",
        f"📦 {side} · {count:g} contract(s)"
        + (f" at <b>{price:.0%}</b> · cost ${count * price:,.2f}" if price else ""),
    ]
    if price is not None and limit is not None and round(limit, 4) != round(price, 4):
        per = abs(limit - price) * 100
        total = per * count / 100
        better = "better" if limit > price else "worse"
        lines.append(
            f"🏷 <i>Limit was {limit:.0%} · filled {per:.1f}c/contract "
            f"{better} (${total:,.2f} total)</i>"
        )
    if fee is not None:
        lines.append(f"🧾 <i>Fee ${fee:,.4f} · max payout ${count:,.2f}</i>")
    if not exact:
        # Never let the posted limit masquerade as the price paid.
        lines.append(
            "⚠️ <i>Fill price unconfirmed - showing the limit. "
            "Check the Kalshi ticket for the real cost.</i>"
        )
    lines.append(f"<i>{escape(note)}</i>")
    if order_id:
        lines.append(f"🧾 <code>{escape(order_id)}</code>")
    return "\n".join(lines)


def execute_buttons(
    count: int, proposal_id: str, override: bool = False
) -> list[tuple[str, str]]:
    """Buttons for one proposal.

    An override - the rule said no and you are taking it anyway - is labelled
    differently so the two cases never look identical on a phone.
    """
    label = f"⚠️ Execute anyway {count}" if override else f"✅ Execute {count}"
    return [(label, f"execute:{proposal_id}"), ("⏭ Skip", f"skip:{proposal_id}")]


def status(
    *, head: str, execution_ready: bool, manual: bool, window: tuple[int, int]
) -> str:
    chip = "\U0001f7e2" if execution_ready else "\U0001f7e1"
    state = "READY" if execution_ready else "NOT CONFIGURED"
    return "\n".join(
        [
            head,
            RULE,
            f"{chip} <b>Execution: {state}</b>",
            f"\U0001f446 Manual Execute button: {'on' if manual else 'off'}",
            f"\U0001f550 Entry scan: {window[0] // 60}m → {window[1] // 60}m before close",
            "\U0001f512 <i>Kalshi keys live only in the local .env file and are never "
            "shown or accepted here.</i>",
        ]
    )


def auto_status(
    *, head: str, on: bool, budget: float, limits, state, blocked: str
) -> str:
    """Auto-trading state, and the limits that will stop it.

    The money here is TODAY's realised P&L - the figure the daily floor is
    checked against - while the header carries the all-time live total. Both
    are real money and they legitimately differ, so each says which it is; an
    unlabelled pair read as a contradiction.
    """
    chip = "\U0001f7e2" if on else "⚪"
    lines = [
        head,
        RULE,
        f"{chip} <b>AUTO TRADING: {'ON' if on else 'OFF'}</b>",
        f"\U0001f4b5 Size <b>${budget:,.2f}</b> per order",
        f"\U0001f6d1 Daily loss floor <code>-${limits.daily_loss_limit:,.2f}</code> · "
        f"realised today <code>{state.realised_today:+,.2f}</code>",
        f"\U0001f4c8 Trades today {state.trades_today}/{limits.max_trades_per_day} · "
        f"this hour {state.trades_last_hour}/{limits.max_trades_per_hour}",
        f"\U0001f513 Open positions {state.open_positions}",
    ]
    lines.append(
        f"⛔ <i>Currently blocked: {escape(blocked)}</i>"
        if blocked
        else "✅ <i>Ready to take the next qualifying signal</i>"
    )
    lines.append("<i>/auto on · /auto off · /autosize 1 · /status</i>")
    return "\n".join(lines)


def auto_filled(
    *,
    head: str,
    ticker: str,
    side: str,
    price: float,
    count: float,
    note: str,
    limit: float | None = None,
    fee: float | None = None,
    exact: bool = True,
) -> str:
    """An order automation placed and filled.

    `price` is what was paid, not what was bid. When the fill beat the limit the
    difference is shown, because at these prices three cents of price
    improvement is a quarter of the trade's gross profit, and reading the limit
    back as the cost would misstate every result that follows.
    """
    lines = [
        head,
        RULE,
        f"🤖 <b>AUTO ORDER PLACED</b> · {escape(ticker)}",
        f"📦 {side} · {count:g} contract(s) at <b>{price:.0%}</b> "
        f"· cost ${count * price:,.2f}",
    ]
    if limit is not None and round(limit, 4) != round(price, 4):
        # Per contract AND in total. At one contract they coincide, which is
        # how a whole-order figure came to be labelled as a per-contract one.
        per = abs(limit - price) * 100
        total = per * count / 100
        better = "better" if limit > price else "worse"
        lines.append(
            f"🏷 <i>Limit was {limit:.0%} · filled {per:.1f}c/contract "
            f"{better} (${total:,.2f} total)</i>"
        )
    if fee is not None:
        lines.append(f"🧾 <i>Fee ${fee:,.4f} · max payout ${count:,.2f}</i>")
    if not exact:
        # The limit is standing in for a price we could not read back. Saying so
        # matters: a buy limit only ever fills at or below itself, so presenting
        # it as the cost overstates what was paid and understates the profit.
        lines.append(
            "⚠️ <i>Fill price unconfirmed - showing the limit. "
            "Check the Kalshi ticket for the real cost.</i>"
        )
    lines.append(f"<i>{escape(note)}</i>")
    lines.append("<i>No press was required. /auto off stops this immediately.</i>")
    return "\n".join(lines)


def auto_exit(
    *,
    head: str,
    ticker: str,
    side: str,
    price: float,
    target: float,
    bid: float,
    remaining: int,
    note: str,
    sold: bool,
) -> str:
    """A position closed by automation on a broken thesis.

    Says plainly whether the sell actually happened. A no-fill leaves the
    position riding to settlement, and reading "exited" when nothing traded
    would be the worst possible message to wake up to.
    """
    chip = "\U0001f534" if sold else "⚠️"
    title = "AUTO EXIT · REVERSAL" if sold else "AUTO EXIT FAILED · STILL HELD"
    return "\n".join(
        [
            head,
            RULE,
            f"{chip} <b>{title}</b> · {escape(ticker)}",
            f"\U0001f4c9 Held <b>{side}</b>, BTC crossed back through "
            f"<code>${target:,.2f}</code>",
            f"\U0001f4b8 Now <code>${price:,.2f}</code> · sold into <b>{bid:.0%}</b>"
            if sold
            else f"\U0001f4b8 Now <code>${price:,.2f}</code> · best bid was <b>{bid:.0%}</b>",
            f"⏱ {remaining // 60}m {remaining % 60:02d}s left",
            f"<i>{escape(note)}</i>",
            "\U0001f916 <i>No press was required. /auto off stops this.</i>",
        ]
    )


def cash_out(
    *,
    head: str,
    ticker: str,
    side: str,
    paid: float,
    bid: float,
    count: float,
    captured: float,
    remaining: int,
    note: str,
    sold: bool,
) -> str:
    """A position banked before expiry because it had already earned its money.

    States what it captured AND what it gave up, because both are real: selling
    at 97c after paying 74c banks 23c and forgoes the last 3c. Reading only the
    first half makes the rule look better than it is.
    """
    chip = "\U0001f4b0" if sold else "⚠️"
    title = "CASHED OUT EARLY" if sold else "CASH-OUT FAILED · STILL HOLDING"
    # NET, after both fees. It reported the gross figure, so a cash-out
    # announced "+0.27" and the settlement four minutes later said "+0.25" for
    # the same trade - which reads as the running total failing to move. Every
    # other money figure here is net; this one was the exception.
    from .validation import kalshi_fee_charged

    profit = (
        (bid - paid) * count
        - kalshi_fee_charged(paid, count)
        - kalshi_fee_charged(bid, count)
    )
    lines = [
        head,
        RULE,
        f"{chip} <b>{title}</b> · {escape(ticker)}",
        f"\U0001f4e6 Held <b>{side}</b> · bought {paid:.0%}, "
        f"{'sold' if sold else 'bid'} {bid:.0%}",
        f"\U0001f3af Banked <b>{profit:+,.2f}</b> · "
        f"{captured:.0%} of the most this trade could make",
        f"\U0001f4b8 Gave up the last <code>${(1.0 - bid) * count:,.2f}</code> "
        f"rather than risk <code>${bid * count:,.2f}</code> on it",
        f"⏱ {remaining // 60}m {remaining % 60:02d}s still to run",
        f"<i>{escape(note)}</i>",
    ]
    return "\n".join(lines)


SESSION_LABELS = {
    "asia": "Asia 00-07",
    "europe": "Europe 07-13",
    "us": "US 13-21",
    "late-us": "Late US 21-24",
}


def sessions(*, head: str, buckets: dict, measured: dict | None = None) -> str:
    """Live record split by trading session, beside what history measured.

    Both columns are shown because the live sample is far too small to mean
    anything on its own: the point is to watch whether it drifts toward the
    measured figure, not to act on this week's best session.
    """
    lines = [head, RULE, "\U0001f552 <b>BY SESSION</b> <i>(UTC)</i>"]
    any_row = False
    for key, label in SESSION_LABELS.items():
        b = buckets.get(key)
        if not b or not b["signals"]:
            continue
        any_row = True
        rate = b["wins"] / b["signals"]
        live = (
            f" · live <b>{b['real']:+,.2f}</b> on {b['trades']}"
            if b["trades"]
            else " · <i>none traded</i>"
        )
        hist = ""
        if measured and key in measured:
            hist = f" · <i>history {measured[key]:+.4f}/c</i>"
        lines.append(
            f"  <b>{label}</b> {b['wins']}/{b['signals']} ({rate:.0%}){live}{hist}"
        )
    if not any_row:
        lines.append("  <i>nothing settled yet</i>")
    lines.append(
        "\U0001f4a1 <i>Every session measured positive over 22,560 historical "
        "entries, but their intervals overlap - two of five straddle zero. "
        "Tracked, not traded on.</i>"
    )
    return "\n".join(lines)


def intel(*, head: str, gates: dict, needed: int) -> str:
    """What each rejected gate would have paid, and whether that is yet knowable.

    Reports EDGE per contract, not win rate. Most favourites win; the question
    is whether they win often enough to beat the price paid, and a win rate
    cannot answer that. Edge is payoff minus price, so zero is a fair market.

    Every row carries its sample count and the whole thing carries the number
    of samples that would be needed before any of it means anything, because a
    handful of signals will always show a large edge in one direction or the
    other and it is almost never real.
    """
    lines = [head, RULE, "\U0001f9e0 <b>WHAT THE RULE TURNED DOWN</b>"]
    rows = sorted(gates.items(), key=lambda kv: -kv[1]["n"])
    if not rows:
        lines.append("  <i>nothing settled yet</i>")
        return "\n".join(lines)
    for name, g in rows:
        if not g["n"]:
            continue
        edge = g["edge"] / g["n"]
        # Only a gate that rejected on its own says anything about itself.
        sole = f" · {g['sole']} alone" if g["sole"] and "taken" not in name else ""
        verdict = "\U0001f7e2" if edge > 0 else "\U0001f534"
        if g["n"] < needed:
            verdict = "⚪"
        lines.append(
            f"  {verdict} <b>{escape(name)}</b> n={g['n']}{sole} · "
            f"{g['wins'] / g['n']:.0%} won · edge <b>{edge:+.3f}</b>/contract"
        )
    lines.append(
        f"⚪ <i>= too few to judge. About <b>{needed:,}</b> settled signals are "
        "needed before an edge of a cent a contract separates from luck; every "
        "row above is far short of that.</i>"
    )
    lines.append(
        "\U0001f4a1 <i>Edge, not win rate: most favourites win, the question is "
        "whether they win more often than their price implies.</i>"
    )
    return "\n".join(lines)


def ledger(*, head: str, rows: list[dict]) -> str:
    """Every real trade with the running balance after it.

    The header carries only a total, so two moves between messages read as one
    unexplained jump: -1.53 to -1.03 was a +0.26 cash-out and a +0.24
    settlement, not a single +0.50. Each line here accounts for itself.
    """
    lines = [head, RULE, "\U0001f9fe <b>LEDGER</b> <i>(real money, newest last)</i>"]
    if not rows:
        lines.append("  <i>no trades yet</i>")
        return "\n".join(lines)
    for row in rows:
        chip = "\U0001f4b0" if row["pnl"] >= 0 else "\U0001f4b8"
        sold = f" → {row['sold_at']:.0%}" if row["sold_at"] is not None else ""
        lines.append(
            f"  {chip} {escape(row['ticker'][-8:])} {row['side']} "
            f"{row['paid']:.0%}{sold} · <b>{row['pnl']:+.2f}</b> "
            f"· running <code>{row['running']:+.2f}</code>"
        )
    lines.append(f"\U0001f4b5 <b>Total {rows[-1]['running']:+,.2f}</b>")
    return "\n".join(lines)
