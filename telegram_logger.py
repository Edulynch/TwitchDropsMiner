from __future__ import annotations

import sys
import html
import asyncio
import logging
from typing import TYPE_CHECKING

import aiohttp

if TYPE_CHECKING:
    pass

TELEGRAM_MAX_MESSAGE_LENGTH = 4096


class TelegramHandler(logging.Handler):
    """
    Async logging handler that batches log messages and sends them
    to a Telegram chat every `flush_interval` seconds as a single message.
    """

    def __init__(
        self,
        bot_token: str,
        chat_id: str,
        flush_interval: float = 5.0,
        level: int = logging.NOTSET,
    ):
        super().__init__(level)
        self._bot_token = bot_token
        self._chat_id = chat_id
        self._flush_interval = flush_interval
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._session: aiohttp.ClientSession | None = None
        self._task: asyncio.Task[None] | None = None
        self._api_url = f"https://api.telegram.org/bot{bot_token}/sendMessage"

    def start(self) -> None:
        """Start the background flush loop. Must be called from an async context."""
        if self._task is None:
            self._task = asyncio.create_task(self._flush_loop())

    def emit(self, record: logging.LogRecord) -> None:
        """Put a formatted log record into the queue for batched sending."""
        try:
            msg = self.format(record)
            self._queue.put_nowait(msg)
        except Exception:
            self.handleError(record)

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=30)
            )
        return self._session

    async def _flush_loop(self) -> None:
        """Background task: waits `flush_interval` seconds, collects all queued
        messages, and sends them as one (or more) Telegram messages."""
        while True:
            try:
                await asyncio.sleep(self._flush_interval)
                await self._flush()
            except asyncio.CancelledError:
                # Final flush before exiting
                await self._flush()
                break
            except Exception as exc:
                # Print to stderr to avoid recursion into this handler
                print(
                    f"[TelegramHandler] Error in flush loop: {exc}",
                    file=sys.stderr,
                )

    async def _flush(self) -> None:
        """Drain the queue and send all accumulated messages."""
        lines: list[str] = []
        while not self._queue.empty():
            try:
                lines.append(self._queue.get_nowait())
            except asyncio.QueueEmpty:
                break

        if not lines:
            return

        # Combine all lines, escape HTML, and wrap in <pre> block
        escaped = html.escape("\n".join(lines))
        full_text = f"<pre>{escaped}</pre>"

        # Split into chunks respecting Telegram's 4096 char limit
        # Account for <pre></pre> tags (11 chars) when splitting
        max_content = TELEGRAM_MAX_MESSAGE_LENGTH - 13  # <pre>...</pre>
        content = escaped
        parts = self._split_text(content, max_content)
        for part in parts:
            await self._send_message(f"<pre>{part}</pre>")

    @staticmethod
    def _split_text(text: str, max_length: int) -> list[str]:
        """Split a long text into chunks of at most `max_length` characters,
        splitting on newline boundaries where possible."""
        if len(text) <= max_length:
            return [text]

        chunks: list[str] = []
        while text:
            if len(text) <= max_length:
                chunks.append(text)
                break

            # Try to split at the last newline within the limit
            split_pos = text.rfind("\n", 0, max_length)
            if split_pos == -1:
                # No newline found, hard split
                split_pos = max_length

            chunks.append(text[:split_pos])
            text = text[split_pos:].lstrip("\n")

        return chunks

    async def _send_message(self, text: str) -> None:
        """Send a single message to the Telegram chat."""
        try:
            session = await self._get_session()
            payload = {
                "chat_id": self._chat_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            }
            async with session.post(self._api_url, json=payload) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    print(
                        f"[TelegramHandler] Telegram API error {resp.status}: {body}",
                        file=sys.stderr,
                    )
        except Exception as exc:
            print(
                f"[TelegramHandler] Failed to send message: {exc}",
                file=sys.stderr,
            )

    async def async_close(self) -> None:
        """Gracefully stop the flush loop and close the HTTP session."""
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

        if self._session is not None and not self._session.closed:
            await self._session.close()
            self._session = None

    def close(self) -> None:
        """Override logging.Handler.close — schedule async cleanup if possible."""
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self.async_close())
        except RuntimeError:
            pass
        super().close()
