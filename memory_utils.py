from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
from astrbot.api.star import StarTools


DEFAULT_ADMIN_QQ_IDS: tuple[str, ...] = ()
_configured_admin_qq_ids: tuple[str, ...] = DEFAULT_ADMIN_QQ_IDS
DEFAULT_OBSERVE_USER_THROTTLE_SECONDS = 120
DEFAULT_RELATED_MEMORY_CACHE_TTL_SECONDS = 60


def _normalize_admin_qq_ids(raw_value: Any) -> tuple[str, ...]:
    candidates: list[str] = []
    if isinstance(raw_value, (list, tuple, set)):
        iterable = list(raw_value)
    else:
        iterable = re.split(r"[,，\s]+", str(raw_value or "").strip()) if str(raw_value or "").strip() else []

    seen: set[str] = set()
    for item in iterable:
        qq_id = str(item or "").strip()
        if not qq_id or not qq_id.isdigit() or qq_id in seen:
            continue
        candidates.append(qq_id)
        seen.add(qq_id)
    return tuple(candidates) if candidates else DEFAULT_ADMIN_QQ_IDS


def configure_memory_admin_qq_ids(raw_value: Any) -> tuple[str, ...]:
    global _configured_admin_qq_ids
    _configured_admin_qq_ids = _normalize_admin_qq_ids(raw_value)
    return _configured_admin_qq_ids


def get_memory_admin_qq_ids() -> tuple[str, ...]:
    return _configured_admin_qq_ids


def get_memory_admin_display_text() -> str:
    admin_ids = get_memory_admin_qq_ids()
    return "、".join(admin_ids) if admin_ids else "未配置"


class UserMemoryStore:
    """基于 SQLite 的用户永久记忆"""

    DB_NAME = "user_memory.sqlite3"
    LEGACY_JSON_NAME = "user_memory.json"
    ALIAS_TYPE_MEMORY = "memory"
    ALIAS_TYPE_PLATFORM = "platform"
    SCENE_GLOBAL = "global"
    SCENE_GROUP = "group"
    SCENE_PRIVATE = "private"

    def __init__(self) -> None:
        self._data_dir = Path(StarTools.get_data_dir("astrbot_all_char"))
        self._data_dir.mkdir(parents=True, exist_ok=True)
        self._db_path = self._data_dir / self.DB_NAME
        self._legacy_json_path = self._data_dir / self.LEGACY_JSON_NAME
        self._observe_user_throttle_seconds = DEFAULT_OBSERVE_USER_THROTTLE_SECONDS
        self._observe_user_cache: dict[str, tuple[float, str]] = {}
        self._related_memory_cache_ttl_seconds = DEFAULT_RELATED_MEMORY_CACHE_TTL_SECONDS
        self._related_memory_cache_lock = threading.RLock()
        self._related_memory_cache_ready = False
        self._related_memory_cache_built_at = 0.0
        self._related_memory_entries_cache: list[dict[str, Any]] = []
        self._related_memory_candidates_cache: dict[str, list[tuple[str, int]]] = {}
        self._related_memory_entries_by_qq_cache: dict[str, dict[str, Any]] = {}
        self._related_memory_inverted_index_cache: dict[str, list[tuple[str, int]]] = {}
        self._related_memory_prefix_index_cache: dict[str, list[str]] = {}
        self._init_db()
        self._maybe_migrate_from_json()

    @contextmanager
    def _get_conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self._get_conn() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    qq_id TEXT PRIMARY KEY,
                    note TEXT NOT NULL DEFAULT '',
                    platform_name TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL DEFAULT ''
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS user_aliases (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    qq_id TEXT NOT NULL,
                    alias TEXT NOT NULL,
                    alias_type TEXT NOT NULL,
                    alias_normalized TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE (qq_id, alias_type, alias_normalized),
                    FOREIGN KEY (qq_id) REFERENCES users(qq_id) ON DELETE CASCADE
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_user_aliases_lookup "
                "ON user_aliases(alias_type, alias_normalized)"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS user_scene_aliases (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    qq_id TEXT NOT NULL,
                    alias TEXT NOT NULL,
                    alias_normalized TEXT NOT NULL,
                    scene_type TEXT NOT NULL,
                    scene_value TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE (qq_id, scene_type, scene_value, alias_normalized),
                    FOREIGN KEY (qq_id) REFERENCES users(qq_id) ON DELETE CASCADE
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_user_scene_aliases_lookup "
                "ON user_scene_aliases(qq_id, scene_type, scene_value, updated_at DESC)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_users_updated_at ON users(updated_at DESC)"
            )
            conn.commit()

    @staticmethod
    def _now_str() -> str:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    @staticmethod
    def _clamp_int(value: Any, default: int, minimum: int, maximum: int) -> int:
        try:
            parsed = int(value)
        except Exception:
            parsed = default
        if parsed < minimum:
            return minimum
        if parsed > maximum:
            return maximum
        return parsed

    def set_observe_user_throttle_seconds(self, seconds: Any) -> int:
        self._observe_user_throttle_seconds = self._clamp_int(
            seconds,
            default=DEFAULT_OBSERVE_USER_THROTTLE_SECONDS,
            minimum=0,
            maximum=24 * 3600,
        )
        return self._observe_user_throttle_seconds

    def set_related_memory_cache_ttl_seconds(self, seconds: Any) -> int:
        self._related_memory_cache_ttl_seconds = self._clamp_int(
            seconds,
            default=DEFAULT_RELATED_MEMORY_CACHE_TTL_SECONDS,
            minimum=0,
            maximum=24 * 3600,
        )
        self._invalidate_related_memory_cache()
        return self._related_memory_cache_ttl_seconds

    def _should_skip_observe_write(self, qq_id: str, sender_name: str, now_tick: float) -> bool:
        throttle = self._observe_user_throttle_seconds
        if throttle <= 0:
            return False
        cached = self._observe_user_cache.get(qq_id)
        if not cached:
            return False
        last_tick, last_name = cached
        if now_tick - last_tick >= throttle:
            return False
        # 节流窗口内若昵称变化，仍然立即落库，避免错过最新平台昵称
        if sender_name and sender_name != last_name:
            return False
        return True

    def _touch_observe_cache(self, qq_id: str, sender_name: str, now_tick: float) -> None:
        current = self._observe_user_cache.get(qq_id)
        cached_name = sender_name or (current[1] if current else "")
        self._observe_user_cache[qq_id] = (now_tick, cached_name)
        if len(self._observe_user_cache) > 4096:
            threshold = now_tick - max(300.0, float(self._observe_user_throttle_seconds) * 2.0)
            self._observe_user_cache = {
                key: value for key, value in self._observe_user_cache.items() if value[0] >= threshold
            }

    def _invalidate_related_memory_cache(self) -> None:
        with self._related_memory_cache_lock:
            self._related_memory_cache_ready = False
            self._related_memory_cache_built_at = 0.0
            self._related_memory_entries_cache = []
            self._related_memory_candidates_cache = {}
            self._related_memory_entries_by_qq_cache = {}
            self._related_memory_inverted_index_cache = {}
            self._related_memory_prefix_index_cache = {}

    def _prepare_entry_match_candidates(self, qq_id: str, entry: dict[str, Any]) -> list[tuple[str, int]]:
        prepared: list[tuple[str, int]] = []
        seen: set[str] = set()
        for candidate, weight in self._entry_match_candidates(qq_id, entry):
            normalized_candidate = self._normalize_alias(candidate)
            if not normalized_candidate or normalized_candidate in seen:
                continue
            seen.add(normalized_candidate)
            prepared.append((normalized_candidate, weight + min(len(normalized_candidate), 12)))
        return prepared

    def _rebuild_related_memory_cache_locked(self, now_tick: float) -> None:
        started = time.monotonic()
        entries = self._list_entries(include_observed_only=False)
        entries_by_qq: dict[str, dict[str, Any]] = {}
        candidates: dict[str, list[tuple[str, int]]] = {}
        inverted_temp: dict[str, dict[str, int]] = {}
        for entry in entries:
            qq_id = str(entry.get("qq_id") or "").strip()
            if not qq_id:
                continue
            entries_by_qq[qq_id] = entry
            prepared = self._prepare_entry_match_candidates(qq_id, entry)
            if prepared:
                candidates[qq_id] = prepared
                for normalized_candidate, score in prepared:
                    score_map = inverted_temp.setdefault(normalized_candidate, {})
                    old_score = score_map.get(qq_id, 0)
                    if score > old_score:
                        score_map[qq_id] = score

        inverted_index: dict[str, list[tuple[str, int]]] = {}
        prefix_index: dict[str, list[str]] = {}
        mapping_count = 0
        for normalized_candidate, score_map in inverted_temp.items():
            pairs = sorted(score_map.items(), key=lambda item: (-item[1], item[0]))
            inverted_index[normalized_candidate] = pairs
            mapping_count += len(pairs)
            prefix = normalized_candidate[0]
            prefix_index.setdefault(prefix, []).append(normalized_candidate)

        for prefix, candidate_list in prefix_index.items():
            candidate_list.sort(key=lambda item: (-len(item), item))

        self._related_memory_entries_cache = entries
        self._related_memory_candidates_cache = candidates
        self._related_memory_entries_by_qq_cache = entries_by_qq
        self._related_memory_inverted_index_cache = inverted_index
        self._related_memory_prefix_index_cache = prefix_index
        self._related_memory_cache_built_at = now_tick
        self._related_memory_cache_ready = True
        logger.info(
            (
                "[astrbot_all_char][memory] 相关人物索引重建: entries=%s, candidates=%s, "
                "mappings=%s, cost_ms=%s, ttl=%ss"
            ),
            len(entries_by_qq),
            len(inverted_index),
            mapping_count,
            int((time.monotonic() - started) * 1000),
            self._related_memory_cache_ttl_seconds,
        )

    def _get_related_memory_cache(
        self,
    ) -> tuple[dict[str, dict[str, Any]], dict[str, list[tuple[str, int]]], dict[str, list[str]], bool]:
        ttl = self._related_memory_cache_ttl_seconds
        now_tick = time.monotonic()
        with self._related_memory_cache_lock:
            need_refresh = not self._related_memory_cache_ready
            if not need_refresh and ttl > 0:
                need_refresh = (now_tick - self._related_memory_cache_built_at) >= ttl
            cache_hit = not need_refresh and ttl > 0
            if need_refresh or ttl <= 0:
                self._rebuild_related_memory_cache_locked(now_tick)
                cache_hit = False
            return (
                self._related_memory_entries_by_qq_cache,
                self._related_memory_inverted_index_cache,
                self._related_memory_prefix_index_cache,
                cache_hit,
            )

    @staticmethod
    def _extract_matching_candidates(
        normalized_text: str,
        prefix_index: dict[str, list[str]],
        *,
        max_candidates: int = 256,
    ) -> set[str]:
        if not normalized_text or not prefix_index:
            return set()

        text_len = len(normalized_text)
        matched: set[str] = set()
        for index, ch in enumerate(normalized_text):
            candidates = prefix_index.get(ch)
            if not candidates:
                continue
            remain_len = text_len - index
            for candidate in candidates:
                if candidate in matched:
                    continue
                if len(candidate) > remain_len:
                    continue
                if normalized_text.startswith(candidate, index):
                    matched.add(candidate)
                    if len(matched) >= max_candidates:
                        return matched
        return matched

    @staticmethod
    def _normalize_alias(value: Any) -> str:
        text = str(value or "").strip()
        if not text:
            return ""
        return re.sub(r"\s+", "", text).lower()

    @staticmethod
    def _safe_sender_id(event: AstrMessageEvent) -> str:
        try:
            sender_id = event.get_sender_id()
            if sender_id:
                return str(sender_id).strip()
        except Exception:
            pass
        return ""

    @staticmethod
    def _safe_sender_name(event: AstrMessageEvent) -> str:
        try:
            sender_name = event.get_sender_name()
            if sender_name:
                return str(sender_name).strip()
        except Exception:
            pass
        return ""

    @staticmethod
    def _safe_message_text(event: AstrMessageEvent) -> str:
        try:
            return str(event.get_message_str() or "").strip()
        except Exception:
            return ""

    @staticmethod
    def _safe_group_id(event: AstrMessageEvent) -> str:
        try:
            group_id = event.get_group_id()
            if group_id:
                return str(group_id).strip()
        except Exception:
            pass
        return ""

    @staticmethod
    def _safe_is_private_chat(event: AstrMessageEvent) -> bool:
        try:
            return bool(event.is_private_chat())
        except Exception:
            return False

    @classmethod
    def _normalize_scene_scope(cls, scene_type: Any, scene_value: Any = "") -> tuple[str, str]:
        raw_scene_type = str(scene_type or "").strip().lower()
        raw_scene_value = str(scene_value or "").strip()
        if raw_scene_type in {cls.SCENE_GROUP, "群", "群聊"}:
            digits = "".join(ch for ch in raw_scene_value if ch.isdigit())
            return cls.SCENE_GROUP, digits
        if raw_scene_type in {cls.SCENE_PRIVATE, "私聊"}:
            return cls.SCENE_PRIVATE, ""
        return cls.SCENE_GLOBAL, ""

    @classmethod
    def _scope_label(cls, scene_type: str, scene_value: str) -> str:
        normalized_type, normalized_value = cls._normalize_scene_scope(scene_type, scene_value)
        if normalized_type == cls.SCENE_GROUP:
            return f"群聊 {normalized_value}" if normalized_value else "群聊"
        if normalized_type == cls.SCENE_PRIVATE:
            return "私聊"
        return "全局"

    @classmethod
    def _event_scene_scope(cls, event: AstrMessageEvent | None) -> tuple[str, str]:
        if not event:
            return cls.SCENE_GLOBAL, ""
        if cls._safe_is_private_chat(event):
            return cls.SCENE_PRIVATE, ""
        group_id = cls._safe_group_id(event)
        if group_id:
            return cls.SCENE_GROUP, group_id
        return cls.SCENE_GLOBAL, ""

    @staticmethod
    def _is_admin_event(event: AstrMessageEvent) -> bool:
        return UserMemoryStore._safe_sender_id(event) in get_memory_admin_qq_ids()

    def _get_meta(self, conn: sqlite3.Connection, key: str) -> str:
        row = conn.execute("SELECT value FROM metadata WHERE key = ?", (key,)).fetchone()
        if row and row["value"] is not None:
            return str(row["value"])
        return ""

    def _set_meta(self, conn: sqlite3.Connection, key: str, value: str) -> None:
        conn.execute(
            """
            INSERT INTO metadata (key, value)
            VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, str(value)),
        )

    def _ensure_user(
        self,
        conn: sqlite3.Connection,
        qq_id: str,
        *,
        created_at: str | None = None,
        updated_at: str | None = None,
        last_seen_at: str | None = None,
        note: str = "",
        platform_name: str = "",
    ) -> None:
        qq_id = str(qq_id or "").strip()
        if not qq_id:
            return
        now = self._now_str()
        conn.execute(
            """
            INSERT INTO users (qq_id, note, platform_name, created_at, updated_at, last_seen_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(qq_id) DO NOTHING
            """,
            (
                qq_id,
                str(note or "").strip(),
                str(platform_name or "").strip(),
                created_at or now,
                updated_at or now,
                last_seen_at or "",
            ),
        )

    def _upsert_alias(
        self,
        conn: sqlite3.Connection,
        qq_id: str,
        alias: str,
        alias_type: str,
        *,
        created_at: str | None = None,
        updated_at: str | None = None,
    ) -> bool:
        qq_id = str(qq_id or "").strip()
        alias = str(alias or "").strip()
        normalized = self._normalize_alias(alias)
        if not qq_id or not alias or not normalized:
            return False

        now = self._now_str()
        conn.execute(
            """
            INSERT INTO user_aliases (
                qq_id, alias, alias_type, alias_normalized, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(qq_id, alias_type, alias_normalized) DO UPDATE SET
                alias = excluded.alias,
                updated_at = excluded.updated_at
            """,
            (
                qq_id,
                alias,
                alias_type,
                normalized,
                created_at or now,
                updated_at or now,
            ),
        )
        return True

    def _upsert_scene_alias(
        self,
        conn: sqlite3.Connection,
        qq_id: str,
        alias: str,
        scene_type: str,
        scene_value: str = "",
        *,
        created_at: str | None = None,
        updated_at: str | None = None,
    ) -> bool:
        qq_id = str(qq_id or "").strip()
        alias = str(alias or "").strip()
        normalized = self._normalize_alias(alias)
        normalized_scene_type, normalized_scene_value = self._normalize_scene_scope(scene_type, scene_value)
        if (
            not qq_id
            or not alias
            or not normalized
            or normalized_scene_type not in {self.SCENE_GROUP, self.SCENE_PRIVATE}
            or (normalized_scene_type == self.SCENE_GROUP and not normalized_scene_value)
        ):
            return False

        now = self._now_str()
        conn.execute(
            """
            INSERT INTO user_scene_aliases (
                qq_id, alias, alias_normalized, scene_type, scene_value, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(qq_id, scene_type, scene_value, alias_normalized) DO UPDATE SET
                alias = excluded.alias,
                updated_at = excluded.updated_at
            """,
            (
                qq_id,
                alias,
                normalized,
                normalized_scene_type,
                normalized_scene_value,
                created_at or now,
                updated_at or now,
            ),
        )
        return True

    def _fetch_aliases(
        self,
        conn: sqlite3.Connection,
        qq_id: str,
        alias_type: str,
    ) -> list[str]:
        rows = conn.execute(
            """
            SELECT alias
            FROM user_aliases
            WHERE qq_id = ? AND alias_type = ?
            ORDER BY updated_at DESC, id DESC
            """,
            (qq_id, alias_type),
        ).fetchall()

        aliases: list[str] = []
        seen: set[str] = set()
        for row in rows:
            alias = str(row["alias"] or "").strip()
            normalized = self._normalize_alias(alias)
            if alias and normalized and normalized not in seen:
                aliases.append(alias)
                seen.add(normalized)
        return aliases

    def _fetch_scene_aliases(
        self,
        conn: sqlite3.Connection,
        qq_id: str,
        *,
        scene_type: str | None = None,
        scene_value: str | None = None,
    ) -> list[dict[str, Any]]:
        cleaned_id = str(qq_id or "").strip()
        if not cleaned_id:
            return []

        query = [
            "SELECT id, qq_id, alias, alias_normalized, scene_type, scene_value, created_at, updated_at",
            "FROM user_scene_aliases",
            "WHERE qq_id = ?",
        ]
        params: list[Any] = [cleaned_id]
        if scene_type is not None:
            normalized_type, normalized_value = self._normalize_scene_scope(scene_type, scene_value)
            query.append("AND scene_type = ?")
            params.append(normalized_type)
            if normalized_type == self.SCENE_GROUP:
                query.append("AND scene_value = ?")
                params.append(normalized_value)
        query.append("ORDER BY updated_at DESC, id DESC")

        rows = conn.execute(" ".join(query), tuple(params)).fetchall()
        results: list[dict[str, Any]] = []
        seen: set[tuple[str, str, str]] = set()
        for row in rows:
            alias = str(row["alias"] or "").strip()
            normalized_alias = self._normalize_alias(alias)
            row_scene_type = str(row["scene_type"] or "").strip()
            row_scene_value = str(row["scene_value"] or "").strip()
            dedupe_key = (row_scene_type, row_scene_value, normalized_alias)
            if not alias or not normalized_alias or dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            item = dict(row)
            item["alias"] = alias
            item["scene_type"] = row_scene_type
            item["scene_value"] = row_scene_value
            item["scope_label"] = self._scope_label(row_scene_type, row_scene_value)
            results.append(item)
        return results

    def _pick_scene_alias_for_event(
        self,
        entry: dict[str, Any],
        event: AstrMessageEvent | None,
    ) -> dict[str, Any] | None:
        scene_type, scene_value = self._event_scene_scope(event)
        if scene_type == self.SCENE_GLOBAL:
            return None
        for item in entry.get("scoped_aliases") or []:
            if (
                str(item.get("scene_type") or "").strip() == scene_type
                and str(item.get("scene_value") or "").strip() == scene_value
            ):
                return item
        return None

    def _build_entry(self, conn: sqlite3.Connection, user_row: sqlite3.Row) -> dict[str, Any]:
        qq_id = str(user_row["qq_id"] or "").strip()
        memory_aliases = self._fetch_aliases(conn, qq_id, self.ALIAS_TYPE_MEMORY)
        platform_aliases = self._fetch_aliases(conn, qq_id, self.ALIAS_TYPE_PLATFORM)
        scoped_aliases = self._fetch_scene_aliases(conn, qq_id)
        return {
            "qq_id": qq_id,
            "memory_name": memory_aliases[0] if memory_aliases else "",
            "memory_aliases": memory_aliases,
            "scoped_aliases": scoped_aliases,
            "note": str(user_row["note"] or "").strip(),
            "platform_name": str(user_row["platform_name"] or "").strip(),
            "seen_names": platform_aliases,
            "created_at": str(user_row["created_at"] or "").strip(),
            "updated_at": str(user_row["updated_at"] or "").strip(),
            "last_seen_at": str(user_row["last_seen_at"] or "").strip(),
        }

    def _list_entries(self, include_observed_only: bool) -> list[dict[str, Any]]:
        with self._get_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM users ORDER BY updated_at DESC, qq_id ASC"
            ).fetchall()

            results: list[dict[str, Any]] = []
            for row in rows:
                entry = self._build_entry(conn, row)
                has_manual_memory = bool(entry["memory_aliases"] or entry.get("scoped_aliases") or entry["note"])
                if include_observed_only or has_manual_memory:
                    results.append(entry)
            return results

    def _maybe_migrate_from_json(self) -> None:
        with self._get_conn() as conn:
            if self._get_meta(conn, "legacy_json_migrated") == "1":
                return

            if not self._legacy_json_path.exists():
                self._set_meta(conn, "legacy_json_migrated", "1")
                conn.commit()
                return

            has_existing_data = conn.execute(
                "SELECT 1 FROM users LIMIT 1"
            ).fetchone() is not None
            if has_existing_data:
                self._set_meta(conn, "legacy_json_migrated", "1")
                conn.commit()
                return

            try:
                with open(self._legacy_json_path, "r", encoding="utf-8") as f:
                    payload = json.load(f)
            except Exception as exc:
                logger.warning("加载旧版 JSON 记忆失败: %s", exc)
                self._set_meta(conn, "legacy_json_migrated", "1")
                conn.commit()
                return

            users = payload.get("users", {}) if isinstance(payload, dict) else {}
            if not isinstance(users, dict):
                self._set_meta(conn, "legacy_json_migrated", "1")
                conn.commit()
                return

            migrated_count = 0
            for raw_qq_id, raw_entry in users.items():
                qq_id = str(raw_qq_id or "").strip()
                entry = raw_entry if isinstance(raw_entry, dict) else {}
                if not qq_id:
                    continue

                created_at = str(entry.get("created_at") or self._now_str()).strip()
                updated_at = str(entry.get("updated_at") or created_at or self._now_str()).strip()
                last_seen_at = str(entry.get("last_seen_at") or "").strip()
                note = str(entry.get("note") or "").strip()
                platform_name = str(entry.get("platform_name") or "").strip()

                self._ensure_user(
                    conn,
                    qq_id,
                    created_at=created_at,
                    updated_at=updated_at,
                    last_seen_at=last_seen_at,
                    note=note,
                    platform_name=platform_name,
                )

                memory_name = str(entry.get("memory_name") or "").strip()
                if memory_name:
                    self._upsert_alias(
                        conn,
                        qq_id,
                        memory_name,
                        self.ALIAS_TYPE_MEMORY,
                        created_at=created_at,
                        updated_at=updated_at,
                    )

                if platform_name:
                    self._upsert_alias(
                        conn,
                        qq_id,
                        platform_name,
                        self.ALIAS_TYPE_PLATFORM,
                        created_at=created_at,
                        updated_at=updated_at,
                    )

                for seen_name in entry.get("seen_names") or []:
                    if not isinstance(seen_name, str):
                        continue
                    self._upsert_alias(
                        conn,
                        qq_id,
                        seen_name,
                        self.ALIAS_TYPE_PLATFORM,
                        created_at=created_at,
                        updated_at=updated_at,
                    )

                migrated_count += 1

            self._set_meta(conn, "legacy_json_migrated", "1")
            conn.commit()
            if migrated_count:
                self._invalidate_related_memory_cache()
                logger.info(
                    "[astrbot_all_char] 已从旧版 JSON 迁移 %s 条用户记忆到 SQLite",
                    migrated_count,
                )

    def observe_user(self, event: AstrMessageEvent) -> str:
        """记录当前用户的 QQ 号和平台昵称"""

        qq_id = self._safe_sender_id(event)
        if not qq_id:
            return ""

        sender_name = self._safe_sender_name(event)
        now_tick = time.monotonic()
        if self._should_skip_observe_write(qq_id, sender_name, now_tick):
            return qq_id

        now = self._now_str()
        with self._get_conn() as conn:
            self._ensure_user(conn, qq_id, created_at=now, updated_at=now, last_seen_at=now)

            if sender_name:
                conn.execute(
                    """
                    UPDATE users
                    SET platform_name = ?, last_seen_at = ?, updated_at = ?
                    WHERE qq_id = ?
                    """,
                    (sender_name, now, now, qq_id),
                )
                self._upsert_alias(
                    conn,
                    qq_id,
                    sender_name,
                    self.ALIAS_TYPE_PLATFORM,
                    updated_at=now,
                )
            else:
                conn.execute(
                    """
                    UPDATE users
                    SET last_seen_at = ?, updated_at = ?
                    WHERE qq_id = ?
                    """,
                    (now, now, qq_id),
                )
            conn.commit()

        self._invalidate_related_memory_cache()
        self._touch_observe_cache(qq_id, sender_name, now_tick)
        return qq_id

    def set_memory(
        self,
        qq_id: str,
        memory_name: str | None = None,
        note: str | None = None,
        scene_type: str | None = None,
        scene_value: str | None = None,
    ) -> dict[str, Any]:
        qq_id = str(qq_id or "").strip()
        if not qq_id:
            return {}

        now = self._now_str()
        normalized_scene_type, normalized_scene_value = self._normalize_scene_scope(scene_type, scene_value)
        with self._get_conn() as conn:
            self._ensure_user(conn, qq_id, created_at=now, updated_at=now)

            if memory_name is not None:
                if normalized_scene_type in {self.SCENE_GROUP, self.SCENE_PRIVATE}:
                    self._upsert_scene_alias(
                        conn,
                        qq_id,
                        str(memory_name).strip(),
                        normalized_scene_type,
                        normalized_scene_value,
                        updated_at=now,
                    )
                else:
                    self._upsert_alias(
                        conn,
                        qq_id,
                        str(memory_name).strip(),
                        self.ALIAS_TYPE_MEMORY,
                        updated_at=now,
                    )

            if note is not None:
                conn.execute(
                    "UPDATE users SET note = ?, updated_at = ? WHERE qq_id = ?",
                    (str(note).strip(), now, qq_id),
                )
            else:
                conn.execute(
                    "UPDATE users SET updated_at = ? WHERE qq_id = ?",
                    (now, qq_id),
                )

            conn.commit()
            self._invalidate_related_memory_cache()
            row = conn.execute("SELECT * FROM users WHERE qq_id = ?", (qq_id,)).fetchone()
            return self._build_entry(conn, row) if row else {}

    def delete_alias(
        self,
        qq_id: str,
        alias: str,
        *,
        scene_type: str | None = None,
        scene_value: str | None = None,
    ) -> bool:
        qq_id = str(qq_id or "").strip()
        normalized = self._normalize_alias(alias)
        if not qq_id or not normalized:
            return False

        normalized_scene_type, normalized_scene_value = self._normalize_scene_scope(scene_type, scene_value)
        with self._get_conn() as conn:
            if normalized_scene_type in {self.SCENE_GROUP, self.SCENE_PRIVATE}:
                cursor = conn.execute(
                    """
                    DELETE FROM user_scene_aliases
                    WHERE qq_id = ? AND scene_type = ? AND scene_value = ? AND alias_normalized = ?
                    """,
                    (qq_id, normalized_scene_type, normalized_scene_value, normalized),
                )
            else:
                cursor = conn.execute(
                    """
                    DELETE FROM user_aliases
                    WHERE qq_id = ? AND alias_type = ? AND alias_normalized = ?
                    """,
                    (qq_id, self.ALIAS_TYPE_MEMORY, normalized),
                )
            deleted = cursor.rowcount > 0
            if deleted:
                conn.execute(
                    "UPDATE users SET updated_at = ? WHERE qq_id = ?",
                    (self._now_str(), qq_id),
                )
                conn.commit()
                self._invalidate_related_memory_cache()
            return deleted

    def update_scene_alias(
        self,
        scene_alias_id: int,
        *,
        alias: str,
        scene_type: str,
        scene_value: str = "",
    ) -> dict[str, Any] | None:
        cleaned_alias = str(alias or "").strip()
        normalized_scene_type, normalized_scene_value = self._normalize_scene_scope(scene_type, scene_value)
        if (
            not cleaned_alias
            or normalized_scene_type not in {self.SCENE_GROUP, self.SCENE_PRIVATE}
            or (normalized_scene_type == self.SCENE_GROUP and not normalized_scene_value)
        ):
            return None

        with self._get_conn() as conn:
            row = conn.execute(
                "SELECT qq_id, created_at FROM user_scene_aliases WHERE id = ?",
                (scene_alias_id,),
            ).fetchone()
            if not row:
                return None
            qq_id = str(row["qq_id"] or "").strip()
            created_at = str(row["created_at"] or self._now_str())
            conn.execute("DELETE FROM user_scene_aliases WHERE id = ?", (scene_alias_id,))
            self._upsert_scene_alias(
                conn,
                qq_id,
                cleaned_alias,
                normalized_scene_type,
                normalized_scene_value,
                created_at=created_at,
                updated_at=self._now_str(),
            )
            conn.execute(
                "UPDATE users SET updated_at = ? WHERE qq_id = ?",
                (self._now_str(), qq_id),
            )
            conn.commit()
            self._invalidate_related_memory_cache()
            normalized_alias = self._normalize_alias(cleaned_alias)
            refreshed = conn.execute(
                """
                SELECT id, qq_id, alias, alias_normalized, scene_type, scene_value, created_at, updated_at
                FROM user_scene_aliases
                WHERE qq_id = ? AND scene_type = ? AND scene_value = ? AND alias_normalized = ?
                """,
                (qq_id, normalized_scene_type, normalized_scene_value, normalized_alias),
            ).fetchone()
            if not refreshed:
                return None
            item = dict(refreshed)
            item["scope_label"] = self._scope_label(normalized_scene_type, normalized_scene_value)
            return item

    def delete_scene_alias(self, scene_alias_id: int) -> tuple[bool, str]:
        with self._get_conn() as conn:
            row = conn.execute(
                "SELECT qq_id FROM user_scene_aliases WHERE id = ?",
                (scene_alias_id,),
            ).fetchone()
            if not row:
                return False, ""
            qq_id = str(row["qq_id"] or "").strip()
            cursor = conn.execute("DELETE FROM user_scene_aliases WHERE id = ?", (scene_alias_id,))
            deleted = cursor.rowcount > 0
            if deleted:
                conn.execute(
                    "UPDATE users SET updated_at = ? WHERE qq_id = ?",
                    (self._now_str(), qq_id),
                )
                conn.commit()
                self._invalidate_related_memory_cache()
            return deleted, qq_id

    def get_memory(self, qq_id: str) -> dict[str, Any] | None:
        qq_id = str(qq_id or "").strip()
        if not qq_id:
            return None

        with self._get_conn() as conn:
            row = conn.execute("SELECT * FROM users WHERE qq_id = ?", (qq_id,)).fetchone()
            if not row:
                return None
            return self._build_entry(conn, row)

    def delete_memory(self, qq_id: str) -> bool:
        qq_id = str(qq_id or "").strip()
        if not qq_id:
            return False

        with self._get_conn() as conn:
            cursor = conn.execute("DELETE FROM users WHERE qq_id = ?", (qq_id,))
            deleted = cursor.rowcount > 0
            if deleted:
                conn.commit()
                self._invalidate_related_memory_cache()
            return deleted

    def list_memories(self) -> list[dict[str, Any]]:
        return self._list_entries(include_observed_only=False)

    def list_all_memories(self) -> list[dict[str, Any]]:
        return self._list_entries(include_observed_only=True)

    def update_user_profile(
        self,
        qq_id: str,
        *,
        note: str | None = None,
        platform_name: str | None = None,
    ) -> dict[str, Any]:
        qq_id = str(qq_id or "").strip()
        if not qq_id:
            return {}

        now = self._now_str()
        invalidate_related_cache = bool(note is not None or platform_name is not None)
        with self._get_conn() as conn:
            self._ensure_user(conn, qq_id, created_at=now, updated_at=now)

            updates: list[str] = ["updated_at = ?"]
            params: list[Any] = [now]
            if note is not None:
                updates.append("note = ?")
                params.append(str(note).strip())
            if platform_name is not None:
                cleaned_name = str(platform_name).strip()
                updates.append("platform_name = ?")
                params.append(cleaned_name)
                if cleaned_name:
                    self._upsert_alias(
                        conn,
                        qq_id,
                        cleaned_name,
                        self.ALIAS_TYPE_PLATFORM,
                        updated_at=now,
                    )

            params.append(qq_id)
            conn.execute(
                f"UPDATE users SET {', '.join(updates)} WHERE qq_id = ?",
                tuple(params),
            )
            conn.commit()
            if invalidate_related_cache:
                self._invalidate_related_memory_cache()
            row = conn.execute("SELECT * FROM users WHERE qq_id = ?", (qq_id,)).fetchone()
            return self._build_entry(conn, row) if row else {}

    def search_memories(
        self,
        keyword: str = "",
        *,
        limit: int = 100,
        include_observed_only: bool = True,
    ) -> list[dict[str, Any]]:
        records = self._list_entries(include_observed_only=include_observed_only)
        normalized_keyword = self._normalize_alias(keyword)
        if not normalized_keyword:
            return records[:limit]

        matched: list[tuple[int, dict[str, Any]]] = []
        for entry in records:
            qq_id = str(entry.get("qq_id") or "").strip()
            if not qq_id:
                continue

            best_score = 0
            for candidate, weight in self._entry_match_candidates(qq_id, entry):
                normalized_candidate = self._normalize_alias(candidate)
                if not normalized_candidate or normalized_keyword not in normalized_candidate:
                    continue
                best_score = max(best_score, weight + min(len(normalized_candidate), 12))

            if best_score <= 0:
                continue
            matched.append((best_score, entry))

        matched.sort(
            key=lambda pair: (
                -pair[0],
                pair[1].get("updated_at", ""),
                pair[1].get("qq_id", ""),
            )
        )
        return [entry for _, entry in matched[:limit]]

    def format_memory(self, qq_id: str, event: AstrMessageEvent | None = None) -> str:
        entry = self.get_memory(qq_id)
        if not entry:
            return f"未找到 QQ {qq_id} 的永久记忆"

        memory_aliases = entry.get("memory_aliases") or []
        scoped_aliases = entry.get("scoped_aliases") or []
        platform_aliases = entry.get("seen_names") or []
        current_scene_alias = self._pick_scene_alias_for_event(entry, event)
        lines = [
            f"🧠 用户记忆：{qq_id}",
            f"记忆别名：{'、'.join(memory_aliases) if memory_aliases else '未设置'}",
            f"当前平台昵称：{entry.get('platform_name') or '--'}",
            f"备注：{entry.get('note') or '--'}",
            f"最近出现时间：{entry.get('last_seen_at') or '--'}",
        ]
        if current_scene_alias:
            lines.append(
                f"当前场景称呼：{current_scene_alias.get('alias') or '--'}（{current_scene_alias.get('scope_label') or '当前场景'}）"
            )
        if scoped_aliases:
            scene_text = "；".join(
                f"{item.get('scope_label') or '场景'} = {item.get('alias') or '--'}"
                for item in scoped_aliases[:8]
            )
            lines.append(f"场景别名：{scene_text}")
        if platform_aliases:
            lines.append(f"历史平台昵称：{', '.join(platform_aliases[:8])}")
        if not memory_aliases and not scoped_aliases:
            lines.append("状态：当前只有自动识别信息，尚未录入管理员长期记忆")
        return "\n".join(lines)

    @staticmethod
    def _clean_match_value(value: Any) -> str:
        return str(value or "").strip()

    def _entry_match_candidates(self, qq_id: str, entry: dict[str, Any]) -> list[tuple[str, int]]:
        candidates: list[tuple[str, int]] = []

        def push(value: Any, weight: int) -> None:
            text = self._clean_match_value(value)
            if not text:
                return
            if text.isdigit():
                if len(text) < 4:
                    return
            elif len(text) < 2:
                return
            candidates.append((text, weight))

        push(qq_id, 120)
        for alias in entry.get("memory_aliases") or []:
            push(alias, 110)
        for scoped in entry.get("scoped_aliases") or []:
            push(scoped.get("alias"), 108)
        push(entry.get("platform_name"), 95)
        push(entry.get("note"), 70)
        for seen_name in entry.get("seen_names") or []:
            push(seen_name, 85)
        return candidates

    def search_related_memories(
        self,
        text: str,
        exclude_qq_ids: set[str] | None = None,
        limit: int = 3,
    ) -> list[dict[str, Any]]:
        raw_text = str(text or "").strip()
        if not raw_text:
            return []

        normalized_text = re.sub(r"\s+", "", raw_text).lower()
        if not normalized_text:
            return []

        excluded = {
            str(item).strip()
            for item in (exclude_qq_ids or set())
            if str(item).strip()
        }
        entries_by_qq, inverted_index, prefix_index, cache_hit = self._get_related_memory_cache()
        matched_candidates = self._extract_matching_candidates(normalized_text, prefix_index)
        if not matched_candidates:
            logger.debug(
                "[astrbot_all_char][memory] 相关人物检索: cache_hit=%s, text_len=%s, matched_candidates=0",
                cache_hit,
                len(normalized_text),
            )
            return []

        score_by_qq: dict[str, int] = {}
        for candidate in matched_candidates:
            for qq_id, score in inverted_index.get(candidate, []):
                if qq_id in excluded:
                    continue
                old_score = score_by_qq.get(qq_id, 0)
                if score > old_score:
                    score_by_qq[qq_id] = score

        matched: list[tuple[int, dict[str, Any]]] = []
        for qq_id, score in score_by_qq.items():
            entry = entries_by_qq.get(qq_id)
            if entry:
                matched.append((score, entry))
        matched.sort(
            key=lambda pair: (
                -pair[0],
                pair[1].get("updated_at", ""),
                pair[1].get("qq_id", ""),
            )
        )
        result = [item for _, item in matched[:limit]]
        logger.debug(
            (
                "[astrbot_all_char][memory] 相关人物检索: cache_hit=%s, text_len=%s, "
                "matched_candidates=%s, matched_users=%s"
            ),
            cache_hit,
            len(normalized_text),
            len(matched_candidates),
            len(result),
        )
        return result

    def build_related_memories_prompt(
        self,
        text: str,
        exclude_qq_ids: set[str] | None = None,
        limit: int = 3,
    ) -> str:
        records = self.search_related_memories(text, exclude_qq_ids=exclude_qq_ids, limit=limit)
        if not records:
            return ""

        lines = [
            "【当前消息提到的相关人物记忆】",
            "以下信息仅在用户明显提到对应对象时再参考：",
        ]
        for index, item in enumerate(records, start=1):
            aliases = item.get("memory_aliases") or []
            lines.append(f"{index}. QQ: {item.get('qq_id') or '--'}")
            lines.append(f"   - 记忆别名: {'、'.join(aliases) if aliases else '未设置'}")
            lines.append(f"   - 当前平台昵称: {item.get('platform_name') or '未知'}")
            if item.get("note"):
                lines.append(f"   - 备注: {item.get('note')}")
            seen_names = item.get("seen_names") or []
            if seen_names:
                lines.append(f"   - 历史平台昵称: {', '.join(seen_names[:5])}")

        lines.extend(
            [
                "使用规则：",
                "1. 仅在用户这条消息明显提到相关对象时再引用这些记忆",
                "2. 如果记忆中的称呼和用户当下表达冲突，以用户当下表达为准",
                "3. 若不能确定用户指的是谁，不要把记忆强行套上去",
            ]
        )
        return "\n".join(lines)

    def build_prompt_for_event(self, event: AstrMessageEvent) -> str:
        """为当前对话用户构建注入给 LLM 的永久记忆提示"""

        qq_id = self._safe_sender_id(event)
        if not qq_id:
            return ""

        entry = self.get_memory(qq_id)
        sender_name = self._safe_sender_name(event)
        if not entry:
            if not sender_name:
                return ""
            return (
                "【用户识别信息】\n"
                f"- 当前对话用户 QQ: {qq_id}\n"
                f"- 当前平台昵称: {sender_name}\n"
                "说明：这是你当前正在对话的用户信息，但暂未设置管理员录入的永久记忆"
            )

        memory_aliases = entry.get("memory_aliases") or []
        scoped_alias = self._pick_scene_alias_for_event(entry, event)
        scoped_aliases = entry.get("scoped_aliases") or []
        platform_name = entry.get("platform_name") or sender_name
        note = entry.get("note") or ""
        seen_names = entry.get("seen_names") or []

        if not memory_aliases and not note and not scoped_aliases:
            if not platform_name:
                return ""
            return (
                "【用户识别信息】\n"
                f"- 当前对话用户 QQ: {qq_id}\n"
                f"- 当前平台昵称: {platform_name}\n"
                "说明：这是你当前正在对话的用户信息，系统已自动识别 QQ 与平台昵称，但管理员尚未录入长期人物记忆"
            )

        lines = [
            "【当前用户永久记忆】",
            f"- 当前对话用户 QQ: {qq_id}",
            f"- 当前平台昵称: {platform_name or '未知'}",
        ]
        if scoped_alias:
            lines.append(f"- 当前场景称呼: {scoped_alias.get('alias') or '未设置'}（{scoped_alias.get('scope_label') or '当前场景'}）")
        lines.append(f"- 全局记忆别名: {'、'.join(memory_aliases) if memory_aliases else '未设置'}")
        if note:
            lines.append(f"- 备注: {note}")
        if scoped_aliases:
            scoped_text = "；".join(
                f"{item.get('scope_label') or '场景'} = {item.get('alias') or '--'}"
                for item in scoped_aliases[:5]
            )
            if scoped_text:
                lines.append(f"- 其他场景别名: {scoped_text}")
        if seen_names:
            lines.append(f"- 历史平台昵称: {', '.join(seen_names[:5])}")
        lines.extend(
            [
                "使用规则：",
                "1. 你可以据此知道当前对话的人是谁",
                "2. 如果群聊/私聊场景称呼与全局记忆不同，优先使用当前场景称呼",
                "3. 仅在合适的时候自然引用这些记忆，不要每次都生硬重复",
                "4. 不要编造未记录的身份、关系或经历",
            ]
        )
        return "\n".join(lines)


_memory_store: UserMemoryStore | None = None


def init_user_memory_store() -> UserMemoryStore:
    global _memory_store
    if _memory_store is None:
        _memory_store = UserMemoryStore()
    return _memory_store


def configure_memory_observe_user_throttle_seconds(raw_value: Any) -> int:
    store = init_user_memory_store()
    return store.set_observe_user_throttle_seconds(raw_value)


def configure_related_memory_cache_ttl_seconds(raw_value: Any) -> int:
    store = init_user_memory_store()
    return store.set_related_memory_cache_ttl_seconds(raw_value)


def _memory_permission_denied_text() -> str:
    if not get_memory_admin_qq_ids():
        return "永久记忆管理员尚未配置，请先在 `memory_admin_qq_ids` 中添加管理员 QQ"
    return f"永久记忆仅允许管理员使用当前允许的管理员 QQ：{get_memory_admin_display_text()}"


def _parse_memory_scene_spec(first_token: str, second_token: str = "") -> tuple[str, str, int] | None:
    token = str(first_token or "").strip()
    next_token = str(second_token or "").strip()
    if not token:
        return None
    if token in {"全局", "global"}:
        return UserMemoryStore.SCENE_GLOBAL, "", 1
    if token in {"私聊", "private"}:
        return UserMemoryStore.SCENE_PRIVATE, "", 1
    if token in {"群", "群聊", "group"} and next_token:
        scene_type, scene_value = UserMemoryStore._normalize_scene_scope(UserMemoryStore.SCENE_GROUP, next_token)
        if scene_value:
            return scene_type, scene_value, 2
    for prefix in ("群:", "群聊:", "group:"):
        if token.startswith(prefix):
            scene_type, scene_value = UserMemoryStore._normalize_scene_scope(
                UserMemoryStore.SCENE_GROUP,
                token.split(":", 1)[1],
            )
            if scene_value:
                return scene_type, scene_value, 1
    return None


def _extract_memory_scene_and_note(args: list[str], start_index: int) -> tuple[str, str, str]:
    scene_type = UserMemoryStore.SCENE_GLOBAL
    scene_value = ""
    note_start = start_index
    first = args[start_index] if len(args) > start_index else ""
    second = args[start_index + 1] if len(args) > start_index + 1 else ""
    parsed = _parse_memory_scene_spec(first, second)
    if parsed:
        scene_type, scene_value, consumed = parsed
        note_start += consumed
    note = " ".join(args[note_start:]).strip()
    return scene_type, scene_value, note


def _format_alias_add_result(
    entry: dict[str, Any],
    qq_id: str,
    alias: str,
    note: str,
    *,
    scene_type: str = UserMemoryStore.SCENE_GLOBAL,
    scene_value: str = "",
) -> str:
    aliases = entry.get("memory_aliases") or []
    lines = []
    if scene_type in {UserMemoryStore.SCENE_GROUP, UserMemoryStore.SCENE_PRIVATE}:
        lines.append(f"已为 QQ {qq_id} 新增场景称呼：{alias}")
        lines.append(f"生效范围：{UserMemoryStore._scope_label(scene_type, scene_value)}")
    else:
        lines.extend(
            [
                f"已为 QQ {qq_id} 新增记忆别名：{alias}",
                f"当前全局别名：{'、'.join(aliases) if aliases else alias}",
            ]
        )
    if note:
        lines.append(f"备注：{note}")
    return "\n".join(lines)


async def handle_memory_command(event: AstrMessageEvent):
    store = init_user_memory_store()
    store.observe_user(event)

    msg = store._safe_message_text(event)
    parts = msg.split()
    command_name = parts[0].lstrip("/").strip().lower() if parts else ""

    if command_name == "认人":
        if not store._is_admin_event(event):
            yield event.plain_result(_memory_permission_denied_text())
            return

        args = parts[1:]
        if len(args) < 2 or not str(args[0]).strip().isdigit():
            yield event.plain_result("用法：/认人 QQ号 别名 [全局|私聊|群:群号] [备注]")
            return

        qq_id = str(args[0]).strip()
        memory_name = args[1].strip()
        scene_type, scene_value, note = _extract_memory_scene_and_note(args, 2)
        if scene_type == UserMemoryStore.SCENE_GROUP and not scene_value:
            yield event.plain_result("群聊场景认人时必须填写群号，例如：/认人 123456 阿周 群:987654321 同学群里的叫法")
            return
        entry = store.set_memory(
            qq_id,
            memory_name=memory_name,
            note=note or None,
            scene_type=scene_type,
            scene_value=scene_value,
        )
        yield event.plain_result(
            _format_alias_add_result(
                entry,
                qq_id,
                memory_name,
                note,
                scene_type=scene_type,
                scene_value=scene_value,
            )
        )
        return

    if len(parts) < 2:
        async for result in _handle_memory_help(event):
            yield result
        return

    subcommand = parts[1].strip().lower()
    args = parts[2:]

    if subcommand in {"帮助", "help", "?"}:
        async for result in _handle_memory_help(event):
            yield result
        return

    if not store._is_admin_event(event):
        yield event.plain_result(_memory_permission_denied_text())
        return

    if subcommand in {"我是", "设置我"}:
        qq_id = store._safe_sender_id(event)
        if not qq_id:
            yield event.plain_result("无法识别当前 QQ 号，不能设置个人记忆")
            return
        if not args:
            yield event.plain_result("用法：/记忆 我是 别名 [全局|私聊|群:群号] [备注]")
            return
        memory_name = args[0].strip()
        scene_type, scene_value, note = _extract_memory_scene_and_note(args, 1)
        if scene_type == UserMemoryStore.SCENE_GROUP and not scene_value:
            yield event.plain_result("群聊场景认人时必须填写群号，例如：/记忆 我是 阿周 群:987654321 同学群里这样叫我")
            return
        entry = store.set_memory(
            qq_id,
            memory_name=memory_name,
            note=note or None,
            scene_type=scene_type,
            scene_value=scene_value,
        )
        yield event.plain_result(
            _format_alias_add_result(
                entry,
                qq_id,
                memory_name,
                note,
                scene_type=scene_type,
                scene_value=scene_value,
            )
        )
        return

    if subcommand in {"设置", "添加", "新增", "别名"}:
        if len(args) < 2 or not str(args[0]).strip().isdigit():
            yield event.plain_result("用法：/记忆 设置 QQ号 别名 [全局|私聊|群:群号] [备注]")
            return
        qq_id = str(args[0]).strip()
        memory_name = args[1].strip()
        scene_type, scene_value, note = _extract_memory_scene_and_note(args, 2)
        if scene_type == UserMemoryStore.SCENE_GROUP and not scene_value:
            yield event.plain_result("群聊场景认人时必须填写群号，例如：/记忆 设置 123456 阿周 群:987654321 同学群里的外号")
            return
        entry = store.set_memory(
            qq_id,
            memory_name=memory_name,
            note=note or None,
            scene_type=scene_type,
            scene_value=scene_value,
        )
        yield event.plain_result(
            _format_alias_add_result(
                entry,
                qq_id,
                memory_name,
                note,
                scene_type=scene_type,
                scene_value=scene_value,
            )
        )
        return

    if subcommand == "备注":
        if len(args) < 2 or not str(args[0]).strip().isdigit():
            yield event.plain_result("用法：/记忆 备注 QQ号 内容")
            return
        qq_id = str(args[0]).strip()
        note = " ".join(args[1:]).strip()
        store.set_memory(qq_id, note=note)
        yield event.plain_result(f"已更新 QQ {qq_id} 的备注：{note}")
        return

    if subcommand in {"查看", "查询"}:
        qq_id = str(args[0]).strip() if args else store._safe_sender_id(event)
        if not qq_id:
            yield event.plain_result("用法：/记忆 查看 QQ号")
            return
        yield event.plain_result(store.format_memory(qq_id, event if not args else None))
        return

    if subcommand in {"删除别名", "移除别名", "删别名"}:
        if len(args) < 2 or not str(args[0]).strip().isdigit():
            yield event.plain_result("用法：/记忆 删除别名 QQ号 别名 [全局|私聊|群:群号]")
            return
        qq_id = str(args[0]).strip()
        alias = str(args[1]).strip()
        scene_type, scene_value, _ = _extract_memory_scene_and_note(args, 2)
        if scene_type == UserMemoryStore.SCENE_GROUP and not scene_value:
            yield event.plain_result("删除群聊场景别名时必须填写群号，例如：/记忆 删除别名 123456 阿周 群:987654321")
            return
        deleted = store.delete_alias(
            qq_id,
            alias,
            scene_type=scene_type,
            scene_value=scene_value,
        )
        yield event.plain_result(
            f"已删除 QQ {qq_id} 的记忆别名：{alias}" if deleted else f"未找到 QQ {qq_id} 的记忆别名：{alias}"
        )
        return

    if subcommand in {"删除", "清除"}:
        qq_id = str(args[0]).strip() if args else store._safe_sender_id(event)
        if not qq_id:
            yield event.plain_result("用法：/记忆 删除 QQ号")
            return
        deleted = store.delete_memory(qq_id)
        yield event.plain_result(
            f"已删除 QQ {qq_id} 的永久记忆" if deleted else f"未找到 QQ {qq_id} 的永久记忆"
        )
        return

    if subcommand == "列表":
        records = store.list_memories()
        if not records:
            yield event.plain_result("当前还没有任何已录入的永久记忆")
            return

        lines = ["🧠 永久记忆列表："]
        for item in records[:50]:
            aliases = item.get("memory_aliases") or []
            alias_text = "、".join(aliases) if aliases else "--"
            lines.append(
                f"- QQ {item.get('qq_id', '--')} | 别名: {alias_text} | 昵称: {item.get('platform_name') or '--'}"
            )
        if len(records) > 50:
            lines.append(f"以上展示前 50 条，共 {len(records)} 条")
        yield event.plain_result("\n".join(lines))
        return

    async for result in _handle_memory_help(event):
        yield result


async def handle_who_am_i_command(event: AstrMessageEvent):
    store = init_user_memory_store()
    qq_id = store.observe_user(event)
    if not store._is_admin_event(event):
        yield event.plain_result(_memory_permission_denied_text())
        return
    if not qq_id:
        yield event.plain_result("无法识别当前 QQ 号")
        return
    yield event.plain_result(store.format_memory(qq_id, event))


async def _handle_memory_help(event: AstrMessageEvent):
    help_text = (
        "🧠 永久记忆说明（SQLite 版）\n\n"
        f"• 管理权限：仅管理员 QQ {get_memory_admin_display_text()} 可以使用 /记忆、/认人、/我是谁\n"
        "• 自动识别：普通聊天进入模型前，会自动按当前 QQ 识别人物记忆并注入\n"
        "• 多别名：同一个 QQ 可以录入多个全局别名，还可以额外记录群聊 / 私聊场景称呼\n"
        "• 存储方式：记忆已改为 SQLite 持久化，并会自动兼容旧版 JSON 数据\n\n"
        "常用命令：\n"
        "• /认人 QQ号 别名 [全局|私聊|群:群号] [备注] - 快速新增全局或场景别名\n"
        "• /记忆 设置 QQ号 别名 [全局|私聊|群:群号] [备注] - 为指定 QQ 新增记忆别名\n"
        "• /记忆 备注 QQ号 内容 - 更新指定 QQ 的备注\n"
        "• /记忆 删除别名 QQ号 别名 [全局|私聊|群:群号] - 删除指定别名\n"
        "• /记忆 查看 [QQ号] - 查看某人的永久记忆，不填默认查看自己\n"
        "• /记忆 删除 [QQ号] - 删除某个 QQ 的整条永久记忆\n"
        "• /记忆 列表 - 查看当前已录入的永久记忆\n"
        "• /我是谁 - 查看 bot 当前记住了你的什么信息（管理员专用）"
    )
    yield event.plain_result(help_text)
