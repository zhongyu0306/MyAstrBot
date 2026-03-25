from __future__ import annotations

import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
from astrbot.api.star import StarTools

from .memory_utils import UserMemoryStore, init_user_memory_store

_GENERIC_VALUES = {
    "",
    "你",
    "我",
    "他",
    "她",
    "它",
    "我们",
    "你们",
    "他们",
    "这个",
    "那个",
    "事情",
    "东西",
    "一个",
    "以后",
    "现在",
    "今天",
    "昨天",
    "最近",
}
_TEMPORAL_WORDS = (
    "今天早上",
    "今天下午",
    "今天",
    "今晚",
    "昨晚",
    "昨天",
    "前天",
    "刚刚",
    "刚才",
    "最近",
    "上周",
    "上个月",
    "周末",
)
_RELATION_HINTS = (
    "朋友",
    "同学",
    "同事",
    "室友",
    "对象",
    "男朋友",
    "女朋友",
    "老婆",
    "老公",
    "家人",
    "姐姐",
    "妹妹",
    "哥哥",
    "弟弟",
    "闺蜜",
    "发小",
    "老板",
    "导师",
    "老师",
    "客户",
    "搭子",
    "邻居",
    "亲戚",
)
_EVENT_VERBS = (
    "去了",
    "去",
    "看了",
    "看",
    "吃了",
    "吃",
    "买了",
    "买",
    "做了",
    "做",
    "见了",
    "见",
    "聊了",
    "聊",
    "收到",
    "参加",
    "完成",
    "开始",
    "结束",
    "搬家",
    "出差",
    "上班",
    "下班",
    "请假",
    "旅游",
    "旅行",
    "约会",
    "表白",
    "分手",
)
_RECALL_HINTS = (
    "还记得",
    "记不记得",
    "上次",
    "之前",
    "后来",
    "那次",
    "那天",
    "当时",
)
_TOKEN_PATTERN = re.compile(r"[\u4e00-\u9fffA-Za-z0-9][\u4e00-\u9fffA-Za-z0-9._-]{1,15}")
_HABIT_LABELS = {
    ("weather", "city"): "天气里常查 {value}",
    ("stock", "stock_code"): "股票里常看 {value}",
    ("stock", "reminder_time"): "股票提醒常设在 {value}",
    ("fund", "fund_code"): "基金里常看 {value}",
    ("fund", "default_fund_code"): "默认基金偏向 {value}",
    ("email", "recipient"): "邮件常发给 {value}",
    ("reminder", "reminder_time"): "提醒常设在 {value}",
    ("chat", "routine"): "聊天里常提到的习惯: {value}",
}


class PassiveMemoryStore:
    DB_NAME = UserMemoryStore.DB_NAME

    def __init__(self) -> None:
        init_user_memory_store()
        self._data_dir = Path(StarTools.get_data_dir("astrbot_all_char"))
        self._data_dir.mkdir(parents=True, exist_ok=True)
        self._db_path = self._data_dir / self.DB_NAME
        self._init_db()

    @contextmanager
    def _get_conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
        finally:
            conn.close()

    @staticmethod
    def _now_str() -> str:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    @staticmethod
    def _normalize_value(value: Any) -> str:
        text = str(value or "").strip()
        if not text:
            return ""
        return re.sub(r"\s+", "", text).lower()

    @staticmethod
    def _trim_clause(text: Any, max_len: int = 24) -> str:
        raw = re.split(r"[。！？!?；;\n]", str(text or "").strip(), maxsplit=1)[0]
        raw = raw.strip(" \t\"'“”‘’[]【】()（）<>《》")
        raw = re.sub(r"(就行|就好|即可|好了|吧|呀|啦|哈)$", "", raw).strip()
        raw = re.sub(r"^(就是|只是|应该是|大概是)", "", raw).strip()
        raw = re.sub(r"\s+", " ", raw)
        if not raw or len(raw) > max_len:
            return ""
        return raw

    @staticmethod
    def _get_message_text(event: AstrMessageEvent) -> str:
        return UserMemoryStore._safe_message_text(event)

    @staticmethod
    def _get_sender_id(event: AstrMessageEvent) -> str:
        return UserMemoryStore._safe_sender_id(event)

    @staticmethod
    def _get_sender_name(event: AstrMessageEvent) -> str:
        return UserMemoryStore._safe_sender_name(event)

    @staticmethod
    def _split_sentences(text: str) -> list[str]:
        parts = re.split(r"[。！？!?；;\n]+", str(text or "").strip())
        return [part.strip() for part in parts if part.strip()]

    @staticmethod
    def _tokenize_text(text: str) -> list[str]:
        tokens: list[str] = []
        seen: set[str] = set()
        for token in _TOKEN_PATTERN.findall(str(text or "")):
            cleaned = token.strip(".,!?;:，。！？；：")
            normalized = PassiveMemoryStore._normalize_value(cleaned)
            if not cleaned or len(cleaned) < 2 or normalized in seen or normalized in _GENERIC_VALUES:
                continue
            tokens.append(cleaned)
            seen.add(normalized)
        return tokens

    @staticmethod
    def _pack_keywords(keywords: list[str]) -> str:
        normalized: list[str] = []
        seen: set[str] = set()
        for item in keywords:
            cleaned = PassiveMemoryStore._trim_clause(item, max_len=16)
            key = PassiveMemoryStore._normalize_value(cleaned)
            if not cleaned or len(cleaned) < 2 or key in seen or key in _GENERIC_VALUES:
                continue
            normalized.append(cleaned)
            seen.add(key)
        return "|".join(normalized[:10])

    @staticmethod
    def _unpack_keywords(payload: str) -> list[str]:
        return [item.strip() for item in str(payload or "").split("|") if item.strip()]

    @staticmethod
    def _relation_supported(relation_text: str) -> bool:
        relation = str(relation_text or "").strip()
        return any(hint in relation for hint in _RELATION_HINTS)

    @staticmethod
    def _is_recall_query(text: str) -> bool:
        message = str(text or "")
        return any(hint in message for hint in _RECALL_HINTS)

    def _split_phrase_candidates(self, text: str, limit: int = 4) -> list[str]:
        clause = re.split(r"[。！？!?；;\n]", str(text or "").strip(), maxsplit=1)[0]
        parts = re.split(r"(?:、|/|和|跟|以及|还有|并且|,|，)", clause)
        values: list[str] = []
        seen: set[str] = set()
        for part in parts:
            cleaned = self._trim_clause(part)
            normalized = self._normalize_value(cleaned)
            if not cleaned or normalized in seen or normalized in _GENERIC_VALUES or len(normalized) < 2:
                continue
            values.append(cleaned)
            seen.add(normalized)
            if len(values) >= limit:
                break
        return values

    def _ensure_base_user(self, qq_id: str) -> None:
        cleaned_id = str(qq_id or "").strip()
        if cleaned_id:
            init_user_memory_store().update_user_profile(cleaned_id)

    def _init_db(self) -> None:
        with self._get_conn() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS user_preferences (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    qq_id TEXT NOT NULL,
                    preference_type TEXT NOT NULL,
                    value TEXT NOT NULL,
                    normalized_value TEXT NOT NULL,
                    confidence REAL NOT NULL DEFAULT 0.6,
                    evidence_count INTEGER NOT NULL DEFAULT 1,
                    last_confirmed_at TEXT NOT NULL DEFAULT '',
                    source_text TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE (qq_id, preference_type, normalized_value),
                    FOREIGN KEY (qq_id) REFERENCES users(qq_id) ON DELETE CASCADE
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS relation_edges (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    qq_id TEXT NOT NULL,
                    target_name TEXT NOT NULL,
                    target_normalized TEXT NOT NULL,
                    relation_type TEXT NOT NULL,
                    note TEXT NOT NULL DEFAULT '',
                    confidence REAL NOT NULL DEFAULT 0.7,
                    evidence_count INTEGER NOT NULL DEFAULT 1,
                    last_confirmed_at TEXT NOT NULL DEFAULT '',
                    source_text TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE (qq_id, target_normalized, relation_type),
                    FOREIGN KEY (qq_id) REFERENCES users(qq_id) ON DELETE CASCADE
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS user_habits (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    qq_id TEXT NOT NULL,
                    module_name TEXT NOT NULL,
                    habit_key TEXT NOT NULL,
                    habit_value TEXT NOT NULL,
                    normalized_value TEXT NOT NULL,
                    use_count INTEGER NOT NULL DEFAULT 1,
                    source_text TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_used_at TEXT NOT NULL,
                    UNIQUE (qq_id, module_name, habit_key, normalized_value),
                    FOREIGN KEY (qq_id) REFERENCES users(qq_id) ON DELETE CASCADE
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS passive_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    qq_id TEXT NOT NULL,
                    event_date_label TEXT NOT NULL DEFAULT '',
                    summary TEXT NOT NULL,
                    normalized_summary TEXT NOT NULL,
                    keywords TEXT NOT NULL DEFAULT '',
                    confidence REAL NOT NULL DEFAULT 0.68,
                    evidence_count INTEGER NOT NULL DEFAULT 1,
                    source_text TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_confirmed_at TEXT NOT NULL DEFAULT '',
                    UNIQUE (qq_id, event_date_label, normalized_summary),
                    FOREIGN KEY (qq_id) REFERENCES users(qq_id) ON DELETE CASCADE
                )
                """
            )
            self._ensure_column(conn, "user_preferences", "evidence_count", "INTEGER NOT NULL DEFAULT 1")
            self._ensure_column(conn, "user_preferences", "last_confirmed_at", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(conn, "relation_edges", "evidence_count", "INTEGER NOT NULL DEFAULT 1")
            self._ensure_column(conn, "relation_edges", "last_confirmed_at", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(conn, "relation_edges", "target_qq_id", "TEXT NOT NULL DEFAULT ''")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_user_preferences_lookup "
                "ON user_preferences(qq_id, preference_type, confidence DESC, evidence_count DESC, updated_at DESC)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_relation_edges_lookup "
                "ON relation_edges(qq_id, confidence DESC, evidence_count DESC, updated_at DESC)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_user_habits_lookup "
                "ON user_habits(qq_id, module_name, habit_key, use_count DESC, updated_at DESC)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_passive_events_lookup "
                "ON passive_events(qq_id, updated_at DESC, confidence DESC, evidence_count DESC)"
            )
            conn.commit()

    def _ensure_column(self, conn: sqlite3.Connection, table_name: str, column_name: str, definition: str) -> None:
        rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
        existing = {str(row['name']) for row in rows}
        if column_name not in existing:
            conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {definition}")

    def _looks_like_name(self, value: str) -> bool:
        normalized = self._normalize_value(value)
        if not value or normalized in _GENERIC_VALUES or normalized.isdigit():
            return False
        if len(value) < 2 or len(value) > 12:
            return False
        if any(token in value for token in ("喜欢", "讨厌", "朋友", "同事", "同学", "提醒")):
            return False
        return True

    def _looks_like_preference_value(self, value: str) -> bool:
        normalized = self._normalize_value(value)
        if not value or normalized in _GENERIC_VALUES:
            return False
        if len(value) < 2 or len(value) > 24:
            return False
        if value.startswith(("叫我", "喊我", "我是", "我叫")):
            return False
        return True

    def _looks_like_event_clause(self, text: str) -> bool:
        clause = self._trim_clause(text, max_len=48)
        if not clause or len(clause) < 6:
            return False
        if clause.startswith(("我喜欢", "我不喜欢", "我讨厌", "我叫", "你可以叫我")):
            return False
        if "我" not in clause and "我们" not in clause:
            return False
        has_time = any(word in clause for word in _TEMPORAL_WORDS)
        has_action = any(verb in clause for verb in _EVENT_VERBS) or "了" in clause
        return has_time and has_action

    def _event_date_from_text(self, text: str) -> str:
        for word in sorted(_TEMPORAL_WORDS, key=len, reverse=True):
            if word in text:
                return word
        return ""

    def _extract_preferred_names(self, text: str) -> list[str]:
        patterns = [
            r"(?:你可以|也可以|以后|平时|直接)?(?:叫我|喊我|称呼我)(?P<value>[^，。！？\n]{1,12})",
            r"(?:朋友|大家|别人)都叫我(?P<value>[^，。！？\n]{1,12})",
            r"我叫(?P<value>[^，。！？\n]{1,12})",
            r"我的名字是(?P<value>[^，。！？\n]{1,12})",
        ]
        names: list[str] = []
        seen: set[str] = set()
        for pattern in patterns:
            for match in re.finditer(pattern, text):
                name = self._trim_clause(match.group("value"), max_len=12)
                normalized = self._normalize_value(name)
                if not self._looks_like_name(name) or normalized in seen:
                    continue
                names.append(name)
                seen.add(normalized)
        return names[:3]

    def _extract_preferences(self, text: str, *, positive: bool) -> list[str]:
        patterns = (
            [
                r"我(?:很|最|还挺|比较|一直都|真的)?喜欢(?P<value>[^。！？\n]{1,30})",
                r"我(?:很|特别|超)?爱(?P<value>[^。！？\n]{1,30})",
                r"我偏爱(?P<value>[^。！？\n]{1,30})",
                r"我(?:平时|通常)?爱吃(?P<value>[^。！？\n]{1,30})",
                r"我最近迷上(?P<value>[^。！？\n]{1,30})",
            ]
            if positive
            else [
                r"我不喜欢(?P<value>[^。！？\n]{1,30})",
                r"我讨厌(?P<value>[^。！？\n]{1,30})",
                r"我不爱(?P<value>[^。！？\n]{1,30})",
                r"我受不了(?P<value>[^。！？\n]{1,30})",
                r"我最烦(?P<value>[^。！？\n]{1,30})",
                r"我不吃(?P<value>[^。！？\n]{1,30})",
            ]
        )
        values: list[str] = []
        seen: set[str] = set()
        for pattern in patterns:
            for match in re.finditer(pattern, text):
                for item in self._split_phrase_candidates(match.group("value")):
                    normalized = self._normalize_value(item)
                    if normalized in seen or not self._looks_like_preference_value(item):
                        continue
                    values.append(item)
                    seen.add(normalized)
        return values[:6]

    def _extract_relations(self, text: str) -> list[tuple[str, str]]:
        relations: list[tuple[str, str]] = []
        seen: set[tuple[str, str]] = set()
        for pattern in [
            r"(?P<target>[\w\u4e00-\u9fff·]{2,16})是我(?:的)?(?P<relation>[^\s，。！？\n]{1,12})",
            r"我是(?P<target>[\w\u4e00-\u9fff·]{2,16})的(?P<relation>[^\s，。！？\n]{1,12})",
            r"我和(?P<target>[\w\u4e00-\u9fff·]{2,16})是(?P<relation>[^\s，。！？\n]{1,12})",
            r"(?P<target>[\w\u4e00-\u9fff·]{2,16})跟我是(?P<relation>[^\s，。！？\n]{1,12})",
        ]:
            for match in re.finditer(pattern, text):
                target = self._trim_clause(match.group("target"), max_len=16)
                relation = self._trim_clause(match.group("relation"), max_len=12)
                normalized_target = self._normalize_value(target)
                if not target or not relation or normalized_target in _GENERIC_VALUES or not self._relation_supported(relation):
                    continue
                key = (normalized_target, relation)
                if key in seen:
                    continue
                relations.append((target, relation))
                seen.add(key)
        return relations[:6]

    def _extract_events(self, text: str) -> list[dict[str, str]]:
        events: list[dict[str, str]] = []
        seen: set[tuple[str, str]] = set()
        for sentence in self._split_sentences(text):
            if not self._looks_like_event_clause(sentence):
                continue
            summary = self._trim_clause(sentence, max_len=48)
            if not summary:
                continue
            date_label = self._event_date_from_text(summary)
            key = (date_label, self._normalize_value(summary))
            if key in seen:
                continue
            events.append(
                {
                    "event_date_label": date_label,
                    "summary": summary,
                    "keywords": self._pack_keywords(self._tokenize_text(summary)),
                }
            )
            seen.add(key)
            if len(events) >= 4:
                break
        return events

    def _extract_chat_routines(self, text: str) -> list[str]:
        routines: list[str] = []
        seen: set[str] = set()
        for pattern in [
            r"我(?:平时|一般|通常)会(?P<value>[^。！？\n]{4,24})",
            r"我习惯(?P<value>[^。！？\n]{4,24})",
        ]:
            for match in re.finditer(pattern, text):
                value = self._trim_clause(match.group("value"), max_len=24)
                normalized = self._normalize_value(value)
                if not value or len(value) < 4 or normalized in seen:
                    continue
                routines.append(value)
                seen.add(normalized)
        return routines[:2]

    def _dampen_opposite_preference(
        self,
        conn: sqlite3.Connection,
        qq_id: str,
        preference_type: str,
        normalized_value: str,
        now: str,
    ) -> None:
        if preference_type not in {"like", "dislike"}:
            return
        opposite = "dislike" if preference_type == "like" else "like"
        conn.execute(
            """
            UPDATE user_preferences
            SET confidence = MAX(0.18, confidence - 0.18),
                updated_at = ?
            WHERE qq_id = ? AND preference_type = ? AND normalized_value = ?
            """,
            (now, qq_id, opposite, normalized_value),
        )

    def _upsert_preference(
        self,
        qq_id: str,
        preference_type: str,
        value: str,
        *,
        confidence: float,
        source_text: str,
    ) -> bool:
        cleaned_value = self._trim_clause(value)
        normalized = self._normalize_value(cleaned_value)
        cleaned_id = str(qq_id or "").strip()
        if not cleaned_id or not self._looks_like_preference_value(cleaned_value):
            return False
        self._ensure_base_user(cleaned_id)
        now = self._now_str()
        with self._get_conn() as conn:
            self._dampen_opposite_preference(conn, cleaned_id, preference_type, normalized, now)
            conn.execute(
                """
                INSERT INTO user_preferences (
                    qq_id, preference_type, value, normalized_value, confidence, evidence_count,
                    last_confirmed_at, source_text, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, ?)
                ON CONFLICT(qq_id, preference_type, normalized_value) DO UPDATE SET
                    value = excluded.value,
                    confidence = MIN(0.98, MAX(user_preferences.confidence, excluded.confidence) + 0.04),
                    evidence_count = user_preferences.evidence_count + 1,
                    last_confirmed_at = excluded.last_confirmed_at,
                    source_text = CASE
                        WHEN excluded.source_text <> '' THEN excluded.source_text
                        ELSE user_preferences.source_text
                    END,
                    updated_at = excluded.updated_at
                """,
                (cleaned_id, preference_type, cleaned_value, normalized, confidence, now, source_text, now, now),
            )
            conn.commit()
        return True

    def _upsert_relation(
        self,
        qq_id: str,
        target_name: str,
        relation_type: str,
        *,
        target_qq_id: str = "",
        source_text: str,
        confidence: float,
        note: str = "",
    ) -> bool:
        cleaned_id = str(qq_id or "").strip()
        cleaned_target_qq_id = str(target_qq_id or "").strip()
        cleaned_target = self._trim_clause(target_name, max_len=18)
        if cleaned_target_qq_id and not cleaned_target:
            target_entry = init_user_memory_store().get_memory(cleaned_target_qq_id) or {}
            aliases = target_entry.get("memory_aliases") or []
            cleaned_target = (
                (aliases[0] if aliases else "")
                or str(target_entry.get("platform_name") or "").strip()
                or cleaned_target_qq_id
            )
            cleaned_target = self._trim_clause(cleaned_target, max_len=18)
        cleaned_relation = self._trim_clause(relation_type, max_len=14)
        target_normalized = self._normalize_value(cleaned_target)
        if (
            not cleaned_id
            or not cleaned_target
            or target_normalized in _GENERIC_VALUES
            or not cleaned_relation
            or not self._relation_supported(cleaned_relation)
        ):
            return False
        self._ensure_base_user(cleaned_id)
        now = self._now_str()
        with self._get_conn() as conn:
            conn.execute(
                """
                INSERT INTO relation_edges (
                    qq_id, target_name, target_normalized, relation_type, target_qq_id, note, confidence,
                    evidence_count, last_confirmed_at, source_text, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?)
                ON CONFLICT(qq_id, target_normalized, relation_type) DO UPDATE SET
                    target_name = excluded.target_name,
                    target_qq_id = excluded.target_qq_id,
                    note = CASE WHEN excluded.note <> '' THEN excluded.note ELSE relation_edges.note END,
                    confidence = MIN(0.99, MAX(relation_edges.confidence, excluded.confidence) + 0.04),
                    evidence_count = relation_edges.evidence_count + 1,
                    last_confirmed_at = excluded.last_confirmed_at,
                    source_text = CASE
                        WHEN excluded.source_text <> '' THEN excluded.source_text
                        ELSE relation_edges.source_text
                    END,
                    updated_at = excluded.updated_at
                """,
                (
                    cleaned_id,
                    cleaned_target,
                    target_normalized,
                    cleaned_relation,
                    cleaned_target_qq_id,
                    note.strip(),
                    confidence,
                    now,
                    source_text,
                    now,
                    now,
                ),
            )
            conn.commit()
        return True

    def _upsert_event(
        self,
        qq_id: str,
        summary: str,
        *,
        event_date_label: str = "",
        keywords: str = "",
        confidence: float,
        source_text: str,
    ) -> bool:
        cleaned_id = str(qq_id or "").strip()
        cleaned_summary = self._trim_clause(summary, max_len=48)
        normalized_summary = self._normalize_value(cleaned_summary)
        if not cleaned_id or not cleaned_summary or len(cleaned_summary) < 6:
            return False
        self._ensure_base_user(cleaned_id)
        now = self._now_str()
        packed_keywords = keywords or self._pack_keywords(self._tokenize_text(cleaned_summary))
        with self._get_conn() as conn:
            conn.execute(
                """
                INSERT INTO passive_events (
                    qq_id, event_date_label, summary, normalized_summary, keywords, confidence,
                    evidence_count, source_text, created_at, updated_at, last_confirmed_at
                )
                VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?)
                ON CONFLICT(qq_id, event_date_label, normalized_summary) DO UPDATE SET
                    summary = excluded.summary,
                    keywords = CASE
                        WHEN excluded.keywords <> '' THEN excluded.keywords
                        ELSE passive_events.keywords
                    END,
                    confidence = MIN(0.98, MAX(passive_events.confidence, excluded.confidence) + 0.03),
                    evidence_count = passive_events.evidence_count + 1,
                    source_text = CASE
                        WHEN excluded.source_text <> '' THEN excluded.source_text
                        ELSE passive_events.source_text
                    END,
                    updated_at = excluded.updated_at,
                    last_confirmed_at = excluded.last_confirmed_at
                """,
                (
                    cleaned_id,
                    str(event_date_label or "").strip(),
                    cleaned_summary,
                    normalized_summary,
                    packed_keywords,
                    confidence,
                    source_text,
                    now,
                    now,
                    now,
                ),
            )
            conn.commit()
        return True

    def record_habit(
        self,
        event: AstrMessageEvent,
        module_name: str,
        habit_key: str,
        habit_value: str,
        *,
        source_text: str = "",
    ) -> bool:
        qq_id = self._get_sender_id(event)
        value = str(habit_value or "").strip()
        normalized = self._normalize_value(value)
        if not qq_id or not module_name or not habit_key or not normalized:
            return False
        init_user_memory_store().observe_user(event)
        now = self._now_str()
        with self._get_conn() as conn:
            conn.execute(
                """
                INSERT INTO user_habits (
                    qq_id, module_name, habit_key, habit_value, normalized_value, use_count,
                    source_text, created_at, updated_at, last_used_at
                )
                VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, ?)
                ON CONFLICT(qq_id, module_name, habit_key, normalized_value) DO UPDATE SET
                    habit_value = excluded.habit_value,
                    use_count = user_habits.use_count + 1,
                    source_text = CASE
                        WHEN excluded.source_text <> '' THEN excluded.source_text
                        ELSE user_habits.source_text
                    END,
                    updated_at = excluded.updated_at,
                    last_used_at = excluded.last_used_at
                """,
                (qq_id, module_name, habit_key, value[:120], normalized, source_text, now, now, now),
            )
            conn.commit()
        return True

    def observe_message(self, event: AstrMessageEvent) -> None:
        qq_id = self._get_sender_id(event)
        message_text = self._get_message_text(event)
        if not qq_id or not message_text or message_text.startswith("/"):
            return

        init_user_memory_store().observe_user(event)
        for preferred_name in self._extract_preferred_names(message_text):
            self._upsert_preference(qq_id, "preferred_name", preferred_name, confidence=0.9, source_text=message_text)
        for like in self._extract_preferences(message_text, positive=True):
            self._upsert_preference(qq_id, "like", like, confidence=0.74, source_text=message_text)
        for dislike in self._extract_preferences(message_text, positive=False):
            self._upsert_preference(qq_id, "dislike", dislike, confidence=0.74, source_text=message_text)
        for target_name, relation_type in self._extract_relations(message_text):
            self._upsert_relation(qq_id, target_name, relation_type, source_text=message_text, confidence=0.8)
        for item in self._extract_events(message_text):
            self._upsert_event(
                qq_id,
                item["summary"],
                event_date_label=item.get("event_date_label", ""),
                keywords=item.get("keywords", ""),
                confidence=0.72,
                source_text=message_text,
            )
        for routine in self._extract_chat_routines(message_text):
            self.record_text_habit(qq_id, routine, source_text=message_text)

    def record_text_habit(self, qq_id: str, routine: str, *, source_text: str = "") -> bool:
        cleaned_id = str(qq_id or "").strip()
        cleaned_value = self._trim_clause(routine, max_len=24)
        normalized = self._normalize_value(cleaned_value)
        if not cleaned_id or not cleaned_value or len(cleaned_value) < 4:
            return False
        self._ensure_base_user(cleaned_id)
        now = self._now_str()
        with self._get_conn() as conn:
            conn.execute(
                """
                INSERT INTO user_habits (
                    qq_id, module_name, habit_key, habit_value, normalized_value, use_count,
                    source_text, created_at, updated_at, last_used_at
                )
                VALUES (?, 'chat', 'routine', ?, ?, 1, ?, ?, ?, ?)
                ON CONFLICT(qq_id, module_name, habit_key, normalized_value) DO UPDATE SET
                    habit_value = excluded.habit_value,
                    use_count = user_habits.use_count + 1,
                    source_text = CASE
                        WHEN excluded.source_text <> '' THEN excluded.source_text
                        ELSE user_habits.source_text
                    END,
                    updated_at = excluded.updated_at,
                    last_used_at = excluded.last_used_at
                """,
                (cleaned_id, cleaned_value, normalized, source_text, now, now, now),
            )
            conn.commit()
        return True

    def _get_preferences(
        self,
        qq_id: str,
        *,
        preference_type: str | None = None,
        limit: int = 6,
        min_confidence: float = 0.0,
    ) -> list[dict[str, Any]]:
        cleaned_id = str(qq_id or "").strip()
        if not cleaned_id:
            return []
        query = "SELECT * FROM user_preferences WHERE qq_id = ? "
        params: list[Any] = [cleaned_id]
        if preference_type:
            query += "AND preference_type = ? "
            params.append(preference_type)
        if min_confidence > 0:
            query += "AND confidence >= ? "
            params.append(min_confidence)
        query += "ORDER BY confidence DESC, evidence_count DESC, updated_at DESC LIMIT ?"
        params.append(limit)
        with self._get_conn() as conn:
            return [dict(row) for row in conn.execute(query, tuple(params)).fetchall()]

    def _get_relations(self, qq_id: str, limit: int = 6, min_confidence: float = 0.0) -> list[dict[str, Any]]:
        cleaned_id = str(qq_id or "").strip()
        if not cleaned_id:
            return []
        query = "SELECT * FROM relation_edges WHERE qq_id = ? "
        params: list[Any] = [cleaned_id]
        if min_confidence > 0:
            query += "AND confidence >= ? "
            params.append(min_confidence)
        query += "ORDER BY confidence DESC, evidence_count DESC, updated_at DESC LIMIT ?"
        params.append(limit)
        with self._get_conn() as conn:
            rows = conn.execute(query, tuple(params)).fetchall()
            return [dict(row) for row in rows]

    def _get_habits(self, qq_id: str, limit: int = 8) -> list[dict[str, Any]]:
        cleaned_id = str(qq_id or "").strip()
        if not cleaned_id:
            return []
        with self._get_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM user_habits WHERE qq_id = ? ORDER BY use_count DESC, updated_at DESC LIMIT ?",
                (cleaned_id, limit),
            ).fetchall()
            return [dict(row) for row in rows]

    def _get_events(self, qq_id: str, limit: int = 8, min_confidence: float = 0.0) -> list[dict[str, Any]]:
        cleaned_id = str(qq_id or "").strip()
        if not cleaned_id:
            return []
        query = "SELECT * FROM passive_events WHERE qq_id = ? "
        params: list[Any] = [cleaned_id]
        if min_confidence > 0:
            query += "AND confidence >= ? "
            params.append(min_confidence)
        query += "ORDER BY updated_at DESC, confidence DESC, evidence_count DESC LIMIT ?"
        params.append(limit)
        with self._get_conn() as conn:
            rows = conn.execute(query, tuple(params)).fetchall()
            return [dict(row) for row in rows]

    def _search_relation_targets(self, qq_id: str, text: str, limit: int = 3) -> list[dict[str, Any]]:
        cleaned_id = str(qq_id or "").strip()
        normalized_text = self._normalize_value(text)
        if not cleaned_id or not normalized_text:
            return []
        with self._get_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM relation_edges WHERE qq_id = ? ORDER BY confidence DESC, evidence_count DESC, updated_at DESC",
                (cleaned_id,),
            ).fetchall()
        results: list[dict[str, Any]] = []
        seen: set[str] = set()
        for row in rows:
            item = dict(row)
            normalized_target = str(item.get("target_normalized") or "")
            if not normalized_target or normalized_target in seen:
                continue
            if normalized_target in normalized_text:
                results.append(item)
                seen.add(normalized_target)
                if len(results) >= limit:
                    break
        return results

    def search_relevant_events(self, qq_id: str, text: str, limit: int = 3) -> list[dict[str, Any]]:
        cleaned_id = str(qq_id or "").strip()
        message = str(text or "").strip()
        if not cleaned_id or not message:
            return []

        normalized_text = self._normalize_value(message)
        recall_mode = self._is_recall_query(message)
        message_tokens = {self._normalize_value(token): token for token in self._tokenize_text(message)}
        candidates = self._get_events(cleaned_id, limit=80, min_confidence=0.45)

        scored: list[tuple[float, dict[str, Any]]] = []
        for item in candidates:
            keywords = self._unpack_keywords(item.get("keywords") or "")
            normalized_keywords = {self._normalize_value(keyword) for keyword in keywords}
            overlap = [key for key in normalized_keywords if key and key in message_tokens]
            score = 0.0
            if overlap:
                score += len(overlap) * 18
            if item.get("normalized_summary") and str(item["normalized_summary"]) in normalized_text:
                score += 22
            if recall_mode and overlap:
                score += 8
            if item.get("event_date_label") and str(item["event_date_label"]) in message:
                score += 6
            score += float(item.get("confidence") or 0) * 10
            score += min(int(item.get("evidence_count") or 1), 5)
            if score < 14:
                continue
            enriched = dict(item)
            enriched["match_score"] = round(score, 2)
            scored.append((score, enriched))

        scored.sort(key=lambda pair: (-pair[0], pair[1].get("updated_at", "")))
        return [item for _, item in scored[:limit]]

    def list_preferences(self, qq_id: str, *, preference_type: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        return self._get_preferences(qq_id, preference_type=preference_type, limit=limit)

    def list_relations(self, qq_id: str, *, limit: int = 100) -> list[dict[str, Any]]:
        return self._get_relations(qq_id, limit=limit)

    def list_habits(self, qq_id: str, *, limit: int = 100) -> list[dict[str, Any]]:
        return self._get_habits(qq_id, limit=limit)

    def list_events(self, qq_id: str, *, limit: int = 100) -> list[dict[str, Any]]:
        return self._get_events(qq_id, limit=limit)

    def get_dashboard_stats(self) -> dict[str, int]:
        with self._get_conn() as conn:
            stats = {}
            for table_name, key in (
                ("user_preferences", "preferences"),
                ("relation_edges", "relations"),
                ("user_habits", "habits"),
                ("passive_events", "events"),
            ):
                row = conn.execute(f"SELECT COUNT(*) AS total FROM {table_name}").fetchone()
                stats[key] = int(row["total"] or 0) if row else 0
            return stats

    def get_counts_by_user(self) -> dict[str, dict[str, int]]:
        counts: dict[str, dict[str, int]] = {}
        with self._get_conn() as conn:
            for table_name, key in (
                ("user_preferences", "preferences"),
                ("relation_edges", "relations"),
                ("user_habits", "habits"),
                ("passive_events", "events"),
            ):
                rows = conn.execute(
                    f"SELECT qq_id, COUNT(*) AS total FROM {table_name} GROUP BY qq_id"
                ).fetchall()
                for row in rows:
                    qq_id = str(row["qq_id"] or "").strip()
                    if not qq_id:
                        continue
                    counts.setdefault(qq_id, {"preferences": 0, "relations": 0, "habits": 0, "events": 0})
                    counts[qq_id][key] = int(row["total"] or 0)
        return counts

    def save_preference(
        self,
        qq_id: str,
        preference_type: str,
        value: str,
        *,
        confidence: float = 0.82,
        source_text: str = "memory_panel",
    ) -> dict[str, Any] | None:
        if not self._upsert_preference(qq_id, preference_type, value, confidence=confidence, source_text=source_text):
            return None
        normalized = self._normalize_value(self._trim_clause(value))
        with self._get_conn() as conn:
            row = conn.execute(
                """
                SELECT * FROM user_preferences
                WHERE qq_id = ? AND preference_type = ? AND normalized_value = ?
                """,
                (str(qq_id).strip(), preference_type, normalized),
            ).fetchone()
            return dict(row) if row else None

    def update_preference(
        self,
        preference_id: int,
        *,
        value: str,
        preference_type: str,
        confidence: float,
        source_text: str,
    ) -> dict[str, Any] | None:
        with self._get_conn() as conn:
            row = conn.execute("SELECT * FROM user_preferences WHERE id = ?", (preference_id,)).fetchone()
            if not row:
                return None
            qq_id = str(row["qq_id"] or "").strip()
        if not self.delete_preference(preference_id):
            return None
        return self.save_preference(
            qq_id,
            preference_type,
            value,
            confidence=confidence,
            source_text=source_text or "memory_panel",
        )

    def delete_preference(self, preference_id: int) -> bool:
        with self._get_conn() as conn:
            cursor = conn.execute("DELETE FROM user_preferences WHERE id = ?", (preference_id,))
            deleted = cursor.rowcount > 0
            if deleted:
                conn.commit()
            return deleted

    def save_relation(
        self,
        qq_id: str,
        target_name: str,
        relation_type: str,
        *,
        target_qq_id: str = "",
        note: str = "",
        confidence: float = 0.84,
        source_text: str = "memory_panel",
    ) -> dict[str, Any] | None:
        if not self._upsert_relation(
            qq_id,
            target_name,
            relation_type,
            target_qq_id=target_qq_id,
            source_text=source_text,
            confidence=confidence,
            note=note,
        ):
            return None
        normalized = self._normalize_value(self._trim_clause(target_name, max_len=18))
        with self._get_conn() as conn:
            row = conn.execute(
                """
                SELECT * FROM relation_edges
                WHERE qq_id = ? AND target_normalized = ? AND relation_type = ?
                """,
                (str(qq_id).strip(), normalized, self._trim_clause(relation_type, max_len=14)),
            ).fetchone()
            return dict(row) if row else None

    def update_relation(
        self,
        relation_id: int,
        *,
        target_name: str,
        relation_type: str,
        target_qq_id: str,
        note: str,
        confidence: float,
        source_text: str,
    ) -> dict[str, Any] | None:
        with self._get_conn() as conn:
            row = conn.execute("SELECT * FROM relation_edges WHERE id = ?", (relation_id,)).fetchone()
            if not row:
                return None
            qq_id = str(row["qq_id"] or "").strip()
        if not self.delete_relation(relation_id):
            return None
        return self.save_relation(
            qq_id,
            target_name,
            relation_type,
            target_qq_id=target_qq_id,
            note=note,
            confidence=confidence,
            source_text=source_text or "memory_panel",
        )

    def delete_relation(self, relation_id: int) -> bool:
        with self._get_conn() as conn:
            cursor = conn.execute("DELETE FROM relation_edges WHERE id = ?", (relation_id,))
            deleted = cursor.rowcount > 0
            if deleted:
                conn.commit()
            return deleted

    def save_habit(
        self,
        qq_id: str,
        module_name: str,
        habit_key: str,
        habit_value: str,
        *,
        source_text: str = "memory_panel",
    ) -> dict[str, Any] | None:
        cleaned_id = str(qq_id or "").strip()
        cleaned_value = str(habit_value or "").strip()
        normalized = self._normalize_value(cleaned_value)
        if not cleaned_id or not module_name or not habit_key or not normalized:
            return None
        self._ensure_base_user(cleaned_id)
        now = self._now_str()
        with self._get_conn() as conn:
            conn.execute(
                """
                INSERT INTO user_habits (
                    qq_id, module_name, habit_key, habit_value, normalized_value, use_count,
                    source_text, created_at, updated_at, last_used_at
                )
                VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, ?)
                ON CONFLICT(qq_id, module_name, habit_key, normalized_value) DO UPDATE SET
                    habit_value = excluded.habit_value,
                    source_text = CASE
                        WHEN excluded.source_text <> '' THEN excluded.source_text
                        ELSE user_habits.source_text
                    END,
                    updated_at = excluded.updated_at,
                    last_used_at = excluded.last_used_at
                """,
                (cleaned_id, module_name, habit_key, cleaned_value[:120], normalized, source_text, now, now, now),
            )
            conn.commit()
            row = conn.execute(
                """
                SELECT * FROM user_habits
                WHERE qq_id = ? AND module_name = ? AND habit_key = ? AND normalized_value = ?
                """,
                (cleaned_id, module_name, habit_key, normalized),
            ).fetchone()
            return dict(row) if row else None

    def update_habit(
        self,
        habit_id: int,
        *,
        module_name: str,
        habit_key: str,
        habit_value: str,
        source_text: str,
    ) -> dict[str, Any] | None:
        with self._get_conn() as conn:
            row = conn.execute("SELECT * FROM user_habits WHERE id = ?", (habit_id,)).fetchone()
            if not row:
                return None
            qq_id = str(row["qq_id"] or "").strip()
            use_count = int(row["use_count"] or 1)
            created_at = str(row["created_at"] or self._now_str())

            cleaned_value = str(habit_value or "").strip()
            normalized = self._normalize_value(cleaned_value)
            if not qq_id or not module_name or not habit_key or not normalized:
                return None

            now = self._now_str()
            conn.execute("DELETE FROM user_habits WHERE id = ?", (habit_id,))
            conn.execute(
                """
                INSERT INTO user_habits (
                    qq_id, module_name, habit_key, habit_value, normalized_value, use_count,
                    source_text, created_at, updated_at, last_used_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    qq_id,
                    module_name,
                    habit_key,
                    cleaned_value[:120],
                    normalized,
                    max(use_count, 1),
                    source_text or "memory_panel",
                    created_at,
                    now,
                    now,
                ),
            )
            conn.commit()
            row = conn.execute(
                """
                SELECT * FROM user_habits
                WHERE qq_id = ? AND module_name = ? AND habit_key = ? AND normalized_value = ?
                """,
                (qq_id, module_name, habit_key, normalized),
            ).fetchone()
            return dict(row) if row else None

    def delete_habit(self, habit_id: int) -> bool:
        with self._get_conn() as conn:
            cursor = conn.execute("DELETE FROM user_habits WHERE id = ?", (habit_id,))
            deleted = cursor.rowcount > 0
            if deleted:
                conn.commit()
            return deleted

    def save_event(
        self,
        qq_id: str,
        summary: str,
        *,
        event_date_label: str = "",
        confidence: float = 0.82,
        source_text: str = "memory_panel",
    ) -> dict[str, Any] | None:
        if not self._upsert_event(
            qq_id,
            summary,
            event_date_label=event_date_label,
            confidence=confidence,
            source_text=source_text,
        ):
            return None
        normalized = self._normalize_value(self._trim_clause(summary, max_len=48))
        with self._get_conn() as conn:
            row = conn.execute(
                """
                SELECT * FROM passive_events
                WHERE qq_id = ? AND event_date_label = ? AND normalized_summary = ?
                """,
                (str(qq_id).strip(), str(event_date_label or "").strip(), normalized),
            ).fetchone()
            return dict(row) if row else None

    def update_event(
        self,
        event_id: int,
        *,
        summary: str,
        event_date_label: str,
        confidence: float,
        source_text: str,
    ) -> dict[str, Any] | None:
        with self._get_conn() as conn:
            row = conn.execute("SELECT * FROM passive_events WHERE id = ?", (event_id,)).fetchone()
            if not row:
                return None
            qq_id = str(row["qq_id"] or "").strip()
        if not self.delete_event(event_id):
            return None
        return self.save_event(
            qq_id,
            summary,
            event_date_label=event_date_label,
            confidence=confidence,
            source_text=source_text or "memory_panel",
        )

    def delete_event(self, event_id: int) -> bool:
        with self._get_conn() as conn:
            cursor = conn.execute("DELETE FROM passive_events WHERE id = ?", (event_id,))
            deleted = cursor.rowcount > 0
            if deleted:
                conn.commit()
            return deleted

    def build_relation_graph(self, qq_id: str, *, center_label: str = "") -> dict[str, Any]:
        cleaned_id = str(qq_id or "").strip()
        center = center_label.strip() or cleaned_id
        nodes = [{"id": "self", "label": center, "kind": "self"}]
        edges: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in self.list_relations(cleaned_id, limit=50):
            target_name = str(item.get("target_name") or "").strip()
            node_id = self._normalize_value(target_name) or f"relation-{item.get('id')}"
            if node_id not in seen:
                nodes.append(
                    {
                        "id": node_id,
                        "label": target_name,
                        "kind": "person",
                        "confidence": float(item.get("confidence") or 0),
                    }
                )
                seen.add(node_id)
            edges.append(
                {
                    "source": "self",
                    "target": node_id,
                    "label": str(item.get("relation_type") or "").strip(),
                    "confidence": float(item.get("confidence") or 0),
                }
            )
        return {"nodes": nodes, "edges": edges}

    def build_profile_prompt(self, event: AstrMessageEvent) -> str:
        qq_id = self._get_sender_id(event)
        if not qq_id:
            return ""
        base_entry = init_user_memory_store().get_memory(qq_id) or {}
        existing_aliases = {
            self._normalize_value(alias)
            for alias in (base_entry.get("memory_aliases") or [])
            if self._normalize_value(alias)
        }
        preferred_names = [
            item["value"]
            for item in self._get_preferences(qq_id, preference_type="preferred_name", limit=4, min_confidence=0.55)
            if self._normalize_value(item.get("value")) not in existing_aliases
        ]
        likes = [item["value"] for item in self._get_preferences(qq_id, preference_type="like", limit=4, min_confidence=0.48)]
        dislikes = [
            item["value"]
            for item in self._get_preferences(qq_id, preference_type="dislike", limit=4, min_confidence=0.48)
        ]
        relations = self._get_relations(qq_id, limit=5, min_confidence=0.5)
        habits = self._get_habits(qq_id, limit=8)
        if not any((preferred_names, likes, dislikes, relations, habits)):
            return ""

        lines = ["【被动记忆补充】"]
        if preferred_names:
            lines.append(f"- 称呼偏好: {'、'.join(preferred_names[:3])}")
        if likes:
            lines.append(f"- 喜欢: {'、'.join(likes[:4])}")
        if dislikes:
            lines.append(f"- 不喜欢: {'、'.join(dislikes[:4])}")
        if relations:
            relation_text = "；".join(f"{item.get('target_name')} = {item.get('relation_type')}" for item in relations[:4])
            if relation_text:
                lines.append(f"- 关系图谱: {relation_text}")
        habit_lines = [self._format_habit_line(item) for item in habits]
        habit_lines = [line for line in habit_lines if line]
        if habit_lines:
            lines.append("- 跨模块习惯:")
            for line in habit_lines[:5]:
                lines.append(f"  - {line}")
        lines.extend(
            [
                "使用规则:",
                "1. 这些内容来自长期对话和功能使用中的被动归纳，不要当成绝对事实硬背。",
                "2. 如果用户当下明确纠正、否认或换了说法，以用户当下表达为准。",
                "3. 只在自然相关时引用，不要把偏好、关系和习惯强行塞进每次回复。",
            ]
        )
        return "\n".join(lines)

    def _format_habit_line(self, item: dict[str, Any]) -> str:
        module_name = str(item.get("module_name") or "").strip()
        habit_key = str(item.get("habit_key") or "").strip()
        habit_value = str(item.get("habit_value") or "").strip()
        template = _HABIT_LABELS.get((module_name, habit_key))
        if not template or not habit_value:
            return ""
        use_count = int(item.get("use_count") or 0)
        suffix = f"（{use_count}次）" if use_count >= 2 else ""
        return template.format(value=habit_value) + suffix

    def build_event_recall_prompt(
        self,
        event: AstrMessageEvent,
        *,
        message_text: str = "",
        limit: int = 3,
    ) -> str:
        qq_id = self._get_sender_id(event)
        if not qq_id or not message_text:
            return ""
        relevant = self.search_relevant_events(qq_id, message_text, limit=limit)
        if not relevant:
            return ""

        lines = ["【事件回忆】"]
        for item in relevant:
            date_label = str(item.get("event_date_label") or "").strip()
            prefix = f"{date_label}: " if date_label else ""
            lines.append(f"- {prefix}{item.get('summary')}")
        lines.extend(
            [
                "使用规则:",
                "1. 只有在当前消息明显和这些旧事相关时才参考它们。",
                "2. 若用户当下对时间、人物或事实有新说法，以当前表达为准。",
                "3. 不要为了展示记忆而强行回忆旧事，优先服务当前对话。",
            ]
        )
        return "\n".join(lines)

    def build_reminiscence_bridge_prompt(
        self,
        context: Any,
        event: AstrMessageEvent,
        *,
        message_text: str = "",
        max_people: int = 3,
        max_events_per_person: int = 2,
    ) -> str:
        store = init_user_memory_store()
        qq_id = self._get_sender_id(event)
        sender_name = self._get_sender_name(event)
        related_entries = store.search_related_memories(message_text, exclude_qq_ids={qq_id} if qq_id else None, limit=max_people)
        people: list[dict[str, Any]] = []
        seen_people: set[str] = set()

        current_entry = store.get_memory(qq_id) if qq_id else None
        current_terms = self._build_person_terms(current_entry, fallback_name=sender_name)
        current_display = self._display_name_for_person(current_entry, fallback_name=sender_name)
        if current_terms and current_display:
            people.append({"label": "当前对话对象", "display": current_display, "terms": current_terms})
            seen_people.add(self._normalize_value(current_display))

        for entry in related_entries:
            display = self._display_name_for_person(entry)
            normalized_display = self._normalize_value(display)
            if not display or normalized_display in seen_people:
                continue
            people.append({"label": "当前消息提到的人", "display": display, "terms": self._build_person_terms(entry)})
            seen_people.add(normalized_display)
            if len(people) >= max_people:
                break

        for relation in self._search_relation_targets(qq_id, message_text, limit=max_people):
            target_name = str(relation.get("target_name") or "").strip()
            normalized_target = self._normalize_value(target_name)
            if not target_name or normalized_target in seen_people:
                continue
            relation_type = str(relation.get("relation_type") or "").strip()
            label = f"关系图谱中的对象（{relation_type}）" if relation_type else "关系图谱中的对象"
            people.append({"label": label, "display": target_name, "terms": [target_name]})
            seen_people.add(normalized_target)
            if len(people) >= max_people:
                break

        if not people:
            return ""

        try:
            reminiscence = context.get_star("local_reminiscence")
        except Exception:
            reminiscence = None
        if not reminiscence or not getattr(reminiscence, "is_model_ready", lambda: False)():
            return ""

        vector_db = getattr(reminiscence, "vector_db", None)
        memory_db = getattr(reminiscence, "db", None)
        if vector_db is None or memory_db is None:
            return ""

        lines = ["【人物记忆联动回忆】"]
        seen_events: set[str] = set()
        added_any = False
        for person in people:
            terms = [term for term in person.get("terms") or [] if self._normalize_value(term)]
            if not terms:
                continue
            try:
                search_results = vector_db.search_events(" ".join(terms[:4]), top_n=6) or []
            except Exception as exc:
                logger.debug("[passive_memory] APLR 搜索失败: %s", exc)
                continue
            person_events: list[dict[str, Any]] = []
            for result in search_results:
                event_id = str(result.get("event_id") or "").strip()
                relevance = float(result.get("relevance") or 0)
                if not event_id or event_id in seen_events or relevance < 35:
                    continue
                try:
                    event_row = memory_db.get_event_by_id(event_id)
                except Exception:
                    event_row = None
                if not event_row:
                    continue
                if not self._event_matches_terms(event_row, terms) and relevance < 65:
                    continue
                person_events.append(
                    {
                        "event_id": event_id,
                        "date": str(event_row.get("date") or "").strip(),
                        "narrative": str(event_row.get("narrative") or "").strip(),
                        "emotion": str(event_row.get("emotion") or "").strip(),
                    }
                )
                seen_events.add(event_id)
                if len(person_events) >= max_events_per_person:
                    break
            if not person_events:
                continue
            added_any = True
            lines.append(f"- {person.get('label')}: {person.get('display')}")
            for item in person_events:
                emotion_suffix = f"（{item['emotion']}）" if item["emotion"] else ""
                lines.append(f"  - {item['date']}: {item['narrative']} {emotion_suffix} [ID: {item['event_id']}]".rstrip())

        if not added_any:
            return ""
        lines.extend(
            [
                "使用规则:",
                "1. 这是按人物身份自动联动出来的旧事件，只在自然相关时参考。",
                "2. 如果用户当下对人名、关系或事实有新说法，以当下表达为准。",
                "3. 不要为了展示记忆而强行提起旧事，优先服务当前对话。",
            ]
        )
        return "\n".join(lines)

    def _build_person_terms(self, entry: dict[str, Any] | None, *, fallback_name: str = "") -> list[str]:
        terms: list[str] = []
        if entry:
            for alias in entry.get("memory_aliases") or []:
                cleaned = self._trim_clause(alias, max_len=18)
                if cleaned:
                    terms.append(cleaned)
            platform_name = self._trim_clause(entry.get("platform_name") or "", max_len=18)
            if platform_name:
                terms.append(platform_name)
            for seen_name in entry.get("seen_names") or []:
                cleaned = self._trim_clause(seen_name, max_len=18)
                if cleaned:
                    terms.append(cleaned)
            note = self._trim_clause(entry.get("note") or "", max_len=18)
            if note and len(self._normalize_value(note)) >= 2:
                terms.append(note)
        elif fallback_name:
            cleaned = self._trim_clause(fallback_name, max_len=18)
            if cleaned:
                terms.append(cleaned)

        unique_terms: list[str] = []
        seen: set[str] = set()
        for term in terms:
            normalized = self._normalize_value(term)
            if not normalized or normalized in seen or normalized in _GENERIC_VALUES:
                continue
            unique_terms.append(term)
            seen.add(normalized)
        return unique_terms[:5]

    def _display_name_for_person(self, entry: dict[str, Any] | None, *, fallback_name: str = "") -> str:
        if entry:
            aliases = entry.get("memory_aliases") or []
            if aliases:
                return str(aliases[0]).strip()
            platform_name = str(entry.get("platform_name") or "").strip()
            if platform_name:
                return platform_name
            qq_id = str(entry.get("qq_id") or "").strip()
            if qq_id:
                return qq_id
        return str(fallback_name or "").strip()

    def _event_matches_terms(self, event_row: dict[str, Any], terms: list[str]) -> bool:
        combined = f"{event_row.get('narrative') or ''} {event_row.get('reflection') or ''}"
        normalized_combined = self._normalize_value(combined)
        if not normalized_combined:
            return False
        for term in terms:
            normalized_term = self._normalize_value(term)
            if len(normalized_term) >= 2 and normalized_term in normalized_combined:
                return True
        return False


_passive_memory_store: PassiveMemoryStore | None = None


def init_passive_memory_store() -> PassiveMemoryStore:
    global _passive_memory_store
    if _passive_memory_store is None:
        _passive_memory_store = PassiveMemoryStore()
    return _passive_memory_store


def record_passive_habit(
    event: AstrMessageEvent,
    module_name: str,
    habit_key: str,
    habit_value: str,
    *,
    source_text: str = "",
) -> bool:
    return init_passive_memory_store().record_habit(
        event,
        module_name,
        habit_key,
        habit_value,
        source_text=source_text,
    )
