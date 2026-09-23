"""Who is allowed to act on the intelligence layer, and how that is enforced.

The layer is retrieval over the research archive. It is not trusted with money
until a promotion report says it should be, so authority is a single explicit
value with a safe default, read through one function that every caller uses.

    SHADOW  infer, record, grade. Never touches an order.
    ASSIST  may adjust confidence and entry TIMING only.
    LIVE    may decide ENTER NOW / WAIT / PASS.

TWO SWITCHES, NOT ONE. `intelligence_mode` names the authority and
`intelligence_authorised` grants it; anything above shadow needs both. A mode
set without the flag falls back to shadow and says so. This exists because one
edited line in a `.env`, or a config copied from a machine where somebody was
experimenting, would otherwise be the whole distance between a shadow model and
real orders.

Nothing here promotes anything. There is no code path that raises the mode.
"""

from __future__ import annotations

SHADOW = "shadow"
ASSIST = "assist"
LIVE = "live"
MODES = (SHADOW, ASSIST, LIVE)


def resolve(mode: str | None, authorised: bool) -> tuple[str, str]:
    """(effective mode, why). Anything unrecognised or unauthorised is SHADOW.

    Returns the reason as well as the verdict so the demotion appears in the
    log and in `/status` rather than being silently applied - a config that
    does not do what it says is worse than one that refuses.
    """
    wanted = (mode or SHADOW).strip().lower()
    if wanted not in MODES:
        return SHADOW, f"unknown mode {wanted!r}; shadow"
    if wanted == SHADOW:
        return SHADOW, "shadow"
    if not authorised:
        return SHADOW, f"{wanted} requested but intelligence_authorised is false; shadow"
    return wanted, f"{wanted} (authorised)"


def may_influence_confidence(mode: str) -> bool:
    """ASSIST and LIVE may move confidence and timing. SHADOW may not."""
    return mode in (ASSIST, LIVE)


def may_decide(mode: str) -> bool:
    """Only LIVE may turn a recommendation into the decision."""
    return mode == LIVE


def may_veto(mode: str) -> bool:
    """May the layer REFUSE a setup the strategy gates accepted?

    A separate function from `may_admit` on purpose. They are opposite risks
    and there is no reason a system that is trusted to stand aside must also be
    trusted to overrule a refusal - the first can only decline to spend money,
    the second spends it on a trade every deployed gate rejected. Splitting
    them means the two can be granted apart if they ever should be, and means a
    test can assert each one separately instead of asserting "decide".
    """
    return mode == LIVE


def may_admit(mode: str) -> bool:
    """May the layer ADMIT a setup the strategy gates refused?

    The stricter of the two in consequence, though they currently share a mode.
    An admission still has to name the single gate it overrides, and it can
    never reach a capital, exposure, loss-floor or execution protection - those
    are not strategy opinions.
    """
    return mode == LIVE


def may_change_size(mode: str) -> bool:
    """NO MODE MAY EVER CHANGE SIZE.

    A function that always returns False, because the rule it encodes is not a
    configuration choice. Sizing is the operator's alone - "I never asked you
    to touch sizing. It stays $1 regardless, I am the only one in control of
    that" - and the honest way to express a permission nothing can hold is a
    permission check that nothing passes. A test asserts this for every mode.
    """
    return False
