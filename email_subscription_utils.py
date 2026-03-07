# 邮件订阅：可配置订阅项、每日定时发送、持久化存储
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, AsyncIterator, Optional

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent
from astrbot.api.star import Context, StarTools

from .email_utils import (
    _get_email_config,
    send_email_sync,
    EMAIL_PATTERN,
)


_SUBSCRIPTION_FILE_NAME = "email_subscriptions.json"
_LOCK = asyncio.Lock()


def _get_subscription_file_path() -> Path:
    base = StarTools.get_data_dir("astrbot_all_char")
    base.mkdir(parents=True, exist_ok=True)
    return base / _SUBSCRIPTION_FILE_NAME


def _load_subscriptions() -> list[dict[str, Any]]:
    path = _get_subscription_file_path()
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        subs = data.get("subscriptions")
        return list(subs) if isinstance(subs, list) else []
    except Exception as e:
        logger.warning("读取邮件订阅列表失败: %s", e)
        return []


def _save_subscriptions(subscriptions: list[dict[str, Any]]) -> None:
    path = _get_subscription_file_path()
    path.write_text(
        json.dumps({"subscriptions": subscriptions}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def get_allowed_topics(config: AstrBotConfig) -> list[str]:
    """从配置读取可订阅项（逗号分隔），如 新闻,天气,每日摘要。"""
    raw = _get_email_config(config, "email_subscription_topics", "新闻,天气,每日摘要")
    return [t.strip() for t in (raw or "").split(",") if t.strip()]


async def add_subscription(umo: str, email: str, topic: str) -> tuple[bool, str]:
    """添加一条订阅。返回 (成功, 提示信息)。"""
    email = (email or "").strip()
    topic = (topic or "").strip()
    if not email or not EMAIL_PATTERN.fullmatch(email):
        return False, "收件邮箱格式不正确。"
    if not topic:
        return False, "请指定订阅项，例如：新闻、天气、每日摘要。"
    async with _LOCK:
        subs = _load_subscriptions()
        for s in subs:
            if (s.get("umo") == umo or s.get("umo") == (umo or "")) and (
                (s.get("topic") or "").strip() == topic
            ):
                s["email"] = email
                _save_subscriptions(subs)
                return True, f"已更新订阅：{topic} → {email}"
        subs.append({"umo": umo or "", "email": email, "topic": topic})
        _save_subscriptions(subs)
    return True, f"已添加订阅：每日将把「{topic}」发到 {email}"


async def remove_subscription(umo: str, topic: str) -> tuple[bool, str]:
    """取消一条订阅。"""
    topic = (topic or "").strip()
    if not topic:
        return False, "请指定要取消的订阅项，例如：取消邮件订阅 新闻"
    async with _LOCK:
        subs = _load_subscriptions()
        before = len(subs)
        subs = [s for s in subs if not ((s.get("umo") == umo or s.get("umo") == (umo or "")) and (s.get("topic") or "").strip() == topic)]
        if len(subs) == before:
            return False, f"未找到对「{topic}」的订阅。"
        _save_subscriptions(subs)
    return True, f"已取消订阅：{topic}"


def list_by_umo(umo: str) -> list[dict[str, Any]]:
    """查询某用户的全部订阅。"""
    subs = _load_subscriptions()
    return [s for s in subs if s.get("umo") == umo or s.get("umo") == (umo or "")]


def get_all_subscriptions() -> list[dict[str, Any]]:
    """返回全部订阅（供定时任务使用）。"""
    return _load_subscriptions()


class _EmailSubscriptionCenter:
    """邮件订阅定时发送中心：每日到点给所有订阅用户发对应内容的邮件。"""

    def __init__(self, context: Context, config: AstrBotConfig):
        self.context = context
        self.config = config
        self._scheduler = None
        self._available = False
        self._start_scheduler()

    def _start_scheduler(self) -> None:
        try:
            from apscheduler.schedulers.asyncio import AsyncIOScheduler
            from apscheduler.triggers.cron import CronTrigger

            time_str = _get_email_config(self.config, "email_subscription_time", "08:00")
            tz = _get_email_config(self.config, "email_subscription_timezone", "Asia/Shanghai")
            parts = (time_str or "08:00").strip().split(":")
            hour = int(parts[0]) if parts else 8
            minute = int(parts[1]) if len(parts) > 1 else 0
            hour = max(0, min(23, hour))
            minute = max(0, min(59, minute))

            scheduler = AsyncIOScheduler(timezone=tz)
            scheduler.add_job(
                self._run_daily,
                CronTrigger(hour=hour, minute=minute, timezone=tz),
                id="email_subscription_daily",
            )
            scheduler.start()
            self._scheduler = scheduler
            self._available = True
            logger.info("邮件订阅定时任务已启动：每日 %02d:%02d（%s）", hour, minute, tz)
        except ImportError:
            logger.warning("未安装 apscheduler，邮件订阅定时不可用。")
        except Exception as e:
            logger.error("邮件订阅调度启动失败: %s", e)

    async def _run_daily(self) -> None:
        """每日到点：为每条订阅生成内容并发邮件。"""
        sender = _get_email_config(self.config, "email_sender", "")
        auth_code = _get_email_config(self.config, "email_auth_code", "")
        if not sender or not auth_code:
            logger.warning("邮件订阅：未配置发件人/授权码，跳过本次发送。")
            return
        subs = get_all_subscriptions()
        if not subs:
            return
        allowed = get_allowed_topics(self.config)
        umo_for_llm = _get_email_config(self.config, "email_subscription_default_umo", "")
        try:
            provider_id = await self.context.get_current_chat_provider_id(umo=umo_for_llm)
        except Exception:
            provider_id = None
        for s in subs:
            topic = (s.get("topic") or "").strip()
            email = (s.get("email") or "").strip()
            if not topic or not email:
                continue
            if allowed and topic not in allowed:
                continue
            subject = f"每日订阅 - {topic}"
            body = f"今日【{topic}】订阅，请查收。"
            if provider_id:
                try:
                    prompt = (
                        f"请生成今日「{topic}」的邮件正文内容，用于每日订阅推送。"
                        "要求：简洁摘要、纯文本、多行可。不要称呼、落款、占位符。直接输出正文。"
                    )
                    llm_resp = await self.context.llm_generate(
                        chat_provider_id=provider_id,
                        prompt=prompt,
                    )
                    out = (getattr(llm_resp, "completion_text", None) or "").strip()
                    if out:
                        body = out
                except Exception as e:
                    logger.warning("邮件订阅生成内容失败 topic=%s: %s", topic, e)
            ok, msg = send_email_sync(
                sender=sender,
                auth_code=auth_code,
                to_addrs=[email],
                subject=subject,
                body=body,
                smtp_host=_get_email_config(self.config, "email_smtp_host", "smtp.qq.com"),
                smtp_port=int(_get_email_config(self.config, "email_smtp_port", "465") or "465"),
            )
            if ok:
                logger.info("邮件订阅已发送: topic=%s to=%s", topic, email)
            else:
                logger.warning("邮件订阅发送失败 topic=%s to=%s: %s", topic, email, msg)


_CENTER: Optional[_EmailSubscriptionCenter] = None


def init_email_subscription_center(
    context: Context, config: AstrBotConfig
) -> Optional[_EmailSubscriptionCenter]:
    global _CENTER
    if _CENTER is not None:
        return _CENTER
    try:
        _CENTER = _EmailSubscriptionCenter(context, config)
        return _CENTER
    except Exception as e:
        logger.exception("邮件订阅中心初始化失败: %s", e)
        return None


async def handle_subscribe_command(
    event: AstrMessageEvent,
    context: Context,
    config: AstrBotConfig,
) -> AsyncIterator:
    """订阅邮件 <订阅项> <收件邮箱>"""
    raw = (event.get_message_str() or "").strip()
    for prefix in ("订阅邮件", "邮件订阅", "/订阅邮件", "/邮件订阅"):
        if raw.startswith(prefix):
            raw = raw[len(prefix) :].strip()
            break
    else:
        return
    parts = raw.split(maxsplit=1)
    if len(parts) < 2:
        topics = get_allowed_topics(config)
        yield event.plain_result(
            "用法：订阅邮件 <订阅项> <收件邮箱>\n"
            f"当前可订阅项：{', '.join(topics) or '未配置'}\n"
            "示例：订阅邮件 新闻 xxx@qq.com"
        )
        return
    topic, email = parts[0].strip(), parts[1].strip()
    allowed = get_allowed_topics(config)
    if allowed and topic not in allowed:
        yield event.plain_result(f"当前可订阅项为：{', '.join(allowed)}，请使用其中之一。")
        return
    umo = getattr(event, "unified_msg_origin", None) or ""
    ok, msg = await add_subscription(umo, email, topic)
    yield event.plain_result(msg)


async def handle_unsubscribe_command(
    event: AstrMessageEvent,
    config: AstrBotConfig,
) -> AsyncIterator:
    """取消邮件订阅 <订阅项>"""
    raw = (event.get_message_str() or "").strip()
    for prefix in ("取消邮件订阅", "邮件退订", "/取消邮件订阅", "/邮件退订"):
        if raw.startswith(prefix):
            raw = raw[len(prefix) :].strip()
            break
    else:
        return
    topic = raw.strip()
    umo = getattr(event, "unified_msg_origin", None) or ""
    ok, msg = await remove_subscription(umo, topic)
    yield event.plain_result(msg)


async def handle_list_subscriptions_command(
    event: AstrMessageEvent,
    config: AstrBotConfig,
) -> AsyncIterator:
    """我的邮件订阅"""
    umo = getattr(event, "unified_msg_origin", None) or ""
    subs = list_by_umo(umo)
    if not subs:
        yield event.plain_result("您当前没有邮件订阅。使用「订阅邮件 新闻 xxx@qq.com」添加。")
        return
    lines = ["您当前的邮件订阅："]
    for s in subs:
        lines.append(f"  · {s.get('topic', '')} → {s.get('email', '')}")
    yield event.plain_result("\n".join(lines))
