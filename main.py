from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

from .natural_language_utils import try_natural_language
from .train_utils import handle_train_command, handle_train_help
from .sy_scheduler_utils import handle_sy_rmd_group, handle_simple_reminder
from .stock_utils import handle_stock_command
from .weather_utils import handle_weather_command, handle_weather_help
from .epic_utils import handle_epic_command, handle_epic_help
from .bookkeeping_utils import (
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
from .ocr_utils import handle_ocr_command
from .qianfan_search_utils import (
    handle_smart_search_command,
    handle_web_search_command,
)
from .config_utils import ensure_flat_config


@register(
    "astrbot_all_char",
    "char",
    "char 系列插件整合版：火车票 / 智能定时任务 / 股票 / 天气 / Epic 免费游戏 / OCR 识别图片（命令模式优先）",
    "0.1.0",
)
class AllCharPlugin(Star):
    """
    统一整合 char 系列插件的入口插件。

    约定：
    - 只在此处做元信息、命令注册和轻量初始化；
    - 具体逻辑全部下沉到对应 utils 模块中；
    - 当前阶段仅实现命令模式，自然语言入口可按需后续补充。
    """

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.context = context
        self.config = ensure_flat_config(config)
        logger.info("astrbot_all_char 插件初始化完成（命令模式优先）")

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

    @filter.command("提醒")
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

    # ---------------- 股票 ----------------

    @filter.command("stock", alias={"股票", "自选股", "行情"})
    async def cmd_stock(self, event: AstrMessageEvent):
        async for result in handle_stock_command(event, self.context, self.config):
            yield result

    # ---------------- 天气 ----------------

    @filter.command("nyweather", alias={"天气", "天气查询", "查天气"})
    async def cmd_weather(self, event: AstrMessageEvent | None = None):
        # 兼容 AstrBot 对 handler 参数检查的同时，避免因为参数注入异常导致直接抛 TypeError
        if event is None:
            logger.error("cmd_weather 被调用时缺少 event 参数")
            return
        event.stop_event()  # 避免同一消息再走自然语言导致回复两次
        async for result in handle_weather_command(event, self.config):
            yield result

    @filter.command("help_nyweather", alias={"天气帮助"})
    async def cmd_weather_help(self, event: AstrMessageEvent):
        async for result in handle_weather_help(event):
            yield result

    # ---------------- Epic 免费游戏 ----------------

    @filter.command("epic", alias={"Epic免费", "epic免费", "喜加一", "e宝"})
    async def cmd_epic(self, event: AstrMessageEvent):
        async for result in handle_epic_command(event, self.config):
            yield result

    @filter.command("help_epic", alias={"Epic帮助", "epic帮助"})
    async def cmd_epic_help(self, event: AstrMessageEvent):
        async for result in handle_epic_help(event):
            yield result

    # ---------------- OCR 图片识别 ----------------

    @filter.command("识别图片", alias={"ocr", "图片识别"})
    async def cmd_ocr(self, event: AstrMessageEvent):
        async for result in handle_ocr_command(event, self.config):
            yield result

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

    # ---------------- 自然语言触发（非 / 开头消息，命中则终止后续 LLM） ----------------
    # 使用 priority=-10，确保在命令处理器（默认 0）之后执行，避免「/天气 武汉」同时命中命令与 NL 导致回复两次
    @filter.event_message_type(filter.EventMessageType.ALL, priority=-10)
    async def on_natural_language(self, event: AstrMessageEvent):
        """
        对未命中任何指令的消息尝试自然语言匹配；命中则复用对应 handler 并终止事件传播，避免 LLM 重复回复。
        指令全部保留，自然语言为增量能力。
        """
        raw = (event.get_message_str() or "").strip()
        if not raw:
            return
        # 有 / 或 ／ 开头的统一不走自然语言，只走命令（优先用消息链首段判断，避免适配器已去掉 / 时误走 NL）
        def _has_command_prefix(ev) -> bool:
            def _check(s):
                return isinstance(s, str) and (s.strip().startswith("/") or s.strip().startswith("／"))
            if _check(ev.get_message_str() or ""):
                return True
            for name in ("raw_message", "message", "message_str", "text", "_message"):
                try:
                    if _check(getattr(ev, name, None) or ""):
                        return True
                except Exception:
                    pass
            try:
                # 消息链首段：部分适配器会把整条消息放在首段，可据此判断是否以 / 开头
                msg_obj = getattr(ev, "message_obj", None)
                if msg_obj:
                    chain = getattr(msg_obj, "message", None) or []
                    for seg in chain:
                        t = getattr(seg, "text", None)
                        if t is None and hasattr(seg, "data"):
                            d = getattr(seg, "data", None)
                            t = (d.get("text") or d.get("content") or "") if isinstance(d, dict) else ""
                        if isinstance(t, str) and t:
                            if _check(t):
                                return True
                            break  # 只检查首段文本
            except Exception:
                pass
            try:
                for m in (getattr(ev, "get_messages", lambda: None)() or []):
                    d = getattr(m, "data", None) or {}
                    t = d.get("text") or d.get("content") or "" if isinstance(d, dict) else ""
                    if _check(t):
                        return True
            except Exception:
                pass
            return False
        if _has_command_prefix(event):
            return
        logger.info("[astrbot_all_char] on_natural_language 收到: len=%d is_cmd=%s", len(raw), raw.startswith("/") or raw.startswith("／"))
        first = True
        async for result in try_natural_language(
            event,
            self.context,
            self.config,
            handle_weather_command=handle_weather_command,
            handle_train_command=handle_train_command,
            handle_simple_reminder=handle_simple_reminder,
            handle_stock_command=handle_stock_command,
            handle_epic_command=handle_epic_command,
            handle_jrys_command=handle_jrys_command,
            handle_smart_search_command=handle_smart_search_command,
            handle_web_search_command=handle_web_search_command,
            handle_bookkeeping_expense=handle_bookkeeping_expense,
            handle_bookkeeping_income=handle_bookkeeping_income,
            handle_bookkeeping_summary=handle_bookkeeping_summary,
            handle_bookkeeping_daily=handle_bookkeeping_daily,
            handle_bookkeeping_monthly=handle_bookkeeping_monthly,
        ):
            if first:
                event.stop_event()
                first = False
            yield result

