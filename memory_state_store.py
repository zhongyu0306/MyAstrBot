from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterator

from astrbot.api import logger
from astrbot.api.star import StarTools


StateNormalizer = Callable[[Any], Any]

_DB_NAME = "user_memory.sqlite3"
_TABLE_NAME = "plugin_state"
_INIT_LOCK = threading.RLock()
_DB_READY = False


def _db_path() -> Path:
    base = Path(StarTools.get_data_dir("astrbot_all_char"))
    base.mkdir(parents=True, exist_ok=True)
    return base / _DB_NAME


def get_shared_db_path() -> Path:
    return _db_path()


@contextmanager
def _get_conn() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(_db_path())
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def shared_conn() -> Iterator[sqlite3.Connection]:
    _ensure_db()
    with _get_conn() as conn:
        yield conn


def _ensure_db() -> None:
    global _DB_READY
    if _DB_READY:
        return
    with _INIT_LOCK:
        if _DB_READY:
            return
        with _get_conn() as conn:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = NORMAL")
            conn.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {_TABLE_NAME} (
                    namespace TEXT NOT NULL,
                    state_key TEXT NOT NULL DEFAULT '',
                    payload TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(namespace, state_key)
                )
                """
            )
            conn.execute(
                f"CREATE INDEX IF NOT EXISTS idx_{_TABLE_NAME}_updated_at "
                f"ON {_TABLE_NAME}(updated_at DESC)"
            )
            conn.commit()
        _DB_READY = True


def _now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _normalize_state_key(state_key: str | None) -> str:
    return str(state_key or "").strip()


def _normalize_payload(payload: Any, normalizer: StateNormalizer | None) -> Any:
    if normalizer is None:
        return payload
    try:
        return normalizer(payload)
    except Exception as exc:
        logger.warning("状态数据标准化失败: %s", exc)
        return None


def load_json_state(
    namespace: str,
    *,
    state_key: str = "",
    default: Any,
    normalizer: StateNormalizer | None = None,
    legacy_path: Path | None = None,
) -> Any:
    _ensure_db()
    normalized_key = _normalize_state_key(state_key)
    with _get_conn() as conn:
        row = conn.execute(
            f"SELECT payload FROM {_TABLE_NAME} WHERE namespace = ? AND state_key = ?",
            (namespace, normalized_key),
        ).fetchone()
        if row is not None:
            try:
                payload = json.loads(str(row["payload"] or "null"))
            except Exception as exc:
                logger.error("读取数据库状态失败 namespace=%s key=%s err=%s", namespace, normalized_key, exc)
                payload = default
            normalized = _normalize_payload(payload, normalizer)
            return default if normalized is None else normalized

    if legacy_path is not None and legacy_path.exists():
        try:
            payload = json.loads(legacy_path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.error("读取旧状态文件失败 namespace=%s path=%s err=%s", namespace, legacy_path, exc)
            payload = default
        normalized = _normalize_payload(payload, normalizer)
        if normalized is None:
            normalized = default
        save_json_state(namespace, normalized, state_key=normalized_key)
        return normalized

    return default


def save_json_state(namespace: str, payload: Any, *, state_key: str = "") -> None:
    _ensure_db()
    normalized_key = _normalize_state_key(state_key)
    encoded = json.dumps(payload, ensure_ascii=False)
    with _get_conn() as conn:
        conn.execute(
            f"""
            INSERT INTO {_TABLE_NAME}(namespace, state_key, payload, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(namespace, state_key)
            DO UPDATE SET payload = excluded.payload, updated_at = excluded.updated_at
            """,
            (namespace, normalized_key, encoded, _now_str()),
        )
        conn.commit()


def delete_json_state(namespace: str, *, state_key: str = "") -> None:
    _ensure_db()
    normalized_key = _normalize_state_key(state_key)
    with _get_conn() as conn:
        conn.execute(
            f"DELETE FROM {_TABLE_NAME} WHERE namespace = ? AND state_key = ?",
            (namespace, normalized_key),
        )
        conn.commit()
