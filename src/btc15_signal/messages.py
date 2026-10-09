"""Telegram message composition.

Telegram has no text colour, so colour is carried by emoji chips - a green
circle for a live entry, red for an exit, white for a pass - and structure by
its HTML subset (bold, monospace). Every message opens with the same scoreboard
line so the running win rate is visible without scrolling back.

Kept apart from main.py so the wording and layout can be tested without a
network client or an event loop.
"""

from html import escape

from . import surface
from .intelligence_policy import VETO

BAR_FULL = "▰"  # ▰
BAR_EMPTY = "▱"  # ▱
RULE = "—" * 16


def bar(fraction: float, width: int = 10) -> str:
    filled = max(0, min(width, round(fraction * width)))
    return BAR_FULL * filled + BAR_EMPTY * (width - filled)


def policy_line(verdict) -> str:
    """One short line, and ONLY when intelligence changed something.

    A layer that narrates every neutral decision trains the reader to skip it,
    and the one message that matters then goes unread too. Silence is the
    correct output for "the pattern supports the existing decision".
    """
    if verdict is None or not getattr(verdict, "changed", False):
        return ""
    action = verdict.final_action
    if action == "veto":
        return (
            "🧠 <b>Entry declined</b> · qualified setup matches an "
            f"adverse pattern <i>(n={verdict.evidence_n})</i>"
        )
    if action == "admit":
        return (
            "🧠 <b>Entry allowed</b> · validated exception to "
            f"{escape(str(verdict.overrides_gate))} <i>(n={verdict.evidence_n})</i>"
        )
    delta = verdict.confidence_delta
    direction = "raised" if delta > 0 else "lowered"
    return (
        f"🧠 Confidence {direction} · similar setups "
        f"{'out-earned' if delta > 0 else 'underperformed'} "
        f"<i>(n={verdict.evidence_n})</i>"
    )


def recovery_line(state, last_add: dict | None = None) -> str:
    """What recovery is doing right now, and why. Empty when inactive.

    Two facts, because one without the other is unreadable: how much is
    outstanding, and what the add-on last decided. A deficit shown with no
    explanation of the silence beside it is what sent the operator to the
    database to find two refusals that missed by fractions - momentum -0.6 bps
    against a floor of zero, distance 9.9x against 10x.
    """
    if state is None:
        return ""
    # SIZE ENDED IS NOT CLEARED, and the difference must be on the screen.
    # `active` means "may upsize"; money can still be owed with the upsize
    # off, and going silent there would read as "paid back".
    if getattr(state, "base_only", False) and getattr(state, "owes", False):
        return (
            f"\U0001f527 <b>Recovery size ended</b> · "
            f"${state.deficit:,.2f} still outstanding · "
            f"{state.wins} winning trades · "
            f"{state.recovered_fraction:.0%} recovered\n"
            f"   <i>continuing at normal base size</i>"
        )
    if not getattr(state, "active", False):
        return ""
    lines = [
        f"\U0001f527 <b>Recovery: ${state.deficit:,.2f} outstanding</b> · "
        f"${state.required_per_trade():,.2f} a trade · "
        f"{max(1, state.steps)} step(s)"
    ]
    if last_add:
        status = str(last_add.get("state") or "")
        reason = (last_add.get("cancel_reason") or "").strip()
        price = last_add.get("limit_price")
        if status.endswith("PENDING") and last_add.get("order_id"):
            lines.append(
                f"   <i>add resting at {price:.2f} · order "
                f"{str(last_add['order_id'])[:8]}</i>"
            )
        elif status.endswith("EXECUTED"):
            lines.append(
                f"   <i>add filled {last_add.get('filled_count')} at "
                f"{last_add.get('fill_price')}</i>"
            )
        elif reason:
            # ONE PHRASE FOR ONE FACT. `surface.position_block` says the same
            # thing about the same row in the trade recap; two spellings of
            # "nothing was placed" in two messages minutes apart is how the
            # operator came to read the add lines as a separate subsystem.
            lines.append(f"   <i>no recovery add · {escape(reason[:90])}</i>")
    return "\n".join(lines)


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
        # The day broken into sessions, under the day's own count. These SUM
        # to the line above - a breakdown that does not add up to the total it
        # sits beneath invites the reader to trust neither.
        sessions = getattr(snapshot, "sessions", None)
        if sessions:
            from .sessions import one_line

            line = one_line(sessions)
            if line:
                lines.append(f"<i>{line}</i>")
    elif not lines:
        lines.append("\U0001f4b0 <b>Live: nothing settled yet</b>")
    # RECOVERY LAST, under the money it is working against. Empty when
    # inactive, so a quiet system stays quiet - a permanent status line for a
    # subsystem that is off is noise, and noise is what hides the real one.
    recovery = recovery_line(
        getattr(snapshot, "recovery", None), getattr(snapshot, "last_add", None)
    ) if surface.recovery_on() else ""
    if recovery:
        lines.append(recovery)
    return "\n".join(lines)


def recovery_armed(state, trigger: str = "", loss_step: float = 0.0,
                   band: tuple[float, float] = (0.70, 0.79),
                   wait: int = 5) -> str:
    """Announced when a realised loss opens a deficit.

    THE SIZING LINE HAS TO MATCH WHAT RUNS. It said "base entries stay at one
    contract" for as long as that was true. Since 2026-09-24 the loss step
    sizes the next trade after a losing market to a fixed dollar budget, so a
    message still promising one contract would describe a system that is
    about to buy six - and this whole thread began with recovery messages
    that contradicted each other.
    """
    head = (
        f"\U0001f527 <b>RECOVERY ARMED</b> · ${state.deficit:,.2f} outstanding"
    )
    sizing = (
        f"After a losing market ONE later entry is sized to "
        f"${loss_step:,.2f} - but it waits for an ask between "
        f"{band[0]:.2f} and {band[1]:.2f}, where the extra contract is worth "
        f"having, so it may be several markets later. It expires unspent after "
        f"{wait} settled markets, fires once, and never escalates; the "
        f"conditional add-on stands down only on the entry it actually sizes."
        if loss_step > 0 else
        "Base entries stay at one contract. Recovery may add ONE extra "
        "contract, resting 2¢ below a fill, and only while the BRTI "
        "evidence still holds."
    )
    body = [
        head,
        f"<i>{escape(trigger)}</i>" if trigger else "",
        "",
        f"Plan: {max(1, state.steps)} step(s), "
        f"${state.required_per_trade():,.2f} a trade.",
        sizing,
    ]
    return "\n".join(x for x in body if x != "")


def recovery_size_ended(state) -> str:
    """The ONE transition message, in the operator's own layout.

    Deliberately not the CLEARED message. That one says the deficit is back
    to $0.00; this says the opposite - the money is still missing and we are
    choosing to stop carrying doubled size while it comes back. Reporting the
    two alike would tell the operator the account had recovered when it had
    not.

    Counts and percentage are the real ones, never the thresholds that were
    met: "4 winning trades - 50% recovered" printed on a cycle that reached
    five wins and 63% would be a template, not a report.
    """
    recovered = f"{state.recovered_fraction:.0%} recovered"
    if getattr(state, "seeded", False):
        # The baseline was ADOPTED from a deficit already in flight, so the
        # percentage measures progress against a migration starting point -
        # not against the loss that originally opened the hole. Printing it
        # bare would claim more than the number knows.
        recovered += " of the carried-over balance"
    return "\n".join([
        "\U0001f527 <b>RECOVERY SIZE ENDED</b>",
        f"{state.wins} winning trades · {recovered}",
        f"Remaining deficit: ${state.deficit:,.2f}",
        "Continuing at normal base size.",
    ])


def recovery_cleared(snapshot) -> str:
    """Announced the moment the money is back. Recovery stops immediately."""
    return "\n".join([
        "✅ <b>RECOVERY CLEARED</b> · deficit back to $0.00",
        "<i>Sizing returns to base. Any unfilled recovery add is cancelled.</i>",
        "",
        _money_block(snapshot),
    ])


def session_close(*, session, day_snapshot, ny_day: str) -> str:
    """The report sent when a trading session ends.

    Two things, in this order: how THAT session went, then where the day
    stands. The session is the news; the day is the context it belongs in.
    A session read without the day behind it is how a good hour gets mistaken
    for a good day.
    """
    from .sessions import LABELS, one_line

    sign = "+" if session.dollars >= 0 else "−"
    chip = "\U0001f4b0" if session.dollars >= 0 else "\U0001f4b8"
    rate = (session.winners / session.markets) if session.markets else 0.0
    lines = [
        f"\U0001f514 <b>{LABELS.get(session.name, session.name)} session closed</b>",
        f"<i>{escape(ny_day)} · New York accounting day</i>",
        "",
        f"{chip} <b>Session: {sign}${abs(session.dollars):,.2f}</b> · "
        f"{session.markets} closed · {session.winners}W–{session.losers}L",
    ]
    if session.markets:
        lines.append(f"<code>{bar(rate)}</code> <i>{rate:.0%} of this session won</i>")
    lines += ["", _money_block(day_snapshot)]
    breakdown = one_line(list(day_snapshot.sessions or ()))
    if breakdown and not session.markets:
        # `_money_block` already prints the breakdown when the day has
        # markets; this covers the quiet-session case.
        lines.append(f"<i>{breakdown}</i>")
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


def _cents(price: float) -> str:
    """A contract price at the precision it actually traded at.

    `0.997` shown as `100¢` claims a fill at a price the book does not offer.
    """
    value = price * 100
    return (f"{value:.0f}¢" if abs(value - round(value)) < 0.05
            else f"{value:.1f}¢")



def _margin_lines(margin: dict | None) -> list[str]:
    """How far past the target the market finished, in bps and in dollars.

    BPS LEADS because the dollar figure is not comparable between instruments
    and the bps figure demonstrably is: measured over 143 BTC and 14 ETH
    executed trades, winners finished a mean 18.3 bps past the target on BOTH,
    and losers fell 6.4 / 6.5 bps short. A $128 move on BTC and a $4.37 move on
    ETH are the same event, and only one of those units says so.

    The wording distinguishes three outcomes rather than signing a number:
    "past" for a favourable finish, "short of" for an adverse one, and a
    settlement exactly on the strike is named as such - that is the case a
    reader would otherwise take for a rounding artefact.

    Returns a LIST so an absent margin contributes no line at all. Kalshi
    publishes the two numbers on the market object and the enrichment pass may
    not have run yet; "not fetched" and "finished level with the target" are
    different facts and must not render the same.
    """
    if not margin:
        return []
    favourable = float(margin.get("favourable") or 0.0)
    bps = float(margin.get("bps") or 0.0)
    if abs(favourable) < 0.005:
        return ["\U0001f4cf Settled exactly ON the target"]
    word = "past" if favourable > 0 else "short of"
    return [f"\U0001f4cf Settled ${abs(favourable):,.2f} {word} "
            f"the target \u00b7 {bps:.1f} bps"]

def settlement(
    *,
    head: str,
    ticker: str,
    side: str,
    winner: str,
    won: bool,
    target: float,
    # THE SIDE THE SIGNAL CALLED, which is not always the side we held.
    # `predictions` is written at ALERT time and keyed on the window, so when
    # the reference flips before the order fills the call and the position are
    # on opposite sides. Both are reported; neither is inferred from the other.
    called_side: str = "",
    contract_price: float | None,
    pnl: float | None,
    qualified: bool,
    basis: str = "1 contract",
    contracts: float | None = None,
    exited_at: float | None = None,
    paper: bool = False,
    exact: bool = True,
    record: str = "",
    # BY HOW MUCH the market finished past the target, from `Store
    # .settlement_margin` - Kalshi's own `floor_strike` and
    # `expiration_value`. The lifecycle recorded whether a trade won and what
    # it paid but never the MARGIN, which is what says an 83c favourite
    # settled comfortably rather than by a hair. None when Kalshi has not
    # published both numbers yet, which is different from a margin of zero:
    # finishing exactly on the strike is a real and reportable outcome.
    margin: dict | None = None,
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
    # THE VERDICT IS THE BROKER'S REALISED P&L AFTER FEES, and nothing else.
    # Not `held side == winner`: an early exit can bank money on a position
    # whose side later loses, and a position held through settlement can still
    # be under water after fees. The money decides the money word.
    flat = pnl is not None and abs(pnl) < 0.005
    made_money = pnl is not None and pnl > 0
    money_tick = (
        "⚪" if flat else ("✅\U0001f4b0" if made_money else "❌\U0001f4b8")
    )
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
            *_margin_lines(margin),
            # WHY it was not traded, not just that it was not. A signal the
            # rule approved and nobody pressed is a different miss from one the
            # rule refused, and only the first is a trade that got away.
            "\U0001f4a4 No order was executed · "
            + ("rule qualified it" if qualified else "rule declined it"),
            # NOT A LOSS. Nothing was bought, so nothing was lost -
            # "Loss: $0.00" under a red chip reads as a small defeat
            # rather than the absence of a trade.
            "💵 Not traded · realised P&amp;L $0.00",
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
            *_margin_lines(margin),
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
            f"💵 Sold before expiry at {_cents(exited_at)}",
            f"\U0001f3c1 Market later settled <b>{winner}</b>",
            f"{'✅' if (called_side or side) == winner else '❌'} "
            f"{escape(called_side or side)} prediction was "
            f"{'correct' if (called_side or side) == winner else 'wrong'}",
        ]
        if made_money and (called_side or side) != winner:
            lines.append(
                "✅ Trade remained profitable because it exited early"
            )
        elif not made_money and (called_side or side) == winner:
            lines.append(
                "❌ Trade still lost because it exited below cost"
            )
        # The running total does not move on this message, and that is correct:
        # the money was counted the moment the sale happened. Unsaid, a second
        # message carrying the same total reads as a total that has stalled.
        lines += ["", "🧾 <i>Profit was already counted at the sale.</i>"]
    else:
        # BREAK-EVEN IS NOT A MARKET OUTCOME. The market settles UP or DOWN,
        # always, and that is reported on its own line below. This word is
        # about the TRADE's net after fees: exactly zero is "closed", not a
        # third thing the market did.
        headline = (
            "CLOSED · $0.00 net" if flat
            else f"{'WIN' if made_money else 'LOSS'} · {amount}"
        )
        lines = [
            f"{money_tick} <b>{headline}</b>",
            f"<code>{escape(ticker)}</code>",
            "",
            f"{chip_side} Bought <b>{side}</b>{priced}",
            f"\U0001f3c1 Market settled <b>{winner}</b>",
            *_margin_lines(margin),
        ]
        # THE CALL, on its own line, every time. It is a different fact from
        # the money and it is the one that was silently inverted: the 12:15
        # market on 2026-09-23 called DOWN, held UP, settled UP and paid
        # +$0.1271, and the recap reported "Bought DOWN ... LOSS -$1.87".
        call = called_side or side
        call_right = call == winner
        lines.append(
            f"{'✅' if call_right else '❌'} {escape(call)} prediction was "
            f"{'correct' if call_right else 'wrong'}"
        )
        if call != side:
            lines.append(
                f"\U0001f4e6 <i>Signal called {escape(call)}; the position "
                f"held {escape(side)}</i>"
            )
        if cost is not None and pnl is not None:
            # Cost, not max payout, is the money at risk - on a loss it IS the
            # loss, so it is named rather than left to be inferred.
            outcome = (
                "$0.00 net" if flat
                else (f"Profit ${pnl:,.2f}" if made_money
                      else f"Lost ${abs(pnl):,.2f}")
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


def _ago(now_ms: int, then_ms: int) -> str:
    """How long ago, in the coarsest unit that is still honest."""
    if not then_ms:
        return "never"
    seconds = max(0, (now_ms - then_ms) // 1000)
    if seconds < 90:
        return f"{seconds}s ago"
    if seconds < 5400:
        return f"{seconds // 60}m ago"
    if seconds < 172800:
        return f"{seconds // 3600}h ago"
    return f"{seconds // 86400}d ago"


def _until(now_ms: int, then_ms: int) -> str:
    if not then_ms:
        return "unscheduled"
    seconds = (then_ms - now_ms) // 1000
    if seconds <= 0:
        return "due now"
    if seconds < 5400:
        return f"in {seconds // 60}m"
    return f"in {seconds // 3600}h"


def learning_state(state: dict) -> list[str]:
    """The four states, on four lines, never collapsed into one.

    The operator asked for these to be distinguishable, and they are genuinely
    four different claims about four different things:

        RUNNING     the loop is scheduled and alive
        UPDATING    a fit is in progress right now
        ADJUSTING   at least one arm is re-rating displayed confidence
        AUTHORISED  an arm may actually change an order

    A system can be running, updating and adjusting confidence while being
    authorised to change nothing - which is exactly the normal state here, and
    reporting it as one word would make it indistinguishable from a system with
    a live veto.
    """
    def tick(flag: bool) -> str:
        return "✅" if flag else "—"

    updating = state.get("updating")
    lines = [
        f"  {tick(state.get('running'))} <b>Running</b> · "
        + (
            f"every {state.get('interval_ms', 0) // 3600000}h or "
            f"{state.get('trigger_threshold')} new settled markets"
            if state.get("running") else "<i>disabled</i>"
        ),
        f"  {tick(updating)} <b>Updating</b> · "
        + ("training now" if updating else
           f"last completed {_ago(state['now_ms'], state.get('last_success_ms') or 0)}"),
        f"  {tick(state.get('adjusting_confidence'))} "
        f"<b>Adjusting confidence</b> · "
        + (
            f"{state.get('confidence_arms')} arm(s) active"
            if state.get("adjusting_confidence")
            else (
                f"{state.get('confidence_arms')} arm(s) carry a delta but mode "
                f"is {escape(str(state.get('mode')))}"
                if state.get("confidence_arms")
                else "no arm has earned one"
            )
        ),
        f"  {tick(state.get('authorised_to_execute'))} "
        f"<b>Authorised to affect execution</b> · "
        + (
            f"{state.get('promoted_arms')} promoted arm(s), mode "
            f"{escape(str(state.get('mode')))}"
            if state.get("authorised_to_execute")
            else (
                f"{state.get('promoted_arms')} arm(s) cleared the evidence bar "
                f"but the operator's switches are not set"
                if state.get("promoted_arms")
                else "no arm has cleared the evidence bar"
            )
        ),
    ]
    return lines


def learning(*, head: str, state: dict | None = None, candidates=None,
             board: list[dict] | None = None,
             min_candidate_n: int = 60, min_promotion_n: int = 120) -> str:
    """The learning loop: what it is doing, what it last did, what is live.

    Four sections, in the order the operator asked for them - the loop's own
    state, the training schedule, what is actually active, and the forward
    evaluation underneath. Promotion is reported SEPARATELY from everything
    else throughout, because "the system is learning" and "an adjustment is
    changing orders" are different claims and only the first is usually true.
    """
    board = board or []
    lines = [head]
    if state:
        lines.append(RULE)
        lines.append("\U0001f9e0 <b>LEARNING LOOP</b>")
        lines.extend(learning_state(state))
        lines.append(RULE)
        lines.append("\U0001f4c5 <b>TRAINING</b>")
        last = state.get("last_complete_run") or {}
        now = state["now_ms"]
        if last:
            status = str(last.get("status") or "?")
            lines.append(
                f"  last successful update <b>"
                f"{_ago(now, int(last.get('finished_ms') or 0))}</b> "
                f"({escape(status)}, trigger {escape(str(last.get('trigger')))})"
            )
            lines.append(
                f"      used <b>{last.get('markets_used') or 0}</b> markets "
                f"({last.get('rows_used') or 0} decisions · "
                f"{last.get('corpus_markets') or 0} archive + "
                f"{last.get('live_markets') or 0} live, "
                f"{last.get('live_actual_fills') or 0} real fills)"
            )
            lines.append(
                f"      fitted {last.get('arms_fitted') or 0} arms · "
                f"{last.get('arms_with_confidence') or 0} confidence · "
                f"{last.get('promoted') or 0} promoted of "
                f"{last.get('candidates_examined') or 0} examined"
            )
        else:
            lines.append("  <i>no completed training run yet</i>")
        lines.append(
            f"  new settled markets since then: "
            f"<b>{state.get('new_settled_markets')}</b> of "
            f"{state.get('trigger_threshold')} needed"
        )
        lines.append(
            f"  next training <b>"
            f"{_until(now, int(state.get('next_due_ms') or 0))}</b>"
            + (" · <i>training now</i>" if state.get("updating") else "")
        )
        if state.get("last_error"):
            lines.append(
                f"  ⚠️ last failure "
                f"{_ago(now, int(state.get('last_error_ms') or 0))} "
                f"({state.get('consecutive_failures')} consecutive): "
                f"<code>{escape(str(state['last_error'])[:160])}</code>"
            )
            lines.append(
                "      <i>the last valid Kalshi policy stayed active</i>"
            )
        lines.append(RULE)
        lines.append("\U0001f4e6 <b>ACTIVE POLICY</b>")
        if state.get("policy_valid"):
            lines.append(
                f"  <code>{escape(str(state.get('policy_version')))}</code> · "
                f"features <code>{escape(str(state.get('feature_version')))}</code>"
                f" <code>{escape(str(state.get('feature_fingerprint'))[:8])}</code>"
            )
            lines.append(
                f"  {state.get('arms')} arms · vetoes "
                f"{'on' if state.get('vetoes_enabled') else 'off'} · "
                f"admissions "
                f"{'on' if state.get('admissions_enabled') else 'off'}"
            )
        else:
            lines.append(
                f"  ❌ <b>cannot act</b> · "
                f"<code>{escape(str(state.get('policy_version')))}</code>"
            )
            lines.append(
                f"      {escape(str(state.get('policy_invalid_reason')))}"
            )
        adjustments = state.get("active_adjustments") or []
        if adjustments:
            lines.append("  <i>active adjustments</i>")
            for item in adjustments[:6]:
                mark = "⚙️" if item["kind"] == "execution" else "\U0001f4ca"
                what = (
                    f"{item['action']}"
                    if item["kind"] == "execution"
                    else f"confidence {item['delta']:+d}"
                )
                lines.append(
                    f"    {mark} {escape(item['context_key'])} · {what} "
                    f"<i>(n={item['n']} over {item['markets']} markets)</i>"
                )
        else:
            lines.append(
                "  <i>no arm is adjusting anything - every decision returns "
                "the base strategy with its evidence beside it</i>"
            )
        for item in (state.get("withdrawals") or [])[:2]:
            lines.append(
                f"  \U0001f6d1 withdrawn {escape(str(item.get('context_key')))} "
                f"· {escape(surface.clipped(item.get('reason')))}"
            )
    lines.append(RULE)
    lines.append("\U0001f9ea <b>FORWARD EVALUATION</b>")
    if candidates is None or not getattr(candidates, "candidates", ()):
        lines.append("  <i>no candidates frozen - nothing is being watched</i>")
        return "\n".join(lines)
    lines.append(
        f"  <i>artefact</i> <code>{escape(candidates.version)}</code> · "
        f"features <code>{escape(candidates.feature_version)}</code>"
    )
    # SCORED BY CONTEXT AS WELL AS BY ID. Candidate ids used to be positional
    # (`c01` was whichever cell had the most rows that day) and are now derived
    # from the context, so the same cell has carried two ids across the change.
    # The CELL is the identity that matters - it is what the forward rows are
    # really about - so its record is accumulated across every id it has had,
    # and no evidence is rewritten to achieve it.
    scored = {r["candidate_id"]: r for r in board}
    by_context: dict = {}
    for row in board:
        bucket = by_context.setdefault(
            row.get("context_key"),
            {"graded": 0, "changes": 0, "incremental": 0.0, "ids": set()},
        )
        bucket["graded"] += row.get("graded") or 0
        bucket["changes"] += row.get("changes") or 0
        bucket["incremental"] += row.get("incremental") or 0.0
        bucket["ids"].add(row.get("candidate_id"))
    for c in candidates.candidates:
        row = scored.get(c.candidate_id) or {}
        merged = by_context.get(c.context_key) or {}
        graded = merged.get("graded") or row.get("graded") or 0
        changes = merged.get("changes") or row.get("changes") or 0
        incremental = merged.get("incremental") or row.get("incremental") or 0.0
        state = "\U0001f7e2 PROMOTED" if c.promotes else "\U0001f441 watching"
        lines.append(
            f"  {state} <b>{escape(c.candidate_id)}</b> "
            f"{escape(c.proposed_action)} · {escape(c.context_key)}"
        )
        lines.append(
            f"      train n={c.train_n} {c.train_mean:+.4f}/ct · "
            f"live {graded} graded, {changes} it would change"
        )
        if graded:
            # AN OPPORTUNITY COST IS NOT A TRADING LOSS. Where the candidate
            # would have stood aside, this figure is what the bot MADE and the
            # candidate would have missed - money that is in the account. It is
            # labelled so it can never be read as a drawdown.
            label = (
                "missed against the unchanged rule"
                if c.proposed_action == VETO and incremental < 0
                else "against the unchanged rule"
            )
            lines.append(
                f"      forward <b>{incremental:+.4f}</b> {label} "
                f"over {changes} market(s)"
            )
            if c.proposed_action == VETO and incremental < 0:
                lines.append(
                    "      <i>estimated opportunity cost, not a realised "
                    "loss - those markets were traded and settled</i>"
                )
    promoted = sum(1 for c in candidates.candidates if c.promotes)
    lines.append(RULE)
    lines.append(
        f"  <b>{promoted}</b> of <b>{len(candidates.candidates)}</b> may change "
        "an order. The rest are recorded and graded only."
    )
    lines.append(
        f"\U0001f4a1 <i>n≥{min_candidate_n} to be watched, "
        f"n≥{min_promotion_n} with a validated interval clear of zero to "
        "control an order. Where no order was placed the forward figure is a "
        "SIMULATED fill: a rejected winner is evidence about direction, not "
        "proof a fill was available.</i>"
    )
    return "\n".join(lines)


def learning_activated(*, policy, report: dict, comparison: dict,
                       reason: str) -> str:
    """Sent when a newly trained policy becomes the live artefact.

    An activation is a change to how the bot decides, so it is announced the
    way any other such change is - with what it replaced, what it is allowed to
    do, and what the evidence behind it was. Silence here would mean the
    decision rule could change under the operator overnight with the only
    record in a log file.
    """
    promoted = int(report.get("promoted") or 0)
    lines = [
        "\U0001f9e0 <b>LEARNING: NEW POLICY ACTIVE</b>",
        RULE,
        f"  <code>{escape(str(policy.version))}</code>",
        f"  features <code>{escape(str(policy.feature_version))}</code> "
        f"<code>{escape(str(policy.feature_fingerprint)[:8])}</code>",
        f"  fitted on <b>{report.get('markets', 0)}</b> markets "
        f"({report.get('rows', 0)} decisions), train {report.get('train_n', 0)} / "
        f"validate {report.get('validate_n', 0)} / holdout "
        f"{report.get('holdout_n', 0)}",
        f"  {report.get('arms_fitted', 0)} arms · "
        f"<b>{report.get('arms_with_confidence', 0)}</b> adjust confidence · "
        f"<b>{promoted}</b> may change an order",
    ]
    if comparison:
        lines.append(
            f"  against the previous policy on the validation slice: "
            f"<b>{comparison.get('delta', 0):+.4f}</b> "
            f"({comparison.get('markets', 0)} markets)"
        )
    lines.append(f"  <i>{escape(reason)}</i>")
    if not promoted:
        lines.append(RULE)
        lines.append(
            "  <i>No adjustment earned the right to change an order. The "
            "policy re-rates confidence only; every entry decision remains "
            "the strategy's.</i>"
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


# ===================================================================== v2
#
# THE SHARED SURFACE. Every trading and learning message below is assembled by
# `surface.compose` in the one fixed order, from the one icon vocabulary, with
# one money snapshot and at most one rotating insight. The older builders above
# remain for the paths that have not moved and for the tests that pin them.


def signal_message(*, side: str, ticker: str, ask: float, remaining: int,
                   confidence: str, facts: list[dict], executable: bool,
                   status_line: str, snapshot=None, insight: str = "",
                   band_hold: tuple[int, int] | None = None,
                   distance_text: str = "", priority=None,
                   verdict: str = "",
                   confidence_note: str = "",
                   policy_note: str = "") -> str:
    """A signal, executed or not. The checks are always visible.

    `confidence` is the ONE Kalshi-native confidence result, computed once by
    `main.model_confidence_points` and passed in - never recomputed here, and
    never recomputed differently in a details body. It describes how good the
    setup looks and says nothing about whether it may be traded; a refused
    signal reading MEDIUM is correct and not a contradiction.
    """
    label = verdict or ("ENTRY READY" if executable else "NOT EXECUTED")
    essentials = [
        f"{surface.PRICE} Ask {surface.cents(ask)} · "
        f"{surface.CLOCK} {remaining // 60}m {remaining % 60:02d}s",
    ]
    if distance_text:
        essentials.append(f"{surface.TARGET} {escape(distance_text)}")
    essentials.append(
        f"{surface.LEARNING} Confidence {escape(confidence)} · "
        f"Entry checks {surface.checks_summary(facts)}"
    )
    # Directly under the word it explains. Without it a 5/5 setup reading
    # MEDIUM has nothing on screen to account for the gap.
    if confidence_note:
        essentials.append(confidence_note)
    # A VETO or an ADMIT belongs in the message, not behind the DETAILS
    # button: one refuses a setup every gate accepted, the other overrules a
    # gate and lets an order through.
    if policy_note:
        essentials.append(policy_note)
    return surface.compose(
        header=f"{surface.side_icon(side)} <b>{escape(side)} SIGNAL · "
               f"{escape(label)}</b>",
        ticker=ticker,
        essentials=essentials,
        checks=surface.checks_block(facts, band_hold),
        status=status_line,
        snapshot=snapshot,
        priority=priority,
        insight=insight,
    )


def fill_message(*, side: str, ticker: str, contracts: float, paid: float,
                 fee: float | None, remaining: int, confidence: str,
                 facts: list[dict], snapshot=None, insight: str = "",
                 band_hold: tuple[int, int] | None = None,
                 decision_ask: float | None = None, priority=None,
                 size_reason: str = "",
                 confidence_note: str = "",
                 policy_note: str = "") -> str:
    """An executed entry. Cost and maximum profit are both NET.

    The old fill report printed `Maximum profit contracts - cost` with no fee
    term while the settlement recap for the same trade subtracted the charged
    fee, so the fill promised $0.20 and the recap paid $0.19.
    """
    size = (f"{surface.PACKAGE} {contracts:g} contract"
            f"{'s' if contracts != 1 else ''} filled at {surface.cents(paid)}")
    if size_reason:
        # WHY THAT MANY, beside how many. A size that rose under a recovery
        # rule and a size that was capped by the account limit look identical
        # on the contract count alone, and the operator reads the count.
        size += f" <i>({escape(surface._typography(size_reason))})</i>"
    essentials = [
        size,
        surface.entry_cost(contracts, paid, fee),
        surface.max_net_profit(contracts, paid, fee),
    ]
    if decision_ask is not None:
        better = decision_ask - paid
        essentials.append(
            f"\U0001f9fe Decision ask {surface.cents(decision_ask)} · "
            f"filled {surface.cents(paid)} "
            f"({abs(better) * 100:.1f}¢ {'better' if better > 0 else 'worse'})"
        )
    essentials.append(
        f"{surface.LEARNING} Confidence {escape(confidence)} · "
        f"Entry checks {surface.checks_summary(facts)}"
    )
    if confidence_note:
        essentials.append(confidence_note)
    # A VETO or an ADMIT belongs in the message, not behind the DETAILS
    # button: one refuses a setup every gate accepted, the other overrules a
    # gate and lets an order through.
    if policy_note:
        essentials.append(policy_note)
    return surface.compose(
        header=f"{surface.side_icon(side)} <b>{escape(side)} FILLED</b>",
        ticker=ticker,
        essentials=essentials,
        checks=surface.checks_block(facts, band_hold),
        status=f"{surface.CLOCK} {remaining // 60}m {remaining % 60:02d}s "
               f"to expiry",
        snapshot=snapshot,
        priority=priority,
        insight=insight,
    )


def not_filled_message(*, ticker: str, note: str, snapshot=None,
                       insight: str = "", priority=None) -> str:
    """An order that went out and bought nothing.

    Announcing a cost here claimed a position that does not exist, so this
    says what was spent - nothing - rather than what was intended.
    """
    return surface.compose(
        header=f"{surface.WARN} <b>ORDER NOT FILLED</b>",
        ticker=ticker,
        essentials=[
            f"{surface.PRICE} No contracts bought · nothing spent",
            f"<i>{escape(surface._typography(note))}</i>" if note else "",
        ],
        checks=[],
        status="",
        snapshot=snapshot,
        priority=priority,
        insight=insight,
    )


def automation_off_message(*, ask: float, snapshot=None,
                           insight: str = "") -> str:
    """Both switches must be on, and only one of them is.

    strategy.json was reset to defaults with `enabled: false` on 2026-09-21 at
    02:02 and automation went silent for four hours while 14 signals passed,
    17 of 18 of which went on to win. The log line existed and was useless:
    `/auto` was on, so there was nothing to notice.
    """
    return surface.compose(
        header=f"{surface.WARN} <b>AUTOMATION IS OFF AT THE STRATEGY</b>",
        ticker="",
        essentials=[
            f"<i>/auto is ON, but strategy.json has "
            f"<code>enabled: false</code>, so no order will be placed — "
            f"this one at {surface.cents(ask)} included.</i>",
            "<i>Both switches must be on. Set enabled: true to resume.</i>",
        ],
        checks=[],
        status="",
        snapshot=snapshot,
        insight=insight,
    )


def cash_out_message(*, ticker: str, side: str, paid: float, bid: float,
                     count: float, captured: float, remaining: int,
                     note: str, sold: bool, entry_fee: float | None = None,
                     exit_fee: float | None = None, snapshot=None,
                     insight: str = "", priority=None) -> str:
    """A position banked before expiry because it had already earned its money.

    States what it captured AND what it gave up, because both are real: selling
    at 97c after paying 74c banks 23c and forgoes the last 3c. Reading only the
    first half makes the rule look better than it is.

    NET, after both fees. The gross figure made a cash-out announce `+0.27`
    and the settlement four minutes later say `+0.25` for the same trade,
    which reads as the running total failing to move.

    THE FEES ARE READ, NOT MODELLED, whenever the exchange has said what it
    charged. `kalshi_fee_charged` is a faithful copy of the published formula
    and still only a copy; the account is debited by Kalshi, not by this
    function. The model stays as the fallback for the moment between placing
    the exit and reading its fill back.
    """
    from .validation import kalshi_fee_charged

    fee_in = entry_fee if entry_fee is not None else kalshi_fee_charged(paid, count)
    fee_out = exit_fee if exit_fee is not None else kalshi_fee_charged(bid, count)
    profit = (bid - paid) * count - fee_in - fee_out

    essentials = [
        f"{surface.side_icon(side)} Held <b>{escape(side)}</b> \u00b7 "
        f"bought {surface.cents(paid)}, "
        f"{'sold' if sold else 'bid'} {surface.cents(bid)}",
    ]
    if sold:
        essentials += [
            f"{surface.TARGET} Banked "
            f"<b>{surface._signed_dollars(profit)}</b> \u00b7 "
            f"{captured:.0%} of the most this trade could make "
            f"<i>(net of fees)</i>",
            f"{surface.PRICE} Gave up the last "
            f"${(1.0 - bid) * count:,.2f} rather than risk "
            f"${bid * count:,.2f} on it",
        ]
    else:
        # NOTHING MOVED. A failed cash-out still printed "Banked +0.20 - 91%
        # of the most this trade could make" directly under "STILL HOLDING",
        # which describes a sale that did not happen and contradicts its own
        # headline. A miss must read as a miss.
        essentials += [
            f"\U0001f4a4 Nothing sold \u00b7 "
            f"<b>{surface._signed_dollars(profit)}</b> is what it WOULD have "
            f"banked at {surface.cents(bid)}",
            "<i>Still fully exposed \u00b7 the position rides to "
            "settlement</i>",
        ]
    if note:
        essentials.append(f"<i>{escape(surface._typography(note))}</i>")

    return surface.compose(
        header=(f"{surface.MONEY} <b>CASHED OUT EARLY</b>" if sold else
                f"{surface.WARN} <b>CASH-OUT FAILED \u00b7 STILL HOLDING</b>"),
        ticker=ticker,
        essentials=essentials,
        checks=[],
        status=f"{surface.CLOCK} {remaining // 60}m {remaining % 60:02d}s "
               f"still to run",
        snapshot=snapshot,
        priority=priority,
        insight=insight,
    )


def auto_exit_message(*, ticker: str, side: str, price: float, target: float,
                      bid: float, remaining: int, note: str, sold: bool,
                      snapshot=None, insight: str = "", priority=None) -> str:
    """The reference crossed back through the strike and the bot sold."""
    essentials = [
        f"{surface.side_icon(side)} Held <b>{escape(side)}</b> \u00b7 "
        f"the reference crossed back through ${target:,.2f}",
        f"{surface.PRICE} Now ${price:,.2f} \u00b7 "
        + (f"sold into {surface.cents(bid)}" if sold
           else f"bid {surface.cents(bid)}"),
    ]
    if note:
        essentials.append(f"<i>{escape(surface._typography(note))}</i>")
    if not sold:
        essentials.append(
            "<i>Still holding \u00b7 the position rides to settlement</i>"
        )
    return surface.compose(
        header=(f"{surface.EXIT} <b>AUTO EXIT \u00b7 REVERSAL</b>" if sold else
                f"{surface.WARN} <b>EXIT FAILED \u00b7 STILL HOLDING</b>"),
        ticker=ticker,
        essentials=essentials,
        checks=[],
        status=f"{surface.CLOCK} {remaining // 60}m {remaining % 60:02d}s left "
               f"\u00b7 {surface.ROBOT} no press was required "
               f"(<code>/auto off</code> stops this)",
        snapshot=snapshot,
        priority=priority,
        insight=insight,
    )


def exit_warning_message(*, ticker: str, side: str, price: float,
                         target: float, bid: float, remaining: int,
                         snapshot=None, insight: str = "",
                         priority=None) -> str:
    """The reference crossed back and NOTHING was sold.

    It is advice, so it says so: a warning that looks like an execution is how
    an operator comes to believe a position was closed when it is still open.
    """
    return surface.compose(
        header=f"{surface.WARN} <b>REVERSAL \u00b7 STILL HOLDING</b>",
        ticker=ticker,
        essentials=[
            f"{surface.side_icon(side)} Held <b>{escape(side)}</b> \u00b7 "
            f"the reference crossed back through ${target:,.2f}",
            f"{surface.PRICE} Now ${price:,.2f} \u00b7 "
            f"bid {surface.cents(bid)}",
            "<i>No order was placed. This is a warning, not an exit.</i>",
        ],
        checks=[],
        status=f"{surface.CLOCK} {remaining // 60}m {remaining % 60:02d}s left",
        snapshot=snapshot,
        priority=priority,
        insight=insight,
    )


def recovery_armed_message(state, trigger: str = "", *, snapshot=None,
                           insight: str = "", loss_step: float = 0.0,
                           band: tuple[float, float] = (0.70, 0.79),
                           wait: int = 5, base: int = 1,
                           add_per_base: int = 1, combo: bool = False,
                           partner: str = "",
                           partner_band: tuple[float, float] = (0.70, 0.85)
                           ) -> str:
    """A realised loss opened a deficit, and a recovery may now follow.

    WHAT IT MAY RISE BY IS STATED HERE rather than left to the reader, and it
    has to be the rule that is actually running. Until 2026-09-24 that was
    one extra contract rested 2c below a fill. Since the loss step shipped,
    the next entry after a losing market is sized to a fixed dollar budget -
    six or seven contracts at band prices - so the old sentence would now
    understate the position by a factor of six. Neither rule is a martingale
    and the message must not read like one.

    `base` is today's base tier. Both recoveries are PER BASE CONTRACT since
    2026-09-27 - the step is `loss_step` per base contract and the resting
    add-on is `add_per_base` per base contract (0 = the add-on is off) - so
    the message leads with the RULE ("2x the base") and gives today's
    contracts beside it. The step can fire several markets later, possibly
    after the base has moved; the rule is what stays true. A message saying
    "$2" while the service buys 4 is the contradiction this began with.
    """
    base = max(1, int(base))
    at_hi = int(loss_step / band[1]) * base if band[1] > 0 else 0
    at_lo = int(loss_step / band[0]) * base if band[0] > 0 else 0
    contracts = f"{at_hi}" if at_hi == at_lo else f"{at_hi}-{at_lo}"
    per_base = at_hi // base if at_hi == at_lo else None
    rule = f"{per_base}x the base" if per_base else "the step"
    add = max(0, int(add_per_base)) * base
    add_line = (
        f" The resting add-on is +{add} ({add_per_base} per base contract), "
        f"2\u00a2 below a fill, and stands down on the entry the step sizes."
        if add > 0 else ""
    )
    if combo:
        # SINCE 2026-09-27 THE RECOVERY IS A COMBO AT BASE SIZE. Said exactly,
        # because the message is the operator's only view of what will run.
        sizing = (
            f"<i>After a losing market ONE later entry goes out as a "
            f"<b>recovery combo at base size ({base})</b> - this market plus "
            f"{escape(partner or 'its partner')} on its own signal side, priced "
            f"{partner_band[0]:.2f}-{partner_band[1]:.2f}, same or opposite "
            f"direction, bought at the cheapest quote at or below the cheaper "
            f"leg's price. It "
            f"waits for an ask between {band[0]:.2f} and {band[1]:.2f}, may "
            f"fire several markets later, expires unspent after {wait} "
            f"settled markets, fires once, and never back to back. No partner "
            f"or no quote: the entry goes out as usual at base size. Nothing "
            f"is upsized.</i>"
        )
    elif loss_step > 0:
        sizing = ("<i>Recovery combos are off - every entry stays at base "
                  "size.</i>")
    else:
        sizing = (
            f"<i>After a losing market ONE later entry is sized to {rule} - "
            f"${loss_step:,.2f} per base contract, {contracts} contracts at "
            f"today's base of {base} - and it WAITS for an ask between "
            f"{band[0]:.2f} and "
            f"{band[1]:.2f} - at 0.90 an extra contract makes about 10¢, which "
            f"base size earns anyway at a better price. So it may fire several "
            f"markets later; it expires unspent after {wait} settled markets, fires "
            f"once, never escalates, and a recovery trade that loses does not "
            f"arm another.{add_line}</i>"
            if loss_step > 0 else
            (f"<i>Base entries stay at {base} contracts. Recovery may add "
             f"{add} extra, resting 2\u00a2 below a fill, and only while "
             f"the BRTI evidence still holds.</i>" if base > 1 else
             "<i>Base entries stay at one contract. Recovery may add ONE "
             "extra contract, resting 2\u00a2 below a fill, and only while "
             "the BRTI evidence still holds.</i>")
        )
    return surface.compose(
        header=f"{surface.RECOVERY} <b>RECOVERY ARMED</b>",
        ticker="",
        essentials=[
            f"{surface.PRICE} <b>${state.deficit:,.2f}</b> outstanding",
            f"<i>{escape(surface._typography(trigger))}</i>" if trigger else "",
            f"{surface.TARGET} Plan: {max(1, state.steps)} step"
            f"{'s' if max(1, state.steps) != 1 else ''} \u00b7 "
            f"${state.required_per_trade():,.2f} a trade",
            sizing,
        ],
        checks=[],
        status="",
        snapshot=snapshot,
        insight=insight,
    )


def recovery_size_ended_message(state, *, snapshot=None,
                                insight: str = "") -> str:
    """Sizing returns to base while the money is STILL MISSING.

    Deliberately not the cleared message. That one says the deficit is back to
    $0.00; this says the opposite - we are choosing to stop carrying extra
    size while it comes back. Reporting the two alike would tell the operator
    the account had recovered when it had not.

    Counts and percentage are the real ones, never the thresholds that were
    met: "4 winning trades - 50% recovered" printed on a cycle that reached
    five wins and 63% would be a template, not a report.
    """
    recovered = f"{state.recovered_fraction:.0%} recovered"
    if getattr(state, "seeded", False):
        # The baseline was ADOPTED from a deficit already in flight, so the
        # percentage measures progress against a migration starting point -
        # not against the loss that originally opened the hole. Printing it
        # bare would claim more than the number knows.
        recovered += " of the carried-over balance"
    return surface.compose(
        header=f"{surface.RECOVERY} <b>RECOVERY SIZE ENDED</b>",
        ticker="",
        essentials=[
            f"{surface.PASS} {state.wins} winning trade"
            f"{'s' if state.wins != 1 else ''} \u00b7 {recovered}",
            f"{surface.PRICE} <b>${state.deficit:,.2f}</b> still outstanding",
            "<i>Continuing at normal base size.</i>",
        ],
        checks=[],
        status="",
        snapshot=snapshot,
        insight=insight,
    )


def recovery_cleared_message(snapshot=None, *, insight: str = "") -> str:
    """The money is back. Recovery stops immediately."""
    return surface.compose(
        header=f"{surface.PASS} <b>RECOVERY CLEARED</b>",
        ticker="",
        essentials=[
            f"{surface.PRICE} Deficit back to <b>$0.00</b>",
            "<i>Sizing returns to base. Any unfilled recovery add is "
            "cancelled.</i>",
        ],
        checks=[],
        status="",
        snapshot=snapshot,
        insight=insight,
    )


def _record(r: dict) -> str:
    """'11W\u20131L 92% +$2.04' - a record, compact. Its P&L is already NET of
    fees (shadow_summary.allsignal_record), under a "(net)" heading."""
    if not r["n"]:
        return "no settled trades yet"
    return (f"{r['wins']}W\u2013{r['n'] - r['wins']}L "
            f"<b>{r['wins'] / r['n']:.0%}</b> {surface._signed_dollars(r['pnl'])}")


def _book_lines(book: list, asset: str | None = None) -> list[str]:
    """Today and overall, one line each: this instrument, then the combined
    book (the last row when there is more than one instrument)."""
    if not book:
        return []
    own = next((r for r in book if r[0] == asset), None) if asset else None
    combined = book[-1] if len(book) > 1 else None
    scopes = [r for r in (own, combined) if r is not None] or book
    lines = []
    for i, when in ((1, "\U0001f4c5 Today (net)"), (2, "\U0001f4c8 Since start (net)")):
        parts = [f"{escape(r[0])} {_record(r[i])}" for r in scopes]
        lines.append(f"{when}: " + " \u00b7 ".join(parts))
    return lines


def _exit_part(window_open: int, fill_price: float, count: float,
               exit_price, exit_count, exited_ms) -> str:
    """'sold 98\u00a2 with 4m12s left' - or '' if nothing was sold."""
    if not exit_count:
        return ""
    left = max(0, int((window_open + 900_000 - int(exited_ms or 0)) / 1000))
    some = "" if float(exit_count) >= float(count) - 1e-9 else f"{float(exit_count):g} "
    return (f"sold {some}{float(exit_price) * 100:.0f}\u00a2 "
            f"with {left // 60}m{left % 60:02d}s left")


def _px(value) -> str:
    """$84,787.45 - cents for a coin above $100, four places below it."""
    v = float(value)
    return f"${v:,.2f}" if v >= 100 else f"${v:,.4f}"


def _vs_target(value, target) -> str:
    bps = (float(value) / float(target) - 1) * 10_000
    return f"{bps:+.1f} bps {'above' if bps >= 0 else 'below'}"


def target_lines(side: str, target=None, ref=None, final=None, asset: str = "BTC") -> list[str]:
    """THE KALSHI TARGET (operator, 2026-10-03: "show the Kalshi target price for
    clarity"): the window's strike and what the side needs - Kalshi settles YES
    (UP) at or above it - plus BTC at the signal, or where it settled. Empty when
    the target is not known; never raises."""
    try:
        if not target or float(target) <= 0:
            return []
        need = "UP wins at or above it" if side == "UP" else "DOWN wins below it"
        if final:
            return [f"\U0001f4cd Kalshi target <b>{_px(target)}</b> \u2192 settled "
                    f"<b>{_px(final)}</b> ({_vs_target(final, target)})"]
        lines = [f"\U0001f4cd Kalshi target <b>{_px(target)}</b> \u00b7 {need}"]
        if ref:
            lines.append(f"\U0001f4b2 {escape(asset)} at the signal {_px(ref)} "
                         f"({_vs_target(ref, target)})")
        return lines
    except (TypeError, ValueError, ZeroDivisionError):
        return []


def allsignal_trade_message(*, asset: str, window_open: int, side: str,
                            signal_ask: float, fill_price: float, count: float,
                            mirrored: bool, won=None, pnl=None,
                            book: list | None = None, exit_price=None,
                            exit_count=None, exited_ms=None, budget: float = 1.0,
                            cushion_note: str = "", target=None, ref=None) -> str:
    """The ENTRY: the trade and nothing else (operator, 2026-09-29: "the
    signal should only be the trade; the result should be the one containing
    account summary and stats"). A trade that never got an entry message is
    reported through this too, with its result and record.
    """
    import datetime as dt
    from zoneinfo import ZoneInfo

    ny = ZoneInfo("America/New_York")
    start = dt.datetime.fromtimestamp(window_open / 1000, ny)
    end = start + dt.timedelta(minutes=15)
    arrow = "\u2b06\ufe0f UP" if side == "UP" else "\u2b07\ufe0f DOWN"
    cashed = bool(exit_count) and float(exit_count) >= float(count) - 1e-9
    sold = _exit_part(window_open, fill_price, count, exit_price, exit_count, exited_ms)
    name = f"{escape(asset)} ${budget:g}"
    if won is None:
        head = f"\U0001f7e2 <b>{name} \u00b7 NEW TRADE</b>"
    elif cashed:
        head = f"\U0001f4b0 <b>{name} \u00b7 CASHED OUT {surface._signed_dollars(pnl or 0)}</b>"
    elif won:
        head = f"\u2705 <b>{name} \u00b7 WON {surface._signed_dollars(pnl or 0)}</b>"
    else:
        head = f"\u274c <b>{name} \u00b7 LOST {surface._signed_dollars(pnl or 0)}</b>"
    who = mirrored if isinstance(mirrored, str) else ("wife's account" if mirrored else "")
    lines = [
        head,
        f"<b>{arrow}</b> \u00b7 \U0001f552 {start:%H:%M}\u2013{end:%H:%M} ET"
        + ("" if won is not None else f" \u00b7 open until {end:%H:%M}"),
        f"\U0001f3af Signal {signal_ask * 100:.0f}\u00a2 \u2192 \u2705 filled "
        f"<b>{fill_price * 100:.0f}\u00a2 \u00d7 {count:g}</b>",
        f"\U0001f4b5 Cost <b>${fill_price * count:.2f}</b> <i>(before fees)</i>",
    ]
    lines[3:3] = target_lines(side, target, ref, asset=asset)
    if who:
        lines.append(f"\U0001f465 Mirrors enabled: {escape(who, quote=False)}")
    if cushion_note:
        lines.append(f"\U0001f6e1 {escape(cushion_note, quote=False)}")
    if sold:
        lines.append(f"\U0001f4e4 Cash-out: {sold}"
                     + ("" if cashed else " \u00b7 rest held to settlement"))
    if book:
        lines += ["", "\U0001f4ca <b>Stats</b>"] + _book_lines(book, asset)
    return "\n".join(lines)


def allsignal_cashout_notice(*, asset: str, window_open: int, side: str,
                             fill_price: float, count: float, sold: float,
                             sold_at: float, bid: float, remaining: int,
                             budget: float = 1.0) -> str:
    """A cash-out that did NOT close the whole position: a miss, or part of
    it. (A full one is announced by `allsignal_settled_message` at the sale.)"""
    left = f"{max(0, remaining) // 60}m{max(0, remaining) % 60:02d}s left"
    if sold <= 0:
        return (f"{surface.WARN} <b>{escape(asset)} ${budget:g} CASH-OUT MISSED</b> \u00b7 "
                f"{side} {fill_price * 100:.0f}\u00a2, bid {bid * 100:.0f}\u00a2, "
                f"no fill \u00b7 {left} \u00b7 <i>holding to settlement</i>")
    return (f"\U0001f4b0 <b>{escape(asset)} ${budget:g} CASHED OUT {sold:g} of {count:g}</b> "
            f"\u00b7 {side} {fill_price * 100:.0f}\u00a2 \u2192 {sold_at * 100:.0f}\u00a2 "
            f"\u00b7 {left} \u00b7 <i>{count - sold:g} held to settlement</i>")


def _balance_lines(balances: list) -> list[str]:
    """'🏦 You: $112.12 → $113.45 (+$1.33) since 15:45' per account.

    The operator's request, 2026-09-28: track each account's starting and
    current balance. Current = cash plus open positions at cost."""
    import datetime as dt
    from zoneinfo import ZoneInfo

    lines = []
    for label, start, at_ms, now in balances or []:
        since = dt.datetime.fromtimestamp(at_ms / 1000, ZoneInfo("America/New_York"))
        if now is None:
            lines.append(f"\U0001f3e6 {escape(label)}: started ${start:,.2f} "
                         f"({since:%m-%d %H:%M}) \u00b7 now unreadable")
            continue
        lines.append(
            f"\U0001f3e6 {escape(label)}: ${start:,.2f} \u2192 <b>${now:,.2f}</b> "
            f"({surface._signed_dollars(now - start)}) since {since:%m-%d %H:%M}")
    return lines


def allsignal_settled_message(*, asset: str, window_open: int, side: str,
                              fill_price: float, won: bool, pnl: float,
                              book: list | None = None, count: float = 0.0,
                              exit_price=None, exit_count=None,
                              exited_ms=None, budget: float = 1.0,
                              target=None, final=None) -> str:
    """The RESULT, sent as a reply to the trade's entry: the outcome, then the
    stats (today and since start, net), then - appended by the caller - the
    account table. An edit alone is silent in Telegram, so this is always a
    new message (2026-09-28 18:00)."""
    import datetime as dt
    from zoneinfo import ZoneInfo

    start = dt.datetime.fromtimestamp(window_open / 1000, ZoneInfo("America/New_York"))
    end = start + dt.timedelta(minutes=15)
    arrow = "\u2b06\ufe0f UP" if side == "UP" else "\u2b07\ufe0f DOWN"
    mark = "\u2705" if won else "\u274c"
    verb = "WON" if won else "LOST"
    # CASHED OUT is said as such (operator: "if that is the case the message
    # needs to better say it") - never dressed up as a settlement win.
    cashed = bool(exit_count) and float(exit_count) >= float(count or 0) - 1e-9
    if cashed:
        mark, verb = "\U0001f4b0", "CASHED OUT"
    sold = _exit_part(window_open, fill_price, count or float(exit_count or 0),
                      exit_price, exit_count, exited_ms)
    outcome = (f"\U0001f4e4 {sold}" + ("" if cashed else " \u00b7 rest held to settlement")
               if sold else f"\U0001f3c1 held to settlement \u00b7 {'won' if won else 'lost'}")
    lines = [
        f"{mark} <b>{escape(asset)} ${budget:g} \u00b7 {verb} "
        f"{surface._signed_dollars(pnl)}</b> <i>net of fees</i>",
        f"<b>{arrow}</b> \u00b7 \U0001f552 {start:%H:%M}\u2013{end:%H:%M} ET",
        f"\U0001f4e5 Entry <b>{fill_price * 100:.0f}\u00a2 \u00d7 {count:g}</b> \u2192 {outcome}",
    ]
    lines += target_lines(side, target, final=final, asset=asset)
    if book:
        lines += ["\u2501" * 14, "\U0001f4ca <b>Stats</b>"] + _book_lines(book, asset)
    return "\n".join(lines)


def allsignal_missed_message(*, asset: str, window_open: int, side: str,
                             signal_ask: float, limit: float,
                             failed: bool = False, budget: float = 1.0,
                             reason: str = "", skipped: bool = False,
                             target=None, ref=None, confirmation_expired: bool = False) -> str:
    """A signal whose order bought nothing - said, so no window looks skipped,
    with the broker's reason when there is one."""
    import datetime as dt
    from zoneinfo import ZoneInfo

    start = dt.datetime.fromtimestamp(window_open / 1000, ZoneInfo("America/New_York"))
    end = start + dt.timedelta(minutes=15)
    arrow = "\u2b06\ufe0f UP" if side == "UP" else "\u2b07\ufe0f DOWN"
    what = ("PRICE CONFIRMATION EXPIRED" if confirmation_expired else
            "SKIPPED" if skipped else ("ORDER FAILED" if failed else "NOT FILLED"))
    lines = [
        f"\u26aa <b>{escape(asset)} ${budget:g} \u00b7 {what}</b>",
        f"<b>{arrow}</b> \u00b7 \U0001f552 {start:%H:%M}\u2013{end:%H:%M} ET",
        f"\U0001f3af Signal {signal_ask * 100:.0f}\u00a2"
        + ("" if skipped or confirmation_expired else f" \u00b7 limit {limit * 100:.0f}\u00a2"),
        *target_lines(side, target, ref, asset=asset),
        f"{surface.WARN} <i>No trade this window"
        + (f": {escape(reason, quote=False)}" if reason else "") + "</i>",
    ]
    return "\n".join(lines)


def allsignal_copy_message(*, asset: str, window_open: int, side: str,
                           signal_ask: float, accounts: str, won=None,
                           missed: bool = False, target=None, ref=None,
                           final=None) -> str:
    """A $ signal the MIRRORS took once the primary was done for the day
    (operator, 2026-10-05; FINDINGS 163): which accounts, at what stake - then
    the market's result on its side, or that the copy missed. No dollar figure:
    a mirror's money is its own broker balance (the session summary)."""
    import datetime as dt
    from zoneinfo import ZoneInfo

    start = dt.datetime.fromtimestamp(window_open / 1000, ZoneInfo("America/New_York"))
    end = start + dt.timedelta(minutes=15)
    arrow = "\u2b06\ufe0f UP" if side == "UP" else "\u2b07\ufe0f DOWN"
    if missed:
        head = f"\u26aa <b>{escape(asset)} \u00b7 COPY NOT FILLED</b>"
    elif won is None:
        head = f"\U0001f501 <b>{escape(asset)} \u00b7 COPIED TO THE MIRRORS</b>"
    else:
        icon = "\u2705" if won else "\u274c"
        head = f"{icon} <b>{escape(asset)} \u00b7 COPY {'WON' if won else 'LOST'}</b>"
    lines = [
        head,
        f"<b>{arrow}</b> \u00b7 \U0001f552 {start:%H:%M}\u2013{end:%H:%M} ET",
        f"\U0001f3af Signal {signal_ask * 100:.0f}\u00a2",
        *target_lines(side, target, ref, final, asset=asset),
        f"\U0001f465 {escape(accounts, quote=False)}",
        "<i>The primary is done for the day; the mirrors trade on.</i>",
    ]
    return "\n".join(lines)


def shadow_summary_message(*, session: str, rows: list, ny_day: str,
                           allsignal: list | None = None,
                           book: list | None = None,
                           balances: list | None = None,
                           copies: dict | None = None, budget: float = 1.0,
                           after_target: float = 0.0, after_loss: float = 0.0,
                           after_loss_trades: int = 2) -> str:
    """The shadow instruments' session, in one message.

    Operator, 2026-09-28: only BTC and gold send alerts; the rest "come in as
    summary while in shadow". Per instrument: the alerts as sent, and the
    trades its own rule would have placed, each graded on the settled outcome
    at one contract, fee-free. Nothing here moved money.
    """
    from .sessions import LABELS

    def line(g):
        if not g["n"]:
            return "none"
        return (f"{g['n']} \u00b7 {g['wins']}W\u2013{g['n'] - g['wins']}L "
                f"({g['wins'] / g['n']:.0%}) \u00b7 "
                f"{g['pnl']:+.2f} at 1 contract")

    title = escape(LABELS.get(session, session))
    # The primary's stake drops after its daily target (2026-09-30): say both.
    stake = f"${budget:g}" + (f" (${after_target:g} while at the target)" if after_target else "")
    if after_loss:
        stake += f" \u00b7 ${after_loss:g} for {int(after_loss_trades)} after a loss"
    out = [f"\U0001f4b5 <b>ALL-SIGNAL {stake} \u00b7 {title} session</b>" if allsignal
           else f"\U0001f441 <b>SESSION SUMMARY \u00b7 {title} session</b>"]
    # THE ALL-SIGNAL STRATEGY: real money, $1 a signal (operator, 2026-09-28).
    for asset, a in allsignal or []:
        text = f"<b>{escape(asset)}</b> this session: " + (
            f"{_record(a)} (fees ${a['fee']:.2f})" if a["n"] else "no settled trades")
        if a.get("open"):
            text += f" \u00b7 {a['open']} open"
        if a.get("missed"):
            text += f" \u00b7 {a['missed']} not filled"
        out.append(text)
    out.extend(_book_lines(book or []))
    out.extend(_balance_lines(balances or []))
    if rows:
        out.append("\U0001f441 <b>SHADOW</b> (no money):")
    for asset, s in rows:
        if "error" in s:
            out.append(f"<b>{escape(asset)}</b>: store unreadable ({escape(s['error'])})")
            continue
        a, r = s["alerts"], s["rule"]
        head = f"<b>{escape(asset)}</b> alerts: {line(a)}"
        if a["n"]:
            head += f" \u00b7 avg {a['avg_ask'] * 100:.0f}\u00a2"
        if a.get("open"):
            head += f" \u00b7 {a['open']} unsettled"
        out.append(head)
        out.append(f"   rule would have traded: {line(r)}")
    if not allsignal:
        tail = "Shadow only - no money moved"
    elif copies is None:
        tail = f"{stake} per primary signal; mirrors use their own budgets \u00b7 after fees"
    else:
        # What each mirror account really copies, per its switch
        # (scripts/mirror_switch.py).
        parts = [f"{escape(who, quote=False)} copies {', '.join(assets) if assets else 'nothing'}"
                 for who, assets in copies.items()] or ["no mirror account"]
        tail = f"{stake} per primary signal \u00b7 " + " \u00b7 ".join(parts) + " \u00b7 after fees"
    out.append(f"<i>{escape(ny_day)} \u00b7 {tail}</i>")
    return "\n".join(out)


def session_close_message(*, session, day_snapshot, ny_day: str,
                          insight: str = "") -> str:
    """How that session went, then where the day stands.

    The session is the news; the day is the context it belongs in, and it
    comes from the same reconciled snapshot as every other message rather
    than a second renderer with its own wording.
    """
    from .sessions import LABELS

    rate = (session.winners / session.markets) if session.markets else 0.0
    essentials = [
        f"{surface.MONEY if session.dollars >= 0 else surface.PRICE} "
        f"<b>{surface._signed_dollars(session.dollars)}</b> this session "
        f"\u00b7 {session.markets} closed \u00b7 "
        f"{session.winners}W\u2013{session.losers}L",
    ]
    if session.markets:
        essentials.append(
            f"<code>{bar(rate)}</code> <i>{rate:.0%} of this session won</i>"
        )
    return surface.compose(
        header=f"\U0001f514 <b>"
               f"{escape(LABELS.get(session.name, session.name))} "
               f"SESSION CLOSED</b>",
        ticker="",
        essentials=essentials,
        checks=[],
        status=f"<i>{escape(ny_day)} \u00b7 New York accounting day</i>",
        snapshot=day_snapshot,
        insight=insight,
    )


def _counted_line(exited_at, position) -> str:
    """Which money the sale accounted for - all of it, or only its own part."""
    if exited_at is None:
        return ""
    sold = float((position or {}).get("exit_count") or 0)
    held = float((position or {}).get("contracts") or 0)
    if sold and held and sold < held:
        return surface.PART_COUNTED
    return surface.ALREADY_COUNTED


def result_message(*, side: str, ticker: str, winner: str, won: bool,
                   traded: bool, pnl: float | None, contracts: float = 0.0,
                   paid: float | None = None, fee: float | None = None,
                   exited_at: float | None = None,
                   called_side: str = "", qualified: bool | None = None,
                   position: dict | None = None,
                   snapshot=None, insight: str = "", priority=None,
                   margin: dict | None = None) -> str:
    """How a window closed. Three facts, never collapsed into one verdict.

        WIN / LOSS / CLOSED       the broker's realised P&L after fees
        Bought UP / DOWN          the side actually HELD
        prediction correct/wrong  the recorded signal's side vs the settlement

    THE MONEY WORD COMES FROM THE MONEY. Not from `held side == winner`: a
    profitable early exit banks money on a position whose side later loses, and
    a position held through settlement can still be under water after fees.

    THE SIDE COMES FROM THE POSITION. `predictions` is written at ALERT time
    and keyed on the window, so when the reference flips before the order fills
    the call and the position sit on opposite sides. Reporting the call as the
    position announced a paid-out win as a loss, twice, on real money.

    "CLOSED" IS NOT A MARKET OUTCOME. The market settles UP or DOWN, always,
    and that is its own line. Closed means the trade netted exactly zero.
    """
    call = called_side or side
    call_right = call == winner
    flat = pnl is not None and abs(pnl) < 0.005
    made_money = pnl is not None and pnl > 0

    if not traded:
        chip = surface.WON_PAPER if call_right else surface.LOST_PAPER
        headline = f"SIGNAL {'WON' if call_right else 'LOST'} · NOT TRADED"
    elif flat:
        # BEFORE THE EARLY-EXIT BRANCH, because a sale that netted exactly zero
        # reached zero through it. `SOLD EARLY - +$0.00` signs zero and offers
        # a verdict where there is none; the sale itself is still stated in the
        # body, which is where the route belongs.
        chip = surface.FLAT_MONEY
        headline = "CLOSED · $0.00 net"
    elif exited_at is not None:
        chip = surface.result_icon(pnl, True, call_right)
        headline = f"SOLD EARLY · {surface._signed_dollars(pnl or 0.0)}"
    else:
        chip = surface.result_icon(pnl, True, call_right)
        headline = (f"{'WIN' if made_money else 'LOSS'} · "
                    f"{surface._signed_dollars(pnl or 0.0)}")

    # WITH A RECONCILED POSITION the price belongs on the leg lines, because
    # there is more than one of them and averaging them away is how a
    # recovery add came to be invisible. Without one, the single price stays.
    legs = surface.position_block(position, side) if traded else []
    essentials = [
        f"{surface.side_icon(side)} "
        + ("Bought" if traded else "Signal:")
        + f" <b>{escape(side)}</b>"
        + ("" if legs else
           (f" at {surface.cents(paid)}" if paid is not None else "")),
    ]
    essentials.extend(legs)
    if exited_at is not None:
        # HOW MANY. An early exit need not close the whole position - two of
        # three contracts were sold here and the third ran to settlement, so
        # "Sold before expiry" on its own describes a flat position that was
        # not flat.
        sold = float((position or {}).get("exit_count") or 0)
        held = float((position or {}).get("contracts") or 0)
        detail = ""
        if sold and held and sold < held:
            detail = (f" \u00b7 {sold:g} of {held:g} \u00b7 "
                      f"{held - sold:g} ran to settlement")
        elif sold:
            detail = f" \u00b7 all {sold:g}"
        essentials.append(
            f"{surface.PRICE} Sold before expiry at "
            f"{surface.cents(exited_at)}{detail}"
        )
    essentials.append(
        f"\U0001f3c1 Market{' later' if exited_at is not None else ''} "
        f"settled <b>{escape(winner)}</b>"
    )
    # BY HOW MUCH, beside the fact that it settled. Whether a trade won was
    # always here; the MARGIN never was, and it is what distinguishes an 83c
    # favourite finishing comfortably from one that finished by a hair.
    essentials.extend(_margin_lines(margin))
    # THE CALL, always, on its own line and in its own words.
    essentials.append(
        f"{surface.PASS if call_right else surface.FAIL} {escape(call)} "
        f"prediction was {'correct' if call_right else 'wrong'}"
    )
    if traded and call != side:
        essentials.append(
            f"{surface.PACKAGE} <i>Signal called {escape(call)}; the position "
            f"held {escape(side)}</i>"
        )
    if not traded:
        if qualified is not None:
            essentials.append(
                "\U0001f4a4 No order was executed \u00b7 "
                + ("rule qualified it" if qualified else "rule declined it")
            )
        essentials.append(surface.NO_TRADE)
    elif pnl is not None and position:
        # THE RECONCILED TOTAL. `position_block` above has already printed the
        # cost across every leg, so this states the outcome against it rather
        # than repeating a figure computed from the base entry alone - which
        # is what made a three-contract position report a two-contract cost.
        outcome = ("$0.00 net" if flat
                   else (f"Profit ${pnl:,.2f}" if made_money
                         else f"Lost ${abs(pnl):,.2f}"))
        essentials.append(
            f"{surface.MONEY} Combined realised {outcome} "
            f"<i>(net of fees, all legs)</i>"
        )
    elif pnl is not None:
        # THE SAME COST THE FILL ANNOUNCED. The fill reports what left the
        # account - stake plus the charged entry fee - and this reported the
        # stake alone, so one trade showed $1.62 when it opened and $1.60 when
        # it closed and neither message said which of them the fee was in.
        cost = (contracts * paid + (fee or 0.0)
                if contracts and paid is not None else None)
        outcome = ("$0.00 net" if flat
                   else (f"Profit ${pnl:,.2f}" if made_money
                         else f"Lost ${abs(pnl):,.2f}"))
        if cost is not None:
            essentials.append(
                f"{surface.PRICE} Cost ${cost:,.2f} \u00b7 {outcome} "
                f"<i>(net of fees)</i>"
            )
        else:
            amount = ("$0.00 net" if flat
                      else f"<b>{surface._signed_dollars(pnl)}</b>")
            essentials.append(
                f"{surface.PRICE} Realised {amount} <i>(net of fees)</i>"
            )
    if exited_at is not None and pnl is not None:
        if made_money and not call_right:
            essentials.append(
                f"{surface.PASS} Trade remained profitable because it exited "
                f"early"
            )
        elif not made_money and call_right:
            essentials.append(
                f"{surface.FAIL} Trade still lost because it exited below cost"
            )

    return surface.compose(
        header=f"{chip} <b>{escape(headline)}</b>",
        ticker=ticker,
        essentials=essentials,
        checks=[],
        status=_counted_line(exited_at, position),
        snapshot=snapshot,
        priority=priority,
        insight=insight,
    )


def learning_update(*, markets: int, confidence_changes: int,
                    entry_changes: int, detail: str = "",
                    series: str = "") -> str:
    """The learning notification. Plain language, no policy identifiers.

    `series` names the instrument that learned, for the label - every instance
    learns separately and all of them post here.

    Policy ids, fingerprints, retired methods and training splits stay in the
    log and in `/learning` details. This message answers one question: did the
    rules the bot trades by change?

    CONFIDENCE AND ENTRY RULES ARE SEPARATE LINES because they are separate
    permissions. A confidence adjustment re-rates the word on the header and
    can do nothing else; an entry-rule adjustment changes what is ordered.
    Reporting them as one count - which `learning_activated` effectively did
    by leading with an arm total - makes a label-only change read as a change
    to how money is spent.
    """
    lines = [
        surface.labelled(f"{surface.LEARNING} <b>LEARNING UPDATE</b>", series),
        f"\U0001f4da Reviewed: {markets:,} historical markets",
        f"{surface.TARGET} Confidence: "
        + (f"{confidence_changes} setup adjustment"
           f"{'s' if confidence_changes != 1 else ''} enabled"
           if confidence_changes else "Unchanged"),
        "\u2699\ufe0f Entry rules: "
        + (f"{entry_changes} adjustment"
           f"{'s' if entry_changes != 1 else ''} enabled"
           if entry_changes else "Unchanged"),
    ]
    if detail:
        lines.append(detail)
    lines.append("\U0001f504 Automatic learning continues.")
    return "\n".join(lines)


def _duration(seconds: float) -> str:
    """"2h 05m", "18m". Hours matter; seconds never do at this scale."""
    minutes = int(seconds // 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m" if hours else f"{minutes}m"


def market_gap_message(gap_s: float, series: str = "") -> str:
    """The exchange is listing no market, and has not been for a while.

    THE SILENT STOP, NAMED. A window boundary is seconds. On 2026-09-24 Kalshi
    listed nothing for TWO HOURS while the service was entirely healthy
    throughout - polling, recording, reconciling - so the only thing the
    operator could see was that the messages had stopped. The absence of
    alerts is not an alert, and this system's worst failure mode is silence.

    It says what is NOT wrong as prominently as what is, because "no market"
    read on a phone at 3am looks exactly like the bot having died.
    """
    return "\n".join([
        surface.labelled(f"{surface.WARN} <b>NO MARKET AT THE EXCHANGE</b>",
                         series),
        f"{surface.WAITING} Kalshi has listed no 15-minute market for "
        f"<b>{_duration(gap_s)}</b>",
        "",
        "<i>The service is running normally - polling, recording and "
        "reconciling. There is simply nothing to trade, so no signals and no "
        "alerts will arrive until a market is listed again.</i>",
        "<i>Any open position is unaffected: it lives at Kalshi and settles "
        "at expiry.</i>",
    ])


def market_back_message(gap_s: float, ticker: str) -> str:
    """The all-clear. A gap that gets reported must also get closed."""
    return "\n".join([
        surface.labelled(f"{surface.PASS} <b>MARKETS ARE BACK</b>", ticker),
        f"{surface.TARGET} Trading resumed on <code>{escape(ticker)}</code>",
        f"<i>The exchange listed nothing for {_duration(gap_s)}.</i>",
    ])


def combo_recovery_message(*, legs, market: str, result, contracts: float,
                           remaining: int, snapshot=None) -> str:
    """The recovery, placed as a combo at base size (operator, 2026-09-27).

    Says what was bought, both legs and the price check - the quote against
    the legs' product it had to beat - and what it pays or loses. When the
    outcome is unknown it says so first: the money may have moved, nothing
    else was sent, and the broker is the place to look.
    """
    trigger, partner = legs[0], legs[1]

    def leg_line(leg):
        arrow = surface.UP if leg.side == "UP" else surface.DOWN
        return (f"{arrow} <b>{escape(leg.asset)} {leg.side}</b> at "
                f"{leg.ask:.2f} · <code>{escape(leg.ticker)}</code>")

    price = result.price or result.product
    cost = contracts * price
    if result.bought:
        header = (f"{surface.RECOVERY} <b>RECOVERY COMBO BOUGHT</b> · "
                  f"{contracts:g} at {price:.2f}")
        status = (f"{surface.PACKAGE} Held to settlement · wins "
                  f"<b>${contracts * (1 - price) - (result.fee or 0):,.2f}</b> "
                  f"if BOTH legs win, loses <b>${cost + (result.fee or 0):,.2f}</b> "
                  f"otherwise <i>(net of fees)</i>")
    else:
        header = (f"{surface.WARN} <b>RECOVERY COMBO - OUTCOME UNKNOWN</b>")
        status = (f"{surface.WARN} {escape(result.reason or 'Outcome not confirmed')}. "
                  f"Treated as held at {price:.2f} so nothing else is bought "
                  f"this window; the broker's fills settle it within minutes "
                  f"- <b>{escape(market)}</b>.")
    essentials = [
        leg_line(trigger),
        leg_line(partner),
        f"{surface.PRICE} {'Bought' if result.bought else 'Quoted'} at "
        f"{price:.4f} · cap {result.limit or 0:.4f} "
        f"(the cheaper leg) · legs multiplied {result.product:.4f} · base "
        f"size, nothing upsized",
        f"{surface.CLOCK} {remaining // 60}m {remaining % 60:02d}s to settle",
    ]
    return surface.compose(
        header=header, ticker=trigger.ticker, essentials=essentials,
        checks=[], status=status, snapshot=snapshot,
    )


def combo_result_message(*, market: str, legs: list, contracts: float,
                         price: float, pnl: float | None,
                         snapshot=None) -> str:
    """A recovery combo settled. The money is the broker's own figure."""
    trigger = legs[0] if legs else {}
    names = " + ".join(f"{l.get('asset')} {l.get('side')}" for l in legs) or market
    if pnl is None:
        head = f"{surface.WAITING} <b>RECOVERY COMBO SETTLED</b> · {escape(names)}"
        money = "Result not reconciled yet - it will show in the money line."
    elif pnl > 0:
        head = f"{surface.WON_MONEY} <b>RECOVERY COMBO WON</b> · {escape(names)}"
        money = f"Realised <b>{surface._signed_dollars(pnl)}</b> <i>(net of fees)</i>"
    else:
        head = f"{surface.LOST_MONEY} <b>RECOVERY COMBO LOST</b> · {escape(names)}"
        money = f"Realised <b>{surface._signed_dollars(pnl)}</b> <i>(net of fees)</i>"
    return surface.compose(
        header=head, ticker=str(trigger.get("ticker") or ""),
        essentials=[f"{surface.PRICE} {contracts:g} at {price:.2f} · "
                    f"<code>{escape(market)}</code>", money],
        checks=[], status="", snapshot=snapshot,
    )


def hypotheses_message(*, asset: str, result: dict, series: str = "") -> str:
    """The local model's ideas that SURVIVED testing - candidates, not rules.

    Sent only when at least one survived. Every number is the statistics',
    never the model's: the slice's advantage over the rest, day-clustered,
    Benjamini-Hochberg across the batch.
    """
    lines = [surface.labelled(
        f"{surface.LEARNING} <b>LOCAL MODEL IDEAS</b>", series)]
    lines.append(
        f"{len(result.get('survivors') or [])} of {result.get('proposed', 0)} "
        f"proposal(s) survived testing over {result.get('windows', 0)} windows "
        f"and {result.get('days', 0)} days:")
    for s in (result.get("survivors") or [])[:5]:
        where = " and ".join(f"{c} {o} {v}" for c, o, v in s.get("terms") or [])
        diff = float(s.get("diff") or 0.0)
        # A two-sided test: a slice can survive for doing WORSE than the rest,
        # which is a condition to avoid, and it is said that way round.
        verb = (f"beats the rest by {diff:.3f}/contract" if diff >= 0 else
                f"trails the rest by {-diff:.3f}/contract - one to avoid")
        lines.append(
            f"• <b>{escape(str(s.get('name')))}</b>: {escape(where)} - "
            f"{verb} (n={s.get('n')}, {s.get('days')} days, "
            f"p={float(s.get('p') or 0):.3f})")
    lines.append("<i>Candidates only: nothing changes an order until one "
                 "clears the promotion bar. The model saw a summary of the "
                 "same data, so treat these as leads.</i>")
    return "\n".join(lines)
