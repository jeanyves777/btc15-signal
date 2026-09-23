"""ONE format for every trading and learning message.

Before this, forty-one builders each chose their own icons, their own field
order and their own typography - two rules spelled the same gate `75¢ · needs
70–93¢` and `75c - needs 70-93c`, the same band-hold number was worded four
different ways, and the fill report and the settlement recap disagreed about
whether a figure was net. A reader cannot learn a surface that changes shape
between messages, so they stop reading it, and the one message that mattered
goes past with the rest.

THE ORDER IS FIXED, and it is fixed because it is a reading order: what
happened, to which market, at what price, on what evidence, what the system did
about it, and only then the money and one rotating extra.

    1  direction and event
    2  market ticker, in full, for traceability
    3  the essential price, position or result
    4  the checks - on signals and on executions, always
    5  what actually happened, or what happens next
    6  ---- divider ----
    7  the money footer, from ONE reconciled snapshot
    8  one rotating insight

DIRECTION AND OUTCOME ARE DIFFERENT AXES. A profitable DOWN trade is 🔴⬇️ for
the direction it took and ✅💰 for what it made. Collapsing them is how a
losing UP trade and a winning DOWN trade end up wearing the same chip.

CONFIDENCE IS NOT ELIGIBILITY, and that is not the bug. A refused signal can
legitimately read MEDIUM or HIGH: the confidence word describes how good the
setup looks, the checks describe whether it may be traded, and one failed gate
does not make a good-looking setup a bad-looking one. The layout keeps them on
separate lines because they are separate facts, not because one should suppress
the other.

THE ACTUAL DEFECT IS TWO CALCULATIONS. The header scores `confidence_label`
over the passing `check_facts`, while the DETAILS body scores a different four
terms of its own - a Binance-scale distance band that is never true on BRTI and
a model probability that does not exist on this path - so one message says HIGH
and its own detail says `base MEDIUM (2/4)`. One Kalshi-native confidence
result is computed once and rendered everywhere.

AND THIS MODULE DOES NOT CHANGE THE SCORE. The reference check passes by
construction, which does flatter the count - but removing it from scoring
changes the model, so it belongs in a versioned learning release and not in a
formatter. Shortening how that check READS (below) is presentation and leaves
`agreeing` exactly as it was.

MONEY IS NET AND IT COMES FROM ONE SNAPSHOT. Every figure on a message is taken
from a single `MoneySnapshot` captured once, because two snapshots taken
milliseconds apart are two different instants and the reader cannot tell which
line belongs to which.
"""

from __future__ import annotations

from html import escape

# ------------------------------------------------------------------ icons
#
# The whole vocabulary, in one place. A message may not invent one.
UP = "\U0001f7e2⬆️"          # 🟢⬆️
DOWN = "\U0001f534⬇️"        # 🔴⬇️
WON_MONEY = "✅\U0001f4b0"         # ✅💰 executed and profitable
LOST_MONEY = "❌\U0001f4b8"        # ❌💸 executed and losing
FLAT_MONEY = "⚪"                  # ⚪ executed, break-even
WON_PAPER = "✅\U0001f4c4"         # ✅📄 right call, not traded
LOST_PAPER = "❌\U0001f4c4"        # ❌📄 wrong call, not traded
WAITING = "⏳"                     # ⏳
LEARNING = "\U0001f9e0"                # 🧠
RECOVERY = "\U0001f527"                # 🔧

PASS = "✅"
FAIL = "❌"
PRICE = "\U0001f4b5"
CLOCK = "⏱"
TARGET = "\U0001f3af"
ROBOT = "\U0001f916"
MONEY = "\U0001f4b0"
TODAY = "\U0001f4c5"
PACKAGE = "\U0001f4e6"
WARN = "⚠️"
# THE EXIT EVENT, which is not a direction. `auto_exit` used the DOWN
# chip as its headline while the body said "Held UP", so one message
# carried two contradictory direction chips - the exact collapse of the
# two axes this vocabulary exists to prevent.
EXIT = "🚪"

DIVIDER = "━" * 18                # ━ × 18, one divider, before the money


def side_icon(side: str) -> str:
    """The DIRECTION chip. Never carries an outcome."""
    return UP if str(side).upper() == "UP" else DOWN


def result_icon(pnl: float | None, traded: bool, won: bool | None) -> str:
    """The OUTCOME chip. Never carries a direction.

    An untraded signal is scored on the CALL (📄); a traded one on the MONEY
    (💰/💸). They come apart constantly - a DOWN position sold at 100c on a
    market that later settles UP made money on a wrong call - and this is the
    axis that decides which chip is correct.
    """
    if not traded:
        return WON_PAPER if won else LOST_PAPER
    if pnl is None:
        return FLAT_MONEY
    if abs(pnl) < 0.005:
        return FLAT_MONEY
    return WON_MONEY if pnl > 0 else LOST_MONEY


# ------------------------------------------------------------------ checks


# The DISPLAY name for each gate. The rules name their facts after the feature
# they read - `BRTI distance`, `Decision ask` - which is the right name in a
# log and the wrong one on a phone. The internal names stay in DETAILS and in
# the archive; the face of the message says what the number IS.
CHECK_NAMES = {
    "decision ask": "Price",
    "brti distance": "Distance",
    "distance": "Distance",
    "brti momentum": "Momentum",
    "momentum": "Momentum",
    "model": "Model",
    "level": "Level",
}


def _typography(text: str) -> str:
    """One spelling for the units. PRESENTATION ONLY - no number changes.

    The two rules disagree: one writes `75¢ · needs 70–93¢`, the other
    `75c - needs 70-93c`. Same gate, same arithmetic, two appearances, and a
    reader cannot tell whether they are looking at the same check. This
    normalises the glyphs and the connective; it never touches a value, and it
    only rewrites a unit that is attached to a digit.
    """
    import re

    text = re.sub(r"(?<=\d)c(?![a-z])", "¢", text)
    text = re.sub(r"(?<=\d)x(?![a-z])", "×", text)
    # ONLY A BARE NUMERIC RANGE. The previous rule was `(?<=\d)-(?=\d)`,
    # which also rewrote the hyphen inside `KXBTC15M-26SEP231400-00` -
    # corrupting the one field on the message that exists to be copied into a
    # search box. A range must stand alone rather than sit inside a longer
    # alphanumeric token, and a date like `2026-09-23` is left alone for the
    # same reason.
    text = re.sub(
        r"(?<![\w-])(\d+)-(\d+)(?![\w-])",
        lambda m: f"{m.group(1)}\u2013{m.group(2)}",
        text,
    )
    text = text.replace(" - needs ", " · minimum ")
    text = text.replace(" · needs ", " · minimum ")
    text = text.replace(" within ", " · range ")
    return text


def display_name(name: str) -> str:
    return CHECK_NAMES.get(str(name).strip().lower(), str(name))


def check_line(fact: dict) -> str:
    """One check, with its measured value AND what was required.

    A refusal that lists only the gate's NAME says a setup was rejected without
    saying how close it came - the operator went to the database to find two
    refusals that missed by fractions. Both rules' `check_facts` already carry
    the value and the threshold in `pass_text`/`fail_text`; this renders them
    and adds nothing, so the word in the header and the ticks beneath it cannot
    disagree.
    """
    tick = PASS if fact.get("passed") else FAIL
    text = fact.get("pass_text") if fact.get("passed") else fact.get("fail_text")
    return (f"{tick} {escape(display_name(fact.get('name')))}: "
            f"{escape(_typography(str(text)))}")


def reference_line(fact: dict) -> str:
    """The reference check, shortened once freshness has been verified.

    Its pass text is a value-and-sample statement - `Kalshi BRTI, 900 pts,
    $85,946.05 vs target $85,906.05` - which is detail, not a check result. The
    sample count and the raw values belong in DETAILS. What the reader needs on
    the face of the message is that the reference was fresh.
    """
    if fact.get("passed"):
        return f"{PASS} Kalshi reference fresh"
    return f"{FAIL} Kalshi reference: {escape(str(fact.get('fail_text')))}"


def checks_block(facts: list[dict], band_hold: tuple[int, int] | None = None
                 ) -> list[str]:
    """Every check, passing and failing, plus band-hold progress.

    ALWAYS RENDERED - on signals, on executions, on refusals. They are the
    decision. Band-hold is appended because it is order ELIGIBILITY rather than
    a qualification check, and it is the condition most often standing between
    a qualified signal and an order: a message that omits it cannot explain why
    nothing was bought.
    """
    lines = []
    for fact in facts or []:
        name = str(fact.get("name", ""))
        lines.append(
            reference_line(fact) if name.lower().startswith("reference")
            else check_line(fact)
        )
    if band_hold is not None:
        held, need = band_hold
        tick = PASS if held >= need else WAITING
        lines.append(f"{tick} Band held: {held}s of {need}s")
    return lines


def clipped(text: str, limit: int = 100) -> str:
    """Shorten on a word boundary, and say that it was shortened.

    A hard slice stops mid-word, so the reader cannot tell a truncated line
    from a corrupted record - and the lines this shortens are withdrawals and
    failures, which are the ones that have to be trustworthy.
    """
    text = str(text).strip()
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0].rstrip(" ,;:-(")
    return f"{cut or text[:limit]}\u2026"


def checks_summary(facts: list[dict]) -> str:
    passed = sum(1 for f in facts or [] if f.get("passed"))
    return f"{passed}/{len(facts or [])}"


# ------------------------------------------------------------- money footer


def cents(price: float) -> str:
    """A contract price, without rounding away a price that was really paid.

    `0.997` printed as `100¢` claims a fill at a price the book does not offer.
    Whole cents render whole; anything finer keeps the decimal it traded at.
    """
    value = price * 100
    if abs(value - round(value)) < 0.05:
        return f"{value:.0f}\u00a2"
    return f"{value:.1f}\u00a2"


def _signed_dollars(amount: float) -> str:
    """`+$4.91` / `−$0.85`. The sign leads, the currency is never implied."""
    sign = "+" if amount >= 0 else "−"
    return f"{sign}${abs(amount):,.2f}"


def money_footer(snapshot) -> list[str]:
    """The permanent footer, from ONE snapshot. Executed trades only.

    `markets`/`winners` here are SETTLED MARKETS WE HELD A POSITION IN, read
    from the broker-backed ledger. Signal statistics are a different
    population and never stand in for these - the rotating insight is where a
    signal figure may appear, clearly labelled as one.

    The daily line is labelled simply "Today". It is the exchange's own day
    boundary and saying so twice on every message earned nothing.

    TODAY IS REALISED, AND ONLY REALISED. It used to print
    `realised + open_mark` under that one word, so on a poll where a position
    had just marked to zero the dollars moved while the count did not - and a
    recap announcing a loss sat above a total that had absorbed the mark but
    not the settlement. An open position is a different quantity from closed
    money and gets its own line, labelled as the mark it is.

    `pending` is set when the trade the message is ABOUT has not come back
    from the broker yet. The totals are then correct as of the last
    reconciliation and are SAID to be, rather than silently excluding the
    trade printed directly above them.
    """
    if snapshot is None:
        return [f"{MONEY} <b>Live: nothing settled yet</b>"]
    lifetime = getattr(snapshot, "lifetime", None)
    lines = []
    if lifetime is not None and getattr(lifetime, "markets", 0):
        lines.append(
            f"{MONEY} <b>{escape(lifetime.label())}: "
            f"{_signed_dollars(lifetime.dollars)}</b>"
        )
        lines.append(
            f"   {lifetime.markets} closed · "
            f"{lifetime.winners}W–{lifetime.markets - lifetime.winners}L"
        )
    else:
        lines.append(f"{MONEY} <b>Live: nothing settled yet</b>")
    lines.append(
        f"{TODAY} <b>Today: {_signed_dollars(snapshot.realised)}</b> · "
        f"{snapshot.markets} closed · "
        f"{snapshot.winners}W–{snapshot.losers}L "
        f"<i>(realised)</i>"
    )
    # AN OPEN POSITION IS NOT CLOSED MONEY. It used to be folded
    # into the same figure, so the total moved when a position
    # marked without any market having settled.
    open_mark = getattr(snapshot, "open_mark", 0.0) or 0.0
    if abs(open_mark) >= 0.005:
        lines.append(
            f"{PACKAGE} Open position: "
            f"{_signed_dollars(open_mark)} "
            f"<i>(marked at the bid, not yet realised)</i>"
        )
    # AND A TOTAL THAT DOES NOT YET INCLUDE THE TRADE ABOVE IT
    # says so, rather than being quietly one settlement behind
    # the headline it sits under.
    if getattr(snapshot, "pending", False):
        lines.append(
            f"{WAITING} <i>Totals are as of the last broker "
            f"reconciliation; this market is not in them yet.</i>"
        )
    return lines


# ---------------------------------------------------------------- insights
#
# One extra line, rotating. It is PRESENTATION: it never changes a decision and
# it never invents a figure. A variant with no data is skipped rather than
# filled in.
INSIGHTS = ("signals", "paper", "sessions", "qualified")


def insight_line(kind: str, data: dict | None) -> str:
    """One rotating insight, or "" when the data is not there.

    Skipping is deliberate. A rotation that must always produce a line ends up
    producing commentary, and commentary about a number nobody measured is the
    thing this system is least allowed to do.
    """
    if not data:
        return ""
    if kind == "signals":
        settled, wins = data.get("settled", 0), data.get("wins", 0)
        if not settled:
            return ""
        return (f"{TARGET} Signals: {wins / settled:.0%} · "
                f"{wins}W–{settled - wins}L over {settled} settled")
    if kind == "paper":
        settled = data.get("settled", 0)
        if not settled:
            return ""
        return (f"\U0001f4c4 Paper: {_signed_dollars(data.get('net', 0.0))} "
                f"on {escape(str(data.get('basis', '1 contract')))}")
    if kind == "sessions":
        text = data.get("line") or ""
        return f"\U0001f30d Sessions: {escape(text)}" if text else ""
    if kind == "qualified":
        settled, wins = data.get("settled", 0), data.get("wins", 0)
        if not settled:
            return ""
        return (f"\U0001f4cc Qualified: {wins / settled:.0%} · "
                f"{wins}/{settled} · paper "
                f"{_signed_dollars(data.get('net', 0.0))}")
    return ""


# ------------------------------------------------------------ priority rows
#
# These outrank the rotating insight and are never displaced by it.


def priority_lines(*, recovery=None, pending: str = "", partial: str = "",
                   slippage: str = "", failure: str = "") -> list[str]:
    """Anything the operator must see regardless of what is rotating."""
    lines = []
    if failure:
        lines.append(f"{WARN} <b>{escape(_typography(failure))}</b>")
    if partial:
        lines.append(f"{PACKAGE} {escape(_typography(partial))}")
    if pending:
        lines.append(f"{WAITING} {escape(_typography(pending))}")
    if slippage:
        lines.append(f"{WARN} {escape(_typography(slippage))}")
    if recovery:
        lines.append(recovery_line(recovery))
    return [line for line in lines if line]


def recovery_line(state) -> str:
    """Recovery in ONE line: what is owed, and whether size may rise.

    Two facts, because either alone misleads. A deficit with no word on sizing
    reads as "still broken"; a sizing note with no deficit reads as "fixed".
    """
    if state is None or not getattr(state, "owes", False):
        return ""
    if getattr(state, "base_only", False):
        # THE SAME WORDS THE TRANSITION USED. "Recovery: $0.16 outstanding"
        # standing under a "RECOVERY SIZE ENDED" sent minutes earlier reads as
        # a second, contradicting subsystem rather than the same fact restated,
        # and the operator read it that way. Echoing "size ended" makes the
        # standing line the continuation of the announcement it follows.
        return (f"{RECOVERY} Recovery size ended · "
                f"${state.deficit:,.2f} still outstanding · base size only")
    return (f"{RECOVERY} Recovery: ${state.deficit:,.2f} outstanding · "
            f"extra sizing allowed")


# ------------------------------------------------------------ the assembler


def compose(*, header: str, ticker: str, essentials: list[str],
            checks: list[str], status: str, snapshot=None,
            priority: list[str] | None = None, insight: str = "") -> str:
    """Assemble one message in the fixed order. The only assembler.

    Every trading message goes through here so the order, the spacing and the
    single divider cannot drift apart between builders.
    """
    lines = [header]
    if ticker:
        lines.append(f"<code>{escape(ticker)}</code>")
    lines.extend(line for line in (essentials or []) if line)
    lines.extend(line for line in (checks or []) if line)
    lines.extend(line for line in (priority or []) if line)
    if status:
        lines.append(status)
    lines.append(DIVIDER)
    lines.extend(money_footer(snapshot))
    if insight:
        lines.append(insight)
    return "\n".join(lines)


# ----------------------------------------------------------- money wording


def position_block(position: dict | None, side: str = "") -> list[str]:
    """Base and recovery add as separate lines, then one total.

    THE LEGS ARE NEVER MERGED into an average. Two contracts at 85c and one
    at 83c is not three at 84.33c: the operator sized the second entry on a
    rule with its own conditions, and averaging them away hides whether that
    rule paid for itself.

    A PENDING ORDER IS NOT A POSITION. An add that is resting, cancelled or
    expired is shown as such and contributes nothing to the quantity or the
    cost - the state is printed so it cannot simply vanish from the recap,
    which is how a fill came to be invisible in the first place.
    """
    if not position:
        return []
    lines = []
    for leg in position.get("legs", ()):
        count, price = leg.get("count") or 0.0, leg.get("price")
        if leg["kind"] == "base":
            if count <= 0:
                continue
            lines.append(
                f"{PACKAGE} Base: {count:g} @ {cents(price)}"
                + ("" if leg.get("confirmed") else " <i>(unconfirmed)</i>")
            )
            continue
        state = leg.get("state")
        if state == "filled":
            lines.append(
                f"{RECOVERY} Recovery add: {count:g} @ {cents(price)}"
            )
        elif state == "skipped":
            # NEVER PLACED, so it never rested and there is no price to quote.
            # Naming one here is what produced "Recovery add: pending - rested
            # at 82c" for an add the runner had declined: the limit price on a
            # skipped row is the price it WOULD have rested at, and printing it
            # beside a state word turned a refusal into a working order. The
            # reason is the fact the operator is actually looking for.
            why = _typography(str(leg.get("reason") or "conditions not met"))
            lines.append(
                f"{RECOVERY} No recovery add \u00b7 <i>{escape(clipped(why, 90))}</i>"
            )
        else:
            # NAMED, NOT DROPPED. A reader who sees nothing cannot tell an add
            # that never happened from one the message forgot.
            detail = f" \u00b7 rested at {cents(price)}" if price else ""
            lines.append(
                f"{RECOVERY} Recovery add: <b>{escape(state)}</b>{detail}"
            )
    contracts = position.get("contracts") or 0.0
    total = position.get("total_cost")
    if contracts and total is not None:
        fees = position.get("fees") or 0.0
        charged = (f"${fees:,.2f}" if fees >= 0.005 else "under 1\u00a2")
        lines.append(
            f"{PRICE} Total cost ${total:,.2f} for {contracts:g} "
            f"contract{'s' if contracts != 1 else ''} "
            f"<i>(incl. {charged} fees)</i>"
        )
    return lines


def entry_cost(contracts: float, paid: float, fee: float | None) -> str:
    """What was actually spent, and whether the figure includes fees.

    `order_filled` used to print `Maximum profit $X` computed as
    `contracts - cost` with no fee term, while the settlement recap for the
    same trade subtracted the Kalshi fee - so the fill promised $0.20 and the
    recap paid $0.19. Every money figure on this surface is net, and this one
    says so rather than leaving the reader to discover it at settlement.
    """
    cost = contracts * paid
    if fee is None:
        return (f"{PRICE} Cost ${cost:,.2f} for {contracts:g} "
                f"contract{'s' if contracts != 1 else ''} "
                f"<i>(before fees)</i>")
    # TWO DECIMALS, like every other dollar figure on this surface. Four
    # decimals printed a two-cent fee as `$0.0200`, which reads as a precision
    # the account statement does not have. A fee that rounds to zero is shown
    # as the sub-cent amount it is rather than as `$0.00`.
    charged = (f"${fee:,.2f}" if fee >= 0.005 else "under 1\u00a2")
    return (f"{PRICE} Cost ${cost + fee:,.2f} for {contracts:g} "
            f"contract{'s' if contracts != 1 else ''} "
            f"<i>(incl. {charged} fees)</i>")


def max_net_profit(contracts: float, paid: float, fee: float | None) -> str:
    """The most this can make, NET, from the real quantity and fill price.

    Computed from the unrounded fill price and the charged fee, never from the
    rounded cents the line above displays.

    WHERE THE FEE HAS NOT LANDED YET the figure is labelled ESTIMATED rather
    than printed bare. The broker's fee arrives with the fill record and the
    message often goes out first; an unlabelled estimate that later disagrees
    with the settlement is the same defect as the gross figure this replaces,
    just smaller.
    """
    gross = contracts * (1.0 - paid)
    if fee is None:
        from .validation import kalshi_fee_charged

        estimated = gross - kalshi_fee_charged(paid, contracts)
        return (f"{TARGET} Maximum net profit ~${estimated:,.2f} "
                f"<i>(estimated · fee not yet reported)</i>")
    return f"{TARGET} Maximum net profit ${gross - fee:,.2f}"


NO_TRADE = f"{PRICE} Not traded \u00b7 realised P&amp;L $0.00"
ALREADY_COUNTED = "\U0001f9fe <i>Already counted at the sale.</i>"
# A PARTIAL EXIT IS NOT counted at the sale. Two of three
# contracts were sold here and the third ran to settlement, so
# part of the money arrived hours after the sale this line says
# accounted for all of it.
PART_COUNTED = ("\U0001f9fe" + " <i>The sale was counted when it happened; "
                "the rest settled at expiry.</i>")
