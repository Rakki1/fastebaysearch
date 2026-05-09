from __future__ import annotations

import asyncio
import html
import logging
from datetime import datetime

from ..config import TelegramConfig
from ..models import SearchResult
from ..utils import safe_url


def build_caption(result: SearchResult) -> str:
    name = html.escape(result.name)
    price = html.escape(result.price)
    keywords = html.escape(result.keywords)
    link = html.escape(safe_url(result.link), quote=True)
    return (
        f"⭐ <b>New listing!</b>\n"
        f"📦 {name}\n"
        f"💰 {price}\n"
        f"🔍 <i>Search: {keywords}</i>\n"
        f'🔗 <a href="{link}">Link to item</a>'
    )


class TelegramNotifier:
    def __init__(self, config: TelegramConfig, logger: logging.Logger | None = None, sleep_seconds: float = 0.2):
        self.config = config
        self.logger = logger or logging.getLogger("fastebaysearch")
        self.sleep_seconds = sleep_seconds

    async def send_header(self, count: int) -> bool:
        import aiohttp

        text = f"<b>SEARCH COMPLETE ({datetime.now().strftime('%d.%m. at %H:%M')})</b>\nFound <b>{count}</b> new items!"
        url = f"https://api.telegram.org/bot{self.config.token}/sendMessage"
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session:
            try:
                async with session.post(url, json={"chat_id": self.config.chat_id, "text": text, "parse_mode": "HTML"}) as response:
                    data = await response.json(content_type=None)
                    if response.status != 200 or not data.get("ok", False):
                        self.logger.error(f"Telegram header error: HTTP {response.status} - {data}")
                        return False
                    return True
            except asyncio.TimeoutError:
                self.logger.error("Telegram header timeout.")
            except (aiohttp.ClientError, OSError, ValueError) as exc:
                self.logger.error(f"Telegram header network error: {exc}")
        return False

    async def send(self, results: list[SearchResult]) -> list[SearchResult]:
        import aiohttp

        results = results[: self.config.max_per_run]
        base_url = f"https://api.telegram.org/bot{self.config.token}"
        sent_results: list[SearchResult] = []
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session:
            for result in results:
                caption = build_caption(result)
                image_url = safe_url(result.image)
                want_photo = bool(image_url) and self.config.send_mode in {"auto", "photo", "photo_only"}

                if want_photo:
                    payload = {"chat_id": self.config.chat_id, "photo": image_url, "caption": caption, "parse_mode": "HTML"}
                    url = f"{base_url}/sendPhoto"
                elif self.config.send_mode == "photo_only":
                    self.logger.info(f"Skipping (photo_only, no image): {result.name}")
                    continue
                else:
                    payload = {
                        "chat_id": self.config.chat_id,
                        "text": caption,
                        "parse_mode": "HTML",
                        "disable_web_page_preview": self.config.disable_web_preview,
                    }
                    url = f"{base_url}/sendMessage"

                if await self._post(session, base_url, url, payload, result):
                    sent_results.append(result)
                await asyncio.sleep(self.sleep_seconds)
        return sent_results

    async def _post(self, session, base_url: str, url: str, payload: dict[str, object], result: SearchResult) -> bool:
        try:
            async with session.post(url, json=payload) as response:
                data = await response.json(content_type=None)
                if response.status == 200 and data.get("ok", False):
                    return True
                self.logger.error(f"Telegram error for {result.name}: HTTP {response.status} - {data}")

                if url.endswith("/sendPhoto") and self.config.send_mode != "photo_only":
                    fallback = {
                        "chat_id": self.config.chat_id,
                        "text": payload["caption"],
                        "parse_mode": "HTML",
                        "disable_web_page_preview": self.config.disable_web_preview,
                    }
                    async with session.post(f"{base_url}/sendMessage", json=fallback) as fallback_response:
                        fallback_data = await fallback_response.json(content_type=None)
                        if fallback_response.status == 200 and fallback_data.get("ok", False):
                            return True
                        else:
                            self.logger.error(
                                f"Telegram text fallback failed for {result.name}: "
                                f"HTTP {fallback_response.status} - {fallback_data}"
                            )
        except asyncio.TimeoutError:
            self.logger.error(f"Telegram timeout for {result.name}")
        except (aiohttp.ClientError, OSError, ValueError) as exc:
            self.logger.error(f"Telegram network error for {result.name}: {exc}")
        return False
