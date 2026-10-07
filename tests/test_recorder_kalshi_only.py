"""The microstructure recorder is KALSHI ONLY (operator: "Never use anything related to
Binance", "Kalshi only"; found still calling Binance 2026-10-05)."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from btc15_signal import recorder  # noqa: E402


class Client:
    def __init__(self):
        self.urls = []

    def get(self, url, params=None):
        self.urls.append(url)
        if url.endswith("/markets"):
            body = {"markets": [{"ticker": "KXBTC15M-T", "open_time": "2000-01-01T00:00:00Z",
                                 "close_time": "2999-01-01T00:00:00Z", "floor_strike": 1.0,
                                 "yes_bid_dollars": "0.40", "yes_ask_dollars": "0.42"}]}
        else:
            body = {"orderbook_fp": {"yes_dollars": [["0.40", "10"]], "no_dollars": [["0.58", "7"]]}}
        return type("R", (), {"json": lambda self, b=body: b})()


def test_it_never_calls_binance_and_still_records_the_kalshi_book(tmp_path):
    rec = recorder.Recorder(str(tmp_path / "m.db"))
    rec.limiter.acquire = lambda: None
    client = Client()
    assert rec.capture(client, "https://api.elections.kalshi.com/trade-api/v2") == "KXBTC15M-T"
    assert client.urls and not any("binance" in u for u in client.urls), client.urls
    row = rec.db.execute("SELECT yes_bid, yes_ask, book_yes, book_no, btc_bid, buy_volume, "
                         "binance_ms FROM book_snapshots").fetchone()
    assert row[0] == 0.40 and row[1] == 0.42
    assert json.loads(row[2]) and json.loads(row[3]), "the Kalshi book"
    assert row[4] is None and row[5] is None and row[6] is None, "no Binance numbers"
