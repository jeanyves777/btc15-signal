import json
from dataclasses import dataclass
from pathlib import Path

from .binance import MarketSnapshot
from .model import Prediction


@dataclass(frozen=True)
class EntryRule:
    enabled: bool = False
    remaining_minutes: int = 5
    min_raw_probability: float = 0.90
    min_ask: float = 0.40
    max_ask: float = 0.90
    min_normalized_distance: float = 0.0
    require_momentum_alignment: bool = False
    fee_buffer: float = 0.02

    @classmethod
    def load(cls, path: str) -> "EntryRule":
        source = Path(path)
        if not source.exists():
            return cls()
        return cls(**json.loads(source.read_text()))

    def matches(
        self, prediction: Prediction, snapshot: MarketSnapshot, ask: float
    ) -> tuple[bool, str]:
        normalized_distance = prediction.distance_bps / max(snapshot.volatility_5m_bps, 1.0)
        direction = 1 if prediction.side == "UP" else -1
        momentum_aligned = direction * snapshot.momentum_5m_bps > 0
        checks = [
            (prediction.raw_probability >= self.min_raw_probability, "model confidence"),
            (self.min_ask <= ask <= self.max_ask, "contract price band"),
            (normalized_distance >= self.min_normalized_distance, "target distance"),
            (
                not self.require_momentum_alignment or momentum_aligned,
                "momentum alignment",
            ),
        ]
        failed = [label for passed, label in checks if not passed]
        return not failed, ", ".join(failed)


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
        return cls(**json.loads(source.read_text()))

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
