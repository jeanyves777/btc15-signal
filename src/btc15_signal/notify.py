"""Sending, de-duplicating and rotating. One place, so the rules hold.

Three things went wrong on the old surface and all three were about WHEN a
message goes out rather than what it says:

  * the same waiting state was re-sent every poll, so one window could produce
    dozens of near-identical notifications and the reader learned to swipe;
  * the guards that did exist were per-process dicts, so a restart either
    re-sent something already read or silently dropped it;
  * every message took its own money snapshot, so two lines of one message
    could describe two different instants.

So: one snapshot per message, one delivery record per event, and a rotation
that advances only on a send that actually succeeded.
"""

from __future__ import annotations

from . import surface


class Notifier:
    """Wraps the Telegram client with delivery identity and one snapshot."""

    def __init__(self, telegram, store, settings) -> None:
        self.telegram = telegram
        self.store = store
        self.settings = settings

    # ------------------------------------------------------------ money
    def snapshot(self, now_ms: int):
        """ONE reconciled snapshot, taken once, for the whole message.

        `money_snapshot()` defaults `now_ms` to the wall clock, so every call
        without one is a separate instant. The caller passes the poll's own
        timestamp and reuses the result for every line.
        """
        return self.store.money_snapshot(now_ms)

    # --------------------------------------------------------- rotation
    def insight_for(self, window_open: int, now_ms: int) -> str:
        """The rotating line for this market, rendered, or "".

        The variant is pinned to the market on first use so the signal, the
        fill and the recap all carry the same one. Data that is not there
        produces no line rather than invented commentary.
        """
        variant = self.store.insight_for(window_open, surface.INSIGHTS)
        if not variant:
            return ""
        self.store.assign_insight(window_open, variant, now_ms)
        return surface.insight_line(variant, self._insight_data(variant, now_ms))

    def _insight_data(self, variant: str, now_ms: int) -> dict | None:
        sizing = {"contracts": self.settings.trade_contract_count}
        try:
            if variant == "signals":
                settled, wins, _net = self.store.scoreboard(**sizing)
                return {"settled": settled, "wins": wins}
            if variant == "paper":
                settled, _wins, net = self.store.scoreboard(**sizing)
                return {"settled": settled, "net": net,
                        "basis": f"{sizing['contracts']:g} contract"
                                 f"{'s' if sizing['contracts'] != 1 else ''}"}
            if variant == "sessions":
                from .sessions import one_line

                rows = self.store.session_rows(now_ms)
                if not rows:
                    return None
                from .sessions import breakdown

                return {"line": one_line(breakdown(rows))}
            if variant == "qualified":
                settled, wins, net = self.store.scoreboard(
                    **sizing, qualified_only=True
                )
                return {"settled": settled, "wins": wins, "net": net}
        except Exception:  # noqa: BLE001 - an insight never breaks a message
            return None
        return None

    # --------------------------------------------------------- delivery
    async def send_once(self, kind: str, key: str, text: str, now_ms: int,
                        buttons=None) -> bool:
        """Send this event exactly once, ever. True if it went out now.

        The delivery row is written only AFTER the send returns, so a failed
        send is retried on the next poll rather than being recorded as
        delivered and lost.
        """
        if self.store.delivered(kind, key) is not None:
            return False
        message_id = await self.telegram.send(text, buttons)
        self.store.mark_delivered(kind, key, now_ms, message_id, text)
        return True

    async def update_status(self, kind: str, key: str, text: str, now_ms: int,
                            buttons=None) -> bool:
        """Edit an existing message in place, or send it if it is not up yet.

        THE BUTTONS ARE ALWAYS PASSED BACK. `editMessageText` drops the inline
        keyboard when `reply_markup` is omitted, so an edit that forgets them
        silently removes the operator's execute button from a live signal.

        Used for the waiting timer: the signal's own status line is rewritten
        as the band-hold advances, rather than a second message being sent.
        """
        record = self.store.delivered(kind, key)
        if record is None:
            await self.send_once(kind, key, text, now_ms, buttons)
            return True
        if (record.get("body") or "") == text:
            return False          # the screen already says this
        message_id = record.get("message_id")
        if not message_id:
            return False
        changed = await self.telegram.edit(message_id, text, buttons)
        if changed:
            self.store.update_delivered(kind, key, now_ms, text)
        return changed

    async def deliver_result(self, window_open: int, kind: str, key: str,
                             text: str, now_ms: int) -> bool:
        """A market-result message, after which the rotation advances.

        Advancing here and nowhere else is what makes the rotation legible:
        one step per settled market that was actually reported, so the reader
        sees each variant in turn instead of it skipping on messages they
        never received.
        """
        sent = await self.send_once(kind, key, text, now_ms)
        if sent:
            self.store.advance_insight(surface.INSIGHTS, now_ms)
        return sent
