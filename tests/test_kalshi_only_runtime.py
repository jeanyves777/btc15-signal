"""Under `kalshi_only`, no Binance model and no Binance rule may run.

The existing Binance guards cover CLIENTS and POLICIES - the network and the
artefact. They do not cover the MODEL and the RULE, and that is where it was
still happening: `archive_observation` called `predict(snapshot)` and
`EntryRule.matches(...)` on every poll regardless of instrument.

Under `kalshi_only` the snapshot is built from BRTI, so this was
Binance-fitted arithmetic applied to Kalshi-scaled inputs, written to
`side`, `raw_probability`, `bucket`, `distance_bps` and `normalized_distance`
- column names that say nothing about which instrument produced them. It is
also what crashed the service every fifteen minutes between 04:04 and 05:04
on 2026-09-23: `strategy.py` compares `prediction.raw_probability >=
min_raw_probability`, and a Kalshi prediction has no `raw_probability`, so
`None >= float` raised TypeError.

The function is wrapped in `except Exception`, so when it broke it broke
silently - and when it worked it was worse, because it wrote numbers.
"""

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import pytest  # noqa: E402

from btc15_signal import main as m  # noqa: E402
from btc15_signal import strategy  # noqa: E402
from btc15_signal.brti import BRTIFeatures  # noqa: E402
from btc15_signal.config import Settings  # noqa: E402
from btc15_signal.store import Store  # noqa: E402

MAIN = (ROOT / "src" / "btc15_signal" / "main.py").read_text(encoding="utf-8")


def brti(**over) -> BRTIFeatures:
    base = dict(
        event_ticker="KXBTC15M-26SEP231530", ts_ms=1_790_000_000_000,
        target=112_500.0, value=112_530.0, signed_distance_bps=2.7,
        brti_momentum_bps=7.1, brti_volatility_bps=0.22,
        brti_normalized_distance=12.4, samples=300, span_ms=300_000,
        stale=False, settlement_projection=112_530.0,
    )
    base.update(over)
    return BRTIFeatures(**base)


def snapshot(**over):
    """A `MarketSnapshot` as the Kalshi path fills it: the same dataclass,
    with the BRTI numbers in the momentum and volatility fields and the
    Binance-only microstructure terms left at zero because nothing on this
    path measures them."""
    base = dict(price=112_530.0, target=112_500.0, bid_imbalance=0.0,
                taker_imbalance=0.0, momentum_5m_bps=7.1,
                volatility_5m_bps=0.22, futures_basis_bps=0.0,
                spread_bps=0.0)
    base.update(over)
    return m.MarketSnapshot(**base)


class _Contract:
    ticker = "KXBTC15M-26SEP231530-30"
    target = 112_500.0
    yes_ask = 0.80
    no_ask = 0.22
    yes_bid = 0.78
    no_bid = 0.20

    def ask(self, side):
        return self.yes_ask if side == "UP" else self.no_ask


@pytest.fixture()
def kalshi(tmp_path, monkeypatch):
    """A live-shaped call with every Binance entry point booby-trapped."""
    settings = Settings()
    object.__setattr__(settings, "kalshi_only", True) \
        if hasattr(settings, "__setattr__") else None
    store = Store(str(tmp_path / "t.db"))

    tripped = []

    def trap(name):
        def _boom(*a, **k):
            tripped.append(name)
            raise AssertionError(f"Binance path invoked: {name}")
        return _boom

    monkeypatch.setattr(m, "predict", trap("predict"), raising=False)
    monkeypatch.setattr(strategy.EntryRule, "matches", trap("EntryRule.matches"))
    monkeypatch.setattr(strategy.EntryRule, "check_facts",
                        trap("EntryRule.check_facts"))
    monkeypatch.setattr(strategy.EntryRule, "check_detail",
                        trap("EntryRule.check_detail"))
    return settings, store, tripped


# ------------------------------------------------ the archiver, at runtime

def test_the_archiver_runs_no_binance_model_under_kalshi_only(kalshi):
    """The real function, with every Binance entry point set to raise."""
    settings, store, tripped = kalshi
    if not settings.kalshi_only:
        pytest.skip("this deployment is not kalshi_only")
    m.archive_observation(
        settings, store, _Contract(), snapshot(),
        opened=1_790_000_000_000, remaining=600,
        now_ms=1_790_000_000_000, brti=brti(),
    )
    assert tripped == [], f"Binance invoked: {tripped}"


def test_the_archiver_writes_nothing_rather_than_guessing(kalshi):
    """Without the reference there is no Kalshi-native side, price band or
    distance. A row of Binance numbers under those column names is worse than
    no row: a missing row is visible, a mislabelled one is not."""
    settings, store, tripped = kalshi
    if not settings.kalshi_only:
        pytest.skip("this deployment is not kalshi_only")
    m.archive_observation(
        settings, store, _Contract(), snapshot(),
        opened=1_790_000_000_000, remaining=600,
        now_ms=1_790_000_000_000, brti=None,
    )
    assert tripped == []


# -------------------------------------------- and statically, at the source

def _guarded(call_line: int) -> bool:
    """Is this line on the branch that only runs when `kalshi_only` is off?

    BOTH FORMS COUNT. Two call sites are written as a ternary -
    `facts if settings.kalshi_only else rule.check_facts(...)` - which is the
    same guard as the statement form, and calling them unguarded would be a
    false positive that teaches the reader to ignore this test.
    """
    tree = ast.parse(MAIN)
    for node in ast.walk(tree):
        if isinstance(node, ast.If):
            test = ast.unparse(node.test)
            if "kalshi_only" not in test:
                continue
            body = [n for x in node.body for n in ast.walk(x)]
            orelse = [n for x in node.orelse for n in ast.walk(x)]
            branch = body if "not " in test else orelse
        elif isinstance(node, ast.IfExp):
            test = ast.unparse(node.test)
            if "kalshi_only" not in test:
                continue
            branch = (list(ast.walk(node.body)) if "not " in test
                      else list(ast.walk(node.orelse)))
        else:
            continue
        if any(getattr(n, "lineno", None) == call_line for n in branch):
            return True
    return False


def test_every_binance_model_call_in_main_is_guarded():
    """`predict`, `EntryRule.matches`, `check_facts` and `check_detail` all
    read Binance-fitted parameters. Each call must sit on the branch that only
    runs when `kalshi_only` is off."""
    tree = ast.parse(MAIN)
    unguarded = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = ""
        if isinstance(node.func, ast.Name):
            name = node.func.id
        elif isinstance(node.func, ast.Attribute):
            name = node.func.attr
        if name not in ("predict", "matches", "check_facts", "check_detail"):
            continue
        src = ast.unparse(node)
        # The Kalshi rule has methods of the same name; it is a different
        # object and reads BRTI.
        if "kalshi" in src.lower() or "setup.side" in src:
            continue
        # `rule.matches(snapshot, remaining_minutes, entry)` is the reversion
        # rule, a separate strategy with its own inputs.
        if "remaining_minutes" in src:
            continue
        if not _guarded(node.lineno):
            unguarded.append((node.lineno, src[:70]))
    assert unguarded == [], f"unguarded Binance-model calls: {unguarded}"


def test_the_archiver_takes_the_reference_it_needs():
    """It cannot be Kalshi-native without the Kalshi features; requiring them
    in the signature is what stops it quietly falling back."""
    tree = ast.parse(MAIN)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef)
              and n.name == "archive_observation")
    assert "brti" in [a.arg for a in fn.args.args]


def test_no_binance_probability_reaches_a_kalshi_row():
    """There is no Kalshi-native model, so `raw_probability` and `bucket` are
    NULL on this path. Writing the Binance ones is how one column comes to
    hold two different meanings."""
    fn_src = MAIN[MAIN.index("def archive_observation"):]
    fn_src = fn_src[:fn_src.index("\ndef ", 1)]
    assert 'getattr(prediction, "raw_probability", None)' in fn_src
    assert 'getattr(prediction, "bucket", None)' in fn_src


def test_the_archived_features_come_from_the_traded_instrument():
    """Spot volatility and BRTI volatility are not the same quantity, and a
    threshold measured for one does not transfer to the other."""
    fn_src = MAIN[MAIN.index("def archive_observation"):]
    fn_src = fn_src[:fn_src.index("\ndef ", 1)]
    for field in ("brti.brti_momentum_bps", "brti.brti_volatility_bps",
                  "brti.brti_normalized_distance"):
        assert field in fn_src, f"{field} is not archived on the Kalshi path"
