from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent
from astrbot.api.star import Context


_SMART_REMINDER = None


def _build_ai_reminder_config(config: AstrBotConfig) -> dict:
    """
    从 astrbot_all_char 的配置中提取 sy_* 字段，构造原 ai_reminder 期望的配置字典。
    """
    return {
        "unique_session": getattr(config, "sy_unique_session", False),
        "wechat_platforms": getattr(config, "sy_wechat_platforms", ["gewechat", "wechatpadpro", "wecom"]),
        "whitelist": getattr(config, "sy_whitelist", ""),
        "enable_context": getattr(config, "sy_enable_context", True),
        "context_prompts": getattr(config, "sy_context_prompts", ""),
        "max_context_count": getattr(config, "sy_max_context_count", 5),
        "enable_reminder_at": getattr(config, "sy_enable_reminder_at", True),
        "enable_task_at": getattr(config, "sy_enable_task_at", True),
        "enable_command_at": getattr(config, "sy_enable_command_at", False),
        "hide_command_identifier": getattr(config, "sy_hide_command_identifier", False),
        "custom_command_prefix": getattr(config, "sy_custom_command_prefix", "/"),
        "max_reminders_per_user": getattr(config, "sy_max_reminders_per_user", 15),
        "max_command_wait_time": getattr(config, "sy_max_command_wait_time", 20),
        "inactive_timeout_hours": getattr(config, "sy_inactive_timeout_hours", 0),
    }


def _get_smart_reminder(context: Context, all_char_config: AstrBotConfig):
    """
    动态从原 `astrbot_plugin_sy-master` 加载 SmartReminder，并用映射后的配置初始化一个单例。

    注意：
    - 依赖目录 `astrbot_plugin_sy-master` 仍需存在；
    - 仅作为逻辑库使用，命令入口由 `astrbot_all_char` 统一接管。
    """
    global _SMART_REMINDER
    if _SMART_REMINDER is not None:
        return _SMART_REMINDER

    plugin_main = Path(__file__).resolve().parent.parent / "astrbot_plugin_sy-master" / "main.py"
    if not plugin_main.is_file():
        logger.error("未找到原定时任务插件目录：%s", plugin_main)
        raise RuntimeError("定时任务核心代码缺失，请保留 astrbot_plugin_sy-master 目录或后续再完全迁移代码。")

    spec = importlib.util.spec_from_file_location("all_char_ai_reminder", str(plugin_main))
    if spec is None or spec.loader is None:
        raise RuntimeError("无法加载 ai_reminder 模块 spec。")
    module = importlib.util.module_from_spec(spec)
    sys.modules["all_char_ai_reminder"] = module
    spec.loader.exec_module(module)  # type: ignore[arg-type]

    from all_char_ai_reminder import SmartReminder  # type: ignore[import]

    ai_conf = _build_ai_reminder_config(all_char_config)
    _SMART_REMINDER = SmartReminder(context, ai_conf)
    logger.info("已在 astrbot_all_char 中初始化 SmartReminder（通过原插件逻辑）")
    return _SMART_REMINDER


async def handle_simple_reminder(event: AstrMessageEvent, context: Context, config: AstrBotConfig):
    """
    简易提醒命令入口：/提醒 <时间> <内容>

    支持示例：
    - /提醒 3分钟后 喝水
    - /提醒 3分钟后提醒我喝水   （会自动按“后”分割）
    - /提醒 2026-02-28-08:00 开会
    - /提醒 08:30 上班打卡
    """
    try:
        smart = _get_smart_reminder(context, config)
    except Exception as e:
        logger.error("初始化 SmartReminder 失败: %s", e)
        yield event.plain_result("⏰ 定时任务核心加载失败，请检查 astrbot_plugin_sy-master 目录是否存在。")
        return

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

    # 复用原插件的 ReminderTools.set_reminder（会使用 parse_datetime_for_llm 支持“3分钟后”等）
    try:
        msg = await smart.tools.set_reminder(
            event,
            text=text,
            datetime_str=time_str,
            user_name=event.get_sender_name() or "用户",
            repeat=None,
            holiday_type=None,
            group_id=None,
            target_is_group="yes",
        )
        yield event.plain_result(msg)
    except Exception as e:
        logger.error("通过 /提醒 设置定时失败: %s", e)
        yield event.plain_result(f"设置提醒时出错：{e}")


async def handle_sy_rmd_group(event: AstrMessageEvent, context: Context, config: AstrBotConfig):
    """
    智能定时任务 /rmd 与 /rmdg 命令入口。

    - 复用原 `ai_reminder` 的完整逻辑（提醒 / 任务 / 指令任务 / 远程群聊等）；
    - 这里只负责解析子命令并转发给 SmartReminder.commands。
    """
    try:
        smart = _get_smart_reminder(context, config)
    except Exception as e:
        logger.error("初始化 SmartReminder 失败: %s", e)
        yield event.plain_result("⏰ 定时任务核心加载失败，请检查 astrbot_plugin_sy-master 目录是否存在。")
        return

    msg = event.get_message_str().strip()
    parts = msg.split()
    if len(parts) < 2:
        # 没有子命令，直接展示帮助
        async for r in smart.commands.show_help(event):
            yield r
        return

    root = parts[0].lstrip("/").lower()
    sub = parts[1].lower()
    args = parts[2:]

    # 本地 /rmd 命令
    if root == "rmd":
        if sub in ("ls",):
            async for r in smart.commands.list_reminders(event):
                yield r
        elif sub == "rm":
            if not args:
                yield event.plain_result("用法：/rmd rm <序号>")
                return
            try:
                index = int(args[0])
            except ValueError:
                yield event.plain_result("序号必须是数字。")
                return
            async for r in smart.commands.remove_reminder(event, index):
                yield r
        elif sub == "add":
            if len(args) < 2:
                yield event.plain_result("用法：/rmd add <内容> <时间> [开始星期] [重复类型] [节假日类型]")
                return
            text = args[0]
            time_str = args[1]
            week = args[2] if len(args) > 2 else None
            repeat = args[3] if len(args) > 3 else None
            holiday_type = args[4] if len(args) > 4 else None
            async for r in smart.commands.add_reminder(event, text, time_str, week, repeat, holiday_type):
                yield r
        elif sub == "task":
            if len(args) < 2:
                yield event.plain_result("用法：/rmd task <内容> <时间> [开始星期] [重复类型] [节假日类型]")
                return
            text = args[0]
            time_str = args[1]
            week = args[2] if len(args) > 2 else None
            repeat = args[3] if len(args) > 3 else None
            holiday_type = args[4] if len(args) > 4 else None
            async for r in smart.commands.add_task(event, text, time_str, week, repeat, holiday_type):
                yield r
        elif sub == "command":
            if len(args) < 2:
                yield event.plain_result("用法：/rmd command <指令> <时间> [开始星期] [重复类型] [节假日类型]")
                return
            command = args[0]
            time_str = args[1]
            week = args[2] if len(args) > 2 else None
            repeat = args[3] if len(args) > 3 else None
            holiday_type = args[4] if len(args) > 4 else None
            async for r in smart.commands.add_command_task(event, command, time_str, week, repeat, holiday_type):
                yield r
        elif sub == "help":
            async for r in smart.commands.show_help(event):
                yield r
        elif sub == "expire":
            if len(args) < 2:
                yield event.plain_result("用法：/rmd expire <序号> <时间>")
                return
            try:
                index = int(args[0])
            except ValueError:
                yield event.plain_result("序号必须是数字。")
                return
            time_str = args[1]
            async for r in smart.commands.set_expire(event, index, time_str):
                yield r
        elif sub == "unexpire":
            if not args:
                yield event.plain_result("用法：/rmd unexpire <序号>")
                return
            try:
                index = int(args[0])
            except ValueError:
                yield event.plain_result("序号必须是数字。")
                return
            async for r in smart.commands.remove_expire(event, index):
                yield r
        else:
            async for r in smart.commands.show_help(event):
                yield r

    # 远程群聊 /rmdg 命令
    elif root == "rmdg":
        if sub == "add":
            if len(args) < 3:
                yield event.plain_result("用法：/rmdg add <群聊ID> <内容> <时间> [开始星期] [重复类型] [节假日类型]")
                return
            group_id = args[0]
            text = args[1]
            time_str = args[2]
            week = args[3] if len(args) > 3 else None
            repeat = args[4] if len(args) > 4 else None
            holiday_type = args[5] if len(args) > 5 else None
            async for r in smart.commands.add_remote_reminder(event, group_id, text, time_str, week, repeat, holiday_type):
                yield r
        elif sub == "task":
            if len(args) < 3:
                yield event.plain_result("用法：/rmdg task <群聊ID> <内容> <时间> [开始星期] [重复类型] [节假日类型]")
                return
            group_id = args[0]
            text = args[1]
            time_str = args[2]
            week = args[3] if len(args) > 3 else None
            repeat = args[4] if len(args) > 4 else None
            holiday_type = args[5] if len(args) > 5 else None
            async for r in smart.commands.add_remote_task(event, group_id, text, time_str, week, repeat, holiday_type):
                yield r
        elif sub == "command":
            if len(args) < 3:
                yield event.plain_result("用法：/rmdg command <群聊ID> <指令> <时间> [开始星期] [重复类型] [节假日类型]")
                return
            group_id = args[0]
            command = args[1]
            time_str = args[2]
            week = args[3] if len(args) > 3 else None
            repeat = args[4] if len(args) > 4 else None
            holiday_type = args[5] if len(args) > 5 else None
            async for r in smart.commands.add_remote_command_task(
                event, group_id, command, time_str, week, repeat, holiday_type
            ):
                yield r
        elif sub == "help":
            async for r in smart.commands.show_remote_help(event):
                yield r
        elif sub == "ls":
            if not args:
                yield event.plain_result("用法：/rmdg ls <群聊ID>")
                return
            group_id = args[0]
            async for r in smart.commands.list_remote_reminders(event, group_id):
                yield r
        elif sub == "rm":
            if len(args) < 2:
                yield event.plain_result("用法：/rmdg rm <群聊ID> <序号>")
                return
            group_id = args[0]
            try:
                index = int(args[1])
            except ValueError:
                yield event.plain_result("序号必须是数字。")
                return
            async for r in smart.commands.remove_remote_reminder(event, group_id, index):
                yield r
        else:
            async for r in smart.commands.show_remote_help(event):
                yield r
    else:
        # 理论上不会走到这里
        async for r in smart.commands.show_help(event):
            yield r

    # 简单调用一次交互追踪，用于不活跃清理功能（不必严格依赖原装装饰器）
    try:
        await smart.track_interaction(event)
    except Exception as e:
        logger.debug("更新定时任务互动时间失败: %s", e)
