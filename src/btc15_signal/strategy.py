import json
from dataclasses import dataclass, fields
from pathlib import Path

from .binance import MarketSnapshot
from .model import Prediction


def _known_fields(cls, raw: dict) -> dict:
    """Drop keys this version of the rule does not define.

    A rule file is edited while the service is running. Passing an unknown key
    straight into the dataclass raises TypeError inside the poll loop and takes
    the whole service down - which is exactly what adding `min_momentum_bps` to
    strategy.json did to a process running the previous build. An unrecognised
    setting should be ignored, never fatal.
    """
    allowed = {f.name for f in fields(cls)}
    unknown = set(raw) - allowed
    if unknown:
        print(f"{cls.__name__}: ignoring unknown setting(s) {sorted(unknown)}", flush=True)
    return {key: value for key, value in raw.items() if key in allowed}


@dataclass(frozen=True)
class EntryRule:
    enabled: bool = False
    remaining_minutes: int = 5
    min_raw_probability: float = 0.90
    min_ask: float = 0.40
    max_ask: float = 0.90
    min_normalized_distance: float = 0.0
    require_momentum_alignment: bool = False
    # Momentum in the direction of the trade, in bps over the last five minutes.
    # Measured over 68 days in the 0.85-0.99 band, 6-11 minutes out: baseline
    # +$0.0105/contract, aligned +$0.0107, >=5 bps +$0.0112, >=10 bps +$0.0137.
    # It does NOT rescue cheaper bands - at 0.80-0.85 the same >=10 bps filter
    # measures -$0.0096 - and 4-hour trend alignment is worse than no filter.
    min_momentum_bps: float = 0.0
    fee_buffer: float = 0.02

    @classmethod
    def load(cls, path: str) -> "EntryRule":
        source = Path(path)
        if not source.exists():
            return cls()
        return cls(**_known_fields(cls, json.loads(source.read_text())))

    def matches(
        self, prediction: Prediction, snapshot: MarketSnapshot, ask: float
    ) -> tuple[bool, str]:
        normalized_distance = prediction.distance_bps / max(snapshot.volatility_5m_bps, 1.0)
        direction = 1 if prediction.side == "UP" else -1
        signed_momentum = direction * snapshot.momentum_5m_bps
        momentum_aligned = signed_momentum > 0
        checks = [
            (prediction.raw_probability >= self.min_raw_probability, "model confidence"),
            (self.min_ask <= ask <= self.max_ask, "contract price band"),
            (normalized_distance >= self.min_normalized_distance, "target distance"),
            (
                not self.require_momentum_alignment or momentum_aligned,
                "momentum alignment",
            ),
            (signed_momentum >= self.min_momentum_bps, "momentum strength"),
        ]
        failed = [label for passed, label in checks if not passed]
        return not failed, ", ".join(failed)

    def check_detail(
        self, prediction: Prediction, snapshot: MarketSnapshot, ask: float
    ) -> list[tuple[str, bool, str]]:
        """Every gate with its verdict and the actual numbers, for display.

        A bare "rule says no: contract price band" does not say how close it
        was, and a near miss is a different decision from a clear reject.
        """
        distance = prediction.distance_bps / max(snapshot.volatility_5m_bps, 1.0)
        direction = 1 if prediction.side == "UP" else -1
        momentum = direction * snapshot.momentum_5m_bps
        return [
            (
                "price band",
                self.min_ask <= ask <= self.max_ask,
                f"{ask:.0%} vs {self.min_ask:.0%}-{self.max_ask:.0%}",
            ),
            (
                "momentum",
                momentum >= self.min_momentum_bps
                and (not self.require_momentum_alignment or momentum > 0),
                f"{momentum:+.1f} bps vs >={self.min_momentum_bps:.0f}",
            ),
            (
                "distance",
                distance >= self.min_normalized_distance,
                f"{distance:.1f}x vol vs >={self.min_normalized_distance:.1f}",
            ),
            (
                "model",
                prediction.raw_probability >= self.min_raw_probability,
                f"{prediction.raw_probability:.0%} vs >={self.min_raw_probability:.0%}",
            ),
        ]


@dataclass(frozen=True)
class ReversionSetup:
    side: str
    key_level: float
    spike_bps: float
    rejection_bps: float
    distance_bps: float


@dataclass(frozen=True)
class ReversionRule:
    enabled: bool = False
    min_remaining_minutes: int = 10
    max_remaining_minutes: int = 12
    min_entry_price: float = 0.30
    max_entry_price: float = 0.35
    min_spike_bps: float = 8.0
    min_rejection_bps: float = 1.0
    max_rejection_bps: float = 12.0
    max_distance_bps: float = 35.0
    take_profit_price: float = 0.50
    fee_buffer: float = 0.02

    @classmethod
    def load(cls, path: str) -> "ReversionRule":
        source = Path(path)
        if not source.exists():
            return cls()
        return cls(**_known_fields(cls, json.loads(source.read_text())))

    def setup(self, snapshot: MarketSnapshot, remaining_minutes: int) -> ReversionSetup | None:
        if not self.min_remaining_minutes <= remaining_minutes <= self.max_remaining_minutes:
            return None
        if snapshot.window_high <= 0 or snapshot.window_low <= 0:
            return None
        signed_distance = (snapshot.price / snapshot.target - 1) * 10_000
        if signed_distance > 0:
            setup = ReversionSetup(
                side="DOWN",
                key_level=snapshot.window_high,
                spike_bps=(snapshot.window_high / snapshot.target - 1) * 10_000,
                rejection_bps=(snapshot.window_high / snapshot.price - 1) * 10_000,
                distance_bps=abs(signed_distance),
            )
        elif signed_distance < 0:
            setup = ReversionSetup(
                side="UP",
                key_level=snapshot.window_low,
                spike_bps=(1 - snapshot.window_low / snapshot.target) * 10_000,
                rejection_bps=(snapshot.price / snapshot.window_low - 1) * 10_000,
                distance_bps=abs(signed_distance),
            )
        else:
            return None
        if not (
            setup.spike_bps >= self.min_spike_bps
            and self.min_rejection_bps <= setup.rejection_bps <= self.max_rejection_bps
            and setup.distance_bps <= self.max_distance_bps
        ):
            return None
        return setup

    def matches(
        self, snapshot: MarketSnapshot, remaining_minutes: int, entry_price: float
    ) -> tuple[ReversionSetup | None, str]:
        setup = self.setup(snapshot, remaining_minutes)
        failed = []
        if setup is None:
            failed.append("spike/rejection structure")
        if not self.min_entry_price <= entry_price <= self.max_entry_price:
            failed.append("30-35 cent entry band")
        return (setup if not failed else None), ", ".join(failed)
