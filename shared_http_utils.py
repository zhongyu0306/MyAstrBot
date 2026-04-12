from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Mapping

import aiohttp

from astrbot.api import logger


@dataclass
class _SharedSessionEntry:
    session: aiohttp.ClientSession
    loop_id: int
    config_signature: tuple[tuple[tuple[str, str], ...], int, int, bool, float | None]


_SHARED_SESSIONS: dict[str, _SharedSessionEntry] = {}
_SHARED_SESSION_LOCK = asyncio.Lock()


def _normalize_headers(default_headers: Mapping[str, str] | None) -> tuple[tuple[str, str], ...]:
    if not default_headers:
        return ()
    return tuple(sorted((str(key), str(value)) for key, value in default_headers.items()))


async def get_shared_http_session(
    name: str,
    *,
    default_headers: Mapping[str, str] | None = None,
    connector_limit: int = 100,
    ttl_dns_cache: int = 300,
    trust_env: bool = False,
    total_timeout_seconds: float | None = None,
) -> aiohttp.ClientSession:
    session_name = str(name or "").strip() or "default"
    current_loop = asyncio.get_running_loop()
    normalized_timeout = None if total_timeout_seconds is None else max(float(total_timeout_seconds), 0.0)
    config_signature = (
        _normalize_headers(default_headers),
        max(1, int(connector_limit)),
        max(0, int(ttl_dns_cache)),
        bool(trust_env),
        normalized_timeout,
    )

    async with _SHARED_SESSION_LOCK:
        existing = _SHARED_SESSIONS.get(session_name)
        if existing and not existing.session.closed and existing.loop_id == id(current_loop):
            if existing.config_signature == config_signature:
                return existing.session
            try:
                await existing.session.close()
            except Exception as exc:
                logger.debug("[shared_http] close reconfigure session failed: session=%s err=%s", session_name, exc)
            _SHARED_SESSIONS.pop(session_name, None)
            logger.debug(
                "[shared_http] recreate session=%s due to config change old=%s new=%s",
                session_name,
                existing.config_signature,
                config_signature,
            )

        if existing and not existing.session.closed:
            logger.debug(
                "[shared_http] session loop changed without explicit close: session=%s old_loop=%s new_loop=%s",
                session_name,
                existing.loop_id,
                id(current_loop),
            )

        connector = aiohttp.TCPConnector(
            limit=config_signature[1],
            ttl_dns_cache=config_signature[2],
            enable_cleanup_closed=True,
        )
        session = aiohttp.ClientSession(
            connector=connector,
            headers=dict(default_headers or {}),
            trust_env=config_signature[3],
            timeout=aiohttp.ClientTimeout(total=normalized_timeout) if normalized_timeout is not None else aiohttp.client.DEFAULT_TIMEOUT,
        )
        _SHARED_SESSIONS[session_name] = _SharedSessionEntry(
            session=session,
            loop_id=id(current_loop),
            config_signature=config_signature,
        )
        return session


async def close_shared_http_sessions() -> None:
    async with _SHARED_SESSION_LOCK:
        entries = list(_SHARED_SESSIONS.items())
        _SHARED_SESSIONS.clear()
    for session_name, entry in entries:
        if entry.session.closed:
            continue
        try:
            await entry.session.close()
        except Exception as exc:
            logger.debug("[shared_http] close session failed: session=%s err=%s", session_name, exc)
