import asyncio
from datetime import datetime

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star
from pydantic import Field
from pydantic.dataclasses import dataclass

from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.agent.tool import FunctionTool, ToolExecResult
from astrbot.core.astr_agent_context import AstrAgentContext

from .train_utils import _fetch_trains, _format_train_text, handle_train_command, handle_train_help, handle_train_query
from .sy_scheduler_utils import (
    _parse_time_expression,
    handle_sy_rmd_group,
    handle_simple_reminder,
    init_simple_reminder_center,
)
from .stock_utils import (
    _fetch_quotes,
    _format_quotes,
    _normalize_code,
    _search_code_by_name,
    handle_stock_command,
)
from .fund_analysis_utils import handle_fund_command
from .weather_utils import (
    _query_weather_text,
    _resolve_weather_query,
    get_weather_runtime_config,
    handle_weather_command,
    handle_weather_help,
)
from .bookkeeping_utils import (
    get_bookkeeping_user_context,
    init_bookkeeping_module,
    handle_bookkeeping_expense,
    handle_bookkeeping_income,
    handle_bookkeeping_summary,
    handle_bookkeeping_daily,
    handle_bookkeeping_monthly,
    handle_bookkeeping_details,
    handle_bookkeeping_by_category,
    handle_bookkeeping_delete,
)
from .jrys_utils import handle_jrys_command, handle_jrys_last_command
from .ocr_utils import handle_ocr_command, _extract_image_from_event
from .anime_utils import (
    handle_animetrace_command,
    _get_animetrace_config,
    _prepare_file_field,
    _call_animetrace,
    _format_animetrace_result,
)
from .qianfan_search_utils import (
    _call_smart_search,
    _call_web_search,
    _get_qianfan_api_key,
    _get_smart_search_prompt_template,
    _get_web_search_prompt_template,
    _get_daily_counts,
    _increment_daily_count,
    handle_smart_search_command,
    handle_web_search_command,
)
from .config_utils import ensure_flat_config
from .music_utils import (
    handle_music_command,
    handle_music_number_selection,
    llm_play_music_by_keyword,
)
from .email_utils import (
    handle_send_email_command,
    handle_send_email_to_command,
    handle_send_email_to_in_message,
    handle_email_intent,
    send_email_sync,
    _get_email_config as get_email_config,
)
from .email_subscription_utils import (
    init_email_subscription_center,
    handle_subscribe_command,
    handle_unsubscribe_command,
    handle_list_subscriptions_command,
)
from .memory_utils import (
    configure_memory_admin_qq_ids,
    configure_memory_observe_user_throttle_seconds,
    configure_related_memory_cache_ttl_seconds,
    handle_memory_command,
    handle_who_am_i_command,
    init_user_memory_store,
)
from .passive_memory_utils import init_passive_memory_store
from .memory_panel_utils import handle_memory_panel_command, maybe_autostart_memory_panel
from .slang_utils import (
    build_slang_prompt_for_event,
    explain_slang_for_event,
    handle_slang_command,
    init_slang_store,
    observe_slang_message,
)
from .proactive_message_utils import (
    configure_proactive_admin_qq_ids,
    handle_proactive_message_command,
    init_proactive_message_center,
    is_proactive_admin_event,
    send_message_to_target,
)


def _get_effective_config(ctx: Context, plugin_config: AstrBotConfig | None) -> AstrBotConfig | dict:
    """
    获取实际可用的配置对象。

    优先使用插件在初始化时保存下来的 config；若不存在，则回退到全局 ctx.config 并做一次扁平化。
    """
    if plugin_config is not None:
        return plugin_config
    base_cfg = getattr(ctx, "config", None)
    if base_cfg is None:
        return {}
    return ensure_flat_config(base_cfg)


_MEMORY_RECALL_HINTS = (
    "还记得",
    "记不记得",
    "上次",
    "之前",
    "那次",
    "那天",
    "当时",
    "后来",
    "以前",
    "回忆",
)

_MEMORY_TEMPORAL_HINTS = (
    "今天",
    "昨天",
    "前天",
    "上周",
    "上个月",
    "去年",
)

_MEMORY_ACTION_HINTS = (
    "说过",
    "提过",
    "聊过",
    "发生",
    "一起",
    "见过",
    "去过",
)


def _to_int_in_range(value: object, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)  # type: ignore[arg-type]
    except Exception:
        parsed = default
    if parsed < minimum:
        return minimum
    if parsed > maximum:
        return maximum
    return parsed


def _trim_prompt_part_to_budget(text: str, budget_chars: int) -> str:
    content = str(text or "").strip()
    if not content or budget_chars <= 0:
        return ""
    if len(content) <= budget_chars:
        return content
    if budget_chars <= 24:
        return content[:budget_chars]

    clipped = content[:budget_chars]
    last_break = clipped.rfind("\n")
    if last_break >= int(budget_chars * 0.6):
        clipped = clipped[:last_break]
    suffix = "\n...(记忆内容已按预算截断)"
    hard_cap = max(0, budget_chars - len(suffix))
    clipped = clipped[:hard_cap].rstrip()
    return f"{clipped}{suffix}" if clipped else content[:budget_chars]


def _compose_memory_prompt_with_budget(named_parts: list[tuple[str, str]], max_chars: int) -> tuple[str, list[str], int]:
    part_caps = {
        "memory_prompt": 700,
        "passive_profile_prompt": 450,
        "slang_prompt": 260,
        "event_recall_prompt": 320,
        "related_prompt": 320,
        "reminiscence_prompt": 520,
    }
    used = 0
    included_names: list[str] = []
    assembled: list[str] = []
    for name, raw_text in named_parts:
        text = str(raw_text or "").strip()
        if not text:
            continue
        remaining = max_chars - used
        if remaining <= 0:
            break
        cap = min(remaining, part_caps.get(name, remaining))
        part = _trim_prompt_part_to_budget(text, cap)
        if not part:
            continue
        assembled.append(part)
        included_names.append(name)
        used += len(part)
    return "\n\n".join(assembled), included_names, used


def _should_trigger_reminiscence(
    message_text: str,
    *,
    related_prompt: str,
    min_message_length: int,
) -> bool:
    message = str(message_text or "").strip()
    if not message or message.startswith("/") or len(message) < min_message_length:
        return False
    if any(hint in message for hint in _MEMORY_RECALL_HINTS):
        return True
    if related_prompt:
        return True
    has_temporal = any(hint in message for hint in _MEMORY_TEMPORAL_HINTS)
    has_action = any(hint in message for hint in _MEMORY_ACTION_HINTS)
    return has_temporal and has_action


class _CmdWrappedEvent:
    """包装原始 event，使 get_message_str 返回伪造的指令字符串，便于在 LLM Tool 中复用现有指令 handler。"""

    __slots__ = ("_event", "_fake_message")

    def __init__(self, original: AstrMessageEvent, fake_message: str):
        self._event = original
        self._fake_message = fake_message.strip()

    def get_message_str(self) -> str:
        return self._fake_message

    def __getattr__(self, name: str):
        return getattr(self._event, name)


class AllCharPlugin(Star):
    """
    统一整合 char 系列插件的入口插件。

    约定：
    - 只在此处做元信息、命令注册和轻量初始化；
    - 具体逻辑全部下沉到对应 utils 模块中；
    - 以命令模式为主，同时提供一组可供 LLM 自动调用的工具（FunctionTool）。
    """

    # 供 LLM Tools 在没有直接拿到插件实例时复用配置
    _shared_config: AstrBotConfig | dict | None = None

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.context = context
        self.config = ensure_flat_config(config)
        AllCharPlugin._shared_config = self.config
        logger.info("astrbot_all_char 插件初始化完成（命令 + LLM 工具模式）")
        # 初始化简易提醒的持久化调度中心，保证重启后可自动恢复未到期提醒
        init_simple_reminder_center(self.context, self.config)
        # 初始化邮件订阅定时发送中心（持久化订阅 + 每日到点发送）
        init_email_subscription_center(self.context, self.config)
        # 初始化用户永久记忆存储
        init_user_memory_store()
        configure_memory_admin_qq_ids(getattr(self.config, "memory_admin_qq_ids", None))
        configure_proactive_admin_qq_ids(getattr(self.config, "proactive_admin_qq_ids", None))
        init_proactive_message_center(self.context, self.config)
        configure_memory_observe_user_throttle_seconds(
            getattr(self.config, "memory_observe_user_throttle_seconds", 120)
        )
        configure_related_memory_cache_ttl_seconds(
            getattr(self.config, "memory_related_memory_cache_ttl_seconds", 60)
        )
        init_passive_memory_store()
        init_slang_store()
        maybe_autostart_memory_panel(self.context, self.config)

        # 注册一批可供 Agent 自动调用的工具（类似 astrbot_plugin_payqr）
        try:
            self.context.add_llm_tools(
                StockQueryTool(),
                WeatherQueryTool(),
                SimpleReminderTool(),
                BookkeepingAddExpenseTool(),
                BookkeepingAddIncomeTool(),
                BookkeepingSummaryTool(),
                SmartSearchTool(),
                WebSearchTool(),
                AnimeTraceTool(),
                MusicPlayTool(),
                SendEmailTool(),
                SlangExplainTool(),
                ProactiveSendMessageTool(),
            )
            logger.info(
                "astrbot_all_char 已注册 LLM 工具：股票、天气、火车票、提醒、记账、智能搜索/网页搜索、动漫识别、点歌、发邮件、黑话解释、主动消息等"
            )
        except Exception as e:
            logger.error("astrbot_all_char 注册 LLM 工具失败: %s", e)

    # ---------------- 火车票 ----------------

    @filter.command("train", alias={"火车票", "车票", "查火车票"})
    async def cmd_train(self, event: AstrMessageEvent):
        async for result in handle_train_command(event, self.config):
            yield result

    @filter.command("help_train", alias={"火车票帮助"})
    async def cmd_train_help(self, event: AstrMessageEvent):
        async for result in handle_train_help(event):
            yield result

    # ---------------- 智能定时任务（/rmd 命令组入口） ----------------

    @filter.command("rmd", alias=set())
    async def cmd_rmd_entry(self, event: AstrMessageEvent):
        """
        兼容原 /rmd 命令组的入口。

        说明：原插件通过 command_group 定义子命令；
        在整合版中，统一从这里解析并委托给 sy_scheduler_utils 处理。
        """
        async for result in handle_sy_rmd_group(event, self.context, self.config):
            yield result

    @filter.command("rmdg", alias=set())
    async def cmd_rmdg_entry(self, event: AstrMessageEvent):
        """
        远程群聊提醒命令入口，对应原插件的 /rmdg 指令组。
        """
        async for result in handle_sy_rmd_group(event, self.context, self.config):
            yield result

    @filter.command("提醒", alias={"提醒事项"})
    async def cmd_simple_reminder(self, event: AstrMessageEvent):
        """
        简易提醒命令：/提醒 <时间> <内容>

        示例：
        - /提醒 3分钟后 喝水
        - /提醒 3分钟后提醒我喝水
        - /提醒 08:30 上班打卡
        """
        async for result in handle_simple_reminder(event, self.context, self.config):
            yield result

    @filter.command("提醒列表", alias={"我的提醒", "查看提醒"})
    async def cmd_simple_reminder_list(self, event: AstrMessageEvent):
        """
        查看当前用户在当前会话中的待执行提醒列表。
        """
        wrapped = _CmdWrappedEvent(event, "/提醒 列表")
        async for result in handle_simple_reminder(wrapped, self.context, self.config):
            yield result

    @filter.command("主动消息", alias={"主动发送", "主动推送"})
    async def cmd_proactive_message(self, event: AstrMessageEvent):
        async for result in handle_proactive_message_command(event, self.context, self.config):
            yield result

    # ---------------- 用户永久记忆 ----------------

    @filter.command("记忆", alias={"认人"})
    async def cmd_memory(self, event: AstrMessageEvent):
        async for result in handle_memory_command(event):
            yield result

    @filter.command("我是谁")
    async def cmd_who_am_i(self, event: AstrMessageEvent):
        async for result in handle_who_am_i_command(event):
            yield result

    @filter.command("记忆面板", alias={"memory_panel", "记忆管理"})
    async def cmd_memory_panel(self, event: AstrMessageEvent):
        async for result in handle_memory_panel_command(event, self.context, self.config):
            yield result

    @filter.command("黑话", alias={"黑话词典", "群黑话"})
    async def cmd_slang(self, event: AstrMessageEvent):
        async for result in handle_slang_command(event, self.config):
            yield result

    # ---------------- 股票 ----------------

    @filter.command("stock", alias={"股票", "自选股", "行情"})
    async def cmd_stock(self, event: AstrMessageEvent):
        # 避免长耗时股票分析在后台执行时，同一条消息又继续走自然语言链路。
        event.stop_event()
        async for result in handle_stock_command(event, self.context, self.config):
            yield result

    @filter.command("基金", alias={"fund"})
    async def cmd_fund(self, event: AstrMessageEvent):
        async for result in handle_fund_command(event, self.context):
            yield result

    # ---------------- 天气 ----------------

    @filter.command("nyweather", alias={"天气", "天气查询", "查天气"})
    async def cmd_weather(self, event: AstrMessageEvent):
        # 避免同一消息再走自然语言导致回复两次
        event.stop_event()  # 避免同一消息再走自然语言导致回复两次
        async for result in handle_weather_command(event, self.config):
            yield result

    @filter.command("help_nyweather", alias={"天气帮助"})
    async def cmd_weather_help(self, event: AstrMessageEvent):
        async for result in handle_weather_help(event):
            yield result

    # ---------------- OCR 图片识别 ----------------

    @filter.command("识别图片", alias={"ocr", "图片识别"})
    async def cmd_ocr(self, event: AstrMessageEvent):
        async for result in handle_ocr_command(event, self.config):
            yield result

    # ---------------- 动漫图片识别（AnimeTrace） ----------------

    @filter.command("搜番", alias={"识别动漫", "番剧识别", "动漫识别"})
    async def cmd_animetrace(self, event: AstrMessageEvent):
        """
        使用 AnimeTrace 识别动漫图片（番剧 / 角色 / 截图来源）。
        请在同一条消息中附带一张图片。
        """
        async for result in handle_animetrace_command(event, self.config):
            yield result

    @filter.llm_tool(name="anime_trace")
    async def tool_anime_trace(self, event: AstrMessageEvent, image: str | None = None):
        """识别动漫图片所属番剧/角色（调用 AnimeTrace）。

        使用建议（给 LLM 的决策规则）：
        - 当用户发来一张动漫截图/人物立绘，并询问「这是谁/出自哪部番/帮我搜番」时优先调用；
        - 若用户已提供图片 URL，则作为 image 参数传入；否则直接调用，本工具会自动从消息事件中取图；
        - 识别结果通常包含番剧标题、相似度、集数/时间点等，可在自然语言回复中进一步解释，但不要编造未返回的信息。

        Args:
            image(string): 图片的 URL 或本地路径（可选）。留空时自动从当前消息中提取第一张图片。
        """
        ctx_wrapper = ContextWrapper[AstrAgentContext](self.context)  # type: ignore[type-arg]
        tool = AnimeTraceTool()
        result = await tool.call(ctx_wrapper, image=image)
        yield event.plain_result(str(result))

    # ---------------- 记账 ----------------

    @filter.command("记账支出")
    async def cmd_book_expense(self, event: AstrMessageEvent):
        async for result in handle_bookkeeping_expense(event, self.context, self.config):
            yield result

    @filter.command("记账收入")
    async def cmd_book_income(self, event: AstrMessageEvent):
        async for result in handle_bookkeeping_income(event, self.context, self.config):
            yield result

    @filter.command("查账统计")
    async def cmd_book_summary(self, event: AstrMessageEvent):
        async for result in handle_bookkeeping_summary(event, self.context, self.config):
            yield result

    @filter.command("日统计")
    async def cmd_book_daily(self, event: AstrMessageEvent):
        async for result in handle_bookkeeping_daily(event, self.context, self.config):
            yield result

    @filter.command("月统计")
    async def cmd_book_monthly(self, event: AstrMessageEvent):
        async for result in handle_bookkeeping_monthly(event, self.context, self.config):
            yield result

    @filter.command("查账详情")
    async def cmd_book_details(self, event: AstrMessageEvent):
        async for result in handle_bookkeeping_details(event, self.context, self.config):
            yield result

    @filter.command("按类统计")
    async def cmd_book_by_category(self, event: AstrMessageEvent):
        async for result in handle_bookkeeping_by_category(event, self.context, self.config):
            yield result

    @filter.command("删除账单")
    async def cmd_book_delete(self, event: AstrMessageEvent):
        async for result in handle_bookkeeping_delete(event, self.context, self.config):
            yield result

    # ---------------- 今日运势 ----------------

    @filter.command("jrys", alias={"今日运势", "运势"})
    async def cmd_jrys(self, event: AstrMessageEvent):
        async for result in handle_jrys_command(event, self.context, self.config):
            yield result

    @filter.command("jrys_last")
    async def cmd_jrys_last(self, event: AstrMessageEvent):
        async for result in handle_jrys_last_command(event, self.context, self.config):
            yield result

    # ---------------- 百度千帆智能搜索 / 网页搜索 ----------------

    @filter.command("智能搜索", alias={"智能搜素"})
    async def cmd_smart_search(self, event: AstrMessageEvent):
        """
        /智能搜索 <问题>：千帆智能搜索后由当前 LLM 整理输出。每日限 100 次。
        """
        async for result in handle_smart_search_command(event, self.context, self.config):
            yield result

    @filter.command("搜索")
    async def cmd_web_search(self, event: AstrMessageEvent):
        """
        /搜索 <关键词>：千帆网页搜索后由当前 LLM 整理输出。每日限 1000 次。
        """
        async for result in handle_web_search_command(event, self.context, self.config):
            yield result

    # ---------------- 邮件发送（QQ 邮箱） ----------------

    @filter.command("发邮件", alias={"发送邮件"})
    async def cmd_send_email(self, event: AstrMessageEvent):
        """
        /发邮件 <收件人邮箱> <内容描述>
        使用 QQ 邮箱发送邮件，邮件主题与正文会由 LLM 基于描述生成。
        """
        async for result in handle_send_email_command(event, self.context, self.config):
            yield result

    @filter.command("发邮件到", alias={"发到邮箱", "发送到邮箱"})
    async def cmd_send_email_to(self, event: AstrMessageEvent):
        """
        发邮件到 <邮箱> <内容描述>：用 LLM 根据描述生成主题与正文并发送，走命令管道优先于主对话。
        示例：发邮件到 1102025067@qq.com 今天晚饭
        """
        async for result in handle_send_email_to_command(event, self.context, self.config):
            yield result

    @filter.regex(r"(?:发邮件到|发到邮箱|发送到邮箱|发送邮件\s*到)\s*[^\s@]+@[^\s@]+\.[^\s@]+\s+.+", priority=9999)
    async def cmd_send_email_to_in_message(self, event: AstrMessageEvent):
        """
        消息任意位置出现「发邮件到 邮箱 内容」或「发到邮箱 邮箱 内容」时解析并发信（如：xxx，发邮件到 xxx@qq.com 今天晚饭）。
        """
        async for result in handle_send_email_to_in_message(event, self.context, self.config):
            yield result

    @filter.regex(r"邮件", priority=9998)
    async def cmd_email_intent(self, event: AstrMessageEvent):
        """
        检测到消息含「邮件」且含邮箱地址时：用 LLM 生成主题与正文并发送。
        """
        async for result in handle_email_intent(event, self.context, self.config):
            yield result

    # ---------------- 邮件订阅（持久化 + 每日定时发送） ----------------

    @filter.command("订阅邮件", alias={"邮件订阅"})
    async def cmd_email_subscribe(self, event: AstrMessageEvent):
        """订阅邮件 <订阅项> <收件邮箱>，每日到点发送。示例：订阅邮件 新闻 xxx@qq.com"""
        async for result in handle_subscribe_command(event, self.context, self.config):
            yield result

    @filter.command("取消邮件订阅", alias={"邮件退订"})
    async def cmd_email_unsubscribe(self, event: AstrMessageEvent):
        """取消邮件订阅 <订阅项>。示例：取消邮件订阅 新闻"""
        async for result in handle_unsubscribe_command(event, self.config):
            yield result

    @filter.command("我的邮件订阅", alias={"邮件订阅列表"})
    async def cmd_email_subscription_list(self, event: AstrMessageEvent):
        """查看当前用户的邮件订阅列表。"""
        async for result in handle_list_subscriptions_command(event, self.config):
            yield result

    @filter.on_llm_request()
    async def inject_user_memory(self, event: AstrMessageEvent, req):
        """
        在普通聊天进入大模型前注入当前 QQ 的永久记忆。
        """
        store = init_user_memory_store()
        passive_store = init_passive_memory_store()
        qq_id = store.observe_user(event)
        message_text = ""
        try:
            message_text = str(event.get_message_str() or "").strip()
        except Exception:
            message_text = ""
        passive_store.observe_message(event)
        await observe_slang_message(self.context, event, self.config)

        memory_prompt_max_chars = _to_int_in_range(
            getattr(self.config, "memory_prompt_max_chars", 1800),
            default=1800,
            minimum=400,
            maximum=12000,
        )
        reminiscence_min_message_length = _to_int_in_range(
            getattr(self.config, "memory_reminiscence_min_message_length", 8),
            default=8,
            minimum=1,
            maximum=200,
        )

        memory_prompt = store.build_prompt_for_event(event)
        passive_profile_prompt = passive_store.build_profile_prompt(event)
        slang_prompt = build_slang_prompt_for_event(event, message_text, self.config)
        event_recall_enabled = bool(getattr(self.config, "memory_event_recall_enabled", True))
        event_recall_prompt = (
            passive_store.build_event_recall_prompt(
                event,
                message_text=message_text,
            )
            if event_recall_enabled
            else ""
        )
        related_prompt = store.build_related_memories_prompt(
            message_text,
            exclude_qq_ids={qq_id} if qq_id else None,
        )
        reminiscence_enabled = bool(getattr(self.config, "memory_reminiscence_enabled", True))
        should_trigger_reminiscence = reminiscence_enabled and _should_trigger_reminiscence(
            message_text,
            related_prompt=related_prompt,
            min_message_length=reminiscence_min_message_length,
        )
        reminiscence_prompt = (
            passive_store.build_reminiscence_bridge_prompt(
                self.context,
                event,
                message_text=message_text,
            )
            if should_trigger_reminiscence
            else ""
        )

        named_parts = [
            ("memory_prompt", memory_prompt),
            ("passive_profile_prompt", passive_profile_prompt),
            ("slang_prompt", slang_prompt),
            ("event_recall_prompt", event_recall_prompt),
            ("related_prompt", related_prompt),
            ("reminiscence_prompt", reminiscence_prompt),
        ]
        extra_prompt, included_parts, used_chars = _compose_memory_prompt_with_budget(
            named_parts,
            memory_prompt_max_chars,
        )
        if not extra_prompt:
            logger.info(
                "[astrbot_all_char] on_llm_request 未生成用户记忆提示词: sender_id=%s",
                qq_id or "unknown",
            )
            return

        base_prompt = getattr(req, "system_prompt", "") or ""
        joiner = "\n\n" if base_prompt else ""
        req.system_prompt = f"{base_prompt}{joiner}{extra_prompt}"
        logger.info(
            (
                "[astrbot_all_char] on_llm_request 记忆注入完成: sender_id=%s, included=%s, "
                "used_chars=%s/%s, event_recall=%s, reminiscence=%s, final_system_prompt_chars=%s"
            ),
            qq_id or "unknown",
            ",".join(included_parts),
            used_chars,
            memory_prompt_max_chars,
            event_recall_enabled,
            bool(reminiscence_prompt),
            len(req.system_prompt or ""),
        )

    # ---------------- LLM Tools（供 AI 自动调用） ----------------

    @filter.llm_tool(name="stock_query")
    async def tool_stock_query(self, event: AstrMessageEvent, query: str):
        """查询股票当前行情。

        Args:
            query(string): 股票代码（如 600519）或名称关键字（如 贵州茅台）。
        """
        logger.info("[astrbot_all_char][LLMTool] stock_query called: query=%s", query)
        # 复用 /股票 查询 的指令解析逻辑
        fake = f"/股票 查询 {query}"
        wrapped = _CmdWrappedEvent(event, fake)
        async for result in handle_stock_command(wrapped, self.context, self.config):
            yield result

    @filter.llm_tool(name="weather_query")
    async def tool_weather_query(
        self,
        event: AstrMessageEvent,
        city: str,
        days: str | None = None,
        when: str | None = None,
    ):
        """查询城市天气。

        Args:
            city(string): 城市名称，例如 北京。
            days(number): 预报天数（1-7，可选）。
            when(string): 自然语言时间，例如 今天、明天、后天、周一、下周三、未来5天。
        """
        logger.info(
            "[astrbot_all_char][LLMTool] 天气查询工具被调用，参数 city=%s, days=%s, when=%s",
            city,
            days,
            when,
        )
        day_int: int | None = None
        if days:
            try:
                d = int(str(days).strip())
                if 1 <= d <= 7:
                    day_int = d
            except ValueError:
                day_int = None

        parts = ["/天气", city]
        if when and str(when).strip():
            parts.append(str(when).strip())
        elif day_int is not None:
            parts.append(str(day_int))
        fake = " ".join(parts)
        wrapped = _CmdWrappedEvent(event, fake)
        async for result in handle_weather_command(wrapped, self.config):
            yield result

    @filter.llm_tool(name="train_query")
    async def tool_train_query(
        self,
        event: AstrMessageEvent,
        departure: str,
        arrival: str,
        travel_date: str | None = None,
    ):
        """查询两地之间的火车票/车次信息。

        使用建议（给 LLM 的决策规则）：
        - 用户询问两地之间的火车/车次/高铁/动车：优先调用本工具；
        - 若用户已明确出发地、目的地和日期（如“帮我查一下4月20号厦门到上海的火车”），请同时填充 `departure/arrival/travel_date`；
        - 该自然语言工具默认仅返回图片形式的车票结果；
        - 仅当用户只问“怎么坐火车”“高铁要多久”且没有具体城市时，再考虑纯聊天回答。

        Args:
            departure(string): 出发地城市或站点名称，例如 厦门。
            arrival(string): 目的地城市或站点名称，例如 上海。
            travel_date(string, optional): 出行日期，可传 `YYYY-MM-DD`、`YYYY年M月D日`、`M月D日/号`、`今天/明天/后天/大后天`。
        """
        logger.info(
            "[astrbot_all_char][LLMTool] 火车票查询工具被调用，参数 departure=%s, arrival=%s, travel_date=%s",
            departure,
            arrival,
            travel_date,
        )
        dep = (departure or "").strip()
        arr = (arrival or "").strip()
        date_text = (travel_date or "").strip()
        if not dep or not arr:
            yield event.plain_result("请同时提供出发地和目的地，例如：departure=厦门, arrival=上海。")
            return

        async for result in handle_train_query(
            event,
            self.config,
            dep,
            arr,
            date_text or None,
            format_override="image",
            image_only=True,
        ):
            yield result

    @filter.llm_tool(name="simple_reminder")
    async def tool_simple_reminder(
        self,
        event: AstrMessageEvent,
        time_expression: str,
        text: str,
    ):
        """设置一个简易定时提醒（等价于命令 /提醒）。

        使用建议（给 LLM 的决策规则）：
        - 用户说“几分钟后提醒我…/几点提醒我…”：优先调用本工具；
        - 可以在 tool_call 之前先帮用户把不规范的说法改成明确时间表达式和提醒内容；
        - 设置成功后，可以用自然语言再补一句“我已经帮你记下来了…”，但不要重复调用工具。

        Args:
            time_expression(string): 时间表达式，如“3分钟后”“2小时后”“2026-02-28-08:00”“08:30”等。
            text(string): 提醒内容，例如“喝水”“去开会”。
        """
        logger.info(
            "[astrbot_all_char][LLMTool] 简易提醒工具被调用，参数 time=%s, text=%s",
            time_expression,
            text,
        )
        time_str = (time_expression or "").strip()
        content = (text or "").strip()
        if not time_str or not content:
            yield event.plain_result(
                "用法示例：\n"
                "  - 3分钟后，提醒内容如「喝水」\n"
                "  - 2小时后，提醒内容如「去开会」\n"
                "  - 08:30，提醒内容如「上班打卡」"
            )
            return

        fake = f"/提醒 {time_str} {content}"
        wrapped = _CmdWrappedEvent(event, fake)
        async for result in handle_simple_reminder(wrapped, self.context, self.config):
            yield result

    @filter.llm_tool(name="bookkeeping_add_expense")
    async def tool_bookkeeping_add_expense(
        self,
        event: AstrMessageEvent,
        amount: float,
        description: str | None = None,
    ):
        """记录一笔支出并自动分类（等价于命令“记账支出 …”）。

        使用建议（给 LLM 的决策规则）：
        - 用户说“帮我记一笔…花了/消费了/付了多少钱”：优先调用本工具；
        - 请将金额解析为数字 `amount`，剩余自然语言作为 `description`；
        - 成功后可以用自然语言总结这笔支出，但不要重复调用工具。

        Args:
            amount(number): 支出金额，单位元，例如 35.5。
            description(string): 支出说明，例如“中午吃饭”“买菜”。可选。
        """
        logger.info(
            "[astrbot_all_char][LLMTool] 记账支出工具被调用，参数 amount=%s, desc=%s",
            amount,
            description,
        )
        try:
            amt = float(amount)
        except Exception:
            yield event.plain_result("金额格式不正确，请使用数字，例如 35.5。")
            return
        if amt <= 0:
            yield event.plain_result("金额必须大于 0。")
            return

        desc = (description or "").strip()
        fake = f"记账支出 {amt}"
        if desc:
            fake += f" {desc}"
        wrapped = _CmdWrappedEvent(event, fake)
        async for result in handle_bookkeeping_expense(wrapped, self.context, self.config):
            yield result

    @filter.llm_tool(name="bookkeeping_add_income")
    async def tool_bookkeeping_add_income(
        self,
        event: AstrMessageEvent,
        amount: float,
        description: str | None = None,
    ):
        """记录一笔收入并自动分类（等价于命令“记账收入 …”）。

        使用建议（给 LLM 的决策规则）：
        - 用户说“今天发工资了/收了红包/入账多少”：优先调用本工具；
        - 请将金额解析为数字 `amount`，剩余自然语言作为 `description`；
        - 成功后可以用自然语言总结这笔收入，但不要重复调用工具。

        Args:
            amount(number): 收入金额，单位元，例如 1000。
            description(string): 收入说明，例如“工资”“发红包”。可选。
        """
        logger.info(
            "[astrbot_all_char][LLMTool] 记账收入工具被调用，参数 amount=%s, desc=%s",
            amount,
            description,
        )
        try:
            amt = float(amount)
        except Exception:
            yield event.plain_result("金额格式不正确，请使用数字，例如 1000。")
            return
        if amt <= 0:
            yield event.plain_result("金额必须大于 0。")
            return

        desc = (description or "").strip()
        fake = f"记账收入 {amt}"
        if desc:
            fake += f" {desc}"
        wrapped = _CmdWrappedEvent(event, fake)
        async for result in handle_bookkeeping_income(wrapped, self.context, self.config):
            yield result

    @filter.llm_tool(name="bookkeeping_summary")
    async def tool_bookkeeping_summary(self, event: AstrMessageEvent):
        """查看当前用户的记账总收入、总支出和余额（等价于命令“查账统计”）。

        使用建议（给 LLM 的决策规则）：
        - 用户问“最近花了多少钱”“帮我看看账本总体情况”：优先调用本工具；
        - 获取结果后，可以在自然语言中进一步解释支出结构，但请基于工具返回内容，不要凭空编造。
        """
        logger.info("[astrbot_all_char][LLMTool] 记账统计工具被调用")
        fake = "查账统计"
        wrapped = _CmdWrappedEvent(event, fake)
        async for result in handle_bookkeeping_summary(wrapped, self.context, self.config):
            yield result

    @filter.llm_tool(name="smart_search")
    async def tool_smart_search(self, event: AstrMessageEvent, query: str):
        """调用百度千帆智能搜索（ai_search/chat/completions）查询复杂问题。

        使用建议（给 LLM 的决策规则）：
        - 当当前模型知识明显过旧/不确定，且需要联网查最新资料时，再调用本工具；
        - 查询结果返回后，再由当前 LLM 用人格语气整理输出（由工具内部完成）；
        - 千帆智能搜索每日本地上限为 100 次，用于较重的问题。

        Args:
            query(string): 要搜索的问题或主题。
        """
        logger.info("[astrbot_all_char][LLMTool] 智能搜索工具被调用，参数 query=%s", query)
        q = (query or "").strip()
        if not q:
            yield event.plain_result("请输入要搜索的问题。")
            return

        fake = f"/智能搜索 {q}"
        wrapped = _CmdWrappedEvent(event, fake)
        async for result in handle_smart_search_command(wrapped, self.context, self.config):
            yield result

    @filter.llm_tool(name="web_search")
    async def tool_web_search(self, event: AstrMessageEvent, query: str):
        """调用百度千帆网页搜索（ai_search/web_search）查询信息。

        使用建议（给 LLM 的决策规则）：
        - 用户需要多个网页结果综合的信息（如新闻、行情、资料汇总）时调用本工具；
        - 若只是简单知识问答、无需联网，可直接用自身知识回答；
        - 千帆网页搜索每日本地上限为 1000 次，适合频率较高但单次开销不大的查询。

        Args:
            query(string): 要搜索的关键词。
        """
        logger.info("[astrbot_all_char][LLMTool] 网页搜索工具被调用，参数 query=%s", query)
        q = (query or "").strip()
        if not q:
            yield event.plain_result("请输入要搜索的关键词。")
            return

        fake = f"/搜索 {q}"
        wrapped = _CmdWrappedEvent(event, fake)
        async for result in handle_web_search_command(wrapped, self.context, self.config):
            yield result

    # ---------------- 点歌命令与数字选择 ----------------

    @filter.command("点歌", alias={"music", "唱歌", "唱"})
    async def cmd_music(self, event: AstrMessageEvent):
        """
        点歌命令入口：/点歌 <关键词>

        示例：
        - /点歌 青花
        - /点歌 夜曲 周杰伦
        """
        await handle_music_command(event, self.context, self.config)

    @filter.regex(r"^\d+$", priority=999)
    async def music_number_selection(self, event: AstrMessageEvent):
        """
        仅在存在点歌会话时拦截纯数字回复，用于选择歌曲序号。
        """
        await handle_music_number_selection(event, self.context, self.config)

    # ---------------- LLM Tool：点歌 ----------------

    @filter.llm_tool(name="music_play")
    async def tool_music_play(self, event: AstrMessageEvent, keyword: str):
        """按关键词点歌，自动选择最匹配的一首并返回播放链接。

        使用建议（给 LLM 的决策规则）：
        - 用户说「帮我点一首歌/放一下 XXX」时优先调用；
        - 关键词可包含歌名与歌手，例如「夜曲 周杰伦」；
        - 工具会返回一段包含歌曲信息与音频 URL 的文本，前端可据此发起真实播放。

        Args:
            keyword(string): 歌曲名称或相关关键词，例如「青花」或「夜曲 周杰伦」。
        """
        ctx_wrapper = ContextWrapper[AstrAgentContext](self.context)  # type: ignore[type-arg]
        tool = MusicPlayTool()
        result = await tool.call(ctx_wrapper, event=event, keyword=keyword)
        yield event.plain_result(str(result))

    @filter.llm_tool(name="send_email")
    async def tool_send_email(
        self,
        event: AstrMessageEvent,
        to_addr: str,
        subject: str,
        body: str,
    ):
        """发送邮件到指定收件人（使用配置的 QQ 邮箱与授权码）。

        禁止：仅在回复中说「已发送/发过去了」而不调用本工具——那样用户收不到邮件。必须实际调用 send_email 传入 to_addr、subject、body。
        使用建议（给 LLM 的决策规则）：
        - 用户说「发到 xxx@qq.com」「发送到 xxx 邮件」「发到我邮箱」时，必须先查/整理好内容，再调用本工具发送；不得只回复「已经发送了」却不调用工具。
        - 参数：to_addr=用户给的邮箱，subject=简短主题，body=要发送的完整正文（如天气内容、快闪店汇总等）。

        Args:
            to_addr(string): 收件人邮箱地址，例如 1102025067@qq.com。
            subject(string): 邮件主题。
            body(string): 邮件正文内容。
        """
        ctx_wrapper = ContextWrapper[AstrAgentContext](self.context)  # type: ignore[type-arg]
        tool = SendEmailTool()
        result = await tool.call(ctx_wrapper, to_addr=to_addr, subject=subject, body=body)
        yield event.plain_result(str(result))

    @filter.llm_tool(name="slang_explain")
    async def tool_slang_explain(self, event: AstrMessageEvent, term: str):
        """解释当前群聊/当前私聊里的一条黑话。

        Args:
            term(string): 要查询的黑话词条，例如 yyds。
        """
        yield event.plain_result(explain_slang_for_event(event, term))


@dataclass
class StockQueryTool(FunctionTool[AstrAgentContext]):
    """
    查询股票当前行情的 LLM 工具。

    复用股票模块的代码，通过代码或名称关键字查询单只股票的实时行情。
    """

    name: str = "stock_query"
    description: str = (
        "查询股票当前行情。支持通过股票代码（如 600519）或名称关键字（如 贵州茅台）进行查询。"
    )
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "股票代码（如 600519）或名称关键字（如 贵州茅台）。",
                }
            },
            "required": ["query"],
        }
    )

    async def call(
        self,
        context: ContextWrapper[AstrAgentContext],
        query: str,
        **kwargs,
    ) -> ToolExecResult:
        q = (query or "").strip()
        if not q:
            return "请提供要查询的股票代码或名称，例如 600519 或 贵州茅台。"

        # 优先按代码解析，其次按名称关键字搜索
        code = _normalize_code(q)
        codes: list[str] = []
        if code:
            codes.append(code)
        else:
            matches = await _search_code_by_name(q, max_results=5)
            if not matches:
                return f"未找到名称包含「{q}」的股票，请改用股票代码再试。"
            if len(matches) > 1:
                lines = ["找到多只匹配的股票，请让用户改用股票代码精确查询："]
                for m in matches:
                    lines.append(f"  • {m['name']}（{m['code']}）")
                return "\n".join(lines)
            codes.append(matches[0]["code"])

        quotes = await _fetch_quotes(codes)
        if not quotes:
            return "暂无行情数据或查询失败，请稍后重试。"
        return _format_quotes(quotes, "股票行情")


@dataclass
class WeatherQueryTool(FunctionTool[AstrAgentContext]):
    """
    查询城市天气的 LLM 工具。
    """

    name: str = "weather_query"
    description: str = "查询指定城市天气，支持未来 1-7 天预报，也支持今天、明天、后天、周几等自然语言时间。"
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "city": {
                    "type": "string",
                    "description": "城市名称，例如 北京。",
                },
                "days": {
                    "type": "integer",
                    "description": "天气预报天数（1-7，可选）。和 when 二选一即可。",
                    "minimum": 1,
                    "maximum": 7,
                },
                "when": {
                    "type": "string",
                    "description": "自然语言时间（可选），例如 今天、明天、后天、周一、下周三、未来5天。",
                },
            },
            "required": ["city"],
        }
    )

    async def call(
        self,
        context: ContextWrapper[AstrAgentContext],
        city: str,
        days: int | None = None,
        when: str | None = None,
        **kwargs,
    ) -> ToolExecResult:
        ctx = context.context.context
        cfg = _get_effective_config(ctx, AllCharPlugin._shared_config)  # type: ignore[arg-type]

        c = (city or "").strip()
        if not c:
            return "请提供要查询的城市名称，例如 北京。"

        query_spec, error_message = _resolve_weather_query(c, days=days, when_text=when)
        if not query_spec:
            return error_message or "天气查询参数错误。"

        resolved_city = str(query_spec["city"] or "").strip()
        forecast_days = int(query_spec["forecast_days"] or 1)
        target_day_offset = query_spec["target_day_offset"]
        target_day_label = str(query_spec["target_day_label"] or "").strip() or None

        weather_cfg = get_weather_runtime_config(cfg)
        text = await _query_weather_text(
            weather_cfg["api_url"],
            resolved_city,
            forecast_days,
            geocoding_api_url=weather_cfg.get("geocoding_api_url"),
            target_day_offset=target_day_offset if isinstance(target_day_offset, int) else None,
            target_day_label=target_day_label,
        )
        if not text:
            provider = weather_cfg.get("provider", "weather")
            if provider == "open-meteo":
                return (
                    "天气查询失败或无数据。\n"
                    "当前默认使用 Open-Meteo；如果是重名城市，"
                    "建议把城市写成“城市, 国家代码”，例如 `Paris, FR`。"
                )
            return (
                "天气查询失败或无数据。\n"
                "若你接的是旧版自定义天气 API，请确认对应接口地址配置正确。"
            )

        if target_day_label:
            title = f"📍 {resolved_city} {target_day_label}天气"
        else:
            title = (
                f"📍 {resolved_city}天气"
                if forecast_days <= 1
                else f"📍 {resolved_city} 未来{forecast_days}天天气预报"
            )
        return f"{title}\n\n{text}"


@dataclass
class TrainQueryTool(FunctionTool[AstrAgentContext]):
    """
    火车票查询 LLM 工具。
    """

    name: str = "train_query"
    description: str = "查询两地之间的火车票/车次信息。"
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "departure": {
                    "type": "string",
                    "description": "出发地城市或站点名称，例如 厦门。",
                },
                "arrival": {
                    "type": "string",
                    "description": "目的地城市或站点名称，例如 上海。",
                },
                "travel_date": {
                    "type": "string",
                    "description": "出行日期（可选），支持 YYYY-MM-DD、YYYY年M月D日、M月D日/号、今天/明天/后天/大后天。",
                },
            },
            "required": ["departure", "arrival"],
        }
    )

    async def call(
        self,
        context: ContextWrapper[AstrAgentContext],
        departure: str,
        arrival: str,
        travel_date: str | None = None,
        **kwargs,
    ) -> ToolExecResult:
        ctx = context.context.context
        cfg = _get_effective_config(ctx, AllCharPlugin._shared_config)  # type: ignore[arg-type]

        dep = (departure or "").strip()
        arr = (arrival or "").strip()
        date_text = (travel_date or "").strip()
        if not dep or not arr:
            return "请同时提供出发地和目的地，例如：出发地=厦门，目的地=上海。"

        api_url = (getattr(cfg, "train_api_url", None) or "https://kyfw.12306.cn/otn").rstrip(
            "/"
        )
        try:
            api_data = await _fetch_trains(api_url, dep, arr, date_text or None)
        except ValueError as exc:
            return str(exc)
        if not api_data:
            return "火车票查询失败或无数据，请检查出发地/目的地是否正确，或稍后重试。"

        text = _format_train_text(api_data)
        query_date = str(api_data.get("date", "")).strip()
        title_suffix = f"（{query_date}）" if query_date else ""
        return f"🚆 火车票查询：{dep} → {arr}{title_suffix}\n\n{text}"


@dataclass
class SimpleReminderTool(FunctionTool[AstrAgentContext]):
    """
    简易提醒 LLM 工具（等价于 /提醒 命令）。
    """

    name: str = "simple_reminder"
    description: str = (
        "设置一个简易定时提醒，相当于用户发送「/提醒 3分钟后 喝水」这类命令。"
    )
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "time_expression": {
                    "type": "string",
                    "description": "时间表达式，如「3分钟后」「2小时后」「2026-02-28-08:00」「08:30」。",
                },
                "text": {
                    "type": "string",
                    "description": "提醒内容，例如「喝水」「去开会」。",
                },
            },
            "required": ["time_expression", "text"],
        }
    )

    async def call(
        self,
        context: ContextWrapper[AstrAgentContext],
        time_expression: str,
        text: str,
        **kwargs,
    ) -> ToolExecResult:
        ctx = context.context.context
        event = context.context.event
        cfg = _get_effective_config(ctx, AllCharPlugin._shared_config)  # type: ignore[arg-type]

        time_str = (time_expression or "").strip()
        content = (text or "").strip()
        if not time_str or not content:
            return (
                "用法示例：\n"
                "  - 3分钟后，提醒内容如「喝水」\n"
                "  - 2小时后，提醒内容如「去开会」\n"
                "  - 08:30，提醒内容如「上班打卡」"
            )

        now = datetime.now()
        target = _parse_time_expression(now, time_str)
        if target is None:
            return (
                "暂时只支持以下时间格式：\n"
                "- N分钟后（如：3分钟后）\n"
                "- N小时后（如：2小时后）\n"
                "- 绝对时间：2026-02-28-08:00\n"
                "- 当天时间：08:30（若已过则顺延到明天）"
            )

        session_id = getattr(event, "unified_msg_origin", None) or getattr(event, "session_id", "")
        if not session_id:
            return "❌ 无法获取当前会话，定时提醒不可用。"

        creator_id = None
        creator_name = None
        try:
            if hasattr(event, "get_sender_id"):
                creator_id = event.get_sender_id()
            if hasattr(event, "get_sender_name"):
                creator_name = event.get_sender_name()
        except Exception:
            creator_id = creator_id or None
            creator_name = creator_name or None

        center = init_simple_reminder_center(ctx, cfg)
        if center is None:
            return (
                "❌ 当前环境未安装 apscheduler，简易提醒的持久化定时不可用。"
                "请在运行环境中安装 apscheduler 后再试。"
            )

        await center.add_reminder(session_id, creator_id, creator_name, content, target)
        target_str = target.strftime("%Y-%m-%d %H:%M:%S")
        return f"✅ 已设置提醒：{target_str} 提醒你「{content}」。\n（重启后也会继续生效）"


@dataclass
class BookkeepingAddExpenseTool(FunctionTool[AstrAgentContext]):
    """
    记账支出 LLM 工具。
    """

    name: str = "bookkeeping_add_expense"
    description: str = "记录一笔支出，自动按描述智能分类。"
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "amount": {
                    "type": "number",
                    "description": "支出金额，单位为元，例如 35.5。",
                },
                "description": {
                    "type": "string",
                    "description": "支出说明，例如「中午吃饭」「买菜」。可选。",
                },
            },
            "required": ["amount"],
        }
    )

    async def call(
        self,
        context: ContextWrapper[AstrAgentContext],
        amount: float,
        description: str | None = None,
        **kwargs,
    ) -> ToolExecResult:
        ctx = context.context.context
        event = context.context.event
        cfg = _get_effective_config(ctx, AllCharPlugin._shared_config)  # type: ignore[arg-type]

        try:
            amt = float(amount)
        except Exception:
            return "金额格式不正确，请使用数字，例如 35.5。"
        if amt <= 0:
            return "金额必须大于 0。"

        desc = (description or "").strip()

        module = init_bookkeeping_module(ctx, cfg)
        user_key, user_name = get_bookkeeping_user_context(event)
        umo = getattr(event, "unified_msg_origin", "")

        category = await module._ai_classify_category(  # type: ignore[attr-defined]
            "expense",
            amt,
            desc,
            umo,
        )
        await module._save_record(  # type: ignore[attr-defined]
            user_key,
            "expense",
            category,
            amt,
            desc,
            legacy_names=(user_name,),
        )

        response = (
            "✅ 记账成功！\n"
            f"类型: 支出\n"
            f"类别: {category}\n"
            f"金额: ¥{amt:.2f}\n"
            f"时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        )
        if desc:
            response += f"\n描述: {desc}"
        return response


@dataclass
class BookkeepingAddIncomeTool(FunctionTool[AstrAgentContext]):
    """
    记账收入 LLM 工具。
    """

    name: str = "bookkeeping_add_income"
    description: str = "记录一笔收入，自动按描述智能分类。"
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "amount": {
                    "type": "number",
                    "description": "收入金额，单位为元，例如 1000。",
                },
                "description": {
                    "type": "string",
                    "description": "收入说明，例如「工资」「发红包」。可选。",
                },
            },
            "required": ["amount"],
        }
    )

    async def call(
        self,
        context: ContextWrapper[AstrAgentContext],
        amount: float,
        description: str | None = None,
        **kwargs,
    ) -> ToolExecResult:
        ctx = context.context.context
        event = context.context.event
        cfg = _get_effective_config(ctx, AllCharPlugin._shared_config)  # type: ignore[arg-type]

        try:
            amt = float(amount)
        except Exception:
            return "金额格式不正确，请使用数字，例如 1000。"
        if amt <= 0:
            return "金额必须大于 0。"

        desc = (description or "").strip()

        module = init_bookkeeping_module(ctx, cfg)
        user_key, user_name = get_bookkeeping_user_context(event)
        umo = getattr(event, "unified_msg_origin", "")

        category = await module._ai_classify_category(  # type: ignore[attr-defined]
            "income",
            amt,
            desc,
            umo,
        )
        await module._save_record(  # type: ignore[attr-defined]
            user_key,
            "income",
            category,
            amt,
            desc,
            legacy_names=(user_name,),
        )

        response = (
            "✅ 记账成功！\n"
            f"类型: 收入\n"
            f"类别: {category}\n"
            f"金额: ¥{amt:.2f}\n"
            f"时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        )
        if desc:
            response += f"\n描述: {desc}"
        return response


@dataclass
class BookkeepingSummaryTool(FunctionTool[AstrAgentContext]):
    """
    查看账户总体统计的 LLM 工具。
    """

    name: str = "bookkeeping_summary"
    description: str = "查看当前用户的记账总收入、总支出和余额，并给出简要 AI 财务建议。"
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {},
            "required": [],
        }
    )

    async def call(
        self,
        context: ContextWrapper[AstrAgentContext],
        **kwargs,
    ) -> ToolExecResult:
        ctx = context.context.context
        event = context.context.event
        cfg = _get_effective_config(ctx, AllCharPlugin._shared_config)  # type: ignore[arg-type]

        module = init_bookkeeping_module(ctx, cfg)
        user_key, user_name = get_bookkeeping_user_context(event)
        records = module._load_records(user_key, legacy_names=(user_name,))  # type: ignore[attr-defined]
        if not records:
            return "📊 您还没有记账数据。"

        total_income = sum(r["amount"] for r in records if r["type"] == "income")
        total_expense = sum(r["amount"] for r in records if r["type"] == "expense")
        balance = total_income - total_expense

        summary = (
            f"📊 {user_name} 的账户统计\n"
            f"总收入: ¥{total_income:.2f}\n"
            f"总支出: ¥{total_expense:.2f}\n"
            f"余额: ¥{balance:.2f}\n"
            f"记录数: {len(records)}"
        )

        analysis_data = (
            "用户账户统计数据：\n"
            f"总收入: ¥{total_income:.2f}\n"
            f"总支出: ¥{total_expense:.2f}\n"
            f"余额: ¥{balance:.2f}\n"
            f"记录数: {len(records)}"
        )
        umo = getattr(event, "unified_msg_origin", "")
        ai_eval = await module._get_ai_evaluation(  # type: ignore[attr-defined]
            user_name,
            analysis_data,
            umo,
        )
        return summary + (ai_eval or "")


@dataclass
class SmartSearchTool(FunctionTool[AstrAgentContext]):
    """
    百度千帆智能搜索 LLM 工具。
    """

    name: str = "smart_search"
    description: str = "使用百度千帆智能搜索（ai_search/chat/completions）查询复杂问题，并整理为自然语言回答。"
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "要搜索的问题或主题，例如「介绍一下 GPT 模型的原理」。",
                }
            },
            "required": ["query"],
        }
    )

    async def call(
        self,
        context: ContextWrapper[AstrAgentContext],
        query: str,
        **kwargs,
    ) -> ToolExecResult:
        ctx = context.context.context
        event = context.context.event
        cfg = _get_effective_config(ctx, AllCharPlugin._shared_config)  # type: ignore[arg-type]

        api_key = _get_qianfan_api_key(cfg)
        if not api_key:
            return "未配置百度千帆 API Key。请在插件配置中填写「千帆 API Key」。"

        q = (query or "").strip()
        if not q:
            return "请输入要搜索的问题。"

        smart_count, _ = await _get_daily_counts()
        from .qianfan_search_utils import DAILY_LIMIT_SMART  # 避免循环引用问题

        if smart_count >= DAILY_LIMIT_SMART:
            return f"今日智能搜索已达本地上限（{DAILY_LIMIT_SMART} 次），明日再试。"

        raw_result = await _call_smart_search(api_key, q)
        if raw_result is None:
            return "智能搜索请求失败或未返回结果，请稍后重试。"
        await _increment_daily_count("smart_search")

        # 交给当前会话的 LLM 再整理一次，使风格与人格一致
        umo = getattr(event, "unified_msg_origin", None) or ""
        try:
            provider_id = await ctx.get_current_chat_provider_id(umo=umo)
            template = _get_smart_search_prompt_template(cfg)
            prompt = template.replace("{smart_search_result}", raw_result)
            llm_resp = await ctx.llm_generate(
                chat_provider_id=provider_id,
                prompt=prompt,
            )
            out = (llm_resp.completion_text or "").strip()
            return out or "（未生成有效回复）"
        except Exception:
            # 如果整理失败，直接返回原始结果
            return raw_result


@dataclass
class WebSearchTool(FunctionTool[AstrAgentContext]):
    """
    百度千帆网页搜索 LLM 工具。
    """

    name: str = "web_search"
    description: str = "使用百度千帆网页搜索（ai_search/web_search）查询信息，并整理为自然语言回答。"
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "要搜索的关键词，例如「今天的 A 股指数」。",
                }
            },
            "required": ["query"],
        }
    )

    async def call(
        self,
        context: ContextWrapper[AstrAgentContext],
        query: str,
        **kwargs,
    ) -> ToolExecResult:
        ctx = context.context.context
        event = context.context.event
        cfg = _get_effective_config(ctx, AllCharPlugin._shared_config)  # type: ignore[arg-type]

        api_key = _get_qianfan_api_key(cfg)
        if not api_key:
            return "未配置百度千帆 API Key。请在插件配置中填写「千帆 API Key」。"

        q = (query or "").strip()
        if not q:
            return "请输入要搜索的关键词。"

        _, web_count = await _get_daily_counts()
        from .qianfan_search_utils import DAILY_LIMIT_WEB  # 避免循环引用问题

        if web_count >= DAILY_LIMIT_WEB:
            return f"今日网页搜索已达本地上限（{DAILY_LIMIT_WEB} 次），明日再试。"

        search_text = await _call_web_search(api_key, q)
        if not search_text:
            return "网页搜索请求失败或未返回结果，请稍后重试。"
        await _increment_daily_count("web_search")

        umo = getattr(event, "unified_msg_origin", None) or ""
        try:
            provider_id = await ctx.get_current_chat_provider_id(umo=umo)
            template = _get_web_search_prompt_template(cfg)
            prompt = template.replace("{query}", q).replace("{search_results}", search_text)
            llm_resp = await ctx.llm_generate(
                chat_provider_id=provider_id,
                prompt=prompt,
            )
            out = (llm_resp.completion_text or "").strip()
            return out or "（未生成有效回复）"
        except Exception:
            # 如果整理失败，直接返回原始结果
            return search_text


@dataclass
class AnimeTraceTool(FunctionTool[AstrAgentContext]):
    """
    动漫图片识别 LLM 工具（AnimeTrace）。

    使用建议（给 LLM 的决策规则）：
    - 用户给出一张动漫截图/角色立绘并问「这是谁」「出自哪部番」「帮我搜番」等问题时调用；
    - 若用户提供了图片 URL，可直接填入 image 参数；否则本工具会尝试从当前会话最近一条带图消息中取图；
    - 结果通常包含番剧标题、相似度、集数/时间点和预览图链接，可在自然语言中转述和解释，但不要编造接口未返回的信息。
    """

    name: str = "anime_trace"
    description: str = "识别动漫图片所属番剧、角色等信息（调用 AnimeTrace API）。"
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "image": {
                    "type": "string",
                    "description": "要识别的图片 URL 或本地路径；留空时会自动从当前消息中提取第一张图片。",
                },
                # 目前服务端对布尔/枚举参数校验较严格，工具层先不暴露 is_multi/ai_detect，避免类型不兼容导致 400。
                "model": {
                    "type": "string",
                    "description": "AnimeTrace 识别模型名称；不填时使用插件配置 animetrace_model。",
                },
            },
            "required": [],
        }
    )

    async def call(
        self,
        context: ContextWrapper[AstrAgentContext],
        image: str | None = None,
        model: str | None = None,
        **kwargs,
    ) -> ToolExecResult:
        ctx = context.context.context
        event = context.context.event
        cfg = _get_effective_config(ctx, AllCharPlugin._shared_config)  # type: ignore[arg-type]

        # 1. 确定图片来源：优先使用参数，其次从事件中提取
        src = (image or "").strip()
        if not src:
            src = _extract_image_from_event(event) or ""
        if not src:
            return "未找到可识别的图片。请先发送一张动漫截图或在 image 参数中提供图片 URL/本地路径。"

        # 2. 读取 AnimeTrace 配置并应用参数覆盖（仅覆盖 model，其他参数使用服务端默认）
        anim_cfg = _get_animetrace_config(cfg)
        if model:
            anim_cfg["model"] = model.strip() or anim_cfg["model"]

        file_field = await _prepare_file_field(src)
        if not file_field:
            return "图片获取失败（无法读取或下载），请检查图片是否可访问。"

        # 仅传必需的 model 参数，避免 is_multi/ai_detect 类型差异导致 400。
        payload = {
            "model": anim_cfg["model"],
        }
        data = await _call_animetrace(anim_cfg["api_url"], payload, file_field)
        if not data:
            return "AnimeTrace 识别失败，请稍后重试或检查网络。"

        text = _format_animetrace_result(data)
        max_len = 4000
        if len(text) > max_len:
            text = text[:max_len] + "\n\n（结果过长，已截断显示。）"
        return text


@dataclass
class MusicPlayTool(FunctionTool[AstrAgentContext]):
    """
    点歌 LLM 工具：根据关键词选择一首歌并返回播放链接。
    """

    name: str = "music_play"
    description: str = "根据歌曲名称或关键词点歌，自动选择最匹配的一首并返回播放链接。"
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "keyword": {
                    "type": "string",
                    "description": "歌曲名称或相关关键词，例如「青花」或「夜曲 周杰伦」。",
                }
            },
            "required": ["keyword"],
        }
    )

    async def call(
        self,
        context: ContextWrapper[AstrAgentContext],
        keyword: str,
        event: AstrMessageEvent | None = None,
        **kwargs,
    ) -> ToolExecResult:
        ctx = context.context.context
        cfg = _get_effective_config(ctx, AllCharPlugin._shared_config)  # type: ignore[arg-type]
        # 优先使用当前事件（若存在），以便在 QQ 等平台发送 node / 语音 / 文件等多模态消息
        real_event = event or context.context.event
        text = await llm_play_music_by_keyword(ctx, cfg, keyword, event=real_event)
        return text


@dataclass
class SendEmailTool(FunctionTool[AstrAgentContext]):
    """
    使用配置的 QQ 邮箱发送邮件的 LLM 工具。
    """

    name: str = "send_email"
    description: str = (
        "向指定收件人发送邮件（QQ 邮箱）。用户要求「发到/发送到 xxx@qq.com 邮件」时，必须调用本工具真正发信；禁止只回复「已发送」却不调用（否则用户收不到）。参数：to_addr 收件人邮箱、subject 主题、body 正文。"
    )
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "to_addr": {
                    "type": "string",
                    "description": "收件人邮箱地址。",
                },
                "subject": {
                    "type": "string",
                    "description": "邮件主题。",
                },
                "body": {
                    "type": "string",
                    "description": "邮件正文内容。",
                },
            },
            "required": ["to_addr", "subject", "body"],
        }
    )

    async def call(
        self,
        context: ContextWrapper[AstrAgentContext],
        to_addr: str,
        subject: str,
        body: str,
        **kwargs,
    ) -> ToolExecResult:
        ctx = context.context.context
        cfg = _get_effective_config(ctx, AllCharPlugin._shared_config)  # type: ignore[arg-type]
        sender = get_email_config(cfg, "email_sender", "")
        auth_code = get_email_config(cfg, "email_auth_code", "")
        if not sender or not auth_code:
            return "未配置发件人邮箱或 QQ 邮箱授权码，请在插件配置的「邮件」中填写。"
        to_addr = (to_addr or "").strip()
        if not to_addr:
            return "请提供收件人邮箱地址。"
        ok, msg = await asyncio.to_thread(
            send_email_sync,
            sender=sender,
            auth_code=auth_code,
            to_addrs=[to_addr],
            subject=(subject or "").strip() or "（无主题）",
            body=(body or "").strip() or "",
            smtp_host=get_email_config(cfg, "email_smtp_host", "smtp.qq.com"),
            smtp_port=int(get_email_config(cfg, "email_smtp_port", "465") or "465"),
        )
        return msg


@dataclass
class SlangExplainTool(FunctionTool[AstrAgentContext]):
    """
    解释当前群聊或私聊上下文中的黑话。
    """

    name: str = "slang_explain"
    description: str = (
        "查询当前群聊/当前私聊里某个黑话、缩写或特殊说法的意思。"
        "适合用户问“xx 是什么意思”“这个梗啥意思”时调用。"
    )
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "term": {
                    "type": "string",
                    "description": "要解释的黑话词条，例如 yyds、绝绝子。",
                },
            },
            "required": ["term"],
        }
    )

    async def call(
        self,
        context: ContextWrapper[AstrAgentContext],
        term: str,
        **kwargs,
    ) -> ToolExecResult:
        del kwargs
        event = context.context.event
        if event is None:
            return "当前没有事件上下文，暂时无法判断要查哪个群或私聊的黑话。"
        return explain_slang_for_event(event, (term or "").strip())


@dataclass
class ProactiveSendMessageTool(FunctionTool[AstrAgentContext]):
    """
    向已绑定的群聊或私聊目标主动发送一条消息。
    """

    name: str = "proactive_send_message"
    description: str = (
        "向已绑定的群聊或私聊目标主动发送消息，仅管理员可用。"
        "当同一个别名同时绑定了群聊和私聊时，请明确指定 scene_type=group 或 private。"
    )
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "target_alias": {
                    "type": "string",
                    "description": "目标别名，例如 工作群、老王私聊。",
                },
                "text": {
                    "type": "string",
                    "description": "要主动发送的纯文本内容。",
                },
                "scene_type": {
                    "type": "string",
                    "description": "可选：group、private、auto；默认 auto。",
                    "enum": ["auto", "group", "private"],
                },
            },
            "required": ["target_alias", "text"],
        }
    )

    async def call(
        self,
        context: ContextWrapper[AstrAgentContext],
        target_alias: str,
        text: str,
        scene_type: str | None = None,
        **kwargs,
    ) -> ToolExecResult:
        del kwargs
        event = context.context.event
        if event is not None and not is_proactive_admin_event(event):
            return "主动消息仅允许管理员使用。"

        ctx = context.context.context
        normalized_scene_type = str(scene_type or "").strip().lower()
        if normalized_scene_type == "auto":
            normalized_scene_type = ""
        ok, msg = await send_message_to_target(
            ctx,
            alias=(target_alias or "").strip(),
            text=(text or "").strip(),
            scene_type=normalized_scene_type,
        )
        return msg
