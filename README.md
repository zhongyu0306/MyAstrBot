## astrbot_all_char 多功能合集插件（char 系列）

整合以下独立插件到一个统一插件中，便于统一维护与配置：

- 火车票查询：原 `astrbot_plugin_train`
- AI 智能定时任务：原 `astrbot_plugin_sy`
- 股票行情与自选股：原 `astrbot_plugin_stock`
- 智能天气：原 `astrbot_plugin_nyweather_char`
- Epic 免费游戏（喜加一）：原 `astrbot_plugin_Epicfell_char`
- 日常记账：原 `astrbot_plugin_bookkeeping`
- 今日运势：原 `astrbot_plugin_jrys`
- **OCR 图片识别**：调用视觉/多模态模型识别图片中的文字，支持多服务商与调用链路兜底
- **百度千帆智能搜索 / 网页搜索**：`/智能搜索` 调用千帆 ai_search 对话接口；`/搜索` 调用网页搜索后将结果交给当前 LLM 整理输出

本仓库当前阶段主要是**设计统一结构、配置 schema 与维护规范**，后续将逐步迁移具体代码逻辑进来。  
**当前优先实现命令模式，不强制实现自然语言识别，后续如有需要再补充自然语言入口。**

---

### 功能与指令规划（不冲突约定，暂以命令为主）

- **火车票模块**  
  - 主命令前缀：`/火车票`（或沿用原有别名，迁移时统一整理）。  
  - 自然语言识别为**可选后续增强**，当前阶段不必实现。

- **定时任务模块（sy）**  
  - **推荐命令**：`/提醒 <时间> <内容>`，例如：`/提醒 3分钟后 喝水`、`/提醒 08:30 上班打卡`。  
  - 高级命令保持与原插件一致：`/rmd`（提醒/任务/指令任务）、`/rmdg`（远程群管理）。  
  - 如需配合 LLM 工具调用，可按原插件风格迁移；自然语言触发可作为后续优化。

- **股票模块**  
  - 主命令：`/股票`、`/stock`（及别名：自选股、行情）。  
  - 当前阶段仅保证命令正常工作，自然语言「帮我查贵州茅台股价」等可留作后续优化。

- **天气模块**  
  - 主命令：`/天气`、`/nyweather`、`/天气查询`、`/查天气`。  
  - 自然语言（如「北京天气」「上海今天多少度」）为可选特性，非必需。

- **Epic 免费游戏模块**  
  - 主命令：`/epic`、`/Epic免费`、`/喜加一`、`/e宝`。  
  - 当前以命令为主，自然语言（如「Epic 免费游戏」「最近有什么游戏可以白嫖的」）可按需后续补充。

- **记账模块**  
  - 命令均为中文前缀：`记账支出`、`记账收入`、`查账统计`、`日统计`、`月统计`、`查账详情`、`按类统计`、`删除账单`。  
  - 以命令为主，内部会按需调用 LLM 做自动分类和财务建议。

- **今日运势模块**  
  - 主命令：`/jrys`，别名：`/今日运势`、`/运势`。  
  - 生成今日运势图片，是否启用关键词触发与节假日爆率由 `jrys_*` 配置控制。  
  - 资源说明：请将原 `astrbot_plugin_jrys-main` 下的 `backgroundFolder` 与 `font` 目录拷贝到 `astrbot_all_char/jrys_assets/` 下（保持同名子目录），打包时只需要带上 `astrbot_all_char` 即可正常出图。

- **OCR 图片识别模块**  
  - 主命令：`/识别图片`，别名：`/ocr`、`/图片识别`。  
  - 发送指令并附带一张图片，由配置的视觉/多模态 API 识别图中文字。  
  - 配置：在 **ocr** 中只需添加「OCR 服务商」，每项填 **API 地址、API Key、模型名称**；可添加多个，按顺序尝试。

- **百度千帆智能搜索 / 网页搜索模块**  
  - **`/智能搜索 <问题>`**：调用千帆 `ai_search/chat/completions` 获取结果，再交给**当前会话的 LLM** 整理后输出（人格 + 正常标点，避免大量*）。**本地统计每日最多 100 次**，达上限后不再允许调用。  
  - **`/搜索 <关键词>`**：调用千帆 `ai_search/web_search` 获取网页结果，再将结果交给**当前会话的 LLM** 整理后输出。**本地统计每日最多 1000 次**，达上限后不再允许调用。  
  - 统计文件：`data/plugin_data/astrbot_all_char/qianfan_search_daily.json`，按日期记录当日已用次数，次日自动重新计数。  
  - 配置：在 **qianfan_search** 中填写 **千帆 API Key**（鉴权头为 `X-Appbuilder-Authorization: Bearer <API Key>`）；可选 **智能搜索交给 LLM 的提示词**（`qianfan_search_smart_prompt`，占位符 `{smart_search_result}`）；可选 **网页搜索交给 LLM 的提示词**（`qianfan_search_web_prompt`，占位符 `{query}`、`{search_results}`）。默认提示词已要求使用当前人格与正常中文标点。

> **约定**：在 `astrbot_all_char` 中，以上每个模块的**主命令前缀不得互相复用**；新增别名前需要检查是否与其他模块冲突。

---

### 目录与代码结构规划

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

后续如需新增功能模块，请优先新增独立的 `xxx_utils.py` 或子包，而不是在 `main.py` 中继续堆代码。

---

### 配置统一：_conf_schema.json 约定

- `astrbot_all_char` 目录下只保留**一个** `_conf_schema.json`，整合所有子功能的配置项。
- **结构**：按功能模块分组，每个模块一个顶层对象（与项目根目录「样例」写法一致）：
  - `train`：火车票查询（`train_api_url`、`train_default_format`、`train_enable_natural`）
  - `sy`：智能定时任务（会话隔离、白名单、上下文、@ 功能、cron 等）
  - `stock`：股票与自选股（`stock_reminder_timezone`）
  - `weather`：天气查询（API 地址、密钥、返回格式、自然语言）
  - `epic`：Epic 免费游戏（API、定时推送、订阅会话等）
  - `jrys`：今日运势（关键词触发、节假日爆率、缓存等）
  - `ocr`：图片文字识别（`ocr_enabled`、`ocr_providers` 服务商，每项仅 API 地址 / Key / 模型名称）
  - `qianfan_search`：百度千帆智能搜索与网页搜索（`qianfan_search_ak`；可选 `qianfan_search_smart_prompt` 智能搜索 LLM 提示词、`qianfan_search_web_prompt` 网页搜索 LLM 提示词）
- 配置项仍使用**模块前缀**命名（如 `train_*`、`sy_*`、`weather_*` 等），避免冲突。
- 若框架按嵌套存储配置，插件通过 `config_utils.ensure_flat_config` 提供扁平化视图，业务代码无需修改。
- 每个字段的 `description` / `hint` 中注明来源模块与用途，方便从原插件追溯。

详细字段请查看同目录下的 `_conf_schema.json`。

---

### 文档与更新规范（重要）

- 在 `astrbot_all_char` 目录下，**本 `README.md` 视为必需文档**：
  - **每一次功能更新、配置变更或指令变化，都必须同步更新本 `.md` 文档**。
  - 建议在下方维护一个简单的「更新记录」小节。
- 如后续新增 `CHANGELOG.md`，同样需要在每次更新时维护。

#### 更新时建议写清楚：

- 更新日期、版本号（如有）。
- 影响到的模块（火车、定时任务、股票、天气、Epic 等）。
- 是否改动 `_conf_schema.json` 中的配置项名/默认值。
- 是否需要用户迁移旧数据或重新配置。

---

### 当前阶段状态（草案）

- **2026-03-01**：新增 **百度千帆智能搜索 / 网页搜索**：命令 `/智能搜索 <问题>`（别名 `/智能搜素`）、`/搜索 <关键词>`；鉴权改为仅 **API Key**（`Authorization: Bearer <API Key>`，无需 Secret Key）；配置项 `qianfan_search_ak`；**本地统计**每日次数，`/智能搜索` 达 100 次、`/搜索` 达 1000 次后拒绝调用；`/搜索` 的结果会交给当前 LLM 整理后输出。
- **2026-03-01**：新增 **OCR 图片识别** 功能：命令 `/识别图片`（别名 `/ocr`、`/图片识别`），支持在配置中添加多个 OCR 服务商（OpenAI 兼容视觉 API）及调用链路兜底；`_conf_schema.json` 增加 `ocr` 模块（`ocr_enabled`、`ocr_chain`、`ocr_providers`）。`config_utils` 增加 `ocr` 以支持扁平化配置。
- **2026-03-01**：`_conf_schema.json` 按模块分组重写（train / sy / stock / weather / epic / jrys 各在一个 `{}` 内），写法对齐项目根目录「样例」；新增 `config_utils.ensure_flat_config` 以兼容嵌套配置，无需用户迁移旧数据。
- 2026-02-27：定时任务模块改为简化实现，仅保留 `/提醒 <时间> <内容>`；`/rmd` / `/rmdg` 目前只返回帮助提示。

- 本插件目前处于「**骨架设计阶段**」：
  - 已分析原插件的 README 与 `_conf_schema.json`；
  - 已在 `.cursor/rules/astrbot-all-char-integration.mdc` 中写入集成规则，方便在 Cursor 中长期记忆与复用；
  - 本 `README.md` 与 `_conf_schema.json` 主要用于约定未来迁移和开发方式。
- 后续可按模块逐个迁移实现，迁移时务必：
  - 复用/保留原有指令和自然语言体验；
  - 保持配置项含义一致，仅在前缀或命名上统一。

