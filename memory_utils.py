from __future__ import annotations

import json
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
from astrbot.api.star import StarTools


ADMIN_QQ_ID = "1102025067"


class UserMemoryStore:
    """基于 SQLite 的用户永久记忆。"""

    DB_NAME = "user_memory.sqlite3"
    LEGACY_JSON_NAME = "user_memory.json"
    ALIAS_TYPE_MEMORY = "memory"
    ALIAS_TYPE_PLATFORM = "platform"

    def __init__(self) -> None:
        self._data_dir = Path(StarTools.get_data_dir("astrbot_all_char"))
        self._data_dir.mkdir(parents=True, exist_ok=True)
        self._db_path = self._data_dir / self.DB_NAME
        self._legacy_json_path = self._data_dir / self.LEGACY_JSON_NAME
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
                "CREATE INDEX IF NOT EXISTS idx_users_updated_at ON users(updated_at DESC)"
            )
            conn.commit()

    @staticmethod
    def _now_str() -> str:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

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
    def _is_admin_event(event: AstrMessageEvent) -> bool:
        return UserMemoryStore._safe_sender_id(event) == ADMIN_QQ_ID

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

    def _build_entry(self, conn: sqlite3.Connection, user_row: sqlite3.Row) -> dict[str, Any]:
        qq_id = str(user_row["qq_id"] or "").strip()
        memory_aliases = self._fetch_aliases(conn, qq_id, self.ALIAS_TYPE_MEMORY)
        platform_aliases = self._fetch_aliases(conn, qq_id, self.ALIAS_TYPE_PLATFORM)
        return {
            "qq_id": qq_id,
            "memory_name": memory_aliases[0] if memory_aliases else "",
            "memory_aliases": memory_aliases,
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
                has_manual_memory = bool(entry["memory_aliases"] or entry["note"])
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
                logger.info(
                    "[astrbot_all_char] 已从旧版 JSON 迁移 %s 条用户记忆到 SQLite",
                    migrated_count,
                )

    def observe_user(self, event: AstrMessageEvent) -> str:
        """记录当前用户的 QQ 号和平台昵称。"""

        qq_id = self._safe_sender_id(event)
        if not qq_id:
            return ""

        sender_name = self._safe_sender_name(event)
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

        return qq_id

    def set_memory(
        self,
        qq_id: str,
        memory_name: str | None = None,
        note: str | None = None,
    ) -> dict[str, Any]:
        qq_id = str(qq_id or "").strip()
        if not qq_id:
            return {}

        now = self._now_str()
        with self._get_conn() as conn:
            self._ensure_user(conn, qq_id, created_at=now, updated_at=now)

            if memory_name is not None:
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
            row = conn.execute("SELECT * FROM users WHERE qq_id = ?", (qq_id,)).fetchone()
            return self._build_entry(conn, row) if row else {}

    def delete_alias(self, qq_id: str, alias: str) -> bool:
        qq_id = str(qq_id or "").strip()
        normalized = self._normalize_alias(alias)
        if not qq_id or not normalized:
            return False

        with self._get_conn() as conn:
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
            return deleted

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
            return deleted

    def list_memories(self) -> list[dict[str, Any]]:
        return self._list_entries(include_observed_only=False)

    def format_memory(self, qq_id: str) -> str:
        entry = self.get_memory(qq_id)
        if not entry:
            return f"未找到 QQ {qq_id} 的永久记忆。"

        memory_aliases = entry.get("memory_aliases") or []
        platform_aliases = entry.get("seen_names") or []
        lines = [
            f"🧠 用户记忆：{qq_id}",
            f"记忆别名：{'、'.join(memory_aliases) if memory_aliases else '未设置'}",
            f"当前平台昵称：{entry.get('platform_name') or '--'}",
            f"备注：{entry.get('note') or '--'}",
            f"最近出现时间：{entry.get('last_seen_at') or '--'}",
        ]
        if platform_aliases:
            lines.append(f"历史平台昵称：{', '.join(platform_aliases[:8])}")
        if not memory_aliases:
            lines.append("状态：当前只有自动识别信息，尚未录入管理员长期记忆。")
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
        matched: list[tuple[int, dict[str, Any]]] = []

        for entry in self.list_memories():
            qq_id = str(entry.get("qq_id") or "").strip()
            if not qq_id or qq_id in excluded:
                continue

            best_score = 0
            for candidate, weight in self._entry_match_candidates(qq_id, entry):
                normalized_candidate = self._normalize_alias(candidate)
                if normalized_candidate and normalized_candidate in normalized_text:
                    best_score = max(
                        best_score,
                        weight + min(len(normalized_candidate), 12),
                    )

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
        return [item for _, item in matched[:limit]]

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
                "1. 仅在用户这条消息明显提到相关对象时再引用这些记忆。",
                "2. 如果记忆中的称呼和用户当下表达冲突，以用户当下表达为准。",
                "3. 若不能确定用户指的是谁，不要把记忆强行套上去。",
            ]
        )
        return "\n".join(lines)

    def build_prompt_for_event(self, event: AstrMessageEvent) -> str:
        """为当前对话用户构建注入给 LLM 的永久记忆提示。"""

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
                "说明：这是你当前正在对话的用户信息，但暂未设置管理员录入的永久记忆。"
            )

        memory_aliases = entry.get("memory_aliases") or []
        platform_name = entry.get("platform_name") or sender_name
        note = entry.get("note") or ""
        seen_names = entry.get("seen_names") or []

        if not memory_aliases and not note:
            if not platform_name:
                return ""
            return (
                "【用户识别信息】\n"
                f"- 当前对话用户 QQ: {qq_id}\n"
                f"- 当前平台昵称: {platform_name}\n"
                "说明：这是你当前正在对话的用户信息，系统已自动识别 QQ 与平台昵称，但管理员尚未录入长期人物记忆。"
            )

        lines = [
            "【当前用户永久记忆】",
            f"- 当前对话用户 QQ: {qq_id}",
            f"- 记忆别名: {'、'.join(memory_aliases) if memory_aliases else '未设置'}",
            f"- 当前平台昵称: {platform_name or '未知'}",
        ]
        if note:
            lines.append(f"- 备注: {note}")
        if seen_names:
            lines.append(f"- 历史平台昵称: {', '.join(seen_names[:5])}")
        lines.extend(
            [
                "使用规则：",
                "1. 你可以据此知道当前对话的人是谁。",
                "2. 如果用户当下自称与记忆别名不同，以用户当下表达为准。",
                "3. 仅在合适的时候自然引用这些记忆，不要每次都生硬重复。",
                "4. 不要编造未记录的身份、关系或经历。",
            ]
        )
        return "\n".join(lines)


_memory_store: UserMemoryStore | None = None


def init_user_memory_store() -> UserMemoryStore:
    global _memory_store
    if _memory_store is None:
        _memory_store = UserMemoryStore()
    return _memory_store


def _memory_permission_denied_text() -> str:
    return f"永久记忆仅允许管理员使用。当前仅 QQ {ADMIN_QQ_ID} 可以管理记忆。"


def _format_alias_add_result(entry: dict[str, Any], qq_id: str, alias: str, note: str) -> str:
    aliases = entry.get("memory_aliases") or []
    lines = [
        f"已为 QQ {qq_id} 新增记忆别名：{alias}",
        f"当前别名：{'、'.join(aliases) if aliases else alias}",
    ]
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
            yield event.plain_result("用法：/认人 QQ号 别名 [备注]")
            return

        qq_id = str(args[0]).strip()
        memory_name = args[1].strip()
        note = " ".join(args[2:]).strip()
        entry = store.set_memory(qq_id, memory_name=memory_name, note=note or None)
        yield event.plain_result(_format_alias_add_result(entry, qq_id, memory_name, note))
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
            yield event.plain_result("无法识别当前 QQ 号，不能设置个人记忆。")
            return
        if not args:
            yield event.plain_result("用法：/记忆 我是 别名 [备注]")
            return
        memory_name = args[0].strip()
        note = " ".join(args[1:]).strip()
        entry = store.set_memory(qq_id, memory_name=memory_name, note=note or None)
        yield event.plain_result(_format_alias_add_result(entry, qq_id, memory_name, note))
        return

    if subcommand in {"设置", "添加", "新增", "别名"}:
        if len(args) < 2 or not str(args[0]).strip().isdigit():
            yield event.plain_result("用法：/记忆 设置 QQ号 别名 [备注]")
            return
        qq_id = str(args[0]).strip()
        memory_name = args[1].strip()
        note = " ".join(args[2:]).strip()
        entry = store.set_memory(qq_id, memory_name=memory_name, note=note or None)
        yield event.plain_result(_format_alias_add_result(entry, qq_id, memory_name, note))
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
        yield event.plain_result(store.format_memory(qq_id))
        return

    if subcommand in {"删除别名", "移除别名", "删别名"}:
        if len(args) < 2 or not str(args[0]).strip().isdigit():
            yield event.plain_result("用法：/记忆 删除别名 QQ号 别名")
            return
        qq_id = str(args[0]).strip()
        alias = " ".join(args[1:]).strip()
        deleted = store.delete_alias(qq_id, alias)
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
            f"已删除 QQ {qq_id} 的永久记忆。" if deleted else f"未找到 QQ {qq_id} 的永久记忆。"
        )
        return

    if subcommand == "列表":
        records = store.list_memories()
        if not records:
            yield event.plain_result("当前还没有任何已录入的永久记忆。")
            return

        lines = ["🧠 永久记忆列表："]
        for item in records[:50]:
            aliases = item.get("memory_aliases") or []
            alias_text = "、".join(aliases) if aliases else "--"
            lines.append(
                f"- QQ {item.get('qq_id', '--')} | 别名: {alias_text} | 昵称: {item.get('platform_name') or '--'}"
            )
        if len(records) > 50:
            lines.append(f"以上展示前 50 条，共 {len(records)} 条。")
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
        yield event.plain_result("无法识别当前 QQ 号。")
        return
    yield event.plain_result(store.format_memory(qq_id))


async def _handle_memory_help(event: AstrMessageEvent):
    help_text = (
        "🧠 永久记忆说明（SQLite 版）\n\n"
        f"• 管理权限：仅管理员 QQ {ADMIN_QQ_ID} 可以使用 /记忆、/认人、/我是谁\n"
        "• 自动识别：普通聊天进入模型前，会自动按当前 QQ 识别人物记忆并注入\n"
        "• 多别名：同一个 QQ 可以录入多个记忆别名，适合同学名、外号、昵称并存\n"
        "• 存储方式：记忆已改为 SQLite 持久化，并会自动兼容旧版 JSON 数据\n\n"
        "常用命令：\n"
        "• /认人 QQ号 别名 [备注] - 快速给某个 QQ 新增一个记忆别名\n"
        "• /记忆 设置 QQ号 别名 [备注] - 为指定 QQ 新增记忆别名\n"
        "• /记忆 备注 QQ号 内容 - 更新指定 QQ 的备注\n"
        "• /记忆 删除别名 QQ号 别名 - 删除指定 QQ 的某个别名\n"
        "• /记忆 查看 [QQ号] - 查看某人的永久记忆，不填默认查看自己\n"
        "• /记忆 删除 [QQ号] - 删除某个 QQ 的整条永久记忆\n"
        "• /记忆 列表 - 查看当前已录入的永久记忆\n"
        "• /我是谁 - 查看 bot 当前记住了你的什么信息（管理员专用）"
    )
    yield event.plain_result(help_text)
