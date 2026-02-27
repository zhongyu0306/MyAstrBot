from astrbot.api import AstrBotConfig
from astrbot.api.event import AstrMessageEvent


async def handle_epic_command(event: AstrMessageEvent, config: AstrBotConfig):
    """
    Epic 免费游戏命令入口（整合版占位实现）。

    说明：
    - 建议从 `astrbot_plugin_Epicfell_char` 迁移 `_fetch_epic`、定时推送与订阅管理逻辑至本模块；
    - 当前仅注册命令占位，方便后续无缝替换。
    """
    yield event.plain_result(
        "🕹 Epic 免费游戏模块正在从 `astrbot_plugin_Epicfell_char` 迁移到 `astrbot_all_char`，"
        "目前为占位实现，请暂时使用原插件或稍后再试。"
    )


async def handle_epic_help(event: AstrMessageEvent):
    text = (
        "🕹 Epic 免费游戏（整合版占位）\n\n"
        "规划命令：/epic 或 /Epic免费 /喜加一 /e宝，用于查询当前 Epic 免费游戏，"
        "以及 /epic 订阅 / 取消订阅 / 订阅列表 等。\n"
        "完整逻辑尚未迁移，迁移完成后会在此处生效。"
    )
    yield event.plain_result(text)

