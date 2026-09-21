"""Local LLM brain: order-book commentary and ledger analysis.

Talks to a local model over plain HTTP - no new dependency, and nothing leaves
the machine. Works with an Ollama-style `/api/generate` endpoint or any
OpenAI-compatible `/v1/chat/completions` server (LM Studio, llama-server), so
the runtime is a choice you make later, not one baked in here.

Two rules the code enforces, rather than asking the prompt nicely:

**Python computes every number; the model only writes prose.** Language models
are poor at arithmetic and this system's figures are already computed correctly
by validation.py. The model receives a `facts` object of finished numbers and
writes about them. `strip_invented_numbers` then checks every figure in the
reply against those facts and rejects the reply outright if one was invented -
a fluent sentence containing a made-up number is worse than no sentence.

**It never gates a trade.** `read_book` is commentary that arrives *alongside*
an alert, on its own task, with a hard timeout. The alert and the Execute button
are already sent before the model is asked anything. If the runtime is down,
slow, or talking nonsense, the trading path is bit-for-bit unchanged - the only
observable difference is that no commentary arrives.
"""

import json
import re
from dataclasses import dataclass, field

import httpx

OLLAMA = "http://localhost:11434"
OPENAI_COMPATIBLE = "http://localhost:1234/v1"

# Any run of digits that could be read as a quantity.
NUMBER = re.compile(r"-?\$?\d[\d,]*\.?\d*%?")


@dataclass
class BrainConfig:
    base_url: str = OLLAMA
    model: str = "llama3.1:8b"
    # Measured on this box: Granite 4.2 3B Q4 generates around 2 tokens/sec on
    # 6 CPU threads, and a cold first call is far slower. Three sentences is
    # about 50 tokens, so the cap is tight and the timeout generous - the call
    # is fire-and-forget after an alert, with roughly fifteen minutes before the
    # next window, so arriving late costs nothing and being cut off loses the
    # whole reply.
    timeout_s: float = 240.0
    temperature: float = 0.1  # commentary, not creativity
    # Sized for a three-sentence decision, not the old one-line book note. At 72
    # it cut off mid-clause - "...and the invalidation condition is" - which is
    # worse than silence, because a truncated reply still looks authoritative.
    # Granite runs about 2 tok/s on this box, so 160 is roughly 80s against a
    # 240s timeout, and the call is fire-and-forget after the alert.
    max_tokens: int = 160


@dataclass
class Reply:
    text: str
    ok: bool
    reason: str = ""
    invented: list[str] = field(default_factory=list)


def _numbers_in(text: str) -> set[str]:
    out = set()
    for match in NUMBER.findall(text):
        cleaned = match.strip("$%").replace(",", "").rstrip(".")
        if cleaned and cleaned not in {"-", ""}:
            try:
                out.add(f"{float(cleaned):g}")
            except ValueError:
                continue
    return out


def _facts_numbers(facts: dict) -> set[str]:
    """Every number the model is allowed to say, including rounded forms."""
    allowed: set[str] = set()

    def walk(value) -> None:
        nonlocal allowed
        if isinstance(value, dict):
            for item in value.values():
                walk(item)
        elif isinstance(value, list | tuple):
            for item in value:
                walk(item)
        elif isinstance(value, bool):
            return
        elif isinstance(value, int | float):
            allowed.add(f"{float(value):g}")
            allowed.add(f"{round(float(value)):g}")
            for digits in (1, 2, 3, 4):
                allowed.add(f"{round(float(value), digits):g}")
            # percentage and cent renderings of the same quantity
            allowed.add(f"{round(float(value) * 100, 1):g}")
            allowed.add(f"{round(float(value) * 100):g}")
        elif isinstance(value, str):
            allowed |= _numbers_in(value)

    walk(facts)
    # Small integers are ordinary prose ("two of three"), not claims.
    allowed |= {str(n) for n in range(0, 13)}
    return allowed


SCAFFOLD = re.compile(r"(?im)^\s*(sentence\s*\d+\s*[:.\-]\s*)")


def strip_scaffolding(text: str) -> str:
    """Remove "Sentence 1:" style labels the model echoes from the prompt.

    Asking for three sentences invites a numbered list back. The instruction not
    to is in the prompt, but a prompt is a request and this is a guarantee.
    """
    return SCAFFOLD.sub("", text).strip()


def strip_invented_numbers(text: str, facts: dict) -> Reply:
    """Reject a reply containing any figure absent from the facts.

    The cheapest guard against the one failure that matters: confident prose
    wrapped around a number nobody computed.
    """
    text = strip_scaffolding(text)
    if not text.strip():
        return Reply(text="", ok=False, reason="empty reply")
    allowed = _facts_numbers(facts)
    invented = sorted(_numbers_in(text) - allowed)
    if invented:
        return Reply(
            text="",
            ok=False,
            reason="reply contained numbers not present in the supplied facts",
            invented=invented,
        )
    return Reply(text=text.strip(), ok=True)


class Brain:
    def __init__(self, config: BrainConfig | None = None) -> None:
        self.config = config or BrainConfig()

    async def available(self) -> bool:
        try:
            async with httpx.AsyncClient(timeout=3) as client:
                if "/v1" in self.config.base_url:
                    response = await client.get(self.config.base_url + "/models")
                else:
                    response = await client.get(self.config.base_url + "/api/tags")
                return response.status_code < 400
        except httpx.HTTPError:
            return False

    async def ask(self, system: str, prompt: str, facts: dict) -> Reply:
        """One guarded call. Never raises; a failure is a Reply with ok=False."""
        try:
            async with httpx.AsyncClient(timeout=self.config.timeout_s) as client:
                if "/v1" in self.config.base_url:
                    payload = {
                        "model": self.config.model,
                        "messages": [
                            {"role": "system", "content": system},
                            {"role": "user", "content": prompt},
                        ],
                        "temperature": self.config.temperature,
                        "max_tokens": self.config.max_tokens,
                        # Thinking models spend the whole budget reasoning and
                        # return empty content with finish_reason=length. On CPU
                        # that is tens of seconds for nothing.
                        "chat_template_kwargs": {"enable_thinking": False},
                    }
                    response = await client.post(
                        self.config.base_url + "/chat/completions", json=payload
                    )
                    response.raise_for_status()
                    choice = response.json()["choices"][0]
                    text = choice["message"].get("content") or ""
                    # A reply cut off at the token limit is incomplete, and half
                    # a sentence reads as confidently as a whole one. Treated
                    # like an invented number: dropped rather than sent.
                    if choice.get("finish_reason") == "length" and text.strip():
                        return Reply(
                            text="",
                            ok=False,
                            reason="reply was truncated at the token limit",
                        )
                    if not text.strip():
                        # Empty content with a reasoning field means thinking
                        # mode ate the budget; say so rather than return silence.
                        reason = (
                            "model returned only reasoning, no answer"
                            if choice["message"].get("reasoning_content")
                            else f"empty reply (finish_reason={choice.get('finish_reason')})"
                        )
                        return Reply(text="", ok=False, reason=reason)
                else:
                    payload = {
                        "model": self.config.model,
                        "system": system,
                        "prompt": prompt,
                        "stream": False,
                        "options": {
                            "temperature": self.config.temperature,
                            "num_predict": self.config.max_tokens,
                        },
                    }
                    response = await client.post(
                        self.config.base_url + "/api/generate", json=payload
                    )
                    response.raise_for_status()
                    text = response.json().get("response", "")
        except (TimeoutError, httpx.HTTPError, KeyError, ValueError) as error:
            return Reply(text="", ok=False, reason=f"{type(error).__name__}: {error}")
        return strip_invented_numbers(text, facts)


# --------------------------------------------------------------- book reading

BOOK_SYSTEM = (
    "You read a prediction-market order book and describe what it shows, in at "
    "most three short sentences. Every number you may use is given to you; never "
    "compute, estimate or invent one, and never state a number that is not in the "
    "input. Describe the shape of the book - where size sits, whether it is "
    "lopsided, whether the touch is thin. Do not give trading advice, do not "
    "predict the outcome, and do not say whether to buy."
)

DECISION_SYSTEM = (
    "You turn a finished trading decision into three short sentences of plain "
    "English. You are not deciding anything and not analysing anything.\n\n"
    "You are given a headline, a list of evidence phrases, an action, a "
    "confidence and an invalidation condition. Every judgement has already "
    "been made. Your ONLY job is to join the evidence into readable prose.\n\n"
    "Rules:\n"
    "- Use ONLY the supplied evidence phrases. Add no facts of your own.\n"
    "- NEVER write a field name, an underscore, or a CAPITALISED code. "
    "Write as a person would speak.\n"
    "- NEVER compare two numbers yourself and never restate arithmetic. "
    "Doing so once produced the claim that 53,883 was larger than 59,779.\n"
    "- Do not repeat the same piece of evidence twice in different words.\n"
    "- Say plainly where the evidence disagrees, and do not talk the "
    "decision up.\n\n"
    "- Write flowing prose. NEVER number your sentences and never write a "
    'label such as "Sentence 1" - output only the sentences themselves.\n\n'
    "The first says what the setup is, from the headline. The second says "
    "what the evidence shows, including any disagreement. The third gives "
    "the action and what would make it wrong."
)


def book_facts(
    *,
    ticker: str,
    remaining_s: int,
    strike: float,
    yes_bid: float | None,
    yes_ask: float | None,
    yes_bid_size: float | None,
    yes_ask_size: float | None,
    yes_levels: list[tuple[float, float]],
    no_levels: list[tuple[float, float]],
    btc: float | None,
) -> dict:
    """Finished numbers describing the book. Python does the arithmetic."""
    top_yes = sorted(yes_levels, reverse=True)[:5]
    top_no = sorted(no_levels, reverse=True)[:5]
    yes_depth = round(sum(size for _, size in yes_levels), 1)
    no_depth = round(sum(size for _, size in no_levels), 1)
    total = yes_depth + no_depth
    return {
        "ticker": ticker,
        "minutes_left": round(remaining_s / 60, 1),
        "strike": strike,
        "btc": btc,
        "quoted_yes_bid": yes_bid,
        "quoted_yes_ask": yes_ask,
        "quoted_bid_size": yes_bid_size,
        "quoted_ask_size": yes_ask_size,
        "top_yes_levels": [[p, s] for p, s in top_yes],
        "top_no_levels": [[p, s] for p, s in top_no],
        "total_yes_depth": yes_depth,
        "total_no_depth": no_depth,
        "yes_share_of_depth": round(yes_depth / total, 3) if total else None,
        "level_count_yes": len(yes_levels),
        "level_count_no": len(no_levels),
    }


async def read_book(brain: Brain, facts: dict) -> Reply:
    """Commentary on a live book. Runs beside an alert, never before it."""
    prompt = (
        "Order book snapshot. Describe only what these numbers show.\n\n"
        + json.dumps(facts, indent=1)
    )
    return await brain.ask(BOOK_SYSTEM, prompt, facts)


async def commentary_task(brain: Brain, facts: dict, send) -> None:
    """Fire-and-forget: ask, and deliver only if the answer survives the guard.

    Scheduled as its own task so a slow or hung model cannot delay the alert
    that has already gone out, nor the poll loop behind it.
    """
    reply = await read_book(brain, facts)
    if reply.ok and reply.text:
        await send("\U0001f9e0 <i>Book read: " + reply.text + "</i>")


def latest_book(db_path: str, ticker: str, max_age_s: float = 120.0) -> dict | None:
    """Most recent recorded book for a ticker, or None if there is nothing fresh.

    Reads what the recorder already captured rather than making its own API
    call, so commentary costs the trading service no extra request and cannot
    contribute to a rate limit on the path that matters.
    """
    import sqlite3
    import time

    try:
        db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        row = db.execute(
            "SELECT captured_ms, remaining_s, strike, yes_bid, yes_ask, yes_bid_size, "
            "yes_ask_size, book_yes, book_no, btc_bid FROM book_snapshots "
            "WHERE ticker=? ORDER BY captured_ms DESC LIMIT 1",
            (ticker,),
        ).fetchone()
    except sqlite3.Error:
        return None
    if not row:
        return None
    captured, remaining, strike, bid, ask, bid_size, ask_size, raw_yes, raw_no, btc = row
    if (time.time() * 1000 - captured) / 1000 > max_age_s:
        return None  # stale book is worse than no commentary
    return book_facts(
        ticker=ticker,
        remaining_s=remaining,
        strike=strike,
        yes_bid=bid,
        yes_ask=ask,
        yes_bid_size=bid_size,
        yes_ask_size=ask_size,
        yes_levels=[tuple(x) for x in json.loads(raw_yes or "[]")],
        no_levels=[tuple(x) for x in json.loads(raw_no or "[]")],
        btc=btc,
    )


async def read_decision(brain: Brain, facts: dict) -> Reply:
    """Narrate a decision the code has already made.

    The facts arrive as finished conclusions, so the model has nothing to get
    arithmetically wrong; `strip_invented_numbers` then catches any figure it
    reaches for that was not supplied.
    """
    # Only the plain-English summary is shown. The FULL fact set still goes to
    # the guard, so any number the model reaches for is checked against
    # everything computed - but handing it the nested dict made it recite field
    # names ("spread_bps is 0.0") instead of writing sentences, and state one
    # fact twice in two dialects as though the two disagreed.
    summary = facts.get("summary", facts)
    prompt = (
        "Write three sentences from this. Use only these evidence phrases, and "
        "no field names.\n\n" + json.dumps(summary, indent=1)
    )
    return await brain.ask(DECISION_SYSTEM, prompt, facts)


async def decision_commentary(brain: Brain, facts: dict, send) -> None:
    """Fire-and-forget: ask, and deliver only if the answer survives the guard."""
    reply = await read_decision(brain, facts)
    if reply.ok and reply.text:
        verdict = facts.get("verdict", {})
        await send(
            f"🧠 <b>{verdict.get('action', '?')}</b> · "
            f"confidence {verdict.get('confidence', '?').lower()} "
            f"({verdict.get('signals_agreeing', 0)}/{verdict.get('signals_total', 5)} "
            f"signals agree)\n<i>" + reply.text + "</i>"
        )


def raw_book(db_path: str, ticker: str) -> dict:
    """Latest recorded book levels and quote for a ticker, or an empty dict.

    Returns the RAW levels for `decision.decision_facts` to reduce to labels.
    Nothing here reaches the model directly.
    """
    import json
    import sqlite3
    import time

    try:
        db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        row = db.execute(
            "SELECT captured_ms, yes_bid, book_yes, book_no FROM book_snapshots "
            "WHERE ticker=? ORDER BY captured_ms DESC LIMIT 1",
            (ticker,),
        ).fetchone()
        db.close()
    except sqlite3.Error:
        return {}
    if not row:
        return {}
    captured, yes_bid, raw_yes, raw_no = row
    try:
        yes_levels = [tuple(level) for level in json.loads(raw_yes or "[]")]
        no_levels = [tuple(level) for level in json.loads(raw_no or "[]")]
    except ValueError:
        yes_levels = no_levels = []
    return {
        "yes_bid": yes_bid,
        "yes_levels": yes_levels,
        "no_levels": no_levels,
        "book_age_s": round((time.time() * 1000 - captured) / 1000, 1),
    }
