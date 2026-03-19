from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
from astrbot.api.star import StarTools


class UserMemoryStore:
    """基于 JSON 的用户永久记忆。"""

    FILE_NAME = "user_memory.json"

    def __init__(self) -> None:
        self._data_dir = Path(StarTools.get_data_dir("astrbot_all_char"))
        self._data_dir.mkdir(parents=True, exist_ok=True)
        self._path = self._data_dir / self.FILE_NAME
        self._data = self._load()

    def _load(self) -> dict[str, Any]:
        if not self._path.exists():
            return {"users": {}}
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict) and isinstance(data.get("users"), dict):
                return data
        except Exception as exc:
            logger.warning("加载用户记忆失败: %s", exc)
        return {"users": {}}

    def _save(self) -> None:
        try:
            with open(self._path, "w", encoding="utf-8") as f:
                json.dump(self._data, f, ensure_ascii=False, indent=2)
        except Exception as exc:
            logger.warning("保存用户记忆失败: %s", exc)

    @staticmethod
    def _now_str() -> str:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    @staticmethod
    def _safe_sender_id(event: AstrMessageEvent) -> str:
        try:
            sender_id = event.get_sender_id()
            if sender_id:
                return str(sender_id)
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

    def _get_user_entry(self, qq_id: str) -> dict[str, Any]:
        users = self._data.setdefault("users", {})
        entry = users.get(qq_id)
        if not isinstance(entry, dict):
            entry = {
                "qq_id": qq_id,
                "memory_name": "",
                "note": "",
                "platform_name": "",
                "seen_names": [],
                "created_at": self._now_str(),
                "updated_at": self._now_str(),
                "last_seen_at": "",
            }
            users[qq_id] = entry
        return entry

    def observe_user(self, event: AstrMessageEvent) -> str:
        """记录当前用户的 QQ 和平台昵称。"""
        qq_id = self._safe_sender_id(event)
        if not qq_id:
            return ""

        sender_name = self._safe_sender_name(event)
        entry = self._get_user_entry(qq_id)
        changed = False

        if sender_name and entry.get("platform_name") != sender_name:
            entry["platform_name"] = sender_name
            changed = True

        if sender_name:
            seen_names = entry.setdefault("seen_names", [])
            if sender_name not in seen_names:
                seen_names.append(sender_name)
                entry["seen_names"] = seen_names[-10:]
                changed = True

        entry["last_seen_at"] = self._now_str()
        entry["updated_at"] = self._now_str()
        self._save() if changed else self._save()
        return qq_id

    def set_memory(
        self,
        qq_id: str,
        memory_name: str | None = None,
        note: str | None = None,
    ) -> dict[str, Any]:
        entry = self._get_user_entry(qq_id)
        if memory_name is not None:
            entry["memory_name"] = str(memory_name).strip()
        if note is not None:
            entry["note"] = str(note).strip()
        entry["updated_at"] = self._now_str()
        self._save()
        return entry

    def get_memory(self, qq_id: str) -> dict[str, Any] | None:
        users = self._data.get("users", {})
        entry = users.get(qq_id)
        return entry if isinstance(entry, dict) else None

    def delete_memory(self, qq_id: str) -> bool:
        users = self._data.get("users", {})
        if qq_id in users:
            del users[qq_id]
            self._save()
            return True
        return False

    def list_memories(self) -> list[dict[str, Any]]:
        users = self._data.get("users", {})
        result = []
        for qq_id, entry in users.items():
            if isinstance(entry, dict):
                item = dict(entry)
                item["qq_id"] = qq_id
                result.append(item)
        result.sort(key=lambda item: item.get("updated_at", ""), reverse=True)
        return result

    def format_memory(self, qq_id: str) -> str:
        entry = self.get_memory(qq_id)
        if not entry:
            return f"未找到 QQ {qq_id} 的永久记忆。"

        seen_names = entry.get("seen_names") or []
        lines = [
            f"🧠 用户记忆：{qq_id}",
            f"记忆姓名：{entry.get('memory_name') or '--'}",
            f"当前平台昵称：{entry.get('platform_name') or '--'}",
            f"备注：{entry.get('note') or '--'}",
            f"最近出现时间：{entry.get('last_seen_at') or '--'}",
        ]
        if seen_names:
            lines.append(f"历史昵称：{', '.join(seen_names)}")
        return "\n".join(lines)

    def build_prompt_for_event(self, event: AstrMessageEvent) -> str:
        """为当前对话用户构建注入给 LLM 的永久记忆提示。"""
        qq_id = self._safe_sender_id(event)
        if not qq_id:
            return ""

        entry = self.get_memory(qq_id)
        if not entry:
            sender_name = self._safe_sender_name(event)
            if not sender_name:
                return ""
            return (
                "【用户识别信息】\n"
                f"- 当前对话用户 QQ: {qq_id}\n"
                f"- 当前平台昵称: {sender_name}\n"
                "说明：这是你当前正在对话的用户信息，但暂未设置永久身份记忆。"
            )

        memory_name = entry.get("memory_name") or ""
        platform_name = entry.get("platform_name") or self._safe_sender_name(event)
        note = entry.get("note") or ""

        lines = [
            "【当前用户永久记忆】",
            f"- 当前对话用户 QQ: {qq_id}",
            f"- 记忆身份: {memory_name or '未设置'}",
            f"- 当前平台昵称: {platform_name or '未知'}",
        ]
        if note:
            lines.append(f"- 备注: {note}")
        lines.extend(
            [
                "使用规则：",
                "1. 你可以据此知道当前对话的人是谁。",
                "2. 仅在合适的时候自然引用这些记忆，不要每次都生硬重复。",
                "3. 不要编造未记录的身份、关系或经历。",
            ]
        )
        return "\n".join(lines)


_memory_store: UserMemoryStore | None = None


def init_user_memory_store() -> UserMemoryStore:
    global _memory_store
    if _memory_store is None:
        _memory_store = UserMemoryStore()
    return _memory_store


async def handle_memory_command(event: AstrMessageEvent):
    store = init_user_memory_store()
    store.observe_user(event)

    msg = event.get_message_str().strip()
    parts = msg.split()
    command_name = parts[0].lstrip("/").strip().lower() if parts else ""

    if command_name == "认人":
        args = parts[1:]
        if not args:
            async for result in _handle_memory_help(event):
                yield result
            return
        if len(args) >= 2 and str(args[0]).strip().isdigit():
            qq_id = str(args[0]).strip()
            memory_name = args[1].strip()
            note = " ".join(args[2:]).strip()
            store.set_memory(qq_id, memory_name=memory_name, note=note or None)
            yield event.plain_result(
                f"已设置 QQ {qq_id} 的永久记忆为：{memory_name}"
                + (f"\n备注：{note}" if note else "")
            )
            return

        qq_id = store._safe_sender_id(event)
        if not qq_id:
            yield event.plain_result("无法识别当前 QQ 号，不能设置个人记忆。")
            return
        memory_name = args[0].strip()
        note = " ".join(args[1:]).strip()
        store.set_memory(qq_id, memory_name=memory_name, note=note or None)
        yield event.plain_result(
            f"已为你设置永久记忆：{memory_name}" + (f"\n备注：{note}" if note else "")
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

    if subcommand in {"我是", "设置我"}:
        qq_id = store._safe_sender_id(event)
        if not qq_id:
            yield event.plain_result("无法识别当前 QQ 号，不能设置个人记忆。")
            return
        if not args:
            yield event.plain_result("用法：/记忆 我是 名字 [备注]")
            return
        memory_name = args[0].strip()
        note = " ".join(args[1:]).strip()
        store.set_memory(qq_id, memory_name=memory_name, note=note or None)
        yield event.plain_result(
            f"已为你设置永久记忆：{memory_name}" + (f"\n备注：{note}" if note else "")
        )
        return

    if subcommand == "设置":
        if len(args) < 2:
            yield event.plain_result("用法：/记忆 设置 QQ号 名字 [备注]")
            return
        qq_id = str(args[0]).strip()
        memory_name = args[1].strip()
        note = " ".join(args[2:]).strip()
        store.set_memory(qq_id, memory_name=memory_name, note=note or None)
        yield event.plain_result(
            f"已设置 QQ {qq_id} 的永久记忆为：{memory_name}" + (f"\n备注：{note}" if note else "")
        )
        return

    if subcommand == "备注":
        if len(args) < 2:
            yield event.plain_result("用法：/记忆 备注 QQ号 内容")
            return
        qq_id = str(args[0]).strip()
        note = " ".join(args[1:]).strip()
        entry = store.get_memory(qq_id)
        current_name = entry.get("memory_name", "") if entry else None
        store.set_memory(qq_id, memory_name=current_name, note=note)
        yield event.plain_result(f"已更新 QQ {qq_id} 的备注：{note}")
        return

    if subcommand in {"查看", "查询"}:
        qq_id = str(args[0]).strip() if args else store._safe_sender_id(event)
        if not qq_id:
            yield event.plain_result("用法：/记忆 查看 QQ号")
            return
        yield event.plain_result(store.format_memory(qq_id))
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
            yield event.plain_result("当前还没有任何永久记忆。")
            return
        lines = ["🧠 永久记忆列表："]
        for item in records[:50]:
            lines.append(
                f"- {item.get('memory_name') or '--'} ({item.get('qq_id', '--')})"
                f" | 昵称: {item.get('platform_name') or '--'}"
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
    if not qq_id:
        yield event.plain_result("无法识别当前 QQ 号。")
        return
    yield event.plain_result(store.format_memory(qq_id))


async def _handle_memory_help(event: AstrMessageEvent):
    help_text = (
        "🧠 永久记忆说明\n\n"
        "• /记忆 我是 名字 [备注] - 为当前 QQ 设置永久身份记忆\n"
        "• /记忆 设置 QQ号 名字 [备注] - 为指定 QQ 设置永久身份记忆\n"
        "• /记忆 备注 QQ号 内容 - 更新指定 QQ 的备注\n"
        "• /记忆 查看 [QQ号] - 查看某人的永久记忆，不填默认看自己\n"
        "• /记忆 删除 [QQ号] - 删除永久记忆，不填默认删自己\n"
        "• /记忆 列表 - 查看当前已保存的永久记忆\n"
        "• /我是谁 - 查看 bot 当前记住了你什么\n\n"
        "说明：普通聊天进入大模型前，会自动读取当前 QQ 的永久记忆并注入给 bot。"
    )
    yield event.plain_result(help_text)
