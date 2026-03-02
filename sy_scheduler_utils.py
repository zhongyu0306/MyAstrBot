from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain
from astrbot.api.message_components import At
from astrbot.api.star import Context, StarTools


_REMINDER_FILE_NAME = "simple_reminders.json"
_REMINDER_LOCK = asyncio.Lock()


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


def _get_reminder_file_path() -> Path:
    """获取简易提醒的持久化存储文件路径。"""
    try:
        base = StarTools.get_data_dir("astrbot_all_char")
    except Exception:
        base = Path("data") / "plugin_data" / "astrbot_all_char"
    base.mkdir(parents=True, exist_ok=True)
    return base / _REMINDER_FILE_NAME


def _load_all_reminders() -> list[dict]:
    path = _get_reminder_file_path()
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return data
        return []
    except Exception as e:
        logger.error("读取简易提醒数据失败: %s", e)
        return []


def _save_all_reminders(reminders: list[dict]) -> None:
    path = _get_reminder_file_path()
    try:
        path.write_text(json.dumps(reminders, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        logger.error("保存简易提醒数据失败: %s", e)


class _SimpleReminderCenter:
    """基于 APScheduler 的持久化简易提醒中心。"""

    def __init__(self, context: Context, config: AstrBotConfig):
        self.context = context
        self.config = config
        self._scheduler = None
        self._available = False
        self._start_scheduler()

    @property
    def is_available(self) -> bool:
        return self._available

    def _start_scheduler(self) -> None:
        try:
            from apscheduler.schedulers.asyncio import AsyncIOScheduler
            from apscheduler.triggers.cron import CronTrigger

            tz = getattr(self.config, "simple_reminder_timezone", "Asia/Shanghai")
            scheduler = AsyncIOScheduler(timezone=tz)
            scheduler.add_job(
                self._run_due_reminders,
                CronTrigger(second="0", timezone=tz),
                id="simple_reminder_tick",
            )
            scheduler.start()
            self._scheduler = scheduler
            self._available = True
            logger.info("简易提醒持久化调度已启动，时区=%s", tz)
        except ImportError:
            logger.warning("未安装 apscheduler，/提醒 持久化定时不可用。")
            self._scheduler = None
            self._available = False
        except Exception as e:
            logger.error("简易提醒调度启动失败: %s", e)
            self._scheduler = None
            self._available = False

    async def add_reminder(
        self,
        session_id: str,
        creator_id: Optional[str],
        creator_name: Optional[str],
        text: str,
        run_at: datetime,
    ) -> None:
        """新增一条提醒记录并持久化，由调度器按时发送。"""
        if not self._available:
            raise RuntimeError("简易提醒调度未就绪，无法添加提醒。")

        record = {
            "session_id": session_id,
            "creator_id": creator_id,
            "creator_name": creator_name,
            "text": text,
            "run_at": run_at.strftime("%Y-%m-%d %H:%M:%S"),
        }
        async with _REMINDER_LOCK:
            reminders = _load_all_reminders()
            reminders.append(record)
            _save_all_reminders(reminders)

    async def _run_due_reminders(self) -> None:
        """每分钟轮询一次，将到期的提醒发送出去。"""
        now = datetime.now()
        async with _REMINDER_LOCK:
            reminders = _load_all_reminders()
            remaining: list[dict] = []
            for r in reminders:
                try:
                    run_at_str = str(r.get("run_at", ""))
                    if not run_at_str:
                        continue
                    run_at = datetime.strptime(run_at_str, "%Y-%m-%d %H:%M:%S")
                except Exception:
                    continue

                if run_at <= now:
                    # 到期，发送提醒
                    session_id = str(r.get("session_id") or "")
                    if not session_id:
                        continue
                    creator_id = r.get("creator_id")
                    creator_name = r.get("creator_name")
                    text = str(r.get("text") or "")
                    asyncio.create_task(
                        self._send_one(session_id, creator_id, creator_name, text)
                    )
                else:
                    remaining.append(r)

            _save_all_reminders(remaining)

    async def _send_one(
        self,
        session_id: str,
        creator_id: Optional[str],
        creator_name: Optional[str],
        text: str,
    ) -> None:
        chain = MessageChain()
        if creator_id and str(creator_id).isdigit():
            try:
                chain.chain.append(At(qq=int(creator_id), name=creator_name or None))
            except Exception:
                pass
        # 默认文案（兜底）
        final_text = f"⏰ 提醒：{text}"

        # 尝试交给当前会话的 LLM 用人格口吻润色提醒文案
        try:
            provider_id = await self.context.get_current_chat_provider_id(umo=session_id)
            if provider_id:
                prompt = (
                    "你是当前会话里的聊天角色，请用你平时的人格和说话风格，"
                    "把下面这条提醒内容说给对方听。\n"
                    "要求：\n"
                    "1. 只回复一句话，简短自然，像正常人群聊提醒。\n"
                    "2. 必须包含提醒的核心内容，不要改变时间或事项含义。\n"
                    "3. 不要解释你在执行系统提醒，也不要说明自己是机器人或助手。\n"
                    f"需要提醒的内容是：{text}"
                )
                llm_resp = await self.context.llm_generate(
                    chat_provider_id=provider_id,
                    prompt=prompt,
                )
                out = (getattr(llm_resp, "completion_text", None) or "").strip()
                if out:
                    final_text = out
        except Exception as e:
            logger.exception("定时提醒生成自然语言文本失败: %s", e)

        chain.message(final_text)
        try:
            await self.context.send_message(session_id, chain)
        except Exception as e:
            logger.error("发送定时提醒到 %s 失败: %s", session_id[:50], e)


_REMINDER_CENTER: _SimpleReminderCenter | None = None


def init_simple_reminder_center(
    context: Context, config: AstrBotConfig
) -> Optional[_SimpleReminderCenter]:
    """初始化（或获取已有的）简易提醒中心，用于持久化定时任务。"""
    global _REMINDER_CENTER
    if _REMINDER_CENTER is not None:
        return _REMINDER_CENTER if _REMINDER_CENTER.is_available else None

    center = _SimpleReminderCenter(context, config)
    if center.is_available:
        _REMINDER_CENTER = center
        return center
    return None


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

    session_id = getattr(event, "unified_msg_origin", None) or getattr(event, "session_id", "")
    if not session_id:
        yield event.plain_result("❌ 无法获取当前会话，定时提醒不可用。")
        return

    creator_id = event.get_sender_id()
    creator_name = event.get_sender_name()

    center = init_simple_reminder_center(context, config)
    if center is None:
        yield event.plain_result(
            "❌ 当前环境未安装 apscheduler，/提醒 的持久化定时不可用。\n"
            "请在运行环境中安装 apscheduler 后重试。"
        )
        return

    await center.add_reminder(session_id, creator_id, creator_name, text, target)

    target_str = target.strftime("%Y-%m-%d %H:%M:%S")
    yield event.plain_result(f"✅ 已设置提醒：{target_str} 提醒你「{text}」。\n（重启后也会继续生效）")


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

