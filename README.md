 多功能生活助手（astrbot_all_char）

`astrbot_all_char` 是对原 char 系列多个热门插件的整合版本，一套插件覆盖火车票查询、天气查询、股票行情、简易提醒、记账、千帆智能搜索等高频需求，统一配置、统一指令、统一维护。

在保持原有「命令模式」兼容的基础上，本插件额外提供了一组 LLM 工具（FunctionTool + add_llm_tools），方便在自然语言对话中由 Agent 自动调用。

 已注册的 LLM 工具一览

- stock_query
  - 功能: 查询股票当前行情。
  - 参数:
    - `query` (string, 必填): 股票代码（如 `600519`）或名称关键字（如 `贵州茅台`）。
  - 说明: 内部使用新浪行情源，若按名称匹配到多只股票，会返回候选列表让模型引导用户改用代码查询。

- weather_query
  - 功能: 查询城市天气。
  - 参数:
    - `city` (string, 必填): 城市名称，例如 `北京`。
    - `days` (integer, 选填): 预报天数 `1-7`，缺省或小于 2 视为当天。
  - 说明: 复用原天气模块逻辑，优先按配置的 `weather_api_url` / `weather_api_key` 请求，默认使用 `api.nycnm.cn`。

- train_query
  - 功能: 查询两地之间的火车票/车次信息。
  - 参数:
    - `departure` (string, 必填): 出发地城市或站点，例如 `厦门`。
    - `arrival` (string, 必填): 目的地城市或站点，例如 `上海`。
  - 说明: 复用原火车票模块的查询接口，默认使用 `https://api.lolimi.cn/API/hc/api`。

- simple_reminder
  - 功能: 设置一个简易定时提醒（等价于命令 `/提醒`）。
  - 参数:
    - `time_expression` (string, 必填): 时间表达式，如 `3分钟后`、`2小时后`、`2026-02-28-08:00`、`08:30`。
    - `text` (string, 必填): 提醒内容，例如 `喝水`、`去开会`。
  - 说明: 使用 APScheduler 做持久化调度，消息重启后仍会按时触发。

- bookkeeping_add_expense
  - 功能: 记录一笔支出，并由 LLM 自动分类。
  - 参数:
    - `amount` (number, 必填): 支出金额，单位元。
    - `description` (string, 选填): 支出描述，例如 `中午吃饭`。

- bookkeeping_add_income
  - 功能: 记录一笔收入，并由 LLM 自动分类。
  - 参数:
    - `amount` (number, 必填): 收入金额，单位元。
    - `description` (string, 选填): 收入描述，例如 `工资`、`发红包`。

- bookkeeping_summary
  - 功能: 查看当前用户的记账总收入、总支出和余额，并给出简要 AI 财务建议。
  - 参数: 无。

- smart_search
  - 功能: 使用百度千帆智能搜索（`ai_search/chat/completions`）查询复杂问题，并交由当前会话 LLM 重新整理输出。
  - 参数:
    - `query` (string, 必填): 要搜索的问题或主题。
  - 说明: 本地统计每日最多 `100` 次（`DAILY_LIMIT_SMART`），超过后会拒绝调用。

- web_search
  - 功能: 使用百度千帆网页搜索（`ai_search/web_search`）查询信息，并交由当前会话 LLM 重新整理输出。
  - 参数:
    - `query` (string, 必填): 要搜索的关键词。
  - 说明: 本地统计每日最多 `1000` 次（`DAILY_LIMIT_WEB`），超过后会拒绝调用。

- anime_trace
  - 功能: 使用 AnimeTrace API 识别动漫图片所属番剧、角色等信息。
  - 参数:
    - `image` (string, 选填): 要识别的图片 URL 或本地路径；留空时会自动从当前会话中最近一条带图消息提取第一张图片。
  - 说明: 适用于「这是谁/出自哪部番/帮我搜番」等需求，返回结果包含番剧标题、相似度、集数/时间点和预览图链接等信息。

- music_play
  - 功能: 根据歌曲名称或关键词点歌，自动选择最匹配的一首并返回播放链接。
  - 参数:
    - `keyword` (string, 必填): 歌曲名称或相关关键词，例如 `青花` 或 `夜曲 周杰伦`。
  - 说明: 复用原 `astrbot_plugin_music_pro` 行为，使用柠柚点歌接口与网易云 API 获取音源，返回文本中会包含可供前端播放的音频 URL。

 与命令模式的对应关系

- 股票:  
  - 命令: `/股票 查询 600519`  
  - LLM 工具: `stock_query`（参数 `query="600519"`）

- 天气:  
  - 命令: `/天气 北京 5`  
  - LLM 工具: `weather_query`（参数 `city="北京"`, `days=5`）

- 火车票:  
  - 命令: `/火车票 厦门 上海`  
  - LLM 工具: `train_query`（参数 `departure="厦门"`, `arrival="上海"`）

- 简易提醒:  
  - 命令: `/提醒 3分钟后 喝水`  
  - LLM 工具: `simple_reminder`（参数 `time_expression="3分钟后"`, `text="喝水"`）

- 记账:  
  - 命令: `记账支出 35 中午吃饭`  
  - LLM 工具: `bookkeeping_add_expense`（参数 `amount=35`, `description="中午吃饭"`）

- 点歌:  
  - 命令: `/点歌 青花`  
  - LLM 工具: `music_play`（参数 `keyword="青花"`）

 在 Agent / Skill 中使用建议

- 工具发现:  
  在构建 Agent 的工具列表时，可以直接暴露上述工具的 `name`、`description` 和 `parameters` 结构，让大模型根据自然语言自动选择合适的工具调用。

- 提示词建议:  
  在系统提示词中，可以用简短中文列出这些工具用途，例如：
  > 你可以使用以下工具：  
  > - `stock_query`: 查询 A 股股票行情  
  > - `weather_query`: 查询城市天气  
  > - `train_query`: 查询火车票车次信息  
  > - `simple_reminder`: 帮用户设置定时提醒  
  > - `bookkeeping_`: 帮用户记账与查看统计  
  > - `smart_search` / `web_search`: 需要上网查资料时调用。  
  > - `anime_trace`: 当用户给出动漫截图/角色立绘并询问来源或人物信息时调用。

这样，大模型在理解用户自然语言意图时，就能像使用 `astrbot_plugin_payqr` 一样，自动发现并调用 `astrbot_all_char` 提供的这些技能。

 多功能生活助手（astrbot_all_char）

`astrbot_all_char` 是对原 char 系列多个热门插件的整合版本，一套插件覆盖火车票查询、天气查询、股票行情、简易提醒、记账、千帆智能搜索等高频需求，统一配置、统一指令、统一维护。

在保持原有「命令模式」兼容的基础上，本插件额外提供了一组 LLM 工具（FunctionTool + add_llm_tools），方便在自然语言对话中由 Agent 自动调用。

 已注册的 LLM 工具一览

- stock_query
  - 功能: 查询股票当前行情。
  - 参数:
    - `query` (string, 必填): 股票代码（如 `600519`）或名称关键字（如 `贵州茅台`）。
  - 说明: 内部使用新浪行情源，若按名称匹配到多只股票，会返回候选列表让模型引导用户改用代码查询。

- weather_query
  - 功能: 查询城市天气。
  - 参数:
    - `city` (string, 必填): 城市名称，例如 `北京`。
    - `days` (integer, 选填): 预报天数 `1-7`，缺省或小于 2 视为当天。
  - 说明: 复用原天气模块逻辑，优先按配置的 `weather_api_url` / `weather_api_key` 请求，默认使用 `api.nycnm.cn`。

- train_query
  - 功能: 查询两地之间的火车票/车次信息。
  - 参数:
    - `departure` (string, 必填): 出发地城市或站点，例如 `厦门`。
    - `arrival` (string, 必填): 目的地城市或站点，例如 `上海`。
  - 说明: 复用原火车票模块的查询接口，默认使用 `https://api.lolimi.cn/API/hc/api`。

- simple_reminder
  - 功能: 设置一个简易定时提醒（等价于命令 `/提醒`）。
  - 参数:
    - `time_expression` (string, 必填): 时间表达式，如 `3分钟后`、`2小时后`、`2026-02-28-08:00`、`08:30`。
    - `text` (string, 必填): 提醒内容，例如 `喝水`、`去开会`。
  - 说明: 使用 APScheduler 做持久化调度，消息重启后仍会按时触发。

- bookkeeping_add_expense
  - 功能: 记录一笔支出，并由 LLM 自动分类。
  - 参数:
    - `amount` (number, 必填): 支出金额，单位元。
    - `description` (string, 选填): 支出描述，例如 `中午吃饭`。

- bookkeeping_add_income
  - 功能: 记录一笔收入，并由 LLM 自动分类。
  - 参数:
    - `amount` (number, 必填): 收入金额，单位元。
    - `description` (string, 选填): 收入描述，例如 `工资`、`发红包`。

- bookkeeping_summary
  - 功能: 查看当前用户的记账总收入、总支出和余额，并给出简要 AI 财务建议。
  - 参数: 无。

- smart_search
  - 功能: 使用百度千帆智能搜索（`ai_search/chat/completions`）查询复杂问题，并交由当前会话 LLM 重新整理输出。
  - 参数:
    - `query` (string, 必填): 要搜索的问题或主题。
  - 说明: 本地统计每日最多 `100` 次（`DAILY_LIMIT_SMART`），超过后会拒绝调用。

- web_search
  - 功能: 使用百度千帆网页搜索（`ai_search/web_search`）查询信息，并交由当前会话 LLM 重新整理输出。
  - 参数:
    - `query` (string, 必填): 要搜索的关键词。
  - 说明: 本地统计每日最多 `1000` 次（`DAILY_LIMIT_WEB`），超过后会拒绝调用。

 与命令模式的对应关系

- 股票:  
  - 命令: `/股票 查询 600519`  
  - LLM 工具: `stock_query`（参数 `query="600519"`）

- 天气:  
  - 命令: `/天气 北京 5`  
  - LLM 工具: `weather_query`（参数 `city="北京"`, `days=5`）

- 火车票:  
  - 命令: `/火车票 厦门 上海`  
  - LLM 工具: `train_query`（参数 `departure="厦门"`, `arrival="上海"`）

- 简易提醒:  
  - 命令: `/提醒 3分钟后 喝水`  
  - LLM 工具: `simple_reminder`（参数 `time_expression="3分钟后"`, `text="喝水"`）

- 记账:  
  - 命令: `记账支出 35 中午吃饭`  
  - LLM 工具: `bookkeeping_add_expense`（参数 `amount=35`, `description="中午吃饭"`）

 在 Agent / Skill 中使用建议

- 工具发现:  
  在构建 Agent 的工具列表时，可以直接暴露上述工具的 `name`、`description` 和 `parameters` 结构，让大模型根据自然语言自动选择合适的工具调用。

- 提示词建议:  
  在系统提示词中，可以用简短中文列出这些工具用途，例如：
  > 你可以使用以下工具：  
  > - `stock_query`: 查询 A 股股票行情  
  > - `weather_query`: 查询城市天气  
  > - `train_query`: 查询火车票车次信息  
  > - `simple_reminder`: 帮用户设置定时提醒  
  > - `bookkeeping_`: 帮用户记账与查看统计  
  > - `smart_search` / `web_search`: 需要上网查资料时调用。

这样，大模型在理解用户自然语言意图时，就能像使用 `astrbot_plugin_payqr` 一样，自动发现并调用 `astrbot_all_char` 提供的这些技能。

 astrbot_all_char 多功能合集插件（char 系列）

整合以下独立插件到一个统一插件中，便于统一维护与配置：

- 火车票查询：原 `astrbot_plugin_train`
- AI 智能定时任务：原 `astrbot_plugin_sy`
- 股票行情与自选股：原 `astrbot_plugin_stock`
- 智能天气：原 `astrbot_plugin_nyweather_char`
- Epic 免费游戏（喜加一）：原 `astrbot_plugin_Epicfell_char`
- 日常记账：原 `astrbot_plugin_bookkeeping`
- 今日运势：原 `astrbot_plugin_jrys`
- OCR 图片识别：调用视觉/多模态模型识别图片中的文字，支持多服务商与调用链路兜底
- 百度千帆智能搜索 / 网页搜索：`/智能搜索` 调用千帆 ai_search 对话接口；`/搜索` 调用网页搜索后将结果交给当前 LLM 整理输出
  - 点歌模块：基于原 `astrbot_plugin_music_pro`，提供 `/点歌` 命令与 `music_play` LLM 工具，通过柠柚点歌接口与网易云 API 播放歌曲。

本仓库当前阶段主要是设计统一结构、配置 schema 与维护规范，后续将逐步迁移具体代码逻辑进来。  
当前版本仅保留指令模式，不再支持自然语言触发，所有功能均通过明确的命令前缀调用，便于与 MCP 等指令/工具体系集成。

---

 功能与指令规划（仅指令模式）

- 火车票模块  
  - 主命令前缀：`/火车票`（或沿用原有别名，迁移时统一整理）。

- 定时任务模块（sy）  
  - 推荐命令：`/提醒 <时间> <内容>`，例如：`/提醒 3分钟后 喝水`、`/提醒 08:30 上班打卡`。  
  - 高级命令保持与原插件一致：`/rmd`（提醒/任务/指令任务）、`/rmdg`（远程群管理）。  
  - 如需配合 LLM 工具调用，可按原插件风格迁移；自然语言触发可作为后续优化。

- 股票模块  
  - 主命令：`/股票`、`/stock`（及别名：自选股、行情）。

- 天气模块  
  - 主命令：`/天气`、`/nyweather`、`/天气查询`、`/查天气`。

- Epic 免费游戏模块  
  - 主命令：`/epic`、`/Epic免费`、`/喜加一`、`/e宝`。

- 点歌模块  
  - 主命令：`/点歌`（别名：`music`、`唱歌`、`唱`），用法：`/点歌 <歌曲名/关键词>`，例如：`/点歌 青花`。  
  - 在返回候选列表后，直接回复对应的数字即可播放该歌曲；若使用 Agent 工具调用，可使用 `music_play` 直接按关键词点歌。

- 记账模块  
  - 命令均为中文前缀：`记账支出`、`记账收入`、`查账统计`、`日统计`、`月统计`、`查账详情`、`按类统计`、`删除账单`。  
  - 以命令为主，内部会按需调用 LLM 做自动分类和财务建议。

- 今日运势模块  
  - 主命令：`/jrys`，别名：`/今日运势`、`/运势`。  
  - 生成今日运势图片，是否启用关键词触发与节假日爆率由 `jrys_` 配置控制。  
  - 资源说明：请将原 `astrbot_plugin_jrys-main` 下的 `backgroundFolder` 与 `font` 目录拷贝到 `astrbot_all_char/jrys_assets/` 下（保持同名子目录），打包时只需要带上 `astrbot_all_char` 即可正常出图。

- OCR 图片识别模块  
  - 主命令：`/识别图片`，别名：`/ocr`、`/图片识别`。  
  - 发送指令并附带一张图片，由配置的视觉/多模态 API 识别图中文字。  
  - 配置：在 ocr 中只需添加「OCR 服务商」，每项填 API 地址、API Key、模型名称；可添加多个，按顺序尝试。

- 百度千帆智能搜索 / 网页搜索模块  
  - `/智能搜索 <问题>`：调用千帆 `ai_search/chat/completions` 获取结果，再交给当前会话的 LLM 整理后输出（人格 + 正常标点，避免大量）。本地统计每日最多 100 次，达上限后不再允许调用。  
  - `/搜索 <关键词>`：调用千帆 `ai_search/web_search` 获取网页结果，再将结果交给当前会话的 LLM 整理后输出。本地统计每日最多 1000 次，达上限后不再允许调用。  
  - 统计文件：`data/plugin_data/astrbot_all_char/qianfan_search_daily.json`，按日期记录当日已用次数，次日自动重新计数。  
  - 配置：在 qianfan_search 中填写 千帆 API Key（鉴权头为 `X-Appbuilder-Authorization: Bearer <API Key>`）；可选 智能搜索交给 LLM 的提示词（`qianfan_search_smart_prompt`，占位符 `{smart_search_result}`）；可选 网页搜索交给 LLM 的提示词（`qianfan_search_web_prompt`，占位符 `{query}`、`{search_results}`）。默认提示词已要求使用当前人格与正常中文标点。

- （已移除）自然语言触发  
  - 早期设计中支持对「非 / 开头」消息做意图匹配并复用各模块逻辑；为适配 MCP 等基于指令/工具的调用方式，当前版本已彻底移除相关代码，仅保留指令调用。

> 约定：在 `astrbot_all_char` 中，以上每个模块的主命令前缀不得互相复用；新增别名前需要检查是否与其他模块冲突。

---

 目录与代码结构规划

建议的核心文件与模块划分如下（后续迁移代码时遵守）：

- `main.py`
  - 只负责：插件元信息、指令路由注册、基础初始化。
  - 不直接堆业务逻辑，逻辑全部下沉到 utils 或子模块。
- `train_utils.py`
  - 火车票查询 API 封装、指令解析（自然语言入口可选，不要求立刻实现）。
- `sy_scheduler_utils.py`
  - 定时任务添加/删除/列表、远程群管理、APScheduler 调度封装。
- `stock_utils.py`
  - 新浪行情查询、自选股增删查、定时提醒逻辑（已移除 AkShare 依赖）。
- `weather_utils.py`
  - 天气 API 请求、城市解析（自然语言触发逻辑为增量需求）。
- `epic_utils.py`
  - Epic 免费游戏查询、订阅/取消订阅、定时推送。
- `bookkeeping_utils.py`
  - 记账逻辑（支出/收入记录、统计、AI 建议），统一由中文命令触发。
- `jrys_utils.py`
  - 今日运势图片生成逻辑的桥接层，统一由 `/jrys` 系列命令触发。
- `ocr_utils.py`
  - OCR 图片识别：从消息中取图，按配置的调用链路请求视觉 API，返回识别文字。
- `qianfan_search_utils.py`
  - 百度千帆：鉴权、`/智能搜索`（千帆智能搜索 + 当前 LLM 整理）、`/搜索`（web_search + 当前 LLM 整理）。
- `natural_language_utils.py`
  - 自然语言意图匹配与统一入口：天气/火车票/提醒/股票/Epic/运势/智能搜索/网页搜索/记账等；命中后复用对应 handler 并建议终止事件传播。

> 开发规范（LLM Tool 一致性）  
> - 每新增一类功能模块（如新的查询/识别/工具服务），应同时提供：  
>   - 对应的命令入口（`@filter.command`）  
>   - 至少一个 LLM 工具封装：  
>     - `@filter.llm_tool` 事件级入口（可选，用于在对话中直接调用）；  
>     - 一个 `FunctionTool` 子类（如 `XXXTool`），并在插件初始化时通过 `context.add_llm_tools(...)` 注册，包含详细的参数 schema 与「使用建议（给 LLM 的决策规则）」文档注释。  
> - 新增功能时，请在本 `README.md` 的「已注册的 LLM 工具一览」中同步补充对应工具的说明，保持文档与实现一致。

后续如需新增功能模块，请优先新增独立的 `xxx_utils.py` 或子包，而不是在 `main.py` 中继续堆代码。

---

 配置统一：_conf_schema.json 约定

- `astrbot_all_char` 目录下只保留一个 `_conf_schema.json`，整合所有子功能的配置项。
- 结构：按功能模块分组，每个模块一个顶层对象（与项目根目录「样例」写法一致）：
- `train`：火车票查询（`train_api_url`、`train_default_format`）
  - `sy`：智能定时任务（会话隔离、白名单、上下文、@ 功能、cron 等）
  - `stock`：股票与自选股（`stock_reminder_timezone`）
  - `weather`：天气查询（API 地址、密钥、返回格式）
  - `epic`：Epic 免费游戏（API、定时推送、订阅会话等）
  - `jrys`：今日运势（关键词触发、节假日爆率、缓存等）
  - `ocr`：图片文字识别（`ocr_enabled`、`ocr_providers` 服务商，每项仅 API 地址 / Key / 模型名称）
- `qianfan_search`：百度千帆智能搜索与网页搜索（`qianfan_search_ak`；可选 `qianfan_search_smart_prompt` 智能搜索 LLM 提示词、`qianfan_search_web_prompt` 网页搜索 LLM 提示词）
  - `music`：点歌与音乐播放（`music_apikey`、`music_api_url`、`music_quality`、`music_search_limit`）
- 配置项仍使用模块前缀命名（如 `train_`、`sy_`、`weather_` 等），避免冲突。
- 若框架按嵌套存储配置，插件通过 `config_utils.ensure_flat_config` 提供扁平化视图，业务代码无需修改。
- 每个字段的 `description` / `hint` 中注明来源模块与用途，方便从原插件追溯。

详细字段请查看同目录下的 `_conf_schema.json`。

---

 文档与更新规范（重要）

- 在 `astrbot_all_char` 目录下，本 `README.md` 视为必需文档：
  - 每一次功能更新、配置变更或指令变化，都必须同步更新本 `.md` 文档。
  - 建议在下方维护一个简单的「更新记录」小节。
- 如后续新增 `CHANGELOG.md`，同样需要在每次更新时维护。

 更新时建议写清楚：

- 更新日期、版本号（如有）。
- 影响到的模块（火车、定时任务、股票、天气、Epic 等）。
- 是否改动 `_conf_schema.json` 中的配置项名/默认值。
- 是否需要用户迁移旧数据或重新配置。

---

 当前阶段状态（草案）

- 2026-03-01：修复 千帆智能搜索 401：智能搜索接口鉴权头由 `Authorization` 改为 `X-Appbuilder-Authorization: Bearer <API Key>`，与千帆要求一致。
- 2026-03-01：自然语言：对以 `http://` 或 `https://` 开头的纯 URL 消息不再进行意图匹配，直接跳过，避免无意义匹配。
- 2026-03-01：修复 自然语言股票误触发：原规则「我的(股票)?」会命中「把我的QQ号…」中的「我的」，导致误判为「股票 列表」；现改为仅当出现「自选/自选股」或「我的股票/我的自选」时才视为查自选列表。
- 2026-03-01：修复 「/天气 武汉」等命令触发两次回复：自然语言处理器改为 `priority=-10`，在命令处理器（默认 0）之后执行，使「/天气 武汉」先命中命令、`stop_event()` 生效，不再重复走自然语言；并加强 `_has_command_prefix`，用 `message_obj.message` 首段判断是否以 `/` 开头。
- 2026-03-01：新增 自然语言触发：在保留全部指令的前提下，对「非 / 开头」消息做意图匹配（天气/火车票/提醒/股票/Epic/运势/智能搜索/网页搜索/记账等），命中则复用对应 handler 并 `event.stop_event()` 避免 LLM 重复回复；新增 `natural_language_utils.py`、配置组 `natural_language`（`nl_enabled` 总开关及各模块开关）。
- 2026-03-01：新增 百度千帆智能搜索 / 网页搜索：命令 `/智能搜索 <问题>`（别名 `/智能搜素`）、`/搜索 <关键词>`；鉴权为 API Key（请求头 `X-Appbuilder-Authorization: Bearer <API Key>`）；配置项 `qianfan_search_ak`；本地统计每日次数，`/智能搜索` 达 100 次、`/搜索` 达 1000 次后拒绝调用；`/搜索` 的结果会交给当前 LLM 整理后输出。
- 2026-03-01：新增 OCR 图片识别 功能：命令 `/识别图片`（别名 `/ocr`、`/图片识别`），支持在配置中添加多个 OCR 服务商（OpenAI 兼容视觉 API）及调用链路兜底；`_conf_schema.json` 增加 `ocr` 模块（`ocr_enabled`、`ocr_chain`、`ocr_providers`）。`config_utils` 增加 `ocr` 以支持扁平化配置。
- 2026-03-01：`_conf_schema.json` 按模块分组重写（train / sy / stock / weather / epic / jrys 各在一个 `{}` 内），写法对齐项目根目录「样例」；新增 `config_utils.ensure_flat_config` 以兼容嵌套配置，无需用户迁移旧数据。
- 2026-02-27：定时任务模块改为简化实现，仅保留 `/提醒 <时间> <内容>`；`/rmd` / `/rmdg` 目前只返回帮助提示。

- 本插件目前处于「骨架设计阶段」：
  - 已分析原插件的 README 与 `_conf_schema.json`；
  - 已在 `.cursor/rules/astrbot-all-char-integration.mdc` 中写入集成规则，方便在 Cursor 中长期记忆与复用；
  - 本 `README.md` 与 `_conf_schema.json` 主要用于约定未来迁移和开发方式。
- 后续可按模块逐个迁移实现，迁移时务必：
  - 复用/保留原有指令和自然语言体验；
  - 保持配置项含义一致，仅在前缀或命名上统一。

