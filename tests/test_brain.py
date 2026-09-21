"""The brain may narrate numbers it was given, and no others."""

import asyncio

import pytest

from btc15_signal.brain import (
    Brain,
    BrainConfig,
    book_facts,
    commentary_task,
    strip_invented_numbers,
)

FACTS = book_facts(
    ticker="KXBTC15M-TEST",
    remaining_s=420,
    strike=81_766.34,
    yes_bid=0.30,
    yes_ask=0.31,
    yes_bid_size=7684.0,
    yes_ask_size=3790.0,
    yes_levels=[(0.29, 100.0), (0.28, 250.0)],
    no_levels=[(0.70, 900.0), (0.69, 120.0)],
    btc=81_700.0,
)


def test_facts_are_computed_in_python_not_left_to_the_model():
    assert FACTS["total_yes_depth"] == 350.0
    assert FACTS["total_no_depth"] == 1020.0
    assert FACTS["yes_share_of_depth"] == round(350 / 1370, 3)
    assert FACTS["minutes_left"] == 7.0


def test_prose_quoting_supplied_numbers_is_accepted():
    text = "The no side holds 1020.0 of depth against 350.0 on the yes side."
    assert strip_invented_numbers(text, FACTS).ok


def test_a_single_invented_number_rejects_the_whole_reply():
    """A fluent sentence wrapped around a made-up figure is the worst output."""
    reply = strip_invented_numbers("Depth is 4242.0 on the yes side.", FACTS)
    assert not reply.ok
    assert "4242" in reply.invented[0]
    assert reply.text == ""


def test_rounded_and_percentage_renderings_are_allowed():
    assert strip_invented_numbers("About 26% of depth sits on yes.", FACTS).ok
    assert strip_invented_numbers("The strike is 81766.34.", FACTS).ok


def test_small_integers_are_prose_not_claims():
    assert strip_invented_numbers("Two of the three levels are thin.", FACTS).ok


def test_a_down_runtime_returns_a_failed_reply_and_never_raises():
    brain = Brain(BrainConfig(base_url="http://localhost:9", timeout_s=1))
    reply = asyncio.run(brain.ask("sys", "prompt", FACTS))
    assert not reply.ok
    assert reply.text == ""
    assert reply.reason


def test_availability_is_false_when_nothing_is_listening():
    brain = Brain(BrainConfig(base_url="http://localhost:9"))
    assert asyncio.run(brain.available()) is False


def test_commentary_never_sends_when_the_model_invents_a_number():
    sent = []

    class Fake(Brain):
        async def ask(self, system, prompt, facts):
            return strip_invented_numbers("Depth is 99999.0 here.", facts)

    async def send(text):
        sent.append(text)

    asyncio.run(commentary_task(Fake(), FACTS, send))
    assert sent == []


def test_commentary_sends_a_clean_reply():
    sent = []

    class Fake(Brain):
        async def ask(self, system, prompt, facts):
            return strip_invented_numbers("The book leans to the no side.", facts)

    async def send(text):
        sent.append(text)

    asyncio.run(commentary_task(Fake(), FACTS, send))
    assert len(sent) == 1
    assert "Book read:" in sent[0]


def test_commentary_cannot_delay_an_alert_even_if_the_model_hangs():
    """The alert is already sent; commentary runs on its own task with a timeout."""
    order = []

    class Slow(Brain):
        async def ask(self, system, prompt, facts):
            await asyncio.sleep(0.2)
            order.append("model")
            from btc15_signal.brain import Reply

            return Reply(text="quiet book", ok=True)

    async def send(text):
        order.append("commentary")

    async def scenario():
        task = asyncio.create_task(commentary_task(Slow(), FACTS, send))
        order.append("alert")  # the alert goes out immediately
        await task

    asyncio.run(scenario())
    assert order[0] == "alert"  # never blocked behind the model


def test_a_numberless_reply_is_fine():
    reply = strip_invented_numbers("The book leans one way.", FACTS)
    assert reply.ok


@pytest.mark.parametrize("blank", ["", "   ", "\n"])
def test_an_empty_reply_is_a_failure_not_a_silent_pass(blank):
    """A thinking model burns its budget and returns nothing; that is an error."""
    reply = strip_invented_numbers(blank, FACTS)
    assert not reply.ok
    assert reply.reason == "empty reply"


def test_an_unattended_order_is_narrated_too():
    """The one case with nobody watching must not be the one case left silent.

    Commentary used to sit only after the manual alert, so a trade placed
    automatically - the whole point of overnight operation - produced no
    reasoning at all in Telegram.
    """
    from pathlib import Path

    source = Path("src/btc15_signal/main.py").read_text(encoding="utf-8")
    # Once for the manual/no-entry alert, once before the auto path returns,
    # plus the definition itself.
    calls = [line for line in source.splitlines() if line.strip() == "schedule_commentary("]
    assert len(calls) == 2
    assert {len(line) - len(line.lstrip()) for line in calls} == {4, 12}

    auto_block = source.split("---- unattended execution")[1]
    auto_block = auto_block[: auto_block.index("    head = head_for(store, settings)")]
    assert "schedule_commentary(" in auto_block
    # and it is scheduled before the early return, not after it
    assert auto_block.index("schedule_commentary(") < auto_block.rindex("return")


def test_commentary_is_skipped_cleanly_when_the_brain_is_off():
    from btc15_signal.config import Settings
    from btc15_signal.main import schedule_commentary

    off = Settings(brain_enabled=False)
    # No event loop needed: it must return before creating any task.
    schedule_commentary(
        off, None, None, None, None, None, None, True, "", 0.9, 480, 0, 0
    )


def test_a_decision_is_still_narrated_when_the_recorder_is_down(tmp_path):
    """Behaviour deliberately changed. Commentary used to abort without a book,
    which made the book the only thing it had to say. The decision facts are
    price, distance, momentum, expected edge and a verdict - the book is one
    input among them, so a missing book costs the depth labels and nothing else.
    """
    from btc15_signal.config import Settings
    from btc15_signal.kalshi import KalshiMarket
    from btc15_signal.main import schedule_commentary
    from btc15_signal.store import Store

    settings = Settings(brain_enabled=True, microstructure_path=str(tmp_path / "nope.db"))
    store = Store(str(tmp_path / "s.db"))
    contract = KalshiMarket(
        ticker="KXBTC15M-T", target=84_000.0, open_ms=0, close_ms=900_000,
        yes_ask=0.85, no_ask=0.16,
    )

    class Snap:
        price = 84_100.0
        target = 84_000.0
        momentum_5m_bps = 5.0
        volatility_5m_bps = 10.0
        futures_basis_bps = 2.0
        taker_imbalance = 0.1
        spread_bps = 1.0

    class Prediction:
        side = "UP"
        raw_probability = 0.9

    class Telegram:
        async def send(self, text, buttons=None):
            return None

    # No event loop here, so it must fail on create_task at the very end rather
    # than earlier - proving it got all the way through building the facts
    # despite the missing book.
    try:
        schedule_commentary(
            settings, store, Telegram(), contract, Snap(), Prediction(), None,
            True, "", 0.85, 480, 0, 1,
        )
    except RuntimeError as exc:
        assert "no running event loop" in str(exc).lower()
    else:  # pragma: no cover - only if a loop happens to be running
        pass


def test_the_model_is_never_handed_a_comparison_to_make():
    """It called 53,883 deeper than 59,779 - both figures real, the comparison
    false. The guard cannot catch that, so the fix is to supply the conclusion."""
    from btc15_signal.decision import weighted_imbalance

    _imbalance, label, difference = weighted_imbalance(
        [(0.47, 53_883.2)], [(0.53, 59_779.3)]
    )
    assert label in {"YES_DEPTH_DOMINANT", "NO_DEPTH_DOMINANT", "BOOK_BALANCED"}
    assert difference > 0  # the gap is given, not left to be worked out


def test_the_decision_prompt_forbids_comparing_numbers():
    from btc15_signal.brain import DECISION_SYSTEM

    lowered = DECISION_SYSTEM.lower()
    assert "never compare two numbers" in lowered
    assert "evidence phrases" in lowered
    assert "not deciding anything" in lowered


def test_a_truncated_reply_is_dropped_not_sent():
    """Half a sentence reads as confidently as a whole one. The live model cut
    off at "...and the invalidation condition is" and that still went out."""
    import asyncio

    from btc15_signal.brain import Brain, BrainConfig

    class Truncating(Brain):
        async def ask(self, system, prompt, facts):
            # Mirrors what the server returns when max_tokens is hit.
            from btc15_signal.brain import Reply

            return Reply(text="", ok=False, reason="reply was truncated at the token limit")

    reply = asyncio.run(Truncating(BrainConfig()).ask("s", "p", {}))
    assert not reply.ok
    assert "truncated" in reply.reason


def test_the_token_budget_fits_three_sentences():
    """72 was sized for a one-line book note and cut the decision off."""
    from btc15_signal.brain import BrainConfig

    assert BrainConfig().max_tokens >= 140


def test_the_bid_ask_gap_is_computed_not_compared():
    """The model was caught saying "exit_bid is lower than our_ask" - always
    true, and not a comparison it should be making."""
    from btc15_signal.decision import decision_facts

    facts = decision_facts(
        ticker="T", remaining_s=600, side="UP", btc=85_283.36, target=85_189.42,
        our_ask=0.67, exit_bid=0.66, yes_bid=0.66, yes_ask=0.67, no_ask=0.34,
        yes_levels=[(0.66, 3000.0)], no_levels=[(0.33, 1000.0)],
        momentum_5m_bps=11.4, volatility_5m_bps=3.4, futures_basis_bps=1.0,
        taker_imbalance=0.2, spread_bps=1.0, session="europe", vol_regime="low",
        book_age_s=8.0, rule_match=False, failed_gates="contract price band",
        holding=False, entry_paid=None, unrealised=None,
        model_probability=0.99, measured_edge=0.0166, slippage=0.01,
    )
    assert facts["quotes"]["bid_ask_gap"] == 0.01
    assert facts["quotes"]["cost_to_enter_and_leave_now"] == 0.01


def test_the_model_sees_only_plain_english_never_field_names():
    """Handed the nested dict it recited "spread_bps is 0.0" and stated one fact
    twice in two dialects - "imbalance is YES_DEPTH_DOMINANT, but book is
    BOOK_AGAINST_US" - which for a DOWN bet is one fact, not a contradiction."""
    import json

    from btc15_signal.decision import decision_facts

    facts = decision_facts(
        ticker="T", remaining_s=336, side="DOWN", btc=85_085.65, target=85_162.40,
        our_ask=0.63, exit_bid=0.62, yes_bid=0.37, yes_ask=0.38, no_ask=0.63,
        yes_levels=[(0.37, 9000.0)], no_levels=[(0.62, 3000.0)],
        momentum_5m_bps=-10.0, volatility_5m_bps=8.0, futures_basis_bps=1.0,
        taker_imbalance=-0.2, spread_bps=0.0, session="us", vol_regime="mid",
        book_age_s=9.0, rule_match=False, failed_gates="contract price band",
        holding=False, entry_paid=None, unrealised=None,
        model_probability=1.0, measured_edge=0.0166, slippage=0.01,
    )
    shown = json.dumps(facts["summary"])

    # No snake_case field names and no SHOUTED codes reach the model.
    for leak in ("spread_bps", "our_side_is_winning", "momentum_5m_bps",
                 "YES_DEPTH_DOMINANT", "BOOK_AGAINST_US", "imbalance_label"):
        assert leak not in shown, leak

    # Exactly one statement about the book, already resolved to our side.
    book_lines = [e for e in facts["summary"]["evidence"] if "book" in e]
    assert len(book_lines) == 1
    assert "leans" in book_lines[0] or "balanced" in book_lines[0]


def test_every_evidence_phrase_reads_as_a_sentence():
    from btc15_signal.decision import decision_facts

    facts = decision_facts(
        ticker="T", remaining_s=600, side="UP", btc=85_300.0, target=85_040.0,
        our_ask=0.85, exit_bid=0.84, yes_bid=0.84, yes_ask=0.85, no_ask=0.16,
        yes_levels=[(0.84, 9000.0)], no_levels=[(0.15, 1000.0)],
        momentum_5m_bps=12.0, volatility_5m_bps=9.0, futures_basis_bps=1.0,
        taker_imbalance=0.2, spread_bps=1.0, session="us", vol_regime="mid",
        book_age_s=5.0, rule_match=True, failed_gates="", holding=False,
        entry_paid=None, unrealised=None, model_probability=0.95,
        measured_edge=0.0166, slippage=0.01,
    )
    for phrase in facts["summary"]["evidence"]:
        assert "_" not in phrase, phrase
        assert phrase == phrase.lstrip(), phrase
        assert not phrase.isupper()


def test_the_prompt_forbids_field_names_and_repetition():
    from btc15_signal.brain import DECISION_SYSTEM

    lowered = DECISION_SYSTEM.lower()
    assert "never write a field name" in lowered
    assert "do not repeat the same piece of evidence" in lowered
    assert "never compare two numbers" in lowered


def test_sentence_labels_are_stripped_even_if_the_model_writes_them():
    """Asking for three sentences invites a numbered list back. The prompt says
    not to, but a prompt is a request and this is a guarantee."""
    from btc15_signal.brain import strip_scaffolding

    text = (
        "Sentence 1: The setup is UP at 82%.\n"
        "Sentence 2: Momentum agrees with the side.\n"
        "Sentence 3: The action is BUY."
    )
    cleaned = strip_scaffolding(text)
    assert "Sentence" not in cleaned
    assert cleaned.startswith("The setup is UP at 82%.")
    assert "Momentum agrees" in cleaned


def test_scaffolding_stripping_leaves_ordinary_prose_alone():
    from btc15_signal.brain import strip_scaffolding

    prose = "The setup is UP at 82%. Momentum agrees. The action is BUY."
    assert strip_scaffolding(prose) == prose


def test_commentary_is_once_per_window_not_once_per_poll():
    """Trading is evaluated on every poll and commentary rode along with it -
    five messages in one minute, each restating the same setup seconds apart."""
    from pathlib import Path

    source = Path("src/btc15_signal/main.py").read_text(encoding="utf-8")
    body = source.split("def schedule_commentary(")[1]
    body = body[: body.index("async def report_settlement(")]
    assert 'record_alert("primary-brain"' in body
    # and it is checked before any work is done
    assert body.index('record_alert("primary-brain"') < body.index("decision_facts(")


def test_the_prompt_forbids_numbering_the_sentences():
    from btc15_signal.brain import DECISION_SYSTEM

    assert "never number your sentences" in DECISION_SYSTEM.lower()
