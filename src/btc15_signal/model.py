from dataclasses import dataclass
from math import exp

from .snapshot import MarketSnapshot


@dataclass(frozen=True)
class Prediction:
    side: str
    raw_probability: float
    distance_bps: float
    score: float
    bucket: int


def predict(snapshot: MarketSnapshot) -> Prediction:
    signed_distance = (snapshot.price / snapshot.target - 1) * 10_000
    side = "UP" if signed_distance >= 0 else "DOWN"
    direction = 1 if side == "UP" else -1
    normalized_distance = abs(signed_distance) / max(snapshot.volatility_5m_bps, 1.0)
    score = (
        1.15 * normalized_distance
        + 0.75 * direction * snapshot.momentum_5m_bps / max(snapshot.volatility_5m_bps, 1.0)
        + 0.55 * direction * snapshot.bid_imbalance
        + 0.65 * direction * snapshot.taker_imbalance
        + 0.10 * direction * snapshot.futures_basis_bps
        - 1.1
    )
    probability = 1 / (1 + exp(-max(min(score, 12), -12)))
    bucket = min(9, max(0, int(probability * 10)))
    return Prediction(side, probability, abs(signed_distance), score, bucket)
