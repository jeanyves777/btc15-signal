import sys

import httpx


def safe_print(text: str) -> None:
    """Print without letting an emoji kill the service.

    Dry-run mode echoes every message to stdout, and a Windows console defaults
    to cp1252, which cannot encode the emoji these messages use. An unguarded
    print raises UnicodeEncodeError and takes the polling loop down with it.
    """
    stream = sys.stdout
    try:
        print(text, flush=True)
    except UnicodeEncodeError:
        encoding = getattr(stream, "encoding", None) or "ascii"
        print(text.encode(encoding, errors="replace").decode(encoding), flush=True)


class Telegram:
    def __init__(self, token: str, chat_id: str, dry_run: bool) -> None:
        self.token = token
        self.chat_id = chat_id
        self.dry_run = dry_run
        self.offset = 0

    async def send(self, text: str, buttons: list[tuple[str, str]] | None = None) -> int | None:
        if self.dry_run or not self.token or not self.chat_id:
            safe_print(text)
            return None
        payload = {
            "chat_id": self.chat_id,
            "text": text,
            # Telegram offers no text colour; the messages carry emoji chips for
            # that and use this HTML subset for weight and monospace alignment.
            "parse_mode": "HTML",
            "link_preview_options": {"is_disabled": True},
        }
        if buttons:
            payload["reply_markup"] = {
                "inline_keyboard": [
                    [{"text": label, "callback_data": data} for label, data in buttons]
                ]
            }
        async with httpx.AsyncClient(timeout=8) as client:
            response = await client.post(
                f"https://api.telegram.org/bot{self.token}/sendMessage",
                json=payload,
            )
            response.raise_for_status()
            return response.json()["result"]["message_id"]

    async def edit(self, message_id: int, text: str,
                   buttons: list[tuple[str, str]] | None = None) -> bool:
        """Rewrite a message already on the screen. True if it changed.

        The waiting message updates its timer IN PLACE. Sending a fresh one
        each poll is the behaviour this replaces: at a ten-second poll a single
        window produced dozens of near-identical notifications, which trains
        the reader to swipe them away - and the one that matters goes with them.

        Telegram answers `message is not modified` when the new text equals the
        old. That is success, not an error: it means the screen already says
        what we wanted it to say.
        """
        if self.dry_run or not self.token or not self.chat_id:
            safe_print(text)
            return False
        payload = {
            "chat_id": self.chat_id,
            "message_id": message_id,
            "text": text,
            "parse_mode": "HTML",
            "link_preview_options": {"is_disabled": True},
        }
        if buttons:
            payload["reply_markup"] = {
                "inline_keyboard": [
                    [{"text": label, "callback_data": data}
                     for label, data in buttons]
                ]
            }
        async with httpx.AsyncClient(timeout=8) as client:
            response = await client.post(
                f"https://api.telegram.org/bot{self.token}/editMessageText",
                json=payload,
            )
        if response.status_code == 400 and "not modified" in response.text:
            return False
        response.raise_for_status()
        return True

    async def updates(self) -> list[dict]:
        if self.dry_run or not self.token:
            return []
        async with httpx.AsyncClient(timeout=8) as client:
            response = await client.get(
                f"https://api.telegram.org/bot{self.token}/getUpdates",
                params={
                    "offset": self.offset,
                    "timeout": 0,
                    "allowed_updates": '["message","callback_query"]',
                },
            )
            response.raise_for_status()
            updates = response.json()["result"]
        if updates:
            self.offset = max(item["update_id"] for item in updates) + 1
        return updates

    async def answer_callback(self, callback_id: str, text: str) -> None:
        if self.dry_run or not self.token:
            return
        async with httpx.AsyncClient(timeout=8) as client:
            response = await client.post(
                f"https://api.telegram.org/bot{self.token}/answerCallbackQuery",
                json={"callback_query_id": callback_id, "text": text, "show_alert": True},
            )
            response.raise_for_status()

    async def clear_buttons(self, chat_id: int, message_id: int) -> None:
        if self.dry_run or not self.token:
            return
        async with httpx.AsyncClient(timeout=8) as client:
            response = await client.post(
                f"https://api.telegram.org/bot{self.token}/editMessageReplyMarkup",
                json={"chat_id": chat_id, "message_id": message_id, "reply_markup": {}},
            )
            response.raise_for_status()
