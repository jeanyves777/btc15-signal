"""One-off: close out past days whose last (23:45) market settled after midnight and
was counted in neither day (2026-10-01; see daily_profit.DailyProfitGuard.close_out).
Reads Kalshi (fills, settlements) per account; writes ONLY those past days' rows.

    python scripts/backfill_day_closeout.py 2026-09-29 2026-09-30
"""
import asyncio
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from btc15_signal.config import Settings  # noqa: E402
from btc15_signal.daily_profit import DailyProfitGuard, realised_events  # noqa: E402
from btc15_signal.execution import KalshiExecutionClient  # noqa: E402
from btc15_signal.mirror import targets_from_settings  # noqa: E402

NY = ZoneInfo("America/New_York")
DB = ROOT / "runtime" / "daily_profit.db"


async def main(days):
    s = Settings()
    accounts = [("primary", "You", s.kalshi_api_key_id, s.kalshi_private_key_path)]
    accounts += [(t.name, getattr(s, f"mirror_{t.name[1:]}_label", t.name), t.api_key_id,
                  t.private_key_path) for t in targets_from_settings(s)]
    for day in days:
        start = int(datetime.fromisoformat(day).replace(tzinfo=NY).timestamp() * 1000)
        midnight = int((datetime.fromisoformat(day) + timedelta(days=1)).replace(
            tzinfo=NY).timestamp() * 1000)
        for name, label, key, path in accounts:
            client = KalshiExecutionClient(s.kalshi_base_url, key, path)
            g = DailyProfitGuard(DB, name, label, client)
            try:
                if g.connect().execute("SELECT 1 FROM profit_days WHERE account=? AND day=?",
                                       (name, day)).fetchone() is None:
                    print(f"{day} {label}: no row")
                    continue
                fills = await g.pages("/portfolio/fills", "fills", start)
                settlements = await g.pages("/portfolio/settlements", "settlements", start)
                got = g.close_out(realised_events(fills, settlements), midnight,
                                  midnight + DailyProfitGuard.CLOSE_OUT_MS)
                print(f"{day} {label}: {got:+.2f}")
            finally:
                await client.client.aclose()


asyncio.run(main(sys.argv[1:] or ["2026-09-29", "2026-09-30"]))
