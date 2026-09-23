"""Settlement-reference recorder. Observes; never orders, never sizes, never exits.

FINDINGS section 40 closed the close-call exit and found the reason it cannot
work: the bot decides on Binance spot while the contract settles on a 60-second
average of CF Benchmarks' BRTI. What section 40 could NOT say - and the
operator was right to call out - is how much of that gap is the FEED and how
much is the AVERAGING, because it compared a single Binance minute-close
against a 60-second average and so mixed the two into one number.

This recorder exists to separate them, and it is built to three rules:

1. **It cannot affect trading.** Every public method swallows its own errors.
   It keeps its own cadence and its own database file, so neither a slow feed
   nor a locked table can reach the order path. Nothing here reads or writes
   `btc15.db`, and nothing here returns a value the trading code consults.
2. **It never substitutes a source.** If BRTI is not available the row says so.
   Binance is recorded in its own column, as a comparison, never as the
   reference. A basis measured against a stand-in is not a basis.
3. **It records its own gaps.** Missing, stale and errored polls are written
   with the reason attached, so coverage can be audited instead of assumed.

WHAT IT PRODUCES. Per poll, one row per source with the raw price, both
timestamps, the age, the staleness verdict, the signed distance to the strike
and that source's own trailing 60-second mean. Per settled market, one
reconciliation row comparing what we computed against Kalshi's published
`expiration_value`, with the decomposition split out:

    feed_basis_bps    60s Binance mean vs official 60s BRTI mean  (FEED only)
    aggregation_bps   Binance last print vs Binance 60s mean      (AVERAGING only)

The HOLD/EXIT shadow model does not begin until `computed_brti_error_bps`
shows this recorder reproducing official settlements reliably. Until then any
decision it logged would be a decision made on the wrong number, which is the
mistake section 40 exists to record.
"""

import contextlib
import time
import traceback

from .brti import KalshiBRTI, features_from_series
from .config import Settings
from .reference import (
    BinanceSeconds,
    KalshiOfficial,
    Observation,
    basis_bps,
    rolling_mean,
)
from .reference_store import ReferenceStore


def new_session_id() -> str:
    return f"ref-{int(time.time())}"


class ReferenceShadow:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._store = ReferenceStore(settings.reference_database_path)
        # BRTI now comes from Kalshi itself (FINDINGS 42). The CF Benchmarks
        # subscription this used to wait for is not needed and never was: the
        # earlier "no passthrough" conclusion came from guessing route names
        # instead of reading the published OpenAPI.
        self._brti = KalshiBRTI(settings.kalshi_base_url)
        self._brti_series: list[tuple[int, float]] = []
        self._brti_event: str | None = None
        self._brti_features = None
        # ARCHIVE ONLY, AND OFF UNDER KALSHI-ONLY. The Binance column existed
        # to decompose feed basis from time aggregation (FINDINGS 41/43).
        # That measurement is finished and its rows stay readable as history,
        # but the client still made a live request every poll - which is an
        # active Binance dependency however it is labelled. A netstat against
        # the running service found the connection open, which is why this is
        # `None` rather than merely unread.
        self._binance = (
            None if settings.kalshi_only
            else BinanceSeconds(settings.spot_base_url, settings.symbol)
        )
        self._kalshi = KalshiOfficial(settings.kalshi_base_url, settings.kalshi_series)
        self._session_id = new_session_id()
        self._last_poll_ms = 0
        self._last_reconcile_ms = 0
        # Trailing samples per source, for each source's own 60-second mean.
        self._samples: dict[str, list[tuple[int, float]]] = {}
        # Open gaps, collapsed into one row per (source, reason) run so a feed
        # that is off for a day does not write 7,000 identical rows.
        self._open_gaps: dict[tuple[str, str], dict] = {}

    async def close(self) -> None:
        try:
            self._flush_gaps()
            await self._brti.close()
            if self._binance is not None:
                await self._binance.close()
            await self._kalshi.close()
            self._store.close()
        except Exception:  # noqa: BLE001 - shutdown must never raise
            pass

    @property
    def brti_configured(self) -> bool:
        """Always true now: BRTI needs Kalshi credentials, nothing further."""
        return True

    # ------------------------------------------------ read by the add-on

    def current_features(self):
        """The latest BRTI gate inputs, or None. Never fetches.

        The recovery add-on reads this rather than polling BRTI itself: one
        request a poll, one series, one set of numbers. A second fetch would
        let the recorder and the order path disagree about the reference at
        the same instant, which is the whole class of bug this work exists to
        remove.
        """
        return self._brti_features

    def crossed_since(self, since_ms: int, side: str) -> bool | None:
        """Has BRTI been on the wrong side of the strike since `since_ms`?

        None when it cannot be answered - no series, or the series does not
        reach back that far. The caller must treat None as "unknown", never as
        "no": an unanswerable safety question is not a pass.
        """
        features = self._brti_features
        if not self._brti_series or features is None or not features.target:
            return None
        window = [(t, v) for t, v in self._brti_series if t >= since_ms]
        if not window:
            return None
        if self._brti_series[0][0] > since_ms:
            return None  # series starts after entry; cannot rule a crossing out
        target = features.target
        if side == "UP":
            return any(value < target for _, value in window)
        return any(value > target for _, value in window)

    # ------------------------------------------------------------- polling

    async def poll(self, now_ms: int, contract) -> None:
        """Record every source once. Never raises."""
        try:
            if now_ms - self._last_poll_ms < self._settings.reference_poll_seconds * 1000:
                return
            self._last_poll_ms = now_ms
            await self._poll(now_ms, contract)
        except Exception as exc:  # noqa: BLE001 - research must never stop trading
            print(f"reference recorder poll failed: {exc!r}", flush=True)
            if self._settings.reference_debug:
                traceback.print_exc()

    async def _poll(self, now_ms: int, contract) -> None:
        target = getattr(contract, "target", None)
        ticker = getattr(contract, "ticker", None)
        window_open = getattr(contract, "open_ms", None)
        close_ms = getattr(contract, "close_ms", None)
        remaining = int((close_ms - now_ms) / 1000) if close_ms else None

        brti = await self._brti_observation(now_ms, contract)
        binance = (
            await self._binance.latest(now_ms) if self._binance is not None
            else Observation(source="binance", status="disabled",
                             received_ms=now_ms,
                             error="kalshi_only: not requested")
        )
        # BRTI-native gate inputs, recorded beside the Binance ones so the two
        # can be compared on identical windows. They are NOT yet compared
        # against any threshold: the deployed numbers were calibrated on raw
        # Binance volatility and a 60-second mean is a different quantity.
        if self._brti_series and target:
            self._brti_features = features_from_series(
                self._brti_event or "", self._brti_series, target, now_ms,
                stale_limit_ms=self._settings.reference_stale_ms,
            )

        # The reference is BRTI and only BRTI. When it is absent the basis
        # column stays NULL rather than silently re-basing onto Binance.
        reference_price = brti.raw_price if brti.status == "ok" else None

        for observation in (brti, binance):
            self._record(
                observation, now_ms, window_open, ticker, target,
                remaining, reference_price,
            )
        self._record_features(
            now_ms, window_open, ticker, target, remaining, binance
        )

    def _record_features(
        self, now_ms: int, window_open, ticker, target, remaining,
        binance: Observation,
    ) -> None:
        """One row per poll: the official view, with the Binance view beside it.

        The Binance columns are ARCHIVE. Nothing reads them to decide anything -
        they exist so the disagreement rate can be measured live, the same way
        FINDINGS 41 measured it on history.
        """
        features = self._brti_features
        if features is None or not target:
            return
        binance_price = binance.raw_price if binance.status == "ok" else None
        binance_signed = basis_bps(binance_price, target)
        binance_side = (
            None if binance_signed is None
            else ("UP" if binance_signed >= 0 else "DOWN")
        )
        self._store.record_brti_features({
            "session_id": self._session_id,
            "window_open_ms": window_open,
            "ticker": ticker,
            "event_ticker": features.event_ticker,
            "received_ms": now_ms,
            "ts_ms": features.ts_ms,
            "remaining_s": remaining,
            "target": features.target,
            "brti_value": features.value,
            "signed_distance_bps": features.signed_distance_bps,
            "brti_momentum_bps": features.brti_momentum_bps,
            "brti_volatility_bps": features.brti_volatility_bps,
            "brti_normalized_distance": features.brti_normalized_distance,
            "brti_side": features.side,
            "settlement_projection": features.settlement_projection,
            "samples": features.samples,
            "span_ms": features.span_ms,
            "stale": int(features.stale),
            "binance_price": binance_price,
            "binance_signed_distance_bps": binance_signed,
            "binance_side": binance_side,
            "sides_agree": (
                None if binance_side is None else int(binance_side == features.side)
            ),
        })

    async def _brti_observation(self, now_ms: int, contract) -> Observation:
        """The official reference, from Kalshi. Never substituted."""
        event = getattr(contract, "event_ticker", None)
        if not event:
            ticker = getattr(contract, "ticker", None)
            # KXBTC15M-26SEP221245-45 -> KXBTC15M-26SEP221245
            event = ticker.rsplit("-", 1)[0] if ticker and "-" in ticker else None
        if not event:
            return Observation(
                source="brti", status="missing", received_ms=now_ms,
                error="no event ticker (between windows)",
            )
        try:
            points = await self._brti.series(event)
        except Exception as exc:  # noqa: BLE001 - a feed error is a value here
            return Observation(
                source="brti", status="error", received_ms=now_ms,
                error=f"{type(exc).__name__}: {exc}"[:200],
            )
        if not points:
            return Observation(
                source="brti", status="missing", received_ms=now_ms,
                error=f"empty timeseries for {event}",
            )
        self._brti_series = sorted(points)
        self._brti_event = event
        ts_ms, value = self._brti_series[-1]
        return Observation(
            source="brti", status="ok", raw_price=value,
            event_ms=ts_ms, received_ms=now_ms,
        )

    def _record(
        self,
        observation: Observation,
        now_ms: int,
        window_open: int | None,
        ticker: str | None,
        target: float | None,
        remaining: int | None,
        reference_price: float | None,
    ) -> None:
        source = observation.source
        stale_limit = self._settings.reference_stale_ms
        status = observation.status
        if status == "ok" and observation.is_stale(stale_limit):
            status = "stale"

        if status == "ok" and observation.raw_price is not None:
            samples = self._samples.setdefault(source, [])
            samples.append((observation.event_ms or now_ms, observation.raw_price))
            cutoff = now_ms - 120_000
            self._samples[source] = [s for s in samples if s[0] >= cutoff]
            self._close_gap(source)
        else:
            self._open_gap(source, status, observation.error, now_ms)

        mean, count, span = rolling_mean(self._samples.get(source, []), now_ms)
        price = observation.raw_price if status == "ok" else None

        self._store.record_observation({
            "session_id": self._session_id,
            "window_open_ms": window_open,
            "ticker": ticker,
            "source": source,
            "status": status,
            "raw_price": price,
            "event_ms": observation.event_ms,
            "received_ms": observation.received_ms or now_ms,
            "age_ms": observation.age_ms,
            "stale": 1 if status == "stale" else 0,
            "target": target,
            "signed_distance_bps": basis_bps(price, target),
            "rolling_60s_mean": mean,
            "rolling_60s_count": count,
            "rolling_60s_span_ms": span,
            # Never basis against ourselves, and never against a stand-in.
            "basis_bps": (
                None if source == "brti" else basis_bps(price, reference_price)
            ),
            "reference_source": None if source == "brti" else "brti",
            "remaining_s": remaining,
            "error": observation.error,
        })

    # ---------------------------------------------------------------- gaps

    def _open_gap(self, source: str, reason: str, detail: str | None, now_ms: int) -> None:
        key = (source, reason)
        gap = self._open_gaps.get(key)
        if gap is None:
            self._open_gaps[key] = {
                "session_id": self._session_id, "source": source, "reason": reason,
                "detail": (detail or "")[:200], "first_ms": now_ms,
                "last_ms": now_ms, "polls": 1,
            }
            return
        gap["last_ms"] = now_ms
        gap["polls"] += 1

    def _close_gap(self, source: str) -> None:
        for key in [k for k in self._open_gaps if k[0] == source]:
            self._store.record_gap(self._open_gaps.pop(key))

    def _flush_gaps(self) -> None:
        for key in list(self._open_gaps):
            with contextlib.suppress(Exception):
                self._store.record_gap(self._open_gaps.pop(key))

    # ------------------------------------------------------- reconciliation

    async def reconcile(self, now_ms: int) -> None:
        """Check our computed averages against Kalshi's official settlements.

        Runs on its own slow beat. Never raises.
        """
        try:
            interval = self._settings.reference_reconcile_seconds * 1000
            if now_ms - self._last_reconcile_ms < interval:
                return
            self._last_reconcile_ms = now_ms
            await self._reconcile(now_ms)
        except Exception as exc:  # noqa: BLE001 - research must never stop trading
            print(f"reference reconciliation failed: {exc!r}", flush=True)
            if self._settings.reference_debug:
                traceback.print_exc()

    async def _reconcile(self, now_ms: int) -> None:
        markets, _ = await self._kalshi.settled(limit=50)
        done = self._store.reconciled_tickers()
        for market in markets:
            ticker = market.get("ticker")
            if not ticker or ticker in done:
                continue
            official = market.get("expiration_value")
            strike = market.get("floor_strike")
            if official in (None, "") or strike is None:
                continue
            try:
                official = float(official)
            except (TypeError, ValueError):
                continue
            close_ms = _iso_ms(market.get("close_time"))
            open_ms = _iso_ms(market.get("open_time"))
            if not close_ms:
                continue
            await self._reconcile_one(
                ticker, open_ms, close_ms, official, float(strike),
                market.get("result"), now_ms,
            )

    async def _reconcile_one(
        self, ticker: str, open_ms: int, close_ms: int, official: float,
        strike: float, result: str | None, now_ms: int,
    ) -> None:
        """One settled market, with the decomposition the operator asked for."""
        start = close_ms - 60_000

        # Our own BRTI ticks for that final minute, if the feed was entitled.
        recorded = [
            (stamp, row["raw_price"])
            for row in self._store.observations(open_ms, "brti")
            if row["raw_price"] is not None
            and start <= (stamp := row["event_ms"] or row["received_ms"]) < close_ms
        ]
        computed = sum(p for _, p in recorded) / len(recorded) if recorded else None

        # Binance per-second over the SAME sixty seconds. Cached so a rerun is
        # free and a rate limit cannot half-write a row.
        bars = self._store.second_bars("binance_spot", start, close_ms)
        if len(bars) < 30:
            try:
                bars = await self._binance.seconds(start, close_ms)
                if bars:
                    self._store.save_second_bars("binance_spot", bars)
            except Exception as exc:  # noqa: BLE001
                print(f"reference: 1s fetch failed for {ticker}: {exc!r}", flush=True)
                bars = []

        binance_mean = sum(p for _, p in bars) / len(bars) if bars else None
        binance_last = bars[-1][1] if bars else None

        # THE DECOMPOSITION. Each line holds one variable fixed:
        #   feed    - both sides are 60-second means, so only the feed differs
        #   aggreg. - both sides are Binance, so only the averaging differs
        feed = basis_bps(binance_mean, official)
        aggregation = basis_bps(binance_last, binance_mean)
        total = basis_bps(binance_last, official)

        self._store.record_reconciliation({
            "ticker": ticker,
            "window_open_ms": open_ms,
            "close_ms": close_ms,
            "official_expiration_value": official,
            "official_strike": strike,
            "official_result": result,
            "computed_brti_mean": computed,
            "computed_brti_count": len(recorded),
            "computed_brti_error": None if computed is None else computed - official,
            "computed_brti_error_bps": basis_bps(computed, official),
            "binance_60s_mean": binance_mean,
            "binance_60s_count": len(bars),
            "binance_last": binance_last,
            "feed_basis_bps": feed,
            "aggregation_bps": aggregation,
            "total_bps": total,
            "outcome_official": int(official >= strike),
            "outcome_binance_last": (
                None if binance_last is None else int(binance_last >= strike)
            ),
            "outcome_binance_60s": (
                None if binance_mean is None else int(binance_mean >= strike)
            ),
            "reconciled_ms": now_ms,
            "note": None if recorded else "no BRTI ticks recorded for this window",
        })


def _iso_ms(value: str | None) -> int:
    if not value:
        return 0
    from .kalshi import iso_ms

    try:
        return iso_ms(value)
    except (ValueError, TypeError):
        return 0
