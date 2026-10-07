"""A mirror account follows an on/off switch per instrument (operator,
2026-09-28: "make the wife mirror account or any other mirror account follow
an on/off flag per asset ... now I want it to only trade BTC")."""

import asyncio
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from btc15_signal import main, messages  # noqa: E402
from btc15_signal import mirror as mirror_mod  # noqa: E402
from btc15_signal import shadow_summary as S  # noqa: E402
from btc15_signal.execution import ExecutionResult  # noqa: E402
from btc15_signal.mirror import MirroringExecutionClient, MirrorTarget  # noqa: E402
from btc15_signal.store import Store, TradeProposal  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


class Fake:
    def __init__(self):
        self.calls = []

    async def execute_with_take_profit(self, prop, slippage=0.0, ceiling=None):
        self.calls.append(("entry", prop.ticker))
        return ExecutionResult("filled", prop.count, "oid", None, "")

    async def close_position(self, ticker, side, count, limit_price, floor=None):
        self.calls.append(("exit", ticker))
        return ExecutionResult("exited", count, "oid-x", None, "")

    async def place_resting_buy(self, ticker, side, price, count, exp, coid):
        self.calls.append(("add", ticker))
        return {"order_id": "a"}

    async def close(self):
        pass


def proposal(ticker="KXGOLD15M-T"):
    return TradeProposal(id="p", strategy="allsignal", window_open=0, ticker=ticker,
                         side="yes", entry_limit=0.7, take_profit=0.0, count=1,
                         expires_at=0, close_ms=0, status="claimed")


def build(monkeypatch, flags):
    made = []

    def ctor(base_url, key_id, key_path):
        made.append(Fake())
        return made[-1]

    monkeypatch.setattr(mirror_mod, "KalshiExecutionClient", ctor)
    targets = [MirrorTarget("m1", "k1", "p1"), MirrorTarget("m2", "k2", "p2")]
    wrapper = MirroringExecutionClient(Fake(), targets, "https://x",
                                       log_path="runtime/test-mirror.jsonl",
                                       gate=lambda name: flags[name])
    return wrapper, made


async def drain(wrapper):
    for m in wrapper._mirrors:
        await m.queue.join()


def test_a_switched_off_account_takes_no_new_position(monkeypatch):
    async def run():
        flags = {"m1": False, "m2": True}
        wrapper, (wife, other) = build(monkeypatch, flags)
        await wrapper.execute_with_take_profit(proposal())
        await wrapper.place_resting_buy("KXGOLD15M-T", "yes", 0.7, 1, 0, "c")
        await drain(wrapper)
        assert wife.calls == [], "off: no entry, no add"
        assert [c[0] for c in other.calls] == ["entry", "add"]
        # Holding nothing we bought for her: no blind sale into her account.
        await wrapper.close_position("KXGOLD15M-T", "yes", 1, 0.98, 0.90)
        await drain(wrapper)
        assert wife.calls == []
    asyncio.run(run())


def test_what_she_already_holds_is_still_closed_after_the_switch(monkeypatch):
    async def run():
        flags = {"m1": True, "m2": True}
        wrapper, (wife, _other) = build(monkeypatch, flags)
        await wrapper.execute_with_take_profit(proposal())
        await drain(wrapper)
        flags["m1"] = False                      # switched off mid-position
        await wrapper.close_position("KXGOLD15M-T", "yes", 1, 0.98, 0.90)
        await drain(wrapper)
        assert wife.calls == [("entry", "KXGOLD15M-T"), ("exit", "KXGOLD15M-T")]
    asyncio.run(run())


def test_a_switch_that_cannot_be_read_copies_nothing_new(monkeypatch):
    async def run():
        def boom(name):
            raise RuntimeError("db locked")
        made = []
        monkeypatch.setattr(mirror_mod, "KalshiExecutionClient",
                            lambda *a: made.append(Fake()) or made[-1])
        wrapper = MirroringExecutionClient(
            Fake(), [MirrorTarget("m1", "k", "p")], "https://x",
            log_path="runtime/test-mirror.jsonl", gate=boom)
        await wrapper.execute_with_take_profit(proposal())
        await drain(wrapper)
        assert made[0].calls == []
    asyncio.run(run())


def test_the_switch_is_a_stored_row_read_live(tmp_path):
    store = Store(str(tmp_path / "gold15.db"))
    assert main.mirror_on(store, "m1") is True, "no row = on (as before)"
    store.set_setting("mirror_m1_enabled", 0.0, 1)
    assert main.mirror_on(store, "m1") is False
    assert main.mirror_on(store, "m2") is True, "per account"


def test_the_service_gates_its_mirroring_client_on_the_switch():
    import inspect
    src = " ".join(inspect.getsource(main.service).split())
    assert "gate=lambda name: mirror_on(store, name)" in src


def test_the_script_flips_one_account_on_one_instrument(tmp_path):
    Store(str(tmp_path / "gold15.db"))
    Store(str(tmp_path / "btc15.db"))
    out = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "mirror_switch.py"),
         "--mirror", "wife", "--asset", "GOLD", "--off", "--root", str(tmp_path)],
        capture_output=True, text=True, cwd=str(ROOT), check=True).stdout
    assert "Wife (m1) on GOLD: OFF" in out
    assert S.mirror_flag(tmp_path / "gold15.db", "m1") is False
    assert S.mirror_flag(tmp_path / "btc15.db", "m1") is True


def test_the_summary_says_what_her_account_really_copies(tmp_path):
    Store(str(tmp_path / "btc15.db"))
    Store(str(tmp_path / "gold15.db")).set_setting("mirror_m1_enabled", 0.0, 1)
    cfg = SimpleNamespace(mirror_enabled=True, mirror_instances="btc,gold",
                          allsignal_instruments="BTC,GOLD")
    assert S.mirror_copies(tmp_path, cfg, "m1") == ["BTC"]
    cfg.mirror_instances = "btc"
    assert S.mirror_copies(tmp_path, cfg, "m1") == ["BTC"]
    cfg.mirror_enabled = False
    assert S.mirror_copies(tmp_path, cfg, "m1") == []
    text = messages.shadow_summary_message(
        session="us", ny_day="2026-09-28", rows=[],
        copies={"wife": ["BTC"], "m2": []},
        allsignal=[("BTC", {"n": 1, "wins": 1, "pnl": 0.2, "fee": 0.0,
                            "open": 0, "missed": 0})])
    assert "wife copies BTC" in text and "m2 copies nothing" in text
    assert "both accounts" not in text


def test_the_entry_names_who_really_copies_it():
    base = dict(asset="GOLD", window_open=1_790_600_400_000, side="UP",
                signal_ask=0.7, fill_price=0.7, count=1)
    # Wording since 09-29 (the IDE session's layout): "Mirrors enabled: ...".
    assert "Mirrors enabled: wife's account + m2 account" in messages.allsignal_trade_message(
        mirrored="wife's account + m2 account", **base)
    assert "Mirrors enabled" not in messages.allsignal_trade_message(mirrored="", **base)
    assert "Mirrors enabled: wife's account" in messages.allsignal_trade_message(
        mirrored=True, **base)


def test_each_account_is_named_in_the_messages():
    cfg = SimpleNamespace(mirror_1_label="Wife", mirror_2_label="Uncle George")
    assert main.mirror_name(cfg, "m1") == "Wife"
    assert main.mirror_name(cfg, "m2") == "Uncle George"
    assert main.mirror_name(None, "m1") == "Wife"
    text = messages.allsignal_trade_message(
        asset="BTC", window_open=1_790_600_400_000, side="UP", signal_ask=0.7,
        fill_price=0.7, count=1, mirrored="Wife + Uncle George")
    assert "Mirrors enabled: Wife + Uncle George" in text
