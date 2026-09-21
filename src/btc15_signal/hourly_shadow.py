"""Shadow recorder for the hourly strike ladder. Observes; never orders.

This runs inside the live service loop, so its first obligation is to be
harmless. Every public entry point swallows its own errors and reports them as
text: a failure to archive a research snapshot must never interrupt settlement
reporting or order placement for the 15-minute strategy that has real money in
it. It also keeps its own cadence, so a 60-second ladder poll does not slow the
12-second trading poll.

There is no entry rule here, deliberately. The 15-minute band (0.85-0.93) was
arrived at by measuring 1,674 windows; transplanting it onto an instrument with
a different horizon, a different fee profile at the same price, and 188
correlated rungs to choose between would be assuming the answer. Hourly gets
its own measurement first, from the data this module collects.
"""

import time

import httpx

from .config import Settings
from .hourly import HourlyChainClient, check_integrity
from .hourly_store import HourlyStore, new_session_id


class HourlyShadow:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._store = HourlyStore(settings.hourly_database_path)
        self._client = HourlyChainClient(
            settings.kalshi_base_url, settings.hourly_series
        )
        self._session_id = new_session_id()
        self._last_poll_ms = 0
        self._last_chain: str | None = None
        # Staleness has to be measured across polls: Kalshi's `updated_time`
        # is stamped at chain open and never moves, so it cannot tell a live
        # ladder from a frozen one.
        self._last_quotes: dict[str, tuple[float, float]] = {}
        self._quotes_moved_ms: int | None = None
        self._settle_client = httpx.AsyncClient(timeout=30)

    async def close(self) -> None:
        await self._client.close()
        await self._settle_client.aclose()
        self._store.close()

    def due(self, now_ms: int) -> bool:
        return (
            now_ms - self._last_poll_ms
            >= self._settings.hourly_poll_seconds * 1000
        )

    async def poll(self, now_ms: int, market) -> None:
        """Record one ladder snapshot. Never raises.

        Takes its OWN Binance reading anchored to the hour's open rather than
        reusing the trading loop's. The two are not interchangeable: the
        loop's snapshot carries `window_high`, `window_low` and
        `elapsed_minutes` measured over a 15-minute window, which would be
        wrong for an hour. Independence also means the ladder keeps recording
        during the gap between 15-minute windows, when the trading loop has no
        market and skips the rest of its cycle.
        """
        if not self._settings.hourly_enabled or not self.due(now_ms):
            return
        self._last_poll_ms = now_ms
        try:
            chain = await self._client.active_chain(now_ms)
            if chain is None:
                if self._last_chain is not None:
                    print("hourly: between chains", flush=True)
                    self._last_chain = None
                return

            snapshot = await market.snapshot(chain.open_ms)
            spot = snapshot.price if snapshot else None
            integrity = check_integrity(
                chain, spot, now_ms, self._quote_age(chain, now_ms)
            )
            archived = (
                chain.near(spot, self._settings.hourly_archive_window)
                if spot
                else chain.strikes
            )
            written = self._store.record_snapshot(
                chain,
                integrity,
                spot=spot,
                momentum_5m_bps=snapshot.momentum_5m_bps if snapshot else None,
                volatility_5m_bps=snapshot.volatility_5m_bps if snapshot else None,
                archived=archived,
                session_id=self._session_id,
            )
            if chain.chain_id != self._last_chain:
                print(
                    f"hourly: watching {chain.chain_id} - {len(chain.strikes)} rungs, "
                    f"{integrity.quotable_count} quotable, {written} archived",
                    flush=True,
                )
                self._last_chain = chain.chain_id
            if not integrity.ok:
                print(
                    f"hourly: {chain.chain_id} not clean - "
                    f"{'; '.join(integrity.reasons)}",
                    flush=True,
                )
        except (httpx.HTTPError, RuntimeError, ValueError, OSError, KeyError) as exc:
            # Shadow work is never allowed to take the trading loop down.
            print(f"hourly poll error: {type(exc).__name__}: {exc}", flush=True)

    def _quote_age(self, chain, now_ms: int) -> float | None:
        """Seconds since any quotable rung last changed its quote.

        Returns None until there is a previous snapshot of the SAME chain to
        compare against - on the first poll of an hour, and across the boundary
        between hours, "nothing has changed yet" is not evidence of anything.
        """
        quotes = {s.ticker: (s.yes_bid, s.yes_ask) for s in chain.quotable()}
        known = self._last_chain == chain.chain_id and self._last_quotes
        if not known:
            self._last_quotes = quotes
            self._quotes_moved_ms = now_ms
            return None
        if any(quotes.get(t) != q for t, q in self._last_quotes.items()):
            self._quotes_moved_ms = now_ms
        self._last_quotes = quotes
        if self._quotes_moved_ms is None:
            return None
        return (now_ms - self._quotes_moved_ms) / 1000

    async def settle(self, now_ms: int) -> None:
        """Record the settled BRTI for finished chains. Never raises.

        One number decides all 188 rungs, so this fetches a single settled
        market per chain rather than 188 results.
        """
        if not self._settings.hourly_enabled:
            return
        try:
            for chain_id, _close_ms in self._store.unsettled_chains(now_ms)[:4]:
                response = await self._settle_client.get(
                    f"{self._settings.kalshi_base_url.rstrip('/')}/markets",
                    params={
                        "event_ticker": chain_id, "status": "settled", "limit": 1
                    },
                )
                response.raise_for_status()
                rows = response.json().get("markets", [])
                if not rows:
                    continue
                try:
                    value = float(rows[0].get("expiration_value"))
                except (TypeError, ValueError):
                    continue
                self._store.record_settlement(
                    chain_id, _close_ms, value, int(time.time() * 1000)
                )
                print(f"hourly: {chain_id} settled at {value:,.2f}", flush=True)
        except (httpx.HTTPError, RuntimeError, ValueError, OSError, KeyError) as exc:
            print(f"hourly settle error: {type(exc).__name__}: {exc}", flush=True)
