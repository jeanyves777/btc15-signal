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


def _money_block(snapshot) -> str:
    """The two money lines every message carries, from ONE snapshot.

    Lifetime leads because that is the question the account actually answers -
    a good day inside a losing record is not a good result, and a headline
    that resets at midnight hides which one you are having. Today is kept, as
    the secondary line, because it is what the Kalshi app shows.

    Both lines come from the same read, so the profit and the counts can never
    disagree: on 2026-09-22 a settlement recap printed "+$3.05 - 30W-5L" while
    the ledger held 30W-6L, because the dollars and the record were taken a
    minute apart.

    PAPER RESULTS ARE NOT HERE. `scoreboard` prices every signal at a
    hypothetical size, including the great majority never traded; it is a
    measure of the strategy, not of the account, and it is labelled where it
    appears.
    """
    # A bare (markets, winners, dollars) tuple is still accepted: callers that
    # only have today's figures - and the tests that pin the paper/real
    # separation - should not have to build a snapshot to render one line.
    if isinstance(snapshot, tuple):
        markets, winners, dollars = snapshot
        if not markets:
            return "\U0001f4b0 <b>Live: nothing settled yet</b>"
        sign = "+" if dollars >= 0 else "−"
        chip = "\U0001f4b0" if dollars >= 0 else "\U0001f4b8"
        return (
            f"{chip} <b>Live today: {sign}${abs(dollars):,.2f}</b> · "
            f"{winners}W–{markets - winners}L"
        )
    lifetime = getattr(snapshot, "lifetime", None)
    lines = []
    if lifetime is not None and lifetime.markets:
        chip = "\U0001f4b0" if lifetime.dollars >= 0 else "\U0001f4b8"
        sign = "+" if lifetime.dollars >= 0 else "−"
        lines.append(
            f"{chip} <b>{lifetime.label()}: {sign}${abs(lifetime.dollars):,.2f}</b> · "
            f"{lifetime.markets} closed · {lifetime.winners}W–{lifetime.losers}L"
        )
    if snapshot.markets:
        sign = "+" if snapshot.headline >= 0 else "−"
        lines.append(
            f"\U0001f4c5 Today (New York): {sign}${abs(snapshot.headline):,.2f} · "
            f"{snapshot.markets} closed · {snapshot.winners}W–{snapshot.losers}L"
        )
    elif not lines:
        return "\U0001f4b0 <b>Live: nothing settled yet</b>"
    return "\n".join(lines)


def _live_line(markets: int, winners: int, dollars: float) -> str:
    """The money line, written the SAME way wherever it appears.

    There were two spellings - "+4.35 · 22 settled (20W-2L)" in the header and
    "+$4.35 · 20W–2L" in the new signal layout - which read as two different
    figures on a phone. One function, one format.
    """
    if not markets:
        return "\U0001f4b0 <b>Live today: nothing settled yet</b>"
    chip = "\U0001f4b0" if dollars >= 0 else "\U0001f4b8"
    sign = "+" if dollars >= 0 else "−"
    return (
        f"{chip} <b>Live today: {sign}${abs(dollars):,.2f}</b> · "
        f"{winners}W–{markets - winners}L"
    )


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
    * **Live** - what did the account actually do TODAY? Read from THE
      EXCHANGE'S OWN settlement record via `Store.realised_record`, plus the
      open position marked to the bid - the same two halves the Kalshi app adds
      to show "+$4.05 (+14.67%)", so the two agree to the cent.

      It is today's figure and not an all-time one because the operator reads
      the app beside it. The reconstruction this replaced was wrong twice over:
      it reported +1.06 on an account down -1.62, and then -1.40 all-time on a
      day the app showed +4.05.

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
        if live is not None:
            head += "\n" + _money_block(live)
        return head
    rate = wins / settled
    lines = [
        f"\U0001f3af <b>{rate:.0%} win rate</b> · {wins}W-{settled - wins}L "
        f"· <i>{settled} signals</i>",
        f"<code>{bar(rate)}</code> <i>paper {net_dollars:+,.2f} on {escape(basis)}</i>",
    ]
    if live is None:
        return "\n".join(lines)
    # The figures above are labelled paper; the money below is real.
    lines.append(_money_block(live))
    return "\n".join(lines)


def _distance_bps(price: float, target: float) -> str:
    if not target:
        return ""
    bps = (price / target - 1) * 10_000
    return f"{bps:+.0f} bps"


def _calibration(model: float, observed: float, samples: int) -> str:
    """Model probability beside the observed rate, with the sample count.

    An observed rate is suppressed below a handful of samples: "observed 100%
    (1 samples)" reads as corroboration when it is one coin flip, and it once
    sat directly beneath a model reading of 71%.

    The model probability is NOT calibrated - two of the earliest live losses
    came at 99.2% and 100.0% - so it is labelled as a score, never as odds.
    """
    if samples < 10:
        return (
            f"🧮 Model score {model:.0%} · "
            f"<i>too few settled signals ({samples}) to compare against</i>"
        )
    return (
        f"🧮 Model score {model:.0%} · observed {observed:.0%} "
        f"({samples} samples)"
    )


def side_chip(side: str) -> str:
    """The direction, as one glyph pair. Green up, red down, never ambiguous."""
    return "\U0001f7e2⬆️" if side == "UP" else "\U0001f534⬇️"


def _gap(price: float, target: float, name_target: bool = True) -> str:
    """How far BTC sits from the strike, in dollars and in plain words."""
    delta = price - target
    where = "above" if delta > 0 else "below"
    return f"BTC ${abs(delta):,.0f} {where}" + (" target" if name_target else "")


def _standing(price: float, target: float, side: str) -> str:
    """Where BTC is, and WHETHER THAT IS WINNING. Never just a direction.

    "BTC $40 above" said neither what it was above nor whether being above was
    good - and it is only good for an UP position. An UP bet needs BTC to
    settle above the strike and a DOWN bet needs it below, so the same $40 is
    the trade working or the trade failing depending on a word elsewhere in the
    message. Stated here so it cannot be read backwards.
    """
    delta = price - target
    ahead = (delta > 0) if side == "UP" else (delta < 0)
    return (
        f"BTC <code>${price:,.2f}</code> · "
        f"${abs(delta):,.0f} {'in the money' if ahead else 'out of the money'}"
    )


def checks_block(facts: list[dict], title: str = "Checks") -> list[str]:
    """The gates, rendered identically wherever they appear.

    EVERY SURFACE RENDERS FROM `EntryRule.check_facts`, which computes them
    once from one snapshot. They used to be reformatted per message, which is
    how the same trade showed momentum as +3.3 bps in the gates and -3.3 bps in
    the context below it - one number, two conventions, no way to tell which
    the rule had actually used.
    """
    lines = [f"<b>{escape(title)}</b>"]
    for fact in facts:
        tick = "✅" if fact["passed"] else "❌"
        text = fact["pass_text"] if fact["passed"] else fact["fail_text"]
        lines.append(f"{tick} {escape(fact['name'])}: {escape(text)}")
    return lines


def intelligence_line(read, mode: str = "shadow") -> str:
    """One line. The full reasoning lives behind 📋 DETAILS.

    Deliberately terse, and deliberately labelled with the mode: this layer has
    no authority over the order while it is in shadow, and a confident-looking
    recommendation printed beside the checks that DID decide will be read as
    though it participated. It did not.

    ENTER NOW shows what it expects to make; PASS shows what it expects to
    lose, because "PASS" with no number is an opinion and "PASS - expected net
    -6.5c" is an argument.
    """
    if read is None:
        return ""
    net = read.enter_now_net if read.action == "ENTER NOW" else -abs(read.enter_now_net)
    tag = "" if mode == "live" else f" <i>({escape(mode)})</i>"
    if read.action == "ENTER NOW":
        fill = "" if read.fill_rate is None else f" · fill {read.fill_rate:.0%}"
        return (
            f"\U0001f9e0 <b>Intelligence: ENTER NOW</b> · "
            f"p(win) {read.win_probability:.0%}{fill} · "
            f"net {net * 100:+.1f}¢ · {read.n} matches{tag}"
        )
    return (
        f"\U0001f9e0 <b>Intelligence: {escape(read.action)}</b> · "
        f"expected net {net * 100:+.1f}¢ · {read.n} matches{tag}"
    )


def signal_alert(
    *,
    side: str,
    ticker: str,
    ask: float,
    price: float,
    target: float,
    remaining: int,
    confidence: str,
    facts: list[dict],
    executable: bool,
    status_line: str = "",
    record: str = "",
    verdict: str = "",
) -> str:
    """One signal - cleared, refused, or paper-only. One layout for all three.

    The four checks stay VISIBLE in every case. They ARE the decision, and
    hiding them behind a button on a refusal is what made a refusal
    unreadable: the old paper alert printed only the names of the gates that
    failed - "model confidence, contract price band, target distance, momentum
    strength" - which says a setup was rejected four times over without saying
    by how much any of them missed. `EntryRule.check_facts` carries the
    measured value into the failure text, so a 0.8x distance reads as
    "0.8x volatility - needs 1.5x" rather than as "target distance".

    Only the extended context and the similar-regime read move behind DETAILS.
    """
    chip = side_chip(side)
    if not verdict:
        verdict = "ENTRY READY" if executable else "NOT EXECUTED"
    lines = [
        f"{chip} <b>{side} SIGNAL · {verdict}</b>",
        f"<code>{escape(ticker)}</code>",
        "",
        f"Price {ask * 100:.0f}¢ · {_gap(price, target)}",
        f"⏱ {remaining // 60}m {remaining % 60:02d}s · "
        f"Confidence {escape(confidence)}",
        "",
        *checks_block(facts),
    ]
    if status_line or record:
        lines.append("")
    if status_line:
        lines.append(status_line)
    if record:
        lines.append(record)
    return "\n".join(lines)


def order_filled(
    *,
    side: str,
    ticker: str,
    contracts: float,
    paid: float,
    confidence: str,
    facts: list[dict],
    band_held: str = "",
    exact: bool = True,
    remaining: int | None = None,
    target: float | None = None,
    price: float | None = None,
    decision_ask: float | None = None,
    size_reason: str = "",
) -> str:
    """An order that actually filled, with the gates AS THEY WERE at execution.

    The checks are the ones the decision was taken on, not a re-read: re-running
    them at report time would describe a market that has already moved, and the
    whole point of showing them here is the audit trail.
    """
    cost = contracts * paid
    lines = [
        f"{side_chip(side)} <b>{side} ORDER FILLED</b>",
        f"<code>{escape(ticker)}</code>",
        "",
        f"\U0001f4e6 {contracts:g} contract{'s' if contracts != 1 else ''} "
        f"filled at {paid * 100:.0f}¢",
        f"\U0001f4b5 Cost ${cost:,.2f} · Maximum profit "
        f"${contracts - cost:,.2f}",
    ]
    # TWO DIFFERENT PRICES, NAMED. The checks below show the ask the decision
    # was taken on; the line above shows what the book actually gave. On
    # 2026-09-22 those read 75¢ and 69¢ in the same message with nothing
    # saying they were different facts.
    if decision_ask is not None and abs(decision_ask - paid) >= 0.005:
        better = decision_ask - paid
        lines.append(
            f"\U0001f9fe Decision ask {decision_ask * 100:.0f}¢ · filled "
            f"{paid * 100:.0f}¢ "
            f"({abs(better) * 100:.0f}¢ {'better' if better > 0 else 'worse'})"
        )
    if size_reason:
        # SIZING IS THE OPERATOR'S, AND SAYS SO. The model never changes it -
        # it is config plus a measured distance band - so the message names
        # the rule that chose the size rather than leaving two contracts
        # looking like something the intelligence decided.
        lines.append(f"\U0001f4d0 Size {contracts:g} · {escape(size_reason)}")
    if target is not None:
        # WHAT IT SETTLES AGAINST. The fill report named the ticker and the
        # price paid but never the strike, so the one number that decides
        # whether this position wins was the one thing it did not carry - and
        # a ticker suffix is not a price anybody reads at a glance.
        lines.append(f"\U0001f3af Target <code>${target:,.2f}</code>")
        if price is not None:
            lines.append(f"\U0001f4ca {_standing(price, target, side)}")
    if remaining is not None:
        # HOW LONG THE MONEY IS AT RISK. The entry alert carried this and the
        # fill report did not, so the one message sent while a position is
        # actually open was the only one that did not say when it resolves.
        lines.append(
            f"⏱ {remaining // 60}m {remaining % 60:02d}s to expiry"
        )
    lines += [
        "",
        *checks_block(facts, "Checks at execution"),
    ]
    if band_held:
        lines.append(f"✅ Band held: {escape(band_held)}")
    lines += [
        "",
        f"\U0001f9e0 Confidence: {escape(confidence)}",
        "⏳ Holding to settlement",
    ]
    if not exact:
        # The fill was never read back, so the cost above is the posted limit.
        # A limit is permission to cross, never the price paid - limit 0.87
        # filled at 0.84 - so presenting it as settled fact overstates the cost
        # and understates the profit.
        lines.append(
            "⚠️ <i>Priced at the posted limit · the fill was not confirmed, "
            "so the real cost was this or lower.</i>"
        )
    return "\n".join(lines)


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
    auto_blocked: str = "",
    context: list[tuple[str, str]] | None = None,
    similar: str = "",
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
        _calibration(model, observed, samples),
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
    if context:
        # The numbers the Checks block does NOT carry. The settle timer in
        # particular decided almost every refusal on 2026-09-21 and appeared
        # nowhere: a setup could show five green ticks while a clock the
        # message never mentioned was the only thing standing in the way.
        lines.append("<b>Context</b>")
        for label, value in context:
            lines.append(f"  \u00b7 {escape(label)}: <code>{escape(value)}</code>")
    if live and auto_blocked:
        # The rule said yes and the bot still did not trade. On 2026-09-21 the
        # 14:45 window showed five green ticks and "ENTRY READY" while the
        # settle timer was refusing it - and could only ever refuse it, since
        # the price reached the band with 422s left and 120s of hold would not
        # complete before the 360s cutoff. Without this line there is no way to
        # tell that from the message.
        lines.append(
            f"\U0001f916 <b>Automation did NOT take this</b> — "
            f"<i>{escape(auto_blocked)}</i>"
        )
        lines.append("\U0001f446 <i>Your press is the only thing that will.</i>")
    elif not live:
        lines.append("\U0001f446 <i>Not auto-validated — your press is the decision.</i>")
    if missing:
        # Name only what is actually missing. Listing settings that are already
        # correct sends you to check things that are fine.
        lines.append(
            f"⚙️ <i>A press will be refused — still needed: {escape(missing)}</i>"
        )
    if note:
        lines.append(f"⚠️ <i>{escape(note)}</i>")
    if similar:
        # Appended LAST and labelled shadow, so it can never be mistaken
        # for the verdict that governs this alert. The deterministic rule
        # above decides; this is a second opinion kept honest in public.
        lines.append(RULE)
        lines.append(similar)
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
            _calibration(model, observed, samples),
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
    record: str = "",
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
    # TWO INDEPENDENT FACTS, NEVER COLLAPSED INTO ONE VERDICT.
    #
    #   the MONEY  - did the account gain or lose?
    #   the CALL   - did the market go the way the signal said?
    #
    # They come apart, and the case where they do is the one worth reading: a
    # position sold at 100c on a DOWN call that later settled UP is a PROFIT
    # and a WRONG PREDICTION at the same time. Headlining it by the call would
    # book a loss the account never took; headlining it by the money alone
    # would hide that the signal was wrong. Both are stated, on their own
    # lines, with their own ticks.
    chip_side = side_chip(side)
    made_money = pnl is not None and pnl >= 0
    money_tick = "✅\U0001f4b0" if made_money else "❌\U0001f4b8"
    priced = f" at {contract_price * 100:.0f}¢" if contract_price else ""

    if paper:
        # No order, so there is no money outcome - only whether the call was
        # right. The tick here is about the CALL, and the money line says zero
        # explicitly rather than being left off and read as an omission.
        tick = "✅\U0001f4c4" if won else "❌\U0001f4c4"
        lines = [
            f"{tick} <b>SIGNAL {'WON' if won else 'LOST'} · NOT TRADED</b>",
            f"<code>{escape(ticker)}</code>",
            "",
            f"{chip_side} Signal: <b>{side}</b>{priced}",
            f"\U0001f3c1 Market settled <b>{winner}</b>",
            # WHY it was not traded, not just that it was not. A signal the
            # rule approved and nobody pressed is a different miss from one the
            # rule refused, and only the first is a trade that got away.
            "\U0001f4a4 No order was executed · "
            + ("rule qualified it" if qualified else "rule declined it"),
            f"\U0001f4b5 {'Profit' if won else 'Loss'}: $0.00",
        ]
        if record:
            lines.append(record)
        return "\n".join(lines)

    # "+$0.22" / "-$0.77": the sign leads, the currency symbol sits inside it.
    # A bare "+0.22" beside a dollar cost two lines down reads as a different
    # unit, and a hyphen is easy to lose at phone size next to a red chip.
    if pnl is None:
        # NO MONEY FIGURE MEANS NO VERDICT ABOUT MONEY. Defaulting to zero put
        # "LOSS - $0.00" under a red chip on a position whose P&L simply had
        # not been computed, which asserts a loss the account may never have
        # taken. Describe the CALL, say the money is still unknown, stop there.
        tick = "✅" if won else "❌"
        lines = [
            f"{tick} <b>{side} CALL {'CORRECT' if won else 'WRONG'} · "
            f"P&amp;L PENDING</b>",
            f"<code>{escape(ticker)}</code>",
            "",
            f"{chip_side} Bought <b>{side}</b>{priced}",
            f"\U0001f3c1 Market settled <b>{winner}</b>",
            "\U0001f9fe <i>The money for this one is not settled yet.</i>",
        ]
        if record:
            lines.append(record)
        return "\n".join(lines)

    amount = f"{'+' if pnl >= 0 else chr(0x2212)}${abs(pnl):,.2f}"
    cost = contracts * contract_price if contracts and contract_price else None

    if exited_at is not None:
        lines = [
            f"{money_tick} <b>SOLD EARLY · {amount}</b>",
            f"<code>{escape(ticker)}</code>",
            "",
            f"{chip_side} Bought <b>{side}</b>{priced}",
            f"\U0001f4b5 Sold before expiry at {exited_at * 100:.0f}¢",
            f"\U0001f3c1 Market later settled <b>{winner}</b>",
            f"{'✅' if won else '❌'} {side} prediction was "
            f"{'correct' if won else 'wrong'}",
        ]
        if made_money and not won:
            lines.append(
                "✅ Trade remained profitable because it exited early"
            )
        elif not made_money and won:
            lines.append(
                "❌ Trade still lost because it exited below cost"
            )
        # The running total does not move on this message, and that is correct:
        # the money was counted the moment the sale happened. Unsaid, a second
        # message carrying the same total reads as a total that has stalled.
        lines += ["", "🧾 <i>Profit was already counted at the sale.</i>"]
    else:
        lines = [
            f"{money_tick} <b>{'WIN' if made_money else 'LOSS'} · {amount}</b>",
            f"<code>{escape(ticker)}</code>",
            "",
            f"{chip_side} Bought <b>{side}</b>{priced}",
            f"\U0001f3c1 Market settled <b>{winner}</b>",
        ]
        if cost is not None and pnl is not None:
            # Cost, not max payout, is the money at risk - on a loss it IS the
            # loss, so it is named rather than left to be inferred.
            outcome = (
                f"Profit ${pnl:,.2f}" if made_money else f"Lost ${abs(pnl):,.2f}"
            )
            lines.append(f"\U0001f4b5 Cost ${cost:,.2f} · {outcome}")
        elif pnl is not None:
            lines.append(f"\U0001f4b5 {amount} on {escape(basis)} after fees")

    if record:
        lines.append(record)
    if not exact:
        # The fill was never read back, so this is priced at the posted limit.
        lines.append(
            "⚠️ <i>Priced at the posted limit · the fill was never "
            "confirmed, so the real cost was this or lower.</i>"
        )
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


def signal_buttons(
    side: str, proposal_id: str | None, key: str, *, override: bool = False
) -> list[tuple[str, str]]:
    """The action, then DETAILS. The action names the DIRECTION.

    "Execute 1" does not say which way, so on a phone the only thing telling
    you whether you are buying UP or DOWN was a line further up the message.
    The button now carries the side and its colour, because that button is the
    last thing read before real money moves.
    """
    buttons: list[tuple[str, str]] = []
    if proposal_id:
        verb = "MANUAL" if override else "EXECUTE"
        buttons.append(
            (f"{side_chip(side)} {verb} {side}", f"execute:{proposal_id}")
        )
    buttons.append(("\U0001f4cb DETAILS", f"details:{key}"))
    return buttons


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
    why: list[tuple[str, str]] | None = None,
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
    if why:
        # WHY this trade, attached to the trade itself. Scattered across four
        # tables afterwards, the answer to "what supported this order" was a
        # reconstruction joined on drifting timestamps. Here it travels with
        # the fill.
        lines.append(RULE)
        lines.append("\U0001f4cb <b>WHY THIS TRADE</b>")
        for label, value in why:
            lines.append(f"  \u00b7 {escape(label)}: <code>{escape(value)}</code>")
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
    entry_fee: float | None = None,
    exit_fee: float | None = None,
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
    # THE FEES ARE READ, NOT MODELLED, whenever the exchange has told us what
    # it charged. `kalshi_fee_charged` is a faithful copy of the published
    # formula and still only a copy; the account is debited by Kalshi, not by
    # this function. The model stays as the fallback for the moment between
    # placing the exit and reading its fill back.
    from .validation import kalshi_fee_charged

    fee_in = entry_fee if entry_fee is not None else kalshi_fee_charged(paid, count)
    fee_out = exit_fee if exit_fee is not None else kalshi_fee_charged(bid, count)
    profit = (bid - paid) * count - fee_in - fee_out
    lines = [
        head,
        RULE,
        f"{chip} <b>{title}</b> · {escape(ticker)}",
        f"\U0001f4e6 Held <b>{side}</b> · bought {paid:.0%}, "
        f"{'sold' if sold else 'bid'} {bid:.0%}",
    ]
    if sold:
        lines += [
            f"\U0001f3af Banked <b>{profit:+,.2f}</b> · "
            f"{captured:.0%} of the most this trade could make",
            f"\U0001f4b8 Gave up the last <code>${(1.0 - bid) * count:,.2f}</code> "
            f"rather than risk <code>${bid * count:,.2f}</code> on it",
        ]
    else:
        # NOTHING moved. The failed cash-out on KXBTC15M-26SEP211400-00 still
        # printed "Banked +0.20 - 91% of the most this trade could make"
        # directly under "STILL HOLDING", which describes a sale that did not
        # happen and contradicts its own headline. A miss must read as a miss.
        lines += [
            f"\U0001f4a4 Nothing sold · <b>{profit:+,.2f}</b> is what it "
            f"WOULD have banked at {bid:.0%}",
            "\U0001f4b5 <i>Still fully exposed · the position rides to "
            "settlement</i>",
        ]
    lines += [
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
