"""Exploratory post-shock contraction features; offline, causal input only.

This candidate was formed AFTER inspecting October 6. It is not an independently
validated filter. The source is sampled Kalshi BRTI 60-second means, not venue
OHLC. Every minute value must have been received by that minute (Tape.value).
"""
from __future__ import annotations

import bisect
from collections import Counter

MINUTE = 60_000


def add_postshock(tape, shock_bps=100.0, after_hours=8.0,
                  contraction_ratio=0.5, efficiency_limit=0.25,
                  breakout_buffer_bps=2.0):
    """Attach a provisional postshock flag to each tape.signals dictionary.

    A shock is an absolute one-hour close-to-close reference move >= shock_bps.
    Use the most recent qualifying minute in the previous after_hours. Flag
    only when the current hour is inefficient, its preceding range has shrunk
    relative to the shock hour, and the last two completed minute samples do
    not both clear that fixed preceding range in the signal direction.

    The range excludes the two confirmation minutes; it cannot chase their
    highs/lows. This is a signal-time test, NOT a persistent lock. Missing
    required current/shock histories pass through with explicit coverage flags.
    """
    if shock_bps <= 0 or after_hours <= 0 or contraction_ratio <= 0:
        raise ValueError("shock, lookback, and contraction thresholds must be positive")
    if not 0 <= efficiency_limit <= 1 or breakout_buffer_bps < 0:
        raise ValueError("invalid efficiency or breakout threshold")
    if not tape.signals:
        return {"signals": 0, "flagged": 0, "coverage": {}}

    first = min(s['at'] for s in tape.signals) // MINUTE * MINUTE
    last = max(s['at'] for s in tape.signals) // MINUTE * MINUTE
    lookback = round(after_hours * 60) * MINUTE
    start = first - lookback - 60 * MINUTE
    prices = {at: tape.value(at) for at in range(start, last + 1, MINUTE)}
    shocks = {}
    for at in range(start + 60 * MINUTE, last + 1, MINUTE):
        current, old = prices[at], prices[at - 60 * MINUTE]
        if current is None or old is None or old <= 0:
            continue
        move = (current / old - 1) * 10_000
        if abs(move) >= shock_bps:
            shocks[at] = move
    shock_times = sorted(shocks)
    coverage = Counter()
    buffer = breakout_buffer_bps / 10_000

    for signal in tape.signals:
        at = signal['at'] // MINUTE * MINUTE
        row = dict(postshock=False, postshock_coverage=False,
                   postshock_reason='no_recent_shock',
                   recent_shock_ms=None, recent_shock_bps=None,
                   shock_range=None, postshock_range=None,
                   postshock_range_ratio=None, postshock_er60=None,
                   postshock_breakout=None)
        p = [prices[at - k * MINUTE] for k in range(60, -1, -1)]
        if any(value is None for value in p):
            row['postshock_reason'] = 'missing_current_hour'
        else:
            travel = sum(abs(b - a) for a, b in zip(p, p[1:]))
            efficiency = abs(p[-1] - p[0]) / travel if travel else 0.0
            high, low = max(p[:-2]), min(p[:-2])
            breakout = (all(v > high * (1 + buffer) for v in p[-2:])
                        if signal['side'] == 'UP' else
                        all(v < low * (1 - buffer) for v in p[-2:]))
            row.update(postshock_er60=efficiency,
                       postshock_range=high - low, postshock_breakout=breakout)
            index = bisect.bisect_right(shock_times, at) - 1
            if index < 0 or shock_times[index] < at - lookback:
                row['postshock_coverage'] = True
            else:
                shock_at = shock_times[index]
                row.update(recent_shock_ms=shock_at,
                           recent_shock_bps=shocks[shock_at])
                shock_prices = [prices[shock_at - k * MINUTE]
                                for k in range(60, -1, -1)]
                if any(value is None for value in shock_prices):
                    row['postshock_reason'] = 'missing_shock_hour'
                else:
                    shock_range = max(shock_prices) - min(shock_prices)
                    ratio = (high - low) / shock_range if shock_range else None
                    flagged = (ratio is not None and ratio <= contraction_ratio
                               and efficiency <= efficiency_limit and not breakout)
                    row.update(postshock_coverage=True, shock_range=shock_range,
                               postshock_range_ratio=ratio, postshock=flagged,
                               postshock_reason='flagged' if flagged else 'conditions_not_met')
        signal.update(row)
        coverage[row['postshock_reason']] += 1

    return dict(signals=len(tape.signals),
                flagged=sum(s['postshock'] for s in tape.signals),
                coverage=dict(coverage),
                settings=dict(shock_bps=shock_bps, after_hours=after_hours,
                              contraction_ratio=contraction_ratio,
                              efficiency_limit=efficiency_limit,
                              breakout_buffer_bps=breakout_buffer_bps))
