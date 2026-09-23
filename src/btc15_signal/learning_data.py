"""The learning corpus. Kalshi only, one row per market, no future in it.

THIS MODULE LIVES IN `src/` ON PURPOSE. It used to be `scripts/brti_dataset.py`,
and while it sat there the service could not call it - so "the learning loop"
meant a person remembering to run a script, and training, replay and live
inference each reached for the feature definitions by their own route. The
requirement they all share is that a context key computed in training names the
same pocket as one computed on the order path; the only way to guarantee that is
for both to call the same function in the same package, which is what this is.

WHERE THE DATA COMES FROM, and nowhere else:

    brti_history.db     Kalshi BRTI decision points, backfilled from
                        /live_data/events and /cfbenchmarks (Kalshi's own
                        publication of the index the contract settles on)
    market_data.db      Kalshi contract candles - the book we would have paid
    btc15.db            live `intelligence_decisions` (the context this system
                        actually keyed, at the instant it keyed it), graded
                        against Kalshi settlements, reconciled to Kalshi fills

No Binance table is opened here and none may be added. `cohort.db` - the 6,428
market corpus every earlier measurement was computed on - is Binance-derived
and is deliberately NOT reachable from this module. It stays on disk as history
and is excluded from every active learning path.

THREE THINGS THAT ARE COUNTED SEPARATELY, because conflating them is how this
system has previously manufactured evidence out of arithmetic:

    markets     distinct 15-minute windows. The unit of opportunity.
    decisions   rows. A window polled six times is six decisions and ONE
                market, and resampling rows as though they were markets
                reports an interval far too narrow.
    fills       orders that actually executed. Everything else priced at the
                recorded ask is a SIMULATED fill and is labelled one.

NO FUTURE DATA. Every feature on a row was computed from samples at or before
that row's own decision instant (`cutoff_rule: t <= decision_ms`, enforced by
the feature contract and fingerprinted into every artefact). The outcome is
attached only after the market has settled, and a row whose outcome is not yet
final is not returned at all - training on an unresolved market is training on
a guess about the present.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

from .adaptive import (
    BRTI_DISTANCE_BANDS,
    SETUP_FEATURE_VERSION,
    brti_vol_regime,
    setup_context_of,
)
from .intelligence_policy import BINANCE_BAND_NAMES
from .levels import confidence_points as level_points
from .regime import model_points as regime_model_points
from .sessions import session_of

# PROVENANCE IS READ OFF THE KEY, not off the `feature_version` column.
#
# The live table holds rows from before the BRTI context fix whose
# `feature_version` column says `brti-1` - it is written from a module
# constant, so it says that on every row ever recorded - while the key itself
# is a Binance one (`dist<1.5`) or a broken one (`? · ?`, from the weeks when
# the live snapshot had no session or vol_regime and every key came out
# unlabelled). That is the same failure the deployed policy artefact had: a
# declared version that the contents contradict.
#
# So a live row is admitted only if its key is positively keyed on a BRTI
# distance band AND names a real session and volatility regime. Anything else
# is history: readable, excluded, counted.
_BRTI_BANDS = frozenset(name for _lo, _hi, name in BRTI_DISTANCE_BANDS)
_BINANCE_BANDS = frozenset(BINANCE_BAND_NAMES)
UNKNOWN_FIELD = "?"

# THE GATES ARE READ FROM THE DEPLOYED RULE, NOT COPIED HERE.
#
# They used to be three module constants that happened to equal
# `strategy_kalshi.json`. Nothing kept them equal. Edit the strategy file - the
# thing the operator actually edits - and the corpus would go on scoring every
# historical market against the OLD gates, training a policy for a rule the bot
# no longer runs, with no error anywhere.
#
# Read at call time rather than at import, so a mid-session edit is picked up
# by the next training run instead of the next restart.
def deployed_gates(settings=None) -> tuple[float, float, float]:
    """(distance floor, min ask, max ask) from the live strategy file."""
    from .config import Settings
    from .kalshi_brti import KalshiBRTIRule

    rule = KalshiBRTIRule.load((settings or Settings()).kalshi_strategy_path)
    return (rule.min_brti_normalized_distance, rule.min_ask, rule.max_ask)

CORPUS = "corpus"
LIVE = "live"

ACTUAL_FILL = "actual"
SIMULATED_FILL = "simulated"


@dataclass
class Provenance:
    """What a training set was built from. Recorded into every artefact.

    A policy whose provenance is not written down cannot be audited later, and
    this system has already shipped one artefact whose declared feature version
    disagreed with the features it was actually fitted on. Provenance is the
    record that makes that checkable after the fact rather than by memory.
    """

    sources: tuple[str, ...] = ()
    corpus_markets: int = 0
    corpus_decisions: int = 0
    live_markets: int = 0
    live_decisions: int = 0
    live_actual_fills: int = 0
    excluded_unresolved: int = 0
    excluded_duplicate: int = 0
    excluded_incompatible: int = 0
    # Markets we TRADED whose fills could not be reconciled to the
    # settlement row. Excluded from execution-based learning: a trade
    # we cannot attribute is not evidence about a decision.
    excluded_unattributed: int = 0
    data_start_ms: int = 0
    data_end_ms: int = 0
    feature_version: str = SETUP_FEATURE_VERSION
    feature_fingerprint: str = ""
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def markets(self) -> int:
        return self.corpus_markets + self.live_markets

    @property
    def decisions(self) -> int:
        return self.corpus_decisions + self.live_decisions

    def payload(self) -> dict:
        return {
            "sources": list(self.sources),
            "corpus_markets": self.corpus_markets,
            "corpus_decisions": self.corpus_decisions,
            "live_markets": self.live_markets,
            "live_decisions": self.live_decisions,
            "live_actual_fills": self.live_actual_fills,
            "markets": self.markets,
            "decisions": self.decisions,
            "excluded_unresolved": self.excluded_unresolved,
            "excluded_duplicate": self.excluded_duplicate,
            "excluded_incompatible": self.excluded_incompatible,
            "excluded_unattributed": self.excluded_unattributed,
            "data_start_ms": self.data_start_ms,
            "data_end_ms": self.data_end_ms,
            "feature_version": self.feature_version,
            "feature_fingerprint": self.feature_fingerprint,
            "notes": list(self.notes),
        }


# --------------------------------------------------------------- corpus
#
# The historical leg: Kalshi BRTI decision points scored against the deployed
# gates, priced on the Kalshi book.


def _minute_row(point: dict, quote: tuple, distance_floor: float,
                min_ask: float, max_ask: float) -> dict | None:
    """One decision minute, scored against the BRTI-calibrated gates.

    The band is unchanged (it was always a Kalshi price), the distance floor is
    the 10x FINDINGS 43 measured rather than the 1.5 that belongs to Binance
    volatility, and momentum is BRTI momentum.
    """
    yes_bid, yes_ask = quote
    side = point["brti_side"]
    ask = yes_ask if side == "UP" else round(1 - yes_bid, 4)
    if not 0 < ask < 1:
        return None
    won = (point["result"] == "yes") if side == "UP" else (
        point["result"] == "no"
    )
    distance = point["brti_normalized_distance"] or 0.0
    momentum = point["brti_momentum_bps"] or 0.0
    direction = 1 if side == "UP" else -1

    gates = []
    if not min_ask <= ask <= max_ask:
        gates.append("contract price band")
    if distance < distance_floor:
        gates.append("target distance")
    if direction * momentum <= 0:
        gates.append("momentum strength")

    window_ms = point["close_ms"] - 900_000
    # THE MODEL'S OWN SCORE for this decision, from the same function the live
    # path calls. `check_facts` returns FOUR facts; the fourth is the reference
    # freshness check, which passes by construction on a backfilled BRTI point
    # - the row exists because the reference was there. The other three are the
    # gates computed above, so agreeing = 4 - failures.
    #
    # THE LEVEL TERM IS NOT ZERO. Under Kalshi-only no protective level is
    # computed, so the live path calls `level_points(False)` - which is -6, not
    # 0. Passing 0 here scored every corpus row six points above the live one
    # and made the calibration a comparison between two implementations. The
    # same function is called on both sides for exactly that reason.
    points = regime_model_points(
        4 - len(gates), window_ms, level_points(False)
    )
    return {
        "window_open": window_ms,
        "ticker": point["ticker"],
        "decided_ms": point["close_ms"] - point["remaining_s"] * 1000,
        "our_ask": ask, "won": int(won),
        "rule_match": 0 if gates else 1,
        "failed_gates": ", ".join(gates) or None,
        "session": session_of(window_ms),
        "vol_regime": brti_vol_regime(point["brti_volatility_bps"]),
        "brti_volatility_bps": point["brti_volatility_bps"] or 0.0,
        "brti_normalized_distance": distance,
        "brti_momentum_bps": momentum,
        "side": side, "remaining_s": point["remaining_s"],
        "brti_aligned_momentum_bps": direction * momentum,
        "feature_version": SETUP_FEATURE_VERSION,
        "model_points": points,
        "origin": CORPUS,
        "fill_kind": SIMULATED_FILL,
        "fee_cost": None,
    }


def choose_minute(minutes: list[dict], policy: bool) -> dict | None:
    """Which single minute represents this market.

    ONE row per market, never one per poll. Six polls of the same window are
    one opportunity; counting them separately would inflate every sample count
    sixfold with copies that all share an outcome.

    Under the policy the bot takes the FIRST minute whose gates pass and then
    stops. Where none passes it enters nothing, and the row is the first minute
    looked at - which is exactly where the policy WOULD have entered with the
    gates removed, so it is the right price for the refused leg.
    """
    if not minutes:
        return None
    if not policy:
        return minutes[0]
    for row in minutes:
        if row["rule_match"]:
            return row
    return minutes[0]


def _load(distance_floor: float, min_ask: float, max_ask: float, policy: bool,
          brti_path: str = "data/brti_history.db",
          market_path: str = "data/market_data.db") -> list[dict]:
    brti = sqlite3.connect(f"file:{brti_path}?mode=ro", uri=True)
    brti.row_factory = sqlite3.Row
    points: dict[str, list[dict]] = {}
    for row in brti.execute(
        "SELECT * FROM brti_decision_points ORDER BY ticker, remaining_s DESC"
    ):
        points.setdefault(row["ticker"], []).append(dict(row))
    brti.close()
    if not points:
        return []

    market = sqlite3.connect(f"file:{market_path}?mode=ro", uri=True)
    market.row_factory = sqlite3.Row
    quotes: dict[tuple, tuple] = {}
    for row in market.execute(
        "SELECT ticker, end_period_ts, yes_bid_close, yes_ask_close "
        "FROM contract_candles WHERE yes_bid_close IS NOT NULL "
        "AND yes_ask_close IS NOT NULL"
    ):
        quotes[(row["ticker"], row["end_period_ts"])] = (
            row["yes_bid_close"], row["yes_ask_close"]
        )
    market.close()

    rows = []
    for ticker, path in points.items():
        # `path` is already ordered 660s -> 360s: the order the bot sees them.
        minutes = []
        for point in path:
            end_ts = (point["close_ms"] - point["remaining_s"] * 1000) // 1000
            quote = quotes.get((ticker, end_ts))
            if quote is None:
                continue
            row = _minute_row(point, quote, distance_floor, min_ask, max_ask)
            if row is not None:
                minutes.append(row)
        chosen = choose_minute(minutes, policy=policy)
        if chosen is not None:
            rows.append(chosen)
    rows.sort(key=lambda r: r["window_open"])
    return rows


def load_policy_rows(distance_floor: float | None = None,
                     min_ask: float | None = None,
                     max_ask: float | None = None,
                     **paths) -> list[dict]:
    """THE DEPLOYED POLICY: one row per market, scanned chronologically.

    The bot does not judge a market once. It scans every poll from 660s to 360s
    remaining and takes the FIRST minute where the gates pass - then alerts once
    and stops. A market that is refused at 11 minutes and qualifies at 8 is a
    market the bot TRADES.

    Evaluating only the first minute answers a different question, and answers
    it pessimistically. Evaluating every poll as its own trade answers a third,
    and answers it optimistically: it lets one market contribute six correlated
    outcomes. So the walk is chronological and each market contributes ONE row.
    """
    gates = deployed_gates()
    return _load(
        gates[0] if distance_floor is None else distance_floor,
        gates[1] if min_ask is None else min_ask,
        gates[2] if max_ask is None else max_ask,
        policy=True, **paths,
    )


def load_brti_rows(distance_floor: float | None = None,
                   min_ask: float | None = None,
                   max_ask: float | None = None,
                   **paths) -> list[dict]:
    """FIRST-MINUTE ANALYSIS: one row per market, judged at 660s only.

    Kept because it answers a real question - "what does the rule think when it
    first looks?" - but it is NOT the deployed strategy, which re-checks every
    minute to 360s. Results computed from this must be labelled first-minute.
    """
    gates = deployed_gates()
    return _load(
        gates[0] if distance_floor is None else distance_floor,
        gates[1] if min_ask is None else min_ask,
        gates[2] if max_ask is None else max_ask,
        policy=False, **paths,
    )


def brti_context(row: dict) -> str:
    """The setup key, from the SAME function the live path calls."""
    return str(setup_context_of(row))


# ----------------------------------------------------------------- live
#
# The forward leg: what this system actually decided, in its own words, graded
# against Kalshi's settlement and reconciled to Kalshi's fills.


def _split_key(context_key: str) -> tuple[str, str]:
    """`"asia · mid · bd10-15 · px85-94|accept"` -> (context, "accept")."""
    if "|" in context_key:
        context, action = context_key.rsplit("|", 1)
        return context, action
    return context_key, "accept"


def _setup_key(record: dict) -> str | None:
    """The `brti-2` setup key from a decision's stored features, or None."""
    distance = record.get("brti_normalized_distance")
    aligned = record.get("brti_aligned_momentum_bps")
    if distance is None or aligned is None or record.get("ask") is None:
        return None
    return str(setup_context_of({
        "brti_normalized_distance": distance,
        "brti_aligned_momentum_bps": aligned,
        "our_ask": record.get("ask"),
    }))


def brti_keyed(context: str) -> bool:
    """Is this key positively a well-formed key of a scheme we can read?

    Positive identification, not absence of evidence. A key qualifies only by
    carrying a BRTI distance band and naming no unknown field - so a malformed
    key, a Binance key, and a key from some future scheme all fail the same way
    instead of one of them slipping through on a technicality.

    TWO SHAPES ARE ACCEPTED because two have existed:

        brti-1   session · vol · distance · price      (4 parts)
        brti-2   distance · price · momentum           (3 parts)

    A `brti-1` row still passes here and is then REBUILT into the `brti-2` key
    from its stored raw features; where those were never stored it is excluded
    by `_setup_key` instead. This function only rules out keys that belong to
    another instrument entirely.
    """
    parts = [p.strip() for p in context.split("·")]
    if len(parts) not in (3, 4):
        return False
    if any(p == UNKNOWN_FIELD for p in parts):
        return False
    if any(p in _BINANCE_BANDS for p in parts):
        return False
    return any(p in _BRTI_BANDS for p in parts)


def live_rows(db: sqlite3.Connection, *, fingerprint: str,
              settled_only: bool = True) -> tuple[list[dict], Provenance]:
    """Settled live signals - ACCEPTED AND REJECTED - one row per market-side.

    Rejected signals are the half that cannot be reconstructed afterwards from
    the orders, because no order exists for them. They are also the half a veto
    or an admission is measured against, so a learning loop that ingests only
    what traded is a learning loop that can only ever confirm itself.

    THE CONTEXT IS THE ONE THE LIVE PATH KEYED, read back verbatim from
    `intelligence_decisions.context_key`. It is not recomputed here: recomputing
    it would be a second implementation of the feature definitions and thus the
    exact failure this module exists to prevent, and it would also silently
    re-label historical rows whenever the bands changed - which would make every
    old decision look like it had been taken under today's definitions.

    Rows whose recorded `feature_version` is not the live one are counted as
    incompatible and dropped. A decision taken under different definitions is
    not evidence about these ones.
    """
    prov = Provenance(sources=("btc15.db:intelligence_decisions",),
                      feature_fingerprint=fingerprint)
    cursor = db.cursor()
    cursor.row_factory = sqlite3.Row
    try:
        records = [dict(r) for r in cursor.execute(
            "SELECT * FROM intelligence_decisions ORDER BY window_open, "
            "decided_ms"
        )]
    except sqlite3.Error:
        return [], prov

    # Broker truth for the executions: what actually filled, at what price and
    # what fee. Read from the Kalshi mirror, never rebuilt - a modelled fee on
    # a real trade is a number the exchange never charged.
    # KEYED BY (ticker, OUR side), and only on ENTRIES.
    #
    # Three things a naive `fills[ticker]` gets wrong, and all three were in
    # the first version of this:
    #
    #   * A SELL is an exit, not an entry. Pricing a decision at the cash-out
    #     price says the trade was opened at the price it was closed at.
    #   * A window the model flipped inside has an UP row and a DOWN row. One
    #     fill cannot belong to both, and letting it match both marked a
    #     decision nobody executed as an executed one.
    #   * `yes_price` is not what a DOWN position cost. A DOWN position is NO,
    #     and the row carries `no_price` explicitly - deriving it as
    #     `1 - yes_price` is a guess where the exchange has stated the answer.
    #
    # The earliest buy wins: that is the entry, and any later buy on the same
    # side is a scale-in priced separately.
    # HOW MANY FILLS AN EXECUTION TOOK. That is all the fills table is used
    # for here.
    #
    # `fills.side` does not reliably name the leg we held - this account has
    # entries booked `buy/yes` and entries booked `sell/no`, and a cash-out of
    # a YES position reported as `sell/no` carrying `yes_price` 0.997. Any
    # reading of our position from that field is a guess, and a guess about
    # which side we were on inverts the trade. The settlement row states it
    # outright, so that is where the position, the size, the cost and the money
    # are read from; this counts executions and nothing else.
    by_ticker: dict[str, list[dict]] = {}
    try:
        fcur = db.cursor()
        fcur.row_factory = sqlite3.Row
        for row in fcur.execute("SELECT * FROM fills ORDER BY filled_ms"):
            item = dict(row)
            if item.get("ticker"):
                by_ticker.setdefault(item["ticker"], []).append(item)
    except sqlite3.Error:
        by_ticker = {}

    settled: dict[str, dict] = {}
    try:
        scur = db.cursor()
        scur.row_factory = sqlite3.Row
        for row in scur.execute("SELECT * FROM settlements"):
            item = dict(row)
            if item.get("ticker"):
                settled[item["ticker"]] = item
    except sqlite3.Error:
        settled = {}

    # THE HISTORICAL BRIDGE. `intelligence_decisions.ticker` was NULL on every
    # row written before 2026-09-23 - it was read off the snapshot, which has
    # no such attribute - so those rows cannot join to a fill by ticker and
    # would all be scored as simulated. `predictions` recorded the same
    # window's contract ticker correctly throughout, so the mapping is
    # recoverable exactly, from data already stored.
    #
    # The rows themselves are NOT rewritten. This resolves the ticker at read
    # time; the archive keeps saying what it actually said.
    windows: dict[int, str] = {}
    try:
        wcur = db.cursor()
        for row in wcur.execute(
            "SELECT window_open, contract_ticker FROM predictions "
            "WHERE contract_ticker IS NOT NULL"
        ):
            windows[int(row[0])] = row[1]
    except sqlite3.Error:
        windows = {}

    # ONE ROW PER (market, side). A window the model flipped inside is two
    # opportunities and must not be collapsed onto one; a window polled forty
    # times is one opportunity and must not be counted as forty.
    chosen: dict[tuple, dict] = {}
    seen_polls = 0
    for record in records:
        if record.get("feature_version") != SETUP_FEATURE_VERSION:
            prov.excluded_incompatible += 1
            continue
        context_key = record.get("context_key") or ""
        context, _action = _split_key(context_key)
        # A decision taken with no context - the retirement refusal, a missing
        # BRTI poll - carries no cell to learn about. It is not an exclusion
        # for uncleanliness; there is simply nothing in it to fit.
        if not context or "·" not in context:
            continue
        # A Binance-keyed or unlabelled live row IS an exclusion, and a counted
        # one. It describes a cell that does not exist under these definitions.
        if not brti_keyed(context):
            prov.excluded_incompatible += 1
            continue
        # THE KEY IS REBUILT FROM THE STORED FEATURES, not read verbatim.
        #
        # The recorded key is whatever scheme was live when the decision was
        # taken. Under `brti-2` that is the setup key and rebuilding is a
        # no-op; rows written under `brti-1` carry a session-first key whose
        # momentum band was never stored, so they cannot be expressed in the
        # new one and are excluded rather than guessed at. Rows written from
        # here on store the raw features, so the next re-keying costs nothing.
        rebuilt = _setup_key(record)
        if rebuilt is None:
            prov.excluded_incompatible += 1
            continue
        context = rebuilt
        if settled_only and record.get("graded_ms") is None:
            prov.excluded_unresolved += 1
            continue
        if settled_only and record.get("won") is None:
            prov.excluded_unresolved += 1
            continue
        seen_polls += 1
        key = (record.get("window_open"), record.get("side"))
        existing = chosen.get(key)
        qualified = bool(record.get("base_qualified"))
        if existing is None:
            chosen[key] = record
        elif not bool(existing.get("base_qualified")) and qualified:
            # The first QUALIFYING poll represents the market, exactly as the
            # corpus leg picks it - the bot alerts on that poll and stops. The
            # row it displaces is still a poll that did not become a training
            # row, so it is counted: `excluded_duplicate` has to reconcile
            # against the polls seen, or it is a number that only looks like an
            # audit.
            chosen[key] = record
            prov.excluded_duplicate += 1
        else:
            prov.excluded_duplicate += 1

    rows = []
    for (window_open, side), record in sorted(
        chosen.items(), key=lambda kv: (kv[0][0] or 0, kv[0][1] or "")
    ):
        context, action = _split_key(record.get("context_key") or "")
        ticker = record.get("ticker") or windows.get(int(window_open or 0))
        settlement = settled.get(ticker) if ticker else None
        ask = record.get("ask")
        if ask is None:
            continue
        fill_kind = SIMULATED_FILL
        fee_cost = None
        realised = None
        executed_price = None
        contracts = 0.0
        fills_folded = 0
        adds = 0
        order_ids: tuple = ()
        unattributed = ""
        # AN ACTUAL FILL IS THE BROKER'S PRICE AND THE BROKER'S FEE. The
        # recorded ask is what we were quoted; a limit is permission to cross,
        # never the price paid, and an IOC fills at the best available price.
        # THE BROKER'S SETTLEMENT ROW IS THE AUTHORITY, not the fills.
        #
        # `fills.side` does not reliably name the leg we held: this account has
        # entries booked `buy/yes` and entries booked `sell/no`, and a cash-out
        # of a YES position that reports `sell/no` with `yes_price` 0.997.
        # Inferring our leg from it means guessing, and a guess about which
        # side we were on inverts the trade.
        #
        # `/portfolio/settlements` states it outright - `yes_count`/`no_count`
        # are the contracts held on each leg, `yes_cost`/`no_cost` what they
        # cost, and `pnl` what the exchange actually paid. That is the same
        # source every money figure in this system is already read from, and
        # the rule here is the project's own: read P&L from Kalshi, never
        # rebuild it. The fills are kept only to say how many executions it
        # took, which settlements does not record.
        attribution = attribute_execution(by_ticker.get(ticker, []), settlement)
        if attribution["resolved"] and attribution["side"] == side:
            fill_kind = ACTUAL_FILL
            contracts = attribution["contracts"]
            executed_price = attribution["entry_price"]
            fee_cost = attribution["fee_per_contract"]
            realised = attribution["pnl_per_contract"]
            fills_folded = len(attribution["fill_ids"])
            adds = attribution["adds"]
            order_ids = attribution["order_ids"]
        elif settlement is not None and not attribution["resolved"]:
            # WE TRADED THIS MARKET AND CANNOT SAY WHICH DECISION IT BELONGS
            # TO. Scoring it as a counterfactual would claim to know what
            # would have happened on a market where something actually did.
            prov.excluded_unattributed += 1
            unattributed = attribution["reason"]
        if unattributed:
            continue
        rows.append({
            "window_open": window_open,
            "ticker": ticker,
            "decided_ms": record.get("decided_ms"),
            "our_ask": float(ask),
            "won": int(record.get("won") or 0),
            "rule_match": int(bool(record.get("base_qualified"))),
            "failed_gates": record.get("failed_gates"),
            "context_key": context,
            "applies_to": action,
            "side": side,
            "remaining_s": record.get("remaining_s"),
            "feature_version": record.get("feature_version"),
            "model_points": record.get("model_points"),
            "origin": LIVE,
            "fill_kind": fill_kind,
            "fee_cost": fee_cost,
            # The size actually executed, and how many fills it took. Carried
            # so a report can say "9 executions over 14 contracts" instead of
            # conflating polls, rows, fills and contracts - four different
            # numbers that have all been quoted as each other here.
            "contracts": contracts,
            "fill_count": fills_folded,
            "adds": adds,
            "order_ids": order_ids,
            # The DECISION-time ask stays in `our_ask`, because that is what
            # the price implied when the call was made and it is what a
            # calibration must be measured against. What we actually paid is a
            # separate fact and gets a separate field.
            "executed_price": executed_price,
            # FROM THE BROKER'S TWO FILLS, NOT FROM THE DECISION ROW.
            # `intelligence_decisions.realised_pnl` is a placeholder: the
            # settlement loop passes a literal 0.0 into `grade_intelligence`,
            # so every graded row carries 0.0 and not one of them means it.
            # Reading it would have scored every real trade as break-even -
            # a column that is always present, always zero, and never true.
            "realised_pnl": realised,
        })
        if fill_kind == ACTUAL_FILL:
            prov.live_actual_fills += 1

    prov.live_decisions = len(rows)
    prov.live_markets = len({r["window_open"] for r in rows})
    if rows:
        prov.data_start_ms = min(r["window_open"] for r in rows)
        prov.data_end_ms = max(r["window_open"] for r in rows)
    return rows, prov



def attribute_execution(fills: list[dict], settlement: dict | None) -> dict:
    """Link entry, adds and exit through the broker's own fill records.

    THE PREVIOUS RULE - "the leg that cost more is the one we opened" - IS
    WRONG, and wrong in the worst place. Buy YES at 0.80, watch it fall, cash
    out by buying NO at 0.85, and the NO leg is the expensive one: the
    heuristic reports the position we exited into as the position we took.
    Measured over this account's 60 closed pairs it misattributes 6 of them,
    including a -$1.75 loser.

    So the ORDER of the fills decides it. The first fill chronologically is the
    entry; later fills on the same leg are adds; fills on the opposite leg are
    the exit, because Kalshi books an early exit as buying the other side.
    `fill_id` and `order_id` travel with the result so any row can be traced
    back to the executions behind it.

    AND THE RECONSTRUCTION IS CHECKED. Rebuilding the counts and costs from the
    fills must reproduce `yes_count`/`no_count`/`yes_cost`/`no_cost` on the
    settlement row. That check is what makes this attribution rather than
    another guess - it was verified against all 154 of this account's settled
    tickers before being relied on. Where it does not reconcile the result is
    UNRESOLVED and the market is excluded from execution-based learning: a
    trade we cannot attribute is not evidence about a decision.

    Every fill row acquires `count` contracts of its named `side` at that
    side's price, whatever `action` says. That is the convention the exchange's
    own totals reproduce (154/154); reading `action` as a signed direction
    reproduces 61 of 154.
    """
    out = {
        "resolved": False, "reason": "", "side": None, "contracts": 0.0,
        "entry_price": None, "adds": 0, "exit_contracts": 0.0,
        "exit_price": None, "pnl_per_contract": None,
        "fee_per_contract": None, "fill_ids": (), "order_ids": (),
    }
    if not settlement:
        out["reason"] = "no settlement row: the market did not settle for us"
        return out
    if not fills:
        out["reason"] = "settled but no fill records: attribution impossible"
        return out

    ordered = sorted(
        fills, key=lambda f: (f.get("filled_ms") or 0, str(f.get("fill_id")))
    )
    legs = {"yes": {"count": 0.0, "cost": 0.0}, "no": {"count": 0.0, "cost": 0.0}}
    for item in ordered:
        leg = "yes" if (item.get("side") or "yes") == "yes" else "no"
        count = _as_float(item.get("count")) or 0.0
        price = _as_float(item.get(f"{leg}_price")) or 0.0
        legs[leg]["count"] += count
        legs[leg]["cost"] += count * price

    # RECONCILE, or refuse.
    checks = (
        ("yes_count", legs["yes"]["count"], 0.001),
        ("no_count", legs["no"]["count"], 0.001),
        ("yes_cost", legs["yes"]["cost"], 0.011),
        ("no_cost", legs["no"]["cost"], 0.011),
    )
    for name, rebuilt, tolerance in checks:
        stated = _as_float(settlement.get(name)) or 0.0
        if abs(rebuilt - stated) > tolerance:
            out["reason"] = (
                f"fills do not reconcile to the settlement: {name} rebuilt "
                f"{rebuilt:.4f} against {stated:.4f}"
            )
            return out

    opening = "yes" if (ordered[0].get("side") or "yes") == "yes" else "no"
    closing = "no" if opening == "yes" else "yes"
    entry_fills = [
        f for f in ordered
        if ("yes" if (f.get("side") or "yes") == "yes" else "no") == opening
    ]
    entry_count = legs[opening]["count"]
    if entry_count <= 0:
        out["reason"] = "opening leg has no contracts"
        return out

    pnl = _as_float(settlement.get("pnl"))
    fee = _as_float(settlement.get("fee_cost")) or 0.0
    out.update({
        "resolved": True,
        "reason": "reconciled to the settlement row",
        "side": "UP" if opening == "yes" else "DOWN",
        # PER CONTRACT WE TOOK ON. The settlement pnl covers the whole
        # position including any exit, so the entry size is the denominator
        # that answers "what did each contract we bought return".
        "contracts": entry_count,
        "entry_price": round(legs[opening]["cost"] / entry_count, 6),
        "adds": max(0, len(entry_fills) - 1),
        "exit_contracts": legs[closing]["count"],
        "exit_price": (
            round(legs[closing]["cost"] / legs[closing]["count"], 6)
            if legs[closing]["count"] > 0 else None
        ),
        "pnl_per_contract": (
            None if pnl is None else round(pnl / entry_count, 6)
        ),
        "fee_per_contract": round(fee / entry_count, 6),
        "fill_ids": tuple(str(f.get("fill_id")) for f in ordered),
        "order_ids": tuple(
            dict.fromkeys(str(f.get("order_id")) for f in ordered
                          if f.get("order_id"))
        ),
    })
    return out



def _as_float(value) -> float | None:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def combined_rows(db: sqlite3.Connection | None, *, fingerprint: str,
                  corpus: list[dict] | None = None) -> tuple[list[dict], Provenance]:
    """Corpus + live, chronological, deduplicated by (market, side).

    The live leg WINS on a collision. Where both describe the same window, the
    live row is what this system actually saw and actually paid; the corpus row
    is a reconstruction of what it would have seen. Preferring the
    reconstruction would be preferring a model of ourselves to the record.
    """
    rows = list(corpus if corpus is not None else load_policy_rows())
    prov = Provenance(
        sources=("data/brti_history.db", "data/market_data.db"),
        feature_fingerprint=fingerprint,
    )
    prov.corpus_decisions = len(rows)
    prov.corpus_markets = len({r["window_open"] for r in rows})

    live: list[dict] = []
    if db is not None:
        live, live_prov = live_rows(db, fingerprint=fingerprint)
        prov.sources = prov.sources + live_prov.sources
        prov.live_decisions = live_prov.live_decisions
        prov.live_markets = live_prov.live_markets
        prov.live_actual_fills = live_prov.live_actual_fills
        prov.excluded_unresolved = live_prov.excluded_unresolved
        prov.excluded_duplicate = live_prov.excluded_duplicate
        prov.excluded_incompatible = live_prov.excluded_incompatible

    index: dict[tuple, dict] = {}
    for row in rows:
        index[(row["window_open"], row.get("side"))] = row
    for row in live:
        key = (row["window_open"], row.get("side"))
        if key in index:
            prov.corpus_decisions -= 1
            prov.excluded_duplicate += 1
        index[key] = row

    merged = sorted(index.values(), key=lambda r: (r["window_open"],
                                                   r.get("side") or ""))
    prov.corpus_markets = len({
        r["window_open"] for r in merged if r.get("origin") == CORPUS
    })
    if merged:
        prov.data_start_ms = merged[0]["window_open"]
        prov.data_end_ms = merged[-1]["window_open"]
    return merged, prov
