import re
import json
from datetime import datetime
from pathlib import Path

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent
from astrbot.api.star import Context, StarTools


class BookkeepingModule:
    """记账模块（从 astrbot_plugin_bookkeeping 简化迁移，命令模式）。"""

    def __init__(self, context: Context, config: AstrBotConfig):
        self.context = context
        self.config = config
        self.plugin_name = "astrbot_plugin_bookkeeping"
        self.data_path = StarTools.get_data_dir(self.plugin_name)
        self.data_path.mkdir(parents=True, exist_ok=True)
        logger.info("记账模块已启动，数据路径: %s", self.data_path)

    def _get_user_file(self, user_name: str) -> Path:
        return self.data_path / f"{user_name}_bookkeeping.json"

    async def _get_ai_evaluation(self, user_name: str, analysis_data: str, umo: str) -> str:
        """获取 AI 评价；LLM 不可用时返回空串。"""
        try:
            provider_id = await self.context.get_current_chat_provider_id(umo=umo)
            prompt = (
                "你是一个专业的财务顾问。请根据下面的账单数据，提供简明的财务评价和建议。"
                "评价要点：1)支出/收入结构 2)消费习惯 3)财务建议。回复要简洁（3-5句话）。\n\n"
                f"{analysis_data}"
            )
            llm_resp = await self.context.llm_generate(
                chat_provider_id=provider_id,
                prompt=prompt,
            )
            return f"\n\n{'=' * 40}\n{llm_resp.completion_text}"
        except Exception as e:
            logger.debug("调用记账 AI 评价失败: %s", e)
            return ""

    async def _ai_classify_category(
        self,
        transaction_type: str,
        amount: float,
        description: str,
        umo: str,
    ) -> str:
        """使用 AI 自动分类交易类别。"""
        try:
            provider_id = await self.context.get_current_chat_provider_id(umo=umo)
            type_name = "支出" if transaction_type == "expense" else "收入"
            prompt = (
                "你是一个智能记账助手。请根据以下交易信息给出合适的分类。\n"
                f"交易类型: {type_name}\n"
                f"金额: {amount}\n"
                f"描述: {description}\n\n"
                "请按以下格式回复，不要添加其他内容：\n"
                "类别: [分类名称]\n\n"
                "分类名称请使用中文，常见支出分类：餐饮、交通、住房、水电、食品、购物、娱乐、医疗、教育、技术服务、其他\n"
                "常见收入分类：工资、奖金、租金、退款、投资、兼职、其他\n"
                "如果无法确定，类别用'其他'"
            )
            llm_resp = await self.context.llm_generate(
                chat_provider_id=provider_id,
                prompt=prompt,
            )
            response = llm_resp.completion_text.strip()
            category = "其他"
            for line in response.split("\n"):
                line = line.strip()
                if line.startswith("类别:"):
                    category = line.replace("类别:", "").strip() or "其他"
                    break
            return category
        except Exception as e:
            logger.debug("记账 AI 分类失败: %s", e)
            return "其他"

    async def record_expense(self, event: AstrMessageEvent):
        """记录支出: 记账支出 <金额> [描述]。"""
        user_name = event.get_sender_name()
        message = event.message_str.strip()
        match = re.search(r"记账支出[\s\n]+(\d+(?:\.\d{1,2})?)(?:[\s\n]+(.+))?", message)
        if not match:
            yield event.plain_result("❌ 格式错误！用法: 记账支出 <金额> [描述]")
            return
        amount = float(match.group(1))
        description = match.group(2).strip() if match.group(2) else ""
        category = await self._ai_classify_category("expense", amount, description, event.unified_msg_origin)
        await self._save_record(user_name, "expense", category, amount, description)
        response = (
            "✅ 记账成功！\n"
            f"类型: 支出\n类别: {category}\n金额: ¥{amount:.2f}\n时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        )
        if description:
            response += f"\n描述: {description}"
        yield event.plain_result(response)

    async def record_income(self, event: AstrMessageEvent):
        """记录收入: 记账收入 <金额> [描述]。"""
        user_name = event.get_sender_name()
        message = event.message_str.strip()
        match = re.search(r"记账收入[\s\n]+(\d+(?:\.\d{1,2})?)(?:[\s\n]+(.+))?", message)
        if not match:
            yield event.plain_result("❌ 格式错误！用法: 记账收入 <金额> [描述]")
            return
        amount = float(match.group(1))
        description = match.group(2).strip() if match.group(2) else ""
        category = await self._ai_classify_category("income", amount, description, event.unified_msg_origin)
        await self._save_record(user_name, "income", category, amount, description)
        response = (
            "✅ 记账成功！\n"
            f"类型: 收入\n类别: {category}\n金额: ¥{amount:.2f}\n时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        )
        if description:
            response += f"\n描述: {description}"
        yield event.plain_result(response)

    async def query_summary(self, event: AstrMessageEvent):
        """查看个人账户全量统计: 查账统计。"""
        user_name = event.get_sender_name()
        records = self._load_records(user_name)
        if not records:
            yield event.plain_result("📊 您还没有记账数据")
            return
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
        ai_eval = await self._get_ai_evaluation(user_name, analysis_data, event.unified_msg_origin)
        yield event.plain_result(summary + ai_eval)

    async def query_daily_summary(self, event: AstrMessageEvent):
        """查看今日/指定日期统计: 日统计 [YYYY-MM-DD]。"""
        user_name = event.get_sender_name()
        message = event.message_str.strip()
        match = re.search(r"日统计[\s\n]*(\d{4}-\d{2}-\d{2})?", message)
        if match and match.group(1):
            date_str = match.group(1)
        else:
            date_str = datetime.now().strftime("%Y-%m-%d")
        records = self._load_records(user_name)
        day_records = [r for r in records if r["time"].startswith(date_str)]
        if not day_records:
            yield event.plain_result(f"📅 {date_str} 没有记账数据")
            return
        daily_income = sum(r["amount"] for r in day_records if r["type"] == "income")
        daily_expense = sum(r["amount"] for r in day_records if r["type"] == "expense")
        balance = daily_income - daily_expense
        summary = (
            f"📅 {user_name} 的 {date_str} 统计\n"
            f"收入: ¥{daily_income:.2f}\n"
            f"支出: ¥{daily_expense:.2f}\n"
            f"结余: ¥{balance:.2f}\n"
            f"记录数: {len(day_records)}"
        )
        analysis_data = (
            f"用户 {date_str} 日统计数据：\n"
            f"收入: ¥{daily_income:.2f}\n"
            f"支出: ¥{daily_expense:.2f}\n"
            f"结余: ¥{balance:.2f}\n"
            f"记录数: {len(day_records)}"
        )
        ai_eval = await self._get_ai_evaluation(user_name, analysis_data, event.unified_msg_origin)
        yield event.plain_result(summary + ai_eval)

    async def query_monthly_summary(self, event: AstrMessageEvent):
        """查看月度统计: 月统计 [YYYY-MM]。"""
        user_name = event.get_sender_name()
        message = event.message_str.strip()
        match = re.search(r"月统计[\s\n]*(\d{4}-\d{2})?", message)
        if match and match.group(1):
            month_str = match.group(1)
        else:
            month_str = datetime.now().strftime("%Y-%m")
        records = self._load_records(user_name)
        month_records = [r for r in records if r["time"].startswith(month_str)]
        if not month_records:
            yield event.plain_result(f"📆 {month_str} 没有记账数据")
            return
        monthly_income = sum(r["amount"] for r in month_records if r["type"] == "income")
        monthly_expense = sum(r["amount"] for r in month_records if r["type"] == "expense")
        balance = monthly_income - monthly_expense
        summary = (
            f"📆 {user_name} 的 {month_str} 统计\n"
            f"收入: ¥{monthly_income:.2f}\n"
            f"支出: ¥{monthly_expense:.2f}\n"
            f"结余: ¥{balance:.2f}\n"
            f"记录数: {len(month_records)}"
        )
        analysis_data = (
            f"用户 {month_str} 月统计数据：\n"
            f"收入: ¥{monthly_income:.2f}\n"
            f"支出: ¥{monthly_expense:.2f}\n"
            f"结余: ¥{balance:.2f}\n"
            f"记录数: {len(month_records)}"
        )
        ai_eval = await self._get_ai_evaluation(user_name, analysis_data, event.unified_msg_origin)
        yield event.plain_result(summary + ai_eval)

    async def query_details(self, event: AstrMessageEvent):
        """查看账户详细记录: 查账详情。"""
        user_name = event.get_sender_name()
        records = self._load_records(user_name)
        if not records:
            yield event.plain_result("📋 您还没有记账数据")
            return
        records = sorted(records, key=lambda x: x["time"], reverse=True)
        details = f"📋 {user_name} 的账户详情\n" + "=" * 40 + "\n"
        for idx, record in enumerate(records[-20:], 1):
            record_type = "📈 收入" if record["type"] == "income" else "📉 支出"
            description_text = f" - {record['description']}" if record.get("description") else ""
            details += (
                f"{idx}. {record_type} | {record['category']} | "
                f"¥{record['amount']:.2f} | {record['time']}{description_text}\n"
            )
        yield event.plain_result(details)

    async def query_by_category(self, event: AstrMessageEvent):
        """按类别统计: 按类统计。"""
        user_name = event.get_sender_name()
        records = self._load_records(user_name)
        if not records:
            yield event.plain_result("📊 您还没有记账数据")
            return
        expense_by_cat: dict[str, float] = {}
        income_by_cat: dict[str, float] = {}
        for record in records:
            cat = record["category"]
            amount = record["amount"]
            if record["type"] == "expense":
                expense_by_cat[cat] = expense_by_cat.get(cat, 0) + amount
            else:
                income_by_cat[cat] = income_by_cat.get(cat, 0) + amount
        summary = f"📊 {user_name} 的分类统计\n" + "=" * 40 + "\n"
        if expense_by_cat:
            summary += "📉 支出分类：\n"
            for cat, total in sorted(expense_by_cat.items(), key=lambda x: x[1], reverse=True):
                summary += f"  {cat}: ¥{total:.2f}\n"
        if income_by_cat:
            summary += "📈 收入分类：\n"
            for cat, total in sorted(income_by_cat.items(), key=lambda x: x[1], reverse=True):
                summary += f"  {cat}: ¥{total:.2f}\n"
        analysis_data = summary.replace("=" * 40, "").strip() + f"\n总记录数: {len(records)}"
        ai_eval = await self._get_ai_evaluation(user_name, analysis_data, event.unified_msg_origin)
        yield event.plain_result(summary + ai_eval)

    async def delete_record(self, event: AstrMessageEvent):
        """删除账单: 删除账单 <序号>。"""
        user_name = event.get_sender_name()
        message = event.message_str.strip()
        match = re.search(r"删除账单[\s\n]+(\d+)", message)
        if not match:
            yield event.plain_result(
                "❌ 格式错误！用法: 删除账单 <序号>\n先使用 查账详情 获取序号"
            )
            return
        index = int(match.group(1))
        records = self._load_records(user_name)
        if not records:
            yield event.plain_result("📋 您还没有记账数据")
            return
        records = sorted(records, key=lambda x: x["time"], reverse=True)
        if index < 1 or index > len(records[-20:]):
            yield event.plain_result(f"❌ 序号无效！请输入 1-{len(records[-20:])} 之间的序号")
            return
        record_to_delete = records[-20:][index - 1]
        records.remove(record_to_delete)
        self._save_records(user_name, records)
        record_type = "收入" if record_to_delete["type"] == "income" else "支出"
        response = (
            "✅ 已删除该账单\n"
            f"类型: {record_type}\n类别: {record_to_delete['category']}\n"
            f"金额: ¥{record_to_delete['amount']:.2f}\n时间: {record_to_delete['time']}"
        )
        if record_to_delete.get("description"):
            response += f"\n描述: {record_to_delete['description']}"
        yield event.plain_result(response)

    async def terminate(self):
        logger.info("记账模块已停用")

    async def _save_record(
        self,
        user_name: str,
        record_type: str,
        category: str,
        amount: float,
        description: str = "",
    ):
        records = self._load_records(user_name)
        now = datetime.now()
        record = {
            "type": record_type,
            "category": category,
            "amount": amount,
            "description": description,
            "time": now.strftime("%Y-%m-%d %H:%M:%S"),
        }
        records.append(record)
        self._save_records(user_name, records)

    def _load_records(self, user_name: str) -> list:
        user_file = self._get_user_file(user_name)
        try:
            if user_file.exists():
                with open(user_file, "r", encoding="utf-8") as f:
                    return json.load(f)
        except Exception as e:
            logger.error("加载 %s 的记账数据失败: %s", user_name, e)
        return []

    def _save_records(self, user_name: str, records: list):
        user_file = self._get_user_file(user_name)
        try:
            with open(user_file, "w", encoding="utf-8") as f:
                json.dump(records, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error("保存 %s 的记账数据失败: %s", user_name, e)


_BOOKKEEPING_MODULE: BookkeepingModule | None = None


def init_bookkeeping_module(context: Context, config: AstrBotConfig) -> BookkeepingModule:
    global _BOOKKEEPING_MODULE
    if _BOOKKEEPING_MODULE is None:
        _BOOKKEEPING_MODULE = BookkeepingModule(context, config)
    return _BOOKKEEPING_MODULE


async def handle_bookkeeping_expense(event: AstrMessageEvent, context: Context, config: AstrBotConfig):
    module = init_bookkeeping_module(context, config)
    async for r in module.record_expense(event):
        yield r


async def handle_bookkeeping_income(event: AstrMessageEvent, context: Context, config: AstrBotConfig):
    module = init_bookkeeping_module(context, config)
    async for r in module.record_income(event):
        yield r


async def handle_bookkeeping_summary(event: AstrMessageEvent, context: Context, config: AstrBotConfig):
    module = init_bookkeeping_module(context, config)
    async for r in module.query_summary(event):
        yield r


async def handle_bookkeeping_daily(event: AstrMessageEvent, context: Context, config: AstrBotConfig):
    module = init_bookkeeping_module(context, config)
    async for r in module.query_daily_summary(event):
        yield r


async def handle_bookkeeping_monthly(event: AstrMessageEvent, context: Context, config: AstrBotConfig):
    module = init_bookkeeping_module(context, config)
    async for r in module.query_monthly_summary(event):
        yield r


async def handle_bookkeeping_details(event: AstrMessageEvent, context: Context, config: AstrBotConfig):
    module = init_bookkeeping_module(context, config)
    async for r in module.query_details(event):
        yield r


async def handle_bookkeeping_by_category(event: AstrMessageEvent, context: Context, config: AstrBotConfig):
    module = init_bookkeeping_module(context, config)
    async for r in module.query_by_category(event):
        yield r


async def handle_bookkeeping_delete(event: AstrMessageEvent, context: Context, config: AstrBotConfig):
    module = init_bookkeeping_module(context, config)
    async for r in module.delete_record(event):
        yield r

