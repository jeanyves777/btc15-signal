"""Persistent, per-account BTC daily profit pause. Exits never use this gate."""

from __future__ import annotations

import asyncio
import dataclasses
import sqlite3
import time
from collections import defaultdict, deque
from datetime import datetime
from html import escape
from pathlib import Path

from .capital import NY, ny_day, ny_day_start_ms
from .execution import KalshiExecutionClient


def timestamp(value):
    return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000)


def realised_events(fills, settlements):
    """FIFO realised net dollars, including partial exits and broker corrections.

    Opposite-side buys close binary inventory for 1 - purchase price. Final
    settlement contributes only the residual, never counts a cash-out twice.
    """
    grouped = defaultdict(list)
    for fill in fills:
        ticker = fill.get("ticker") or fill.get("market_ticker") or ""
        if ticker.startswith("KXBTC15M-"):
            grouped[ticker].append(fill)
    events = []
    realised = defaultdict(float)
    for ticker, rows in grouped.items():
        lots = deque()
        for fill in sorted(rows, key=lambda f: (f["created_time"], f.get("fill_id", ""))):
            count = float(fill["count_fp"])
            if count <= 0:
                continue
            # V2's action is the YES-book direction: ask/sell acquires NO.
            # outcome_side names the acquired binary leg, including closing
            # the opposite inventory. Do not negate that sign a second time.
            v2 = fill.get("book_side") in ("bid", "ask") and fill.get("outcome_side") in (
                "yes",
                "no",
            )
            side = fill["outcome_side"] if v2 else fill["side"]
            price = float(fill[f"{side}_price_dollars"])
            fee = float(fill.get("fee_cost") or 0) / count
            sign = 1 if side == "yes" else -1
            buying = v2 or fill["action"] == "buy"
            if not buying:
                sign = -sign
            remaining = count
            while lots and lots[0][0] != sign and remaining > 1e-9:
                lot = lots[0]
                quantity = min(remaining, lot[1])
                proceeds = (1 - price) if buying else price
                pnl = quantity * (proceeds - lot[2] - lot[3] - fee)
                events.append((timestamp(fill["created_time"]), pnl, ticker))
                realised[ticker] += pnl
                remaining -= quantity
                lot[1] -= quantity
                if lot[1] < 1e-9:
                    lots.popleft()
            if remaining > 1e-9:
                if not buying:
                    raise ValueError("Exit inventory missing from broker history")
                lots.append([sign, remaining, price, fee])
    for row in settlements:
        ticker = row.get("ticker", "")
        if ticker.startswith("KXBTC15M-"):
            net = KalshiExecutionClient.settlement_pnl(row)
            residual = net - realised[ticker]
            events.append((timestamp(row["settled_time"]), residual, ticker))
    return sorted(events)


def fetch_since(midnight: int) -> int:
    """Where the broker read starts: far enough back for today AND the whole
    previous day, which `close_out` recomputes. The New York day of the DST
    fall-back (2026-11-01) is 25 hours: a flat 24 h read missed its first hour,
    and close_out rewrote that day short on 11-02 (review 2026-10-01)."""
    return min(midnight - 86_400_000, ny_day_start_ms(midnight - 1))


class DailyProfitGuard:
    # > 0: once this account's target is reached it is NOT paused - it keeps
    # trading at this stake until 00:00 New York (the primary since 2026-09-30,
    # $3 after $6). 0: paused at the target, as every mirror still is.
    after_target_stake = 0.0
    # > 0: the day's target is capped at this many wins at `entry_budget`
    # (mirrors since 2026-10-01); 0: the plain rate x opening.
    max_target_wins = 0.0
    target_win_price = 0.75
    # > 0: THE STAKE SCALES WITH THE ACCOUNT (mirrors since 2026-10-01): the
    # day's stake = this fraction of the opening in whole dollars, never below
    # `entry_budget` (the operator's stake) nor above `stake_max`. Captured with
    # the opening; `stake_target` is the live mirror (main._Mirror), whose
    # FROZEN MirrorTarget is swapped for one at that stake, so its copies and its
    # pre-funding size by it. 0: fixed.
    stake_rate = 0.0
    stake_max = 0.0
    stake_target = None
    # Exact dynamic all-signal sizing. Unlike the legacy mirror scale, this is
    # a fee-inclusive ceiling and is allowed above 5% when explicitly set for
    # an account. after_loss_risk_rate=0 means use the same ceiling after loss.
    entry_risk_rate = 0.0
    after_loss_risk_rate = 0.0
    # > 0: THE DAILY CAP (the primary since 2026-10-02, FINDINGS 135): once the
    # day's realised P&L reaches this fraction of the opening, NO new entry until
    # 00:00 New York - latched in `capped_ms`, so a later loss does not reopen
    # the day; exits and recording continue. 0: no cap (every mirror).
    stop_rate = 0.0
    # Said under DAILY CAP REACHED (the primary, 2026-10-05): who still trades.
    done_note = ""

    def done_for_day(self, state=None) -> bool:
        """DONE for the day at the daily cap - the cap on and reached (latched,
        or the live figure), exactly as `block_reason` refuses. Never raises:
        False (2026-10-05, FINDINGS 163: the mirrors trade on past it)."""
        try:
            state = self.state() if state is None else state
            return bool(self.stop_rate and state and (
                state.get("capped_ms")
                or state["pnl"] + 1e-8 >= self.stop_rate * float(state["opening"])))
        except Exception:  # noqa: BLE001
            return False

    def taking_entries(self, strategy: str = "") -> bool:
        """Would this account's OWN DAY take a new `strategy` entry now: figures
        fresh, not done at a cap, not paused at its target unless it trades on
        past it. Says whom to copy a signal to once the primary is done; the
        order's own check (`block_reason`) still decides. Never raises: False."""
        try:
            state = self.state()
            if not state or self.error \
                    or int(time.time() * 1000) - state["updated_ms"] > 60_000:
                return False
            if self.done_for_day(state):
                return False
            continuing = bool(self.after_target_stake) and strategy == "allsignal"
            return not (state["paused_ms"] and not continuing)
        except Exception:  # noqa: BLE001
            return False

    def day_stake(self, opening: float) -> float:
        """This account's $ stake for a day opening at `opening`: the operator's
        `entry_budget`, raised to `stake_rate` x the opening in whole dollars
        when that is more, never above `stake_max`."""
        floor = float(getattr(self, "entry_budget", 0.0) or 0.0)
        dynamic = float(getattr(self, "entry_risk_rate", 0.0) or 0.0)
        if 0 < dynamic <= 1 and opening and opening > 0:
            return float(opening) * dynamic
        rate, top = float(self.stake_rate or 0.0), float(self.stake_max or 0.0)
        # A MIS-SET SCALE SCALES NOTHING: a rate above 5% ("2" typed for 2%
        # would send every mirror to the ceiling) or no ceiling at all leaves
        # the operator's stake.
        if not 0 < rate <= 0.05 or top <= 0 or not opening or opening <= 0:
            return floor
        return max(floor, min(top, float(int(opening * rate + 1e-9))))

    def stake_today(self, state=None) -> float:
        """Today's $ stake: today's opening under the CURRENT settings, so the
        operator's change to a mirror's stake, or the scale switched off, applies
        at the restart that loads it - not at the next midnight, as reading the
        stored figure did (review 2026-10-01). The `stake` column records what
        the stake today's target is built on. No row yet: the operator's stake."""
        if state is None:
            try:
                state = self.state()
            except Exception:  # noqa: BLE001 - the operator's stake, never raise
                state = None
        opening = (state or {}).get("opening")
        if opening:
            return self.day_stake(float(opening))
        return float(getattr(self, "entry_budget", 0.0) or 0.0)

    def entry_count(self, limit: float, after_loss: bool = False) -> int | None:
        """Fee-inclusive contract cap from today's recorded opening capital.

        ``None`` means dynamic sizing is disabled and the caller should use its
        legacy fixed-dollar path. Zero means the cap cannot fund one contract.
        """
        rate = float(self.entry_risk_rate or 0.0)
        if after_loss and self.after_loss_risk_rate:
            rate = float(self.after_loss_risk_rate)
        if not 0 < rate <= 1:
            return None
        state = self.state()
        if not state or self.error or int(time.time() * 1000) - state["updated_ms"] > 60_000:
            return 0
        from .validation import contracts_for_risk_cap

        return contracts_for_risk_cap(float(state["opening"]), rate, float(limit))

    def sync_stake(self, state):
        """Point the mirror's copies at today's stake. Every refresh, so a
        restart or the midnight capture is picked up within 15 s. Never raises."""
        mirror = self.stake_target
        if mirror is None or not state:
            return
        try:
            stake = self.stake_today(state)
            current = mirror.target
            if stake > 0 and abs(float(current.allsignal_budget) - stake) > 1e-9:
                # MirrorTarget is FROZEN: assigning to it raised, the error was
                # swallowed and every copy stayed at the floor (review
                # 2026-10-01). Swap in a copy; the copier and the pre-funding
                # read `mirror.target` at each order.
                mirror.target = dataclasses.replace(current, allsignal_budget=stake)
                print(f"daily profit [{self.label}]: stake ${float(current.allsignal_budget):g} -> "
                      f"${stake:g} per signal (opening ${state['opening']:.2f})", flush=True)
        except Exception as exc:  # noqa: BLE001
            print(f"daily profit [{self.label}]: stake not applied ({exc!r})", flush=True)

    def day_target(self, opening: float) -> float:
        """The day's target in dollars: rate x opening, capped at
        `max_target_wins` wins at this account's stake for that day, so the %
        falls as the account grows and the target stays reachable."""
        target = opening * self.rate
        budget = self.day_stake(opening)
        if self.max_target_wins and budget:
            from .validation import contracts_for_budget

            p = float(self.target_win_price)
            win = contracts_for_budget(float(budget), p) * (1.0 - p)
            target = min(target, self.max_target_wins * win)
        return target

    def __init__(self, path, account, label, client, rate=0.03, opening_seed=None):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.account, self.label, self.client = account, label, client
        self.rate, self.opening_seed = rate, opening_seed
        self.lock = asyncio.Lock()
        self.last_attempt = 0
        self.error = "waiting for broker reconciliation"
        with self.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS profit_days (
                account TEXT NOT NULL, day TEXT NOT NULL, label TEXT NOT NULL,
                opening REAL NOT NULL, captured_ms INTEGER NOT NULL,
                start_ms INTEGER NOT NULL, basis TEXT NOT NULL, target REAL NOT NULL,
                pnl REAL NOT NULL DEFAULT 0, peak REAL NOT NULL DEFAULT 0,
                paused_ms INTEGER, updated_ms INTEGER NOT NULL DEFAULT 0,
                notified TEXT NOT NULL DEFAULT '', PRIMARY KEY(account,day))""")
            # THE DAY'S STAKE (2026-10-01). CREATE TABLE IF NOT EXISTS never adds
            # a column to the live table; rows from before it read NULL, which
            # means the operator's stake.
            # ...and when the day reached its daily cap (2026-10-02).
            have = {r[1] for r in db.execute("PRAGMA table_info(profit_days)")}
            for column, kind in (("stake", "REAL"), ("capped_ms", "INTEGER")):
                if column in have:
                    continue
                try:
                    db.execute(f"ALTER TABLE profit_days ADD COLUMN {column} {kind}")
                except sqlite3.OperationalError as exc:
                    if "duplicate column" not in str(exc):
                        raise

    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    def state(self, now_ms=None):
        now_ms = now_ms or int(time.time() * 1000)
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM profit_days WHERE account=? AND day=?",
                (self.account, ny_day(now_ms)),
            ).fetchone()
        return dict(row) if row else None

    async def pages(self, path, key, since):
        out, cursor = [], ""
        while True:
            params = {"limit": 200, "min_ts": since // 1000}
            if cursor:
                params["cursor"] = cursor
            response = await self.client.client.get(
                self.client.base_url + path,
                params=params,
                headers=self.client._headers("GET", path),
            )
            response.raise_for_status()
            data = response.json()
            out.extend(data.get(key) or [])
            cursor = data.get("cursor")
            if not cursor:
                return out

    async def refresh(self, force=False):
        async with self.lock:
            now = int(time.time() * 1000)
            try:
                state = self.state(now)
            except Exception as exc:  # a store error fails closed, never raises
                self.error = type(exc).__name__
                print(f"daily profit [{self.label}]: state unreadable ({self.error})",
                      flush=True)
                return
            # A FAILED last attempt is retried at once. Under the 15 s throttle
            # one transient read error refused the next signal on all three
            # accounts and that window was lost (review, 2026-09-29).
            self.sync_stake(state)
            if not force and state and not self.error and now - self.last_attempt < 15_000:
                return
            self.last_attempt = now
            midnight = ny_day_start_ms(now)
            if state is None and now < midnight + 30_000:
                # A NEW DAY'S OPENING WAITS FOR 00:00:30. The 23:45-00:00
                # market settles ~6.5 s after midnight (6.4-7.3 s observed);
                # an opening read before that valued it at cost, one read
                # after it held the result - and the result was ALSO booked
                # into the new day's P&L (review, 2026-09-29). Entries stay
                # blocked meanwhile; no signal comes that early in a window.
                return
            try:
                since = fetch_since(midnight)
                fills = await self.pages("/portfolio/fills", "fills", since)
                settlements = await self.pages("/portfolio/settlements", "settlements", since)
                events = realised_events(fills, settlements)
                if state is None:
                    value = await self.client.account_value()
                    if value is None or value <= 0:
                        raise ValueError("Opening account capital unavailable")
                    captured = int(time.time() * 1000)
                    start, basis = captured, "activation"
                    if captured - midnight < 120_000:
                        start, basis = midnight, "day opening"
                    if self.opening_seed:
                        seed = self.opening_seed(ny_day(now))
                        if seed and midnight <= seed[1] < midnight + 120_000:
                            value, captured = seed
                            start, basis = midnight, "archived day opening"
                    with self.connect() as db:
                        db.execute(
                            """INSERT OR IGNORE INTO profit_days
                            (account,day,label,opening,captured_ms,start_ms,basis,target,stake)
                            VALUES (?,?,?,?,?,?,?,?,?)""",
                            (
                                self.account,
                                ny_day(now),
                                self.label,
                                value,
                                captured,
                                start,
                                basis,
                                round(self.day_target(value), 6),
                                round(self.day_stake(value), 6),
                            ),
                        )
                    state = self.state(now)
                    self.sync_stake(state)
                # THE TARGET FOLLOWS THE STAKE (review 2026-10-01). A capped
                # account's target is 7 wins at the stake it TRADES, and that
                # stake follows the current settings (`stake_today`): after a
                # mid-day change and restart the stored target stayed at the old
                # stake - lowered to $2 from $6, a $14 target was 28 wins and the
                # day's pause was effectively gone. Re-derived until the day has
                # paused (a pause is latched), and announced again. The primary
                # (no cap) keeps its record as captured.
                if self.max_target_wins and not state["paused_ms"]:
                    opening = float(state["opening"])
                    target = round(self.day_target(opening), 6)
                    stake = round(self.day_stake(opening), 6)
                    moved = abs(target - state["target"]) > 1e-6
                    if moved or state.get("stake") != stake:
                        # A moved target is announced again ('' re-sends the
                        # DAILY TARGET ACTIVE notice with the new figures).
                        with self.connect() as db:
                            db.execute("UPDATE profit_days SET target=?, stake=?, notified="
                                       "CASE WHEN ? THEN '' ELSE notified END "
                                       "WHERE account=? AND day=? AND paused_ms IS NULL",
                                       (target, stake, int(moved), self.account, state["day"]))
                        if moved:
                            print(f"daily profit [{self.label}]: target ${state['target']:.2f} -> "
                                  f"${target:.2f} at ${stake:g} per signal (settings changed)",
                                  flush=True)
                        state = self.state(now)
                pnl, peak, hit, capped_at = 0.0, 0.0, None, None
                cap = self.stop_rate * float(state["opening"]) if self.stop_rate else None
                for at, amount, ticker in events:
                    if not state["start_ms"] <= at <= now:
                        continue
                    # A MARKET THAT CLOSED AT OR BEFORE THE PERIOD BEGAN is the
                    # previous period's, whenever its settlement lands: the
                    # 00:00 market's result is in the new opening, never in
                    # the new day's P&L.
                    opened = KalshiExecutionClient.market_open_ms(ticker)
                    if opened is not None and opened + 900_000 <= state["start_ms"]:
                        continue
                    pnl += amount
                    peak = max(peak, pnl)
                    if hit is None and pnl + 1e-8 >= state["target"]:
                        hit = at
                    if cap is not None and capped_at is None and pnl + 1e-8 >= cap:
                        capped_at = at
                with self.connect() as db:
                    db.execute(
                        """UPDATE profit_days SET pnl=?,peak=MAX(peak,?),
                        paused_ms=COALESCE(paused_ms,?),capped_ms=COALESCE(capped_ms,?),
                        updated_ms=? WHERE account=? AND day=?""",
                        (round(pnl, 6), round(peak, 6), hit, capped_at, now, self.account,
                         ny_day(now)),
                    )
                # THE PREVIOUS DAY'S LAST MARKET settles after midnight: settle
                # that day up from the same broker events, all through the new
                # day - an outage across the first half hour (the first version's
                # window) left it in neither day again (review 2026-10-01).
                # Idempotent; see `close_out`.
                if now - midnight < self.CLOSE_OUT_MS:
                    self.close_out(events, midnight, now)
                self.error = ""
            except Exception as exc:
                # Fail closed without logging credential-bearing request URLs.
                self.error = type(exc).__name__
                print(
                    f"daily profit [{self.label}]: reconciliation unavailable ({self.error})",
                    flush=True,
                )

    CLOSE_OUT_MS = 24 * 3_600_000

    def close_out(self, events, midnight, now):
        """Settle the PREVIOUS day's figure up to `now`, with its own markets only.

        The day's last market (opened 23:45, closing at midnight) settles a few
        seconds AFTER midnight - after that day's last refresh - and the new day
        rightly leaves it out (its result is already in the new opening). So a
        position held to settlement there was counted in NEITHER day: 09-30's
        23:45 window lost -5.81 and the day read +8.08 instead of +2.26 (review
        2026-10-01). This recomputes the previous day from scratch - every event
        from its start to now for markets that closed after its start and by
        midnight - so it is idempotent and never touches the new day. Returns the
        new figure, or None if there is no previous-day row."""
        prev = ny_day(midnight - 1)
        with self.connect() as db:
            row = db.execute("SELECT start_ms, pnl FROM profit_days WHERE account=? AND day=?",
                             (self.account, prev)).fetchone()
        if row is None:
            return None
        start = row["start_ms"]
        pnl = peak = 0.0
        for at, amount, ticker in events:
            if not start <= at <= now:
                continue
            opened = KalshiExecutionClient.market_open_ms(ticker)
            if opened is None:
                if at >= midnight:
                    continue          # no time in the ticker: the old day keeps its own clock
            elif not start < opened + 900_000 <= midnight:
                continue              # the period before it, or the new day's market
            pnl += amount
            peak = max(peak, pnl)
        if abs(pnl - row["pnl"]) > 1e-6:
            with self.connect() as db:
                db.execute("UPDATE profit_days SET pnl=?, peak=MAX(peak,?) WHERE account=? AND day=?",
                           (round(pnl, 6), round(peak, 6), self.account, prev))
            print(f"daily profit [{self.label}]: {prev} closed out at {pnl:+.2f} "
                  f"(was {row['pnl']:+.2f}) - its last market settled after midnight", flush=True)
        return pnl

    async def block_reason(self, ticker, strategy="", count=None, price=None):
        if not ticker.startswith("KXBTC15M-"):
            return ""
        # Past the target at a LOWER STAKE: only the $ strategy's entries, which
        # are sized for it (main.allsignal_stake_now). Every other entry on this
        # account - the main strategy, recovery adds, a manual press - still
        # pauses at the target, as before (review, 2026-09-30).
        continuing = bool(self.after_target_stake) and strategy == "allsignal"
        try:
            state = self.state()
            # FRESH FIGURES NEED NO BROKER READ ON THE ORDER PATH: the monitor
            # refreshes every 15 s, and a read here delayed the order behind it
            # (2026-09-30: 3.2 s decision->Kalshi, the order missed). Stale,
            # errored or missing figures are still re-read before deciding.
            fresh = (state is not None and not self.error
                     and int(time.time() * 1000) - state["updated_ms"] < 30_000)
            if not fresh:
                await self.refresh()
                state = self.state()
        except Exception as exc:  # noqa: BLE001 - FAIL CLOSED, never raise
            # A locked or unreadable daily_profit.db raised out of here into the
            # order path, and from the main strategy's order out of the poll
            # loop (2026-09-30 audit). Unknown figures pause the entry.
            print(f"daily profit [{self.label}]: state unreadable ({exc!r})", flush=True)
            return "BTC entry paused: daily capital/profit reconciliation unavailable"
        # THE DAILY CAP (primary, FINDINGS 135): done for the day once reached -
        # every entry, the $ strategy's included. Latched by `capped_ms`; the
        # live figure counts too, so a settlement since the last refresh cannot
        # let one more entry through. "profit target" in the note keeps the
        # order path's miss notice quiet, as for the pause.
        if state and self.stop_rate and (
                state.get("capped_ms")
                or state["pnl"] + 1e-8 >= self.stop_rate * float(state["opening"])):
            return (f"BTC {self.stop_rate:.0%} daily profit target cap reached; "
                    "done until midnight New York")
        if state and state["paused_ms"] and not continuing:
            return (f"BTC {self.rate:.0%} daily profit target reached; "
                    "paused until midnight New York")
        # AT OR ABOVE THE TARGET the $ entry must be at the lower stake; below it
        # (before the target, or back below after losses - operator 17:5x) the
        # base stake is allowed again. THE ENTRY'S SIZE DECIDES, not when the
        # figures were read: one sized at the base stake just before the day
        # reached the target is refused, whatever the timing (review
        # 2026-09-30). Said, not silent: no "profit target" in the note.
        if continuing and state and state["pnl"] + 1e-8 >= state["target"]:
            if count is not None and price:
                from .validation import contracts_for_budget

                allowed = contracts_for_budget(self.after_target_stake, float(price))
                if float(count) > allowed:
                    return (f"BTC {self.rate:.0%} daily target reached just before this entry, "
                            f"which was sized at the base stake - skipped; the next is at "
                            f"${self.after_target_stake:g}")
        # Past the target with a lower stake the account keeps trading - but
        # unknown figures still block, exactly as before the target.
        if self.error or not state or int(time.time() * 1000) - state["updated_ms"] > 60_000:
            return "BTC entry paused: daily capital/profit reconciliation unavailable"
        return ""

    def line(self):
        state = self.state()
        label = escape(self.label)
        if not state:
            return f"{label}: waiting for opening capital; new BTC entries blocked"
        stale = self.error or int(time.time() * 1000) - state["updated_ms"] > 60_000
        lower = self.after_target_stake
        at = state["pnl"] + 1e-8 >= state["target"]
        status = (
            "DONE FOR THE DAY (daily cap) until 00:00 ET" if capped(self, state)
            else "PAUSED until 00:00 ET" if state["paused_ms"] and not lower
            else "WAITING FOR BROKER" if stale
            else f"AT TARGET \u00b7 ${lower:g}/entry" if state["paused_ms"] and at
            else "BACK BELOW TARGET" if state["paused_ms"]
            else "ACTIVE"
        )
        captured = datetime.fromtimestamp(state["captured_ms"] / 1000, NY)
        basis = f"start {captured:%H:%M} ET" if state["basis"] == "activation" else "day start"
        percent = 100 * state["pnl"] / state["opening"]
        budget = (self.stake_today(state)
                  if getattr(self, "entry_budget", None) is not None else None)
        risk = float(getattr(self, "entry_risk_rate", 0.0) or 0.0)
        size = (f" · max {100 * risk:g}%/entry incl fees" if risk
                else f" · ${budget:g}/entry" if budget is not None else "")
        return (
            f"<b>{label}</b> · {status}{size}\n"
            f"Starting capital: ${state['opening']:.2f} ({basis})\n"
            f"Realized: ${state['pnl']:+.2f} ({percent:+.2f}%) · target ${state['target']:.2f}"
        )


def capped(guard, state) -> bool:
    """The daily cap IN FORCE today: latched and switched on. Everything shown
    follows what block_reason enforces - with the cap switched off mid-day the
    latch alone said CAPPED with a $0.00 cap (review 2026-10-02)."""
    return bool(getattr(guard, "stop_rate", 0.0) and state and state.get("capped_ms"))


def summary(guards):
    """EVERY ACCOUNT'S DAY as an aligned table - what a RESULT and the session
    summary carry (operator, 2026-09-29: the result "should be the one
    containing account summary and stats"; "where are the icons and design
    and table formatting"). Monospace <pre>, plain text inside so it aligns.
    """
    now = int(time.time() * 1000)
    rows = [f"{'Account':<13}{'Start':>8}{'Today':>8}{'Target':>8}  Status"]
    paused, lowered, restored, blocked = [], [], [], []   # names, for the lines below
    capped_rows = []
    for guard in guards:
        state = guard.state()
        label = guard.label[:13]
        if not state:
            rows.append(f"{label:<13}{'-':>8}{'-':>8}{'-':>8}  WAITING")
            continue
        stale = guard.error or now - state["updated_ms"] > 60_000
        lower = guard.after_target_stake
        # A real pause first; then NO DATA (entries are blocked whatever the
        # target says); then the lower stake (review, 2026-09-30).
        at = state["pnl"] + 1e-8 >= state["target"]
        back = getattr(guard, "entry_budget", None)
        status = ("CAPPED" if capped(guard, state)
                  else "PAUSED" if state["paused_ms"] and not lower
                  else "NO DATA" if stale
                  else f"HIT ${lower:g}" if state["paused_ms"] and at
                  else (f"BACK ${back:g}" if back else "BELOW") if state["paused_ms"]
                  else "ACTIVE")
        name = escape(guard.label, quote=False)
        if status == "PAUSED":
            paused.append(name)
        elif status == "CAPPED":
            capped_rows.append((name, state["pnl"], guard.stop_rate * state["opening"],
                           guard.stop_rate))
        elif status == "NO DATA":
            blocked.append(name)
        elif state["paused_ms"] and at:
            lowered.append((name, lower, back))
        elif state["paused_ms"]:
            restored.append((name, back))
        rows.append(f"{label:<13}{state['opening']:>8.2f}{state['pnl']:>+8.2f}"
                    f"{state['target']:>8.2f}  {status}")
    # Each account's EFFECTIVE % today (its target / opening): a mirror's falls
    # below its rate once the wins cap holds its target (2026-10-01).
    def pct(g):
        st = g.state()
        return (st["target"] / st["opening"]) if st and st["opening"] else g.rate

    rates = {round(pct(g), 3) for g in guards}
    if len(rates) == 1:
        head = f"{pct(guards[0]):.1%} daily target".replace(".0%", "%")
    else:
        head = "daily targets " + " \u00b7 ".join(
            f"{escape(g.label, quote=False)} {pct(g):.1%}".replace(".0%", "%") for g in guards)
    out = [f"\U0001f3e6 <b>Accounts \u00b7 {head}</b> <i>(net of fees)</i>",
           "<pre>" + escape("\n".join(rows), quote=False) + "</pre>"]
    # WHO, not "paused accounts" (operator, 2026-09-30: "should be specific
    # about what is paused").
    for name, pnl, cap, rate in capped_rows:
        out.append(f"\U0001f3c1 <b>{name}</b> \u00b7 daily cap reached "
                   f"(${pnl:+.2f} of ${cap:.2f}, {100 * rate:g}%) \u00b7 done: no new BTC "
                   "entries until 00:00 ET; open positions still exit; shadow tracking continues")
    for name, lower, back in lowered:
        out.append(f"\u2b07\ufe0f <b>{name}</b> \u00b7 at or above the target \u00b7 "
                   f"<b>${lower:g}</b> per signal"
                   + (f" \u00b7 back to ${back:g} if the day drops below it" if back else "")
                   + " \u00b7 resets 00:00 ET")
    for name, back in restored:
        out.append(f"\u2b06\ufe0f <b>{name}</b> \u00b7 back below the target \u00b7 "
                   + (f"<b>${back:g}</b> per signal" if back else "the base stake")
                   + " until it is reached again")
    if paused:
        out.append(f"\u23f8 <b>{' and '.join(paused) if len(paused) < 3 else ', '.join(paused)}"
                   "</b> \u00b7 target hit \u00b7 no new BTC entries until 00:00 ET; "
                   "open positions still exit; shadow tracking continues")
    if blocked:
        out.append(f"\u26a0\ufe0f <b>{', '.join(blocked)}</b> \u00b7 Kalshi figures unavailable "
                   "\u00b7 new BTC entries blocked until they are read again")
    return "\n".join(out)


def compact(guards):
    """ONE line for a result: each account's realised day against its target,
    e.g. "🎯 3% target: Primary +0.84/3.14 · Wife +0.21/0.76 ⏸"."""
    parts = []
    for guard in guards:
        state = guard.state()
        label = escape(guard.label, quote=False)
        if not state:
            parts.append(f"{label} waiting")
            continue
        at = state["pnl"] + 1e-8 >= state["target"]
        mark = (" \U0001f3c1" if capped(guard, state)
                else " \u23f8" if state["paused_ms"] and not guard.after_target_stake
                else " \u2b07" if state["paused_ms"] and at else "")
        parts.append(f"{label} {state['pnl']:+.2f}/{state['target']:.2f}{mark}")
    return "\U0001f3af 3% target: " + " \u00b7 ".join(parts)


def footer(guards):
    return (
        "<b>BTC daily target: 3% · after fees</b>\n"
        + "\n\n".join(g.line() for g in guards)
        + "\n\n<i>Reset: 00:00 New York. Target pauses entries; exits continue."
        + " Each account pauses independently; shadow tracking continues.</i>"
    )


async def _say(telegram, text):
    try:
        await telegram.send(text)
    except Exception:  # noqa: BLE001 - a notice never stops the monitor
        pass


def _note(guard) -> str:
    """The guard's `done_note` - text, or a callable that builds it now (the
    mirrors still trading, 2026-10-05). Never raises: ''."""
    try:
        note = getattr(guard, "done_note", "")
        return str((note() if callable(note) else note) or "")
    except Exception:  # noqa: BLE001
        return ""


async def monitor(guards, telegram):
    """Refresh every account every 15 s, announce each pause/activation once,
    and each broker-read outage once (in and out). NEVER DIES: one account's
    exception is logged and the loop goes on (review, 2026-09-29 - a dead
    monitor silently stopped the announcements and the midnight capture)."""
    while True:
        for guard in guards:
            try:
                await _monitor_one(guard, telegram)
            except Exception as exc:  # noqa: BLE001
                print(f"daily profit monitor [{guard.label}]: {type(exc).__name__}",
                      flush=True)
        await asyncio.sleep(15)


async def _monitor_one(guard, telegram):
    await guard.refresh()
    reader = getattr(guard.client, "shard_balances", None)
    if reader is not None:
        try:
            await reader()      # the order path spends this cache (ensure_funds)
        except Exception:  # noqa: BLE001 - the order path then reads it itself
            pass
    label = escape(guard.label, quote=False)
    # OUTAGES ARE SAID, not only enforced: a refused entry used to leave no
    # trace (review, 2026-09-29). Two failing passes in a row before speaking.
    guard.fail_passes = (getattr(guard, "fail_passes", 0) + 1) if guard.error else 0
    # A DAY ALREADY DONE AT ITS CAP stays done whatever the read does: the
    # outage pair said "allowed again" on a capped day (review 2026-10-02).
    try:
        state = guard.state()
        done = capped(guard, state) or bool(
            state and state["paused_ms"] and not guard.after_target_stake)
    except Exception:  # noqa: BLE001 - a local read; the plain wording then
        done = False
    if guard.fail_passes >= 2 and not getattr(guard, "outage_said", False):
        guard.outage_said = True
        await _say(telegram,
                   f"\u26a0\ufe0f <b>TARGET CHECK UNAVAILABLE \u00b7 {label}</b>\n"
                   f"\U0001f6e1 Kalshi could not be read ({escape(guard.error, quote=False)}); "
                   + ("the day is already done at its daily cap - no new BTC entries until "
                      "00:00 ET either way." if done else
                      "new BTC entries on this account are BLOCKED until it recovers."))
    elif not guard.error and getattr(guard, "outage_said", False):
        guard.outage_said = False
        await _say(telegram, f"\u2705 <b>TARGET CHECK RESTORED \u00b7 {label}</b>\n"
                   + ("\U0001f3c1 Still done for the day at the daily cap - no new BTC entries "
                      "until 00:00 ET." if done else
                      "\u25b6\ufe0f New BTC entries allowed again."))
    state = guard.state()
    if state and not guard.error:
        if capped(guard, state):
            status = "capped"            # the daily cap: done for the day, said once
        elif guard.after_target_stake:
            # The stake follows the day: at/above the target 'lowered', back
            # below after reaching it 'restored' - each change said once.
            at = state["pnl"] + 1e-8 >= state["target"]
            status = "lowered" if at else ("restored" if state["paused_ms"] else "active")
        else:
            status = "paused" if state["paused_ms"] else "active"
        if state["notified"] != status:
            try:
                label = escape(guard.label, quote=False)
                pct = (100 * state["pnl"] / state["opening"]) if state["opening"] else 0.0
                lower = guard.after_target_stake
                back = getattr(guard, "entry_budget", None)
                again = state["notified"] == "restored"
                sent = await telegram.send(
                    (f"\U0001f3c1 <b>DAILY CAP REACHED \u00b7 {label}</b>\n"
                     f"\U0001f3af <b>{state['pnl']:+.2f}</b> of the "
                     f"${guard.stop_rate * state['opening']:.2f} cap "
                     f"({100 * guard.stop_rate:g}% of ${state['opening']:.2f}) \u00b7 net of fees\n"
                     "\U0001f6d1 Done for the day: no new BTC entries until 00:00 ET \u00b7 "
                     "open positions still exit; shadow tracking continues"
                     + (f"\n{escape(_note(guard), quote=False)}" if _note(guard) else ""))
                    if status == "capped" else
                    (f"\U0001f3af <b>TARGET REACHED{' AGAIN' if again else ''} \u00b7 {label}</b>\n"
                     f"\U0001f3af <b>{state['pnl']:+.2f}</b> of ${state['target']:.2f} "
                     f"({pct:+.2f}%) \u00b7 net of fees\n"
                     f"\u2b07\ufe0f <b>${lower:g}</b> per signal while the day stays at or above it"
                     + (f" \u00b7 back to ${back:g} if it drops below" if back else "")
                     + " \u00b7 resets 00:00 ET")
                    if status == "lowered" else
                    (f"\u2b06\ufe0f <b>BACK BELOW TARGET \u00b7 {label}</b>\n"
                     f"\U0001f3af <b>{state['pnl']:+.2f}</b> of ${state['target']:.2f} "
                     f"({pct:+.2f}%) \u00b7 net of fees\n"
                     + (f"\u2b06\ufe0f Back to <b>${back:g}</b> per signal" if back
                        else "\u2b06\ufe0f Back to the base stake")
                     + " until the target is reached again")
                    if status == "restored" else
                    f"\u23f8 <b>TARGET REACHED \u00b7 {label}</b>\n"
                    f"\U0001f3af <b>{state['pnl']:+.2f}</b> of ${state['target']:.2f} "
                    f"({pct:+.2f}%) \u00b7 net of fees\n"
                    "\U0001f6d1 New BTC entries paused until 00:00 ET \u00b7 "
                    "open positions still exit; shadow tracking continues"
                    if status == "paused" else
                    f"\u25b6\ufe0f <b>DAILY TARGET ACTIVE \u00b7 {label}</b>\n"
                    f"\U0001f3e6 Opening capital <b>${state['opening']:.2f}</b>\n"
                    f"\U0001f3af Target <b>${state['target']:.2f}</b> "
                    f"({100 * state['target'] / state['opening']:.1f}%, net of fees"
                    + (f"; capped at {guard.max_target_wins:g} wins at "
                       f"${guard.stake_today(state):g}"
                       if state["target"] + 1e-6 < state["opening"] * guard.rate else "")
                    + ") \u00b7 resets 00:00 ET"
                    # THE DAY'S STAKE, set from this opening (mirrors, 2026-10-01).
                    + (f"\n\U0001f4b5 <b>${guard.stake_today(state):g}</b> per signal today "
                       f"\u00b7 {100 * guard.stake_rate:g}% of the opening, never below "
                       f"${float(guard.entry_budget):g}"
                       + (f" or above ${guard.stake_max:g}" if guard.stake_max else "")
                       if guard.stake_rate else "")
                    + (f"\n\U0001f4b5 Entry risk cap <b>{100 * guard.entry_risk_rate:g}%</b> "
                       "of opening capital, including fee"
                       + (f" · after loss {100 * guard.after_loss_risk_rate:g}%"
                          if guard.after_loss_risk_rate and
                          abs(guard.after_loss_risk_rate - guard.entry_risk_rate) > 1e-12
                          else "")
                       if guard.entry_risk_rate else "")
                    # THE DAILY CAP, said with the day's target (2026-10-02).
                    + (f"\n\U0001f3c1 Daily cap <b>${guard.stop_rate * state['opening']:.2f}</b> "
                       f"({100 * guard.stop_rate:g}%) \u00b7 done for the day once reached"
                       if guard.stop_rate else "")
                )
                if sent:
                    with guard.connect() as db:
                        db.execute(
                            "UPDATE profit_days SET notified=? WHERE account=? AND day=?",
                            (status, guard.account, state["day"]),
                        )
            except Exception:
                pass
