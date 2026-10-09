"""Offline, causal five-minute BRTI market-regime measurements.

Only the local reference SQLite archive is read. No trading code is imported.
Each bar uses event timestamps in (bar start, bar end] and observations already
received by bar end. These are sampled BRTI OHLC bars, not exchange tick candles.

``build_states(path, start_ms, end_ms)`` returns completed bars with ``at_ms``
between the requested endpoints. A signal may use the last state whose at_ms is
<= its decision timestamp. Missing data remains unknown, never assumed choppy.
"""
from __future__ import annotations

import math
import sqlite3
from collections import deque
from contextlib import closing
from pathlib import Path
from typing import Iterable

BAR_MS = 300_000
MAX_GAP_MS = 60_000
PERIOD = 14
WARMUP_MS = 2 * 86_400_000


def states_from_bars(bars: Iterable[dict]) -> list[dict]:
    """Calculate indicators, resetting all history after any invalid bar.

    Wilder ATR/DM seed with the first 14 true ranges/directional moves. The
    first bar contributes high-low and zero DM. ADX seeds from the first 14 DX
    values, then uses Wilder smoothing. ``valid`` additionally requires SMA50
    and its three-bar (15-minute) slope, i.e. at least 53 continuous good bars.
    """
    output: list[dict] = []
    highs: deque[float] = deque(maxlen=PERIOD)
    lows: deque[float] = deque(maxlen=PERIOD)
    trs: deque[float] = deque(maxlen=PERIOD)
    closes: deque[float] = deque(maxlen=50)
    mas: deque[tuple[float, float]] = deque(maxlen=4)
    previous = None
    count = 0
    tr_seed = plus_seed = minus_seed = 0.0
    atr = plus_dm = minus_dm = adx = None
    dx_seed: list[float] = []
    for bar in bars:
        row = dict(bar)
        row.update(bar_valid=bool(bar['valid']), valid=False, adx=None,
                   chop=None, ma_gap_bps=None, ma_flat=None, regime_chop=None,
                   atr=None, sma20=None, sma50=None, ma20_slope_bps=None,
                   ma50_slope_bps=None, chop_votes=None)
        if not bar['valid']:
            highs.clear(); lows.clear(); trs.clear(); closes.clear(); mas.clear()
            previous = None
            count = 0
            tr_seed = plus_seed = minus_seed = 0.0
            atr = plus_dm = minus_dm = adx = None
            dx_seed.clear()
            output.append(row)
            continue
        high, low, close = bar['high'], bar['low'], bar['close']
        if previous is None:
            tr = high - low
            pdm = mdm = 0.0
        else:
            tr = max(high - low, abs(high - previous['close']),
                     abs(low - previous['close']))
            up = high - previous['high']
            down = previous['low'] - low
            pdm = up if up > down and up > 0 else 0.0
            mdm = down if down > up and down > 0 else 0.0
        previous = bar
        count += 1
        highs.append(high); lows.append(low); trs.append(tr); closes.append(close)
        if count <= PERIOD:
            tr_seed += tr; plus_seed += pdm; minus_seed += mdm
            if count == PERIOD:
                atr = tr_seed / PERIOD
                plus_dm = plus_seed / PERIOD
                minus_dm = minus_seed / PERIOD
        else:
            atr = (atr * (PERIOD - 1) + tr) / PERIOD
            plus_dm = (plus_dm * (PERIOD - 1) + pdm) / PERIOD
            minus_dm = (minus_dm * (PERIOD - 1) + mdm) / PERIOD
        if atr is not None:
            # DI's common ATR factor cancels in DX. Zero movement has DX=0.
            dm_total = plus_dm + minus_dm
            dx = 100 * abs(plus_dm - minus_dm) / dm_total if dm_total else 0.0
            if adx is None:
                dx_seed.append(dx)
                if len(dx_seed) == PERIOD:
                    adx = sum(dx_seed) / PERIOD
            else:
                adx = (adx * (PERIOD - 1) + dx) / PERIOD
            row['atr'] = atr
            row['adx'] = adx
        if len(trs) == PERIOD:
            price_range = max(highs) - min(lows)
            # Degenerate constant prices are maximal chop; no directional edge.
            row['chop'] = (100 * math.log10(sum(trs) / price_range)
                           / math.log10(PERIOD)) if price_range > 0 else 100.0
        if len(closes) == 50:
            sma20 = sum(list(closes)[-20:]) / 20
            sma50 = sum(closes) / 50
            mas.append((sma20, sma50))
            row.update(sma20=sma20, sma50=sma50,
                       ma_gap_bps=abs(sma20 / sma50 - 1) * 10_000)
            if len(mas) == 4:
                slope20 = abs(sma20 / mas[0][0] - 1) * 10_000
                slope50 = abs(sma50 / mas[0][1] - 1) * 10_000
                row.update(ma20_slope_bps=slope20, ma50_slope_bps=slope50,
                           ma_flat=row['ma_gap_bps'] <= 10 and
                           slope20 <= 5 and slope50 <= 5)
        if row['adx'] is not None and row['chop'] is not None and row['ma_flat'] is not None:
            votes = int(row['adx'] < 20) + int(row['chop'] > 61.8) + int(row['ma_flat'])
            row.update(valid=True, chop_votes=votes, regime_chop=votes >= 2)
        output.append(row)
    return output


def build_states(ref_path: str | Path, start: int, end: int) -> list[dict]:
    """Read an archive in read-only mode and return completed causal states.

    Bar quality requires >=5 unique event timestamps, a maximum sampling gap of
    60 seconds including the two bar edges, and all observations received by
    bar end. ``stale=1`` values are excluded. For duplicate event timestamps the
    earliest received eligible observation wins. A full two days of warmup is
    loaded without including any data past ``end``.
    """
    if end < start:
        raise ValueError('end must not precede start')
    warm_start = ((start - WARMUP_MS) // BAR_MS) * BAR_MS
    last_end = (end // BAR_MS) * BAR_MS
    buckets: dict[int, dict[int, float]] = {}
    uri = Path(ref_path).resolve().as_uri() + '?mode=ro'
    with closing(sqlite3.connect(uri, uri=True)) as conn:
        conn.execute('PRAGMA query_only=ON')
        for ts, received, price in conn.execute(
            'SELECT ts_ms,received_ms,brti_value FROM brti_features '
            'WHERE stale=0 AND ts_ms>? AND ts_ms<=? AND received_ms<=? '
            'ORDER BY ts_ms,received_ms,id', (warm_start, last_end, last_end)
        ):
            bar_end = ((ts + BAR_MS - 1) // BAR_MS) * BAR_MS
            if received > bar_end or not math.isfinite(price) or price <= 0:
                continue
            buckets.setdefault(bar_end, {}).setdefault(ts, float(price))
    bars = []
    for at in range(warm_start + BAR_MS, last_end + 1, BAR_MS):
        ticks = sorted(buckets.get(at, {}).items())
        times = [at - BAR_MS] + [t for t, _ in ticks] + [at]
        max_gap = max(b - a for a, b in zip(times, times[1:]))
        prices = [p for _, p in ticks]
        bars.append(dict(at_ms=at, valid=len(ticks) >= 5 and max_gap <= MAX_GAP_MS,
                         open=prices[0] if prices else None,
                         high=max(prices) if prices else None,
                         low=min(prices) if prices else None,
                         close=prices[-1] if prices else None,
                         samples=len(ticks), max_gap_ms=max_gap))
    return [state for state in states_from_bars(bars) if start <= state['at_ms'] <= end]


def self_test() -> None:
    """Dependency-free synthetic checks, including delayed-data exclusion."""
    import tempfile

    trend = [dict(at_ms=i * BAR_MS, valid=True, high=100 + i + 0.5,
                  low=100 + i - 0.5, close=100 + i) for i in range(100)]
    t = states_from_bars(trend)[-1]
    assert t['valid'] and abs(t['adx'] - 100) < 1e-9
    assert not t['regime_chop'] and t['chop'] < 20
    flat = [dict(at_ms=i * BAR_MS, valid=True, high=100.02,
                 low=99.98, close=100 + (0.005 if i % 2 else -0.005)) for i in range(100)]
    f = states_from_bars(flat)[-1]
    assert f['valid'] and f['adx'] == 0 and f['chop'] > 99
    assert f['ma_flat'] and f['regime_chop']
    disrupted = flat[:60] + [dict(flat[60], valid=False)] + flat[61:]
    assert all(not r['valid'] for r in states_from_bars(disrupted)[60:])
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / 'reference.db'
        with closing(sqlite3.connect(path)) as conn:
            conn.execute('CREATE TABLE brti_features '
                         '(id INTEGER PRIMARY KEY,ts_ms INTEGER,received_ms INTEGER,brti_value REAL,stale INTEGER)')
            rows = [(ms, ms, 100.0, 0) for ms in range(10_000, BAR_MS * 60 + 1, 10_000)]
            conn.executemany('INSERT INTO brti_features(ts_ms,received_ms,brti_value,stale) VALUES (?,?,?,?)', rows)
            # A spectacular intrabar price only arrives after the bar ends.
            conn.execute('INSERT INTO brti_features(ts_ms,received_ms,brti_value,stale) VALUES (?,?,?,?)',
                         (BAR_MS * 60 - 5_000, BAR_MS * 60 + 1, 9999.0, 0))
            conn.commit()
        states = build_states(path, BAR_MS, BAR_MS * 60)
        assert states[-1]['valid'] and states[-1]['high'] == 100
        assert states[-1]['at_ms'] == BAR_MS * 60
        assert build_states(path, BAR_MS, BAR_MS * 60 - 1)[-1]['at_ms'] == BAR_MS * 59


if __name__ == '__main__':
    self_test()
    print('OHLC regime self-tests passed')
