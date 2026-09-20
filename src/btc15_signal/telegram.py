import httpx


class Telegram:
    def __init__(self, token: str, chat_id: str, dry_run: bool) -> None:
        self.token = token
        self.chat_id = chat_id
        self.dry_run = dry_run
        self.offset = 0

    async def send(self, text: str, buttons: list[tuple[str, str]] | None = None) -> int | None:
        if self.dry_run or not self.token or not self.chat_id:
            print(text, flush=True)
            return None
        payload = {"chat_id": self.chat_id, "text": text}
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
