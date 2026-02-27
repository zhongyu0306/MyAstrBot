from __future__ import annotations

import asyncio
import re
from datetime import datetime, timedelta
from typing import Optional

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain
from astrbot.api.message_components import At
from astrbot.api.star import Context


_PENDING_TASKS: set[asyncio.Task] = set()


def _parse_time_expression(base: datetime, expr: str) -> Optional[datetime]:
    """
    解析简单的时间表达式：
    - "3分钟后"
    - "2小时后"
    - "2026-02-28-08:00"
    - "08:30"（今日/次日）
    """
    expr = expr.strip()

    # N 分钟后
    m = re.match(r"^(\d+)\s*分钟后$", expr)
    if m:
        minutes = int(m.group(1))
        if minutes <= 0:
            return None
        return base + timedelta(minutes=minutes)

    # N 小时后
    m = re.match(r"^(\d+)\s*小时后$", expr)
    if m:
        hours = int(m.group(1))
        if hours <= 0:
            return None
        return base + timedelta(hours=hours)

    # 绝对时间：YYYY-MM-DD-HH:MM
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})-(\d{2}):(\d{2})$", expr)
    if m:
        year, month, day, hour, minute = map(int, m.groups())
        try:
            dt = datetime(year, month, day, hour, minute)
        except ValueError:
            return None
        if dt <= base:
            return None
        return dt

    # 当天时间：HH:MM（若已过则顺延到明天）
    m = re.match(r"^(\d{1,2}):(\d{2})$", expr)
    if m:
        hour, minute = map(int, m.groups())
        if hour < 0 or hour > 23 or minute < 0 or minute > 59:
            return None
        dt = base.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if dt <= base:
            dt = dt + timedelta(days=1)
        return dt

    return None


async def _reminder_task(
    delay: float,
    context: Context,
    session_id: str,
    creator_id: Optional[str],
    creator_name: Optional[str],
    text: str,
) -> None:
    try:
        if delay <= 0:
            delay = 1.0
        await asyncio.sleep(delay)

        chain = MessageChain()
        # 简单 @ 一下发起人（如果是 QQ 数字 ID）
        if creator_id and creator_id.isdigit():
            try:
                chain.chain.append(At(qq=int(creator_id), name=creator_name or None))
            except Exception:
                pass

        chain.message(f"⏰ 提醒：{text}")
        try:
            await context.send_message(session_id, chain)
        except Exception as e:
            logger.error("发送定时提醒到 %s 失败: %s", session_id[:50], e)
    finally:
        _PENDING_TASKS.discard(asyncio.current_task())  # type: ignore[arg-type]


async def handle_simple_reminder(event: AstrMessageEvent, context: Context, config: AstrBotConfig):
    """
    简易提醒命令入口：/提醒 <时间> <内容>

    支持示例：
    - /提醒 3分钟后 喝水
    - /提醒 3分钟后提醒我喝水   （会自动按“后”分割）
    - /提醒 2026-02-28-08:00 开会
    - /提醒 08:30 上班打卡
    """
    raw = event.get_message_str().strip()
    # 去掉前缀（/提醒 或 提醒）
    m = re.match(r"^[\/／]?提醒[\s\n]+(.+)$", raw)
    if not m:
        yield event.plain_result("用法：/提醒 <时间> <内容>\n例如：/提醒 3分钟后 喝水")
        return

    body = m.group(1).strip()

    time_str = ""
    text = ""

    # 优先按空格拆分：/提醒 3分钟后 喝水
    if " " in body or "\t" in body or "\n" in body:
        first, rest = body.split(maxsplit=1)
        time_str = first.strip()
        text = rest.strip()
    else:
        # 没有空格时，尝试按“后”分割：/提醒 3分钟后提醒我喝水
        idx = body.find("后")
        if idx != -1:
            time_str = body[: idx + 1].strip()
            text = body[idx + 1 :].strip()

    if not time_str or not text:
        yield event.plain_result(
            "用法：/提醒 <时间> <内容>\n"
            "示例：\n"
            "  /提醒 3分钟后 喝水\n"
            "  /提醒 3分钟后提醒我喝水\n"
            "  /提醒 08:30 上班打卡"
        )
        return

    now = datetime.now()
    target = _parse_time_expression(now, time_str)
    if target is None:
        yield event.plain_result(
            "暂时只支持以下时间格式：\n"
            "- N分钟后（如：3分钟后）\n"
            "- N小时后（如：2小时后）\n"
            "- 绝对时间：2026-02-28-08:00\n"
            "- 当天时间：08:30（若已过则顺延到明天）"
        )
        return

    delay = (target - now).total_seconds()
    if delay < 1:
        yield event.plain_result("时间太近了，请至少设置 1 秒之后。")
        return

    session_id = getattr(event, "unified_msg_origin", None) or getattr(event, "session_id", "")
    if not session_id:
        yield event.plain_result("❌ 无法获取当前会话，定时提醒不可用。")
        return

    creator_id = event.get_sender_id()
    creator_name = event.get_sender_name()

    task = asyncio.create_task(
        _reminder_task(delay, context, session_id, creator_id, creator_name, text)
    )
    _PENDING_TASKS.add(task)

    target_str = target.strftime("%Y-%m-%d %H:%M:%S")
    yield event.plain_result(f"✅ 已设置提醒：{target_str} 提醒你「{text}」。")


async def handle_sy_rmd_group(event: AstrMessageEvent, context: Context, config: AstrBotConfig):
    """
    简化后的 /rmd 与 /rmdg：目前不再提供复杂子命令，只给出帮助提示。
    """
    yield event.plain_result(
        "当前定时任务功能已简化，仅支持：\n"
        "  /提醒 <时间> <内容>\n"
        "示例：/提醒 3分钟后 喝水\n"
        "原 /rmd /rmdg 高级用法暂未实现。"
    )

