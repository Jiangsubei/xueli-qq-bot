from __future__ import annotations

from typing import Any, Dict, Optional

import aiohttp


class AIHTTPSessionManager:
    """Own the aiohttp session lifecycle for the AI facade."""

    def __init__(
        self,
        *,
        api_key: str,
        extra_headers: Dict[str, str] | None = None,
        timeout: int = 60,
    ):
        self.api_key = api_key
        self.extra_headers = dict(extra_headers or {})
        self.timeout = timeout
        self.session: Optional[aiohttp.ClientSession] = None

    async def ensure_session(self) -> aiohttp.ClientSession:
        if self.session is None or self.session.closed:
            headers = {
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            }
            headers.update(self.extra_headers)
            self.session = aiohttp.ClientSession(
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=self.timeout),
            )
        return self.session

    async def post_text(self, url: str, payload: Dict[str, Any]) -> tuple[int, str]:
        session = await self.ensure_session()
        async with session.post(url, json=payload) as response:
            return response.status, await response.text()

    async def close(self) -> None:
        if self.session and not self.session.closed:
            await self.session.close()
