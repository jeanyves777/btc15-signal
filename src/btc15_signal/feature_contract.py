"""ONE feature contract, enforced at runtime by a fingerprint.

The recurring failure in this system is never an exception. It is a number
arriving under the wrong name and every downstream calculation proceeding
normally:

  * live keyed contexts on Binance distance bands while candidates were
    frozen on BRTI bands - the key still formatted, it just named a pocket
    no artefact contained;
  * the deployed policy declared `feature_version: brti-1` over seven arms
    that were entirely Binance - a mislabel that defeated the version guard
    built to catch exactly it.

Both were caught by reading code. Neither would have been caught by running
it, and a comment saying "keep these in sync" is what was already there.

So the definitions live in ONE frozen object and are hashed. Every artefact
records the fingerprint it was fitted under; every decision checks it. A
changed lookback, band boundary, unit, cadence or cutoff rule changes the
hash, and the mismatch is refused with the difference named.

WHAT IS IN THE CONTRACT is everything that changes what a number MEANS:

    source          which feed, and which endpoint
    units           bps, and what the ratio is against
    sampling        cadence of the series
    smoothing       BRTI is a published trailing 60s mean; we add none
    lookbacks       momentum and volatility windows
    cutoff          strictly `t <= decision_ms`, so no future sample can
                    change an earlier decision
    bands           the boundaries every context key is cut on
"""

import hashlib
import json
from dataclasses import asdict, dataclass, field

from .adaptive import (
    SETUP_FEATURE_VERSION,
    BRTI_DISTANCE_BANDS,
    BRTI_MOMENTUM_BANDS,
    BRTI_VOL_BANDS,
    SETUP_PRICE_BANDS,
)


@dataclass(frozen=True)
class FeatureContract:
    """Frozen. Changing any field changes the fingerprint, by design."""

    version: str = SETUP_FEATURE_VERSION
    family: str = "brti"
    # Kalshi only. No Binance endpoint appears here, and none may be added:
    # a fallback that silently re-bases onto another exchange is the failure
    # this whole contract exists to make impossible.
    source: str = "kalshi:/live_data/events/{event} + /cfbenchmarks/values"
    price_source: str = "kalshi:orderbook"
    units: str = "bps; normalized_distance = |signed_bps| / volatility_bps"
    sampling_cadence_s: int = 1
    # BRTI is PUBLISHED as a trailing 60-second mean. We add nothing on top;
    # averaging it again double-smooths and was measured doing so (0/25
    # settlement reproductions before the fix, 12/12 after).
    smoothing: str = "none added; BRTI is a published trailing 60s mean"
    momentum_window_s: int = 300
    volatility_window_s: int = 300
    # THE LEVEL LOOKBACK, widened from 900s to 2,700s on 2026-09-25 and the
    # reason this contract was cut anew rather than edited in place. It landed as
    # brti-3 and the contract is now brti-4; the version is read from
    # `adaptive.SETUP_FEATURE_VERSION`, which is the ONE definition, so it is
    # deliberately not restated here - a second literal is exactly the drift
    # that made `learning_data` discard every row once already.
    #
    # At 900s the lookback was the market's OWN window, and the strike IS that
    # window's opening price - so every window began with price on the strike,
    # inside the rejection threshold, and a clean one-way move scored exactly
    # one rejection: its own departure. The `rejections >= 2` gate therefore
    # refused the cleanest setups, and over 19,305 corpus points the refused
    # bucket led on distance, hold, momentum and win rate alike.
    #
    # The feed always carried the fix - each market's series is the hour ending
    # at its close, so 45 minutes before the window opened was fetched and
    # discarded. A rejection now means price approached this level and was
    # turned back, which is what the name always claimed.
    #
    # EVERY THRESHOLD MEASURED UNDER brti-2 IS INVALIDATED BY THIS, which is
    # precisely what the fingerprint guard is for: the five live policies will
    # refuse to load and refit rather than apply numbers fitted to a different
    # quantity.
    level_window_s: int = 2700
    # STRICT. A sample at exactly the decision instant is in; anything after
    # it is out. This is what makes a replayed decision reproducible.
    cutoff_rule: str = "t <= decision_ms"
    distance_bands: tuple = field(default=BRTI_DISTANCE_BANDS)
    vol_bands: tuple = field(default=BRTI_VOL_BANDS)
    # Cut where the RULE cuts: 0.70 and 0.93, not 0.94.
    price_bands: tuple = field(default=SETUP_PRICE_BANDS)
    # MOMENTUM IS PART OF THE SETUP. It is one of the four deployed gates and
    # `brti-1` left it out of the key entirely, so a cell mixed aligned and
    # unaligned setups and the layer could not see the difference.
    momentum_bands: tuple = field(default=BRTI_MOMENTUM_BANDS)
    # What the key is BUILT FROM. Session and volatility regime are recorded
    # against every decision as context and are deliberately NOT here: keying
    # on them fragmented the evidence into cells too small to speak.
    key_dimensions: tuple = ("distance", "price", "momentum")
    # RECORDED, NOT KEYED. The 45-minute momentum and volatility sit beside
    # the 300-second pair rather than replacing them: the gates read the short
    # window, exactly as before, and these answer whether the last five minutes
    # are typical of the last forty-five. A 300s window cannot tell a market
    # that has drifted all hour from one that just turned - both read the same
    # momentum, and only the wider view separates them.
    #
    # They are in the contract because the learning can see them, and anything
    # the learning can see has to be declared - that is what the fingerprint is
    # for. Adding them moves it, so every policy refits; the fitted quantities
    # are unchanged, so the arms come back the same.
    context_recorded: tuple = ("session", "vol_regime", "band_hold_s",
                               "remaining_s", "momentum_45m", "volatility_45m")

    def payload(self) -> dict:
        data = asdict(self)
        # Tuples of tuples round-trip through JSON as lists; normalise so the
        # hash does not depend on which side computed it.
        for key in ("distance_bands", "vol_bands", "price_bands",
                    "momentum_bands"):
            data[key] = [list(b) for b in data[key]]
        for key in ("key_dimensions", "context_recorded"):
            data[key] = list(data[key])
        return data

    def fingerprint(self) -> str:
        blob = json.dumps(self.payload(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(blob.encode()).hexdigest()[:16]

    def differences(self, other_payload: dict) -> list[str]:
        """Which fields disagree. A fingerprint says no; this says why."""
        mine = self.payload()
        out = []
        for key in sorted(set(mine) | set(other_payload or {})):
            a, b = mine.get(key, "<absent>"), (other_payload or {}).get(key, "<absent>")
            if a != b:
                out.append(f"{key}: {a!r} != {b!r}")
        return out


CONTRACT = FeatureContract()
FINGERPRINT = CONTRACT.fingerprint()


def compatible(fingerprint: str | None) -> bool:
    """Unknown is NOT compatible.

    An artefact that does not state what it was fitted under cannot be shown
    to match, and "cannot be shown to match" is the same as "must not act".
    """
    return bool(fingerprint) and fingerprint == FINGERPRINT


def describe() -> str:
    return (f"{CONTRACT.version} fp={FINGERPRINT} "
            f"src={CONTRACT.family} "
            f"mom={CONTRACT.momentum_window_s}s "
            f"vol={CONTRACT.volatility_window_s}s "
            f"cutoff='{CONTRACT.cutoff_rule}'")
