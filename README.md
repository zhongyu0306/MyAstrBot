# 多功能生活助手（astrbot_all_char）

`astrbot_all_char` 是原 char 系列多个热门插件的整合版：**一套插件**覆盖火车票、天气、股票、简易提醒、记账、千帆智能/网页搜索、点歌、今日运势、OCR、动漫识别、发邮件等高频需求，**统一配置、统一指令、统一维护**。

- **命令模式**：所有功能均通过明确的前缀指令调用（见下方「所有指令一览」）。
- **口语化 / Agent 调用**：插件注册了一组 LLM 工具（FunctionTool），在自然语言对话中由 Agent 自动选择并调用，无需记命令（见「口语化调用」与「已注册的 LLM 工具一览」）。

---

## 一、所有指令一览

| 模块 | 主命令及别名 | 用法示例 |
|------|--------------|----------|
| **火车票** | `/火车票`、`/车票`、`/查火车票`；帮助：`/火车票帮助` | `/火车票 厦门 上海` |
| **简易提醒** | `/提醒`、`/提醒列表`、`/我的提醒` | `/提醒 3分钟后 喝水`、`/提醒 08:30 上班打卡`、`/提醒 列表` |
| **永久记忆** | `/记忆`、`/认人`、`/我是谁` | `/记忆 我是 张三`、`/记忆 设置 123456789 张三 我同学`、`/我是谁` |
| **定时任务** | `/rmd`、`/rmdg` | 兼容原 sy 插件子命令，详见帮助 |
| **股票** | `/股票`、`/stock`、`/自选股`、`/行情` | 查询：`/股票 查询 600519`、`/股票 查询 贵州茅台`；自选：`/股票 添加 600519`、`/股票 删除 600519`、`/股票 列表`；提醒：`/股票 提醒 09:30`、`/股票 跌到 600519 1800`、`/股票 涨到 600519 2000`；分析：`/股票 智能分析 600519`、`/股票 量化分析 600519` |
| **基金/量化分析** | 主入口：`/基金`；子命令：`搜索`、`设置`、`分析`、`历史`、`对比`、`量化`、`智能`、`博弈` | `/基金 161226`、`/基金 分析 161226`、`/基金 量化 161226`、`/基金 博弈 161226` |
| **天气** | `/天气`、`/天气查询`、`/查天气`；帮助：`/天气帮助` | `/天气 北京`、`/天气 北京 5` |
| **Epic 免费游戏** | `/epic`、`/Epic免费`、`/喜加一`、`/e宝`；帮助：`/Epic帮助` | `/epic` |
| **点歌** | `/点歌`、`/music`、`/唱歌`、`/唱` | `/点歌 青花`；返回候选后直接回复数字序号即可播放 |
| **记账** | `记账支出`、`记账收入`、`查账统计`、`日统计`、`月统计`、`查账详情`、`按类统计`、`删除账单` | `记账支出 35 中午吃饭`、`记账收入 5000 工资`、`查账统计`、`日统计 2026-03-07`、`月统计 2026-03`、`查账详情`、`按类统计`、`删除账单 3` |
| **今日运势** | `/jrys`、`/今日运势`、`/运势`；原图：`/jrys_last` | `/jrys`（海报含运势总结、幸运星、幸运色、幸运食物、幸运数字、幸运方位、适合事项等；附加素材可在 `jrys_assets/daily_highlights.json` 自定义，内容过多时会自动扩展为更高的长图；Linux 下会优先尝试系统中文字体做缺字回退） |
| **OCR 图片识别** | `/识别图片`、`/ocr`、`/图片识别` | 发送指令并附带一张图片 |
| **动漫/番剧识别** | `/搜番`、`/识别动漫`、`/番剧识别`、`/动漫识别` | 发送指令并附带一张动漫截图 |
| **智能搜索** | `/智能搜索`、`/智能搜素` | `/智能搜索 今天北京天气怎么样`（千帆 ai_search，每日限 100 次） |
| **网页搜索** | `/搜索` | `/搜索 关键词`（千帆 web_search，每日限 1000 次） |
| **发邮件** | `/发邮件`、`/发送邮件` | `/发邮件 someone@qq.com 告诉他今晚来吃饭`（需配置 QQ 邮箱与授权码，主题和正文由 LLM 生成） |
| **邮件订阅** | `订阅邮件`、`邮件订阅`、`取消邮件订阅`、`我的邮件订阅` | `订阅邮件 新闻 xxx@qq.com`；配置中可设每日发送时间与可订阅项（如 新闻,天气,每日摘要），其中「新闻」订阅默认走千帆智能搜索生成正文 |

> 约定：各模块主命令前缀不互相复用；新增别名前需检查是否与其他模块冲突。

---

## 二、口语化调用（自然语言怎么说）

在 **Agent / 技能** 对话中，用户**不用记命令**，直接说需求，大模型会自动选择并调用对应 LLM 工具。示例说法如下。

| 你想做的事 | 可以这样说（示例） | 对应工具 |
|------------|--------------------|----------|
| 查股票 | 「贵州茅台现在多少钱」「查一下 600519 的行情」 | `stock_query` |
| 查天气 | 「北京今天天气怎么样」「上海未来 5 天天气」 | `weather_query` |
| 查火车票 | 「厦门到上海有哪几趟车」「查一下明天厦门到上海的火车」 | `train_query` |
| 设提醒 | 「3 分钟后提醒我喝水」「明天早上 8 点半提醒我打卡」 | `simple_reminder` |
| 记一笔支出 | 「中午吃饭花了 35」「帮我记一笔支出 50 买咖啡」 | `bookkeeping_add_expense` |
| 记一笔收入 | 「今天发工资 5000」「收到红包 200 记一下」 | `bookkeeping_add_income` |
| 看账本 | 「我最近花了多少钱」「帮我看看账本总体情况」 | `bookkeeping_summary` |
| 上网查资料 | 「查一下最新的某某新闻」「帮我搜一下某某」 | `smart_search` / `web_search` |
| 识番/识角色 | 「这张图是哪个番」「这是谁」（并带图） | `anime_trace` |
| 点歌 | 「放一首青花」「帮我点一首夜曲 周杰伦」 | `music_play` |
| 发邮件 | 「帮我发封邮件给 xxx@qq.com，主题是…内容是…」 | `send_email` |

---

## 三、已注册的 LLM 工具一览

- **stock_query**  
  - 功能：查询股票当前行情。  
  - 参数：`query`（必填，string）— 股票代码如 `600519` 或名称关键字如 `贵州茅台`。  
  - 说明：统一走 `akshare` 路径，优先取东方财富 A 股快照，失败时回退到 `akshare` 暴露的新浪接口；按名称匹配到多只时返回候选列表，由模型引导用户改用代码查询。

- **weather_query**  
  - 功能：查询城市天气。  
  - 参数：`city`（必填，string）；`days`（选填，integer，1–7，缺省或 &lt;2 视为当天）。  
  - 说明：优先使用配置的 `weather_api_url` / `weather_api_key`，默认 `api.nycnm.cn`。

- **train_query**  
  - 功能：查询两地之间火车票/车次。  
  - 参数：`departure`、`arrival`（必填，string），如 `厦门`、`上海`。  
  - 说明：默认使用 `https://api.lolimi.cn/API/hc/api`。

- **simple_reminder**  
  - 功能：设置简易定时提醒（等价于 `/提醒`）。  
  - 参数：`time_expression`（必填，如 `3分钟后`、`08:30`、`2026-02-28-08:00`）；`text`（必填，提醒内容）。  
  - 说明：APScheduler 持久化，重启后仍会按时触发。

- **bookkeeping_add_expense**  
  - 功能：记录一笔支出并由 LLM 自动分类。  
  - 参数：`amount`（必填，number）；`description`（选填，string）。

- **bookkeeping_add_income**  
  - 功能：记录一笔收入并由 LLM 自动分类。  
  - 参数：`amount`（必填，number）；`description`（选填，string）。

- **bookkeeping_summary**  
  - 功能：查看当前用户记账总收入、总支出、余额及简要 AI 财务建议。  
  - 参数：无。

- **smart_search**  
  - 功能：百度千帆智能搜索（ai_search/chat/completions），结果交由当前会话 LLM 整理输出。  
  - 参数：`query`（必填，string）。  
  - 说明：本地每日最多 100 次（`DAILY_LIMIT_SMART`），超限拒绝。

- **web_search**  
  - 功能：百度千帆网页搜索（ai_search/web_search），结果交由当前会话 LLM 整理输出。  
  - 参数：`query`（必填，string）。  
  - 说明：本地每日最多 1000 次（`DAILY_LIMIT_WEB`），超限拒绝。

- **anime_trace**  
  - 功能：AnimeTrace 识别动漫图片所属番剧、角色等。  
  - 参数：`image`（选填，string）— 图片 URL 或本地路径；留空时从当前会话最近一条带图消息取第一张图。  
  - 说明：适用于「这是谁/出自哪部番/帮我搜番」等，返回番剧标题、相似度、集数/时间点及预览链接等。

- **music_play**  
  - 功能：按歌曲名或关键词点歌，自动选最匹配的一首并返回播放链接。  
  - 参数：`keyword`（必填，string），如 `青花`、`夜曲 周杰伦`。  
  - 说明：复用原 music_pro 行为，柠柚点歌 + 网易云 API，返回文本含可播放音频 URL。

- **send_email**  
  - 功能：使用配置的 QQ 邮箱向指定收件人发送邮件。  
  - 参数：`to_addr`（必填，string）、`subject`（必填，string）、`body`（必填，string）。  
  - 说明：需在插件配置中填写发件人邮箱与 QQ 邮箱授权码。若 AI 在对话中只说「已发送」却未真正发信（用户收不到），请直接用命令「/发邮件 收件人 主题 正文」确保发出。

---

## 四、命令与 LLM 工具对应关系

| 功能 | 命令示例 | LLM 工具 |
|------|----------|----------|
| 股票 | `/股票 查询 600519` | `stock_query` |
| 天气 | `/天气 北京 5` | `weather_query` |
| 火车票 | `/火车票 厦门 上海` | `train_query` |
| 简易提醒 | `/提醒 3分钟后 喝水` | `simple_reminder` |
| 记账支出/收入/统计 | `记账支出 35 午饭`、`查账统计` | `bookkeeping_add_expense` / `bookkeeping_add_income` / `bookkeeping_summary` |
| 点歌 | `/点歌 青花` | `music_play` |
| 智能/网页搜索 | `/智能搜索 …`、`/搜索 …` | `smart_search`、`web_search` |
| 动漫识别 | `/搜番`（带图） | `anime_trace` |
| 发邮件 | `/发邮件 收件人 主题 正文` | `send_email` |

---

## 五、股票模块说明

- **支持的入口**：`/股票`、`/stock`、`/自选股`、`/行情` 都会进入同一套股票能力。
- **支持的查询参数**：既可以用 6 位股票代码，如 `600519`、`000001`，也可以直接用股票名称关键字，如 `贵州茅台`、`平安银行`。
- **查询命令**：`/股票 查询 代码或名称`
  例如：`/股票 查询 600519`、`/股票 查询 贵州茅台`
- **自选股管理**：`/股票 添加 代码`、`/股票 删除 代码`、`/股票 列表`
  例如：`/股票 添加 600519`、`/股票 删除 000001`、`/股票 列表`
- **定时提醒**：`/股票 提醒 HH:MM`
  例如：`/股票 提醒 09:30`
  说明：用于在固定时间推送自选股行情，通常配合“添加”后的自选股一起使用。
- **价格提醒**：`/股票 跌到 代码 价格`、`/股票 涨到 代码 价格`
  例如：`/股票 跌到 600519 1800`、`/股票 涨到 600519 2000`
- **分析能力**：`/股票 量化分析 代码`、`/股票 智能分析 代码`
  例如：`/股票 量化分析 600519`、`/股票 智能分析 000001`
- **返回结果说明**：查询会优先返回实时行情；名称匹配到多只股票时，会返回候选列表，建议改用股票代码再次查询以获得更稳定的结果。
- **数据源说明**：默认优先走东方财富 A 股快照，若主源暂时不可用，会自动切换到新浪备用源继续取数。

---

## 六、在 Agent / Skill 中使用建议

- **工具发现**：将上述工具的 `name`、`description`、`parameters` 暴露给 Agent，由大模型根据自然语言自动选工具。
- **提示词建议**（可写入系统提示词）：
  > 你可以使用以下工具：  
  > - `stock_query`：查询 A 股股票行情  
  > - `weather_query`：查询城市天气  
  > - `train_query`：查询火车票车次  
  > - `simple_reminder`：帮用户设置定时提醒  
  > - `bookkeeping_add_expense` / `bookkeeping_add_income` / `bookkeeping_summary`：记账与查统计  
  > - `smart_search` / `web_search`：需要联网查资料时调用  
  > - `anime_trace`：用户发动漫截图并问「这是谁/出自哪部番」时调用  
  > - `music_play`：用户要点歌、放歌时调用  
  > - `send_email`：用户要发邮件时调用  

这样 Agent 在理解用户自然语言意图时，即可自动发现并调用本插件提供的这些技能。

---

## 七、插件整合与结构说明

### 整合来源与参考代码

- 火车票：原 `astrbot_plugin_train`
- 智能定时任务：原 `astrbot_plugin_sy`
- 股票与自选股：原 `astrbot_plugin_stock`
- 基金/量化分析：原 `astrbot_plugin_fund_analyzer-master`
- 天气：原 `astrbot_plugin_nyweather_char`
- Epic 免费游戏：原 `astrbot_plugin_Epicfell_char`
- 记账：原 `astrbot_plugin_bookkeeping`
- 今日运势：原 `astrbot_plugin_jrys`
- 点歌：原 `astrbot_plugin_music_pro`
- OCR：调用视觉/多模态模型识别图中文字，支持多服务商与兜底
- 千帆：`/智能搜索`（ai_search 对话）、`/搜索`（web_search + 当前 LLM 整理）
- 动漫识别：AnimeTrace API
- 发邮件：QQ 邮箱 SMTP

本插件**不**对「非 `/` 开头」的普通消息做意图识别；所有功能通过**命令**或 **Agent 内 LLM 工具**调用，便于与 MCP 等指令/工具体系集成。

### 基金/股票分析代码来源说明

- `fund_analysis_utils.py`：基金命令路由、兼容 `/股票 基金...` 子命令的整合层，基于 `astrbot_plugin_fund_analyzer-master/main.py` 的相关基金分析逻辑改造后接入当前插件。
- `fund_analyzer/`：量化指标、AI 分析、新闻与因子整理等底层能力，主要来自 `astrbot_plugin_fund_analyzer-master/ai_analyzer/` 与其周边实现。
- `fund_stock/`：A 股搜索/行情、六维分析、多空博弈提示词与结果汇总，主体来源于 `astrbot_plugin_fund_analyzer-master/stock/`，其中多智能体博弈架构参考了原仓库 README 中提到的 [FinGenius](https://github.com/HuaYaoAI/FinGenius) 思路。
- `eastmoney_api.py`：基金/ETF/LOF 实时行情、历史走势、资金流等接口封装，来自 `astrbot_plugin_fund_analyzer-master` 的东方财富数据访问逻辑，并按当前仓库结构做了整理。
- `fund_templates/`、`image_generator.py`：基金分析图片报告模板与渲染逻辑，来自 `astrbot_plugin_fund_analyzer-master/templates/` 及其配套生成代码。
- `stock_utils.py`：当前仓库自有的股票自选、提醒、查询入口；现已统一改为通过 `fund_stock/analyzer.py` 的 `akshare` 路径获取 A 股行情与名称搜索结果。

### 目录与代码结构

- `main.py`：插件元信息、指令路由注册、LLM 工具注册，业务逻辑下沉到 utils。
- `train_utils.py`、`sy_scheduler_utils.py`、`stock_utils.py`、`fund_analysis_utils.py`、`weather_utils.py`、`epic_utils.py`、`bookkeeping_utils.py`、`jrys_utils.py`、`ocr_utils.py`、`qianfan_search_utils.py`、`music_utils.py`、`anime_utils.py`、`email_utils.py`、`memory_utils.py`：各功能实现。
- `fund_analyzer/`、`fund_stock/`、`eastmoney_api.py`：基金/量化分析与多智能体博弈分析的底层模块。
- `memory_utils.py`：基于 QQ 号的永久记忆层，支持手工绑定“谁是谁”，并在普通聊天进入 LLM 前自动注入当前用户记忆。
- 基金相关能力现已支持直接通过 `/基金` 调用，原 `/股票` 下的基金子命令保留兼容。
- 股票行情与股票名称搜索已切到 `akshare` 路径；`/股票 搜索股票`、`/股票 查询`、自选股列表与提醒均通过这一链路取数，依赖 `akshare` 与 `pandas`。
- `/基金 智能`、`/基金 博弈` 以及兼容的 `/股票 智能分析`、`/股票 股票智能分析` 依赖已配置的大模型提供商。
- `/股票 量化分析`、`/股票 智能分析`、`/股票 股票智能分析` 现已切到真正的 A 股数据链路，使用 `akshare` 提供的 A 股实时行情、历史 K 线和个股资金流。
- `/基金 博弈` 与 `/股票 股票智能分析` 当前都已收敛为 3 次 AI 对话：六维联合分析 1 次、多空综合辩论 1 次、裁判裁定 1 次。
- 普通聊天进入默认大模型前，会通过 `on_llm_request` 钩子自动读取当前 QQ 的永久记忆，让 bot 先知道“当前这个人是谁”再回复。
- 新增功能请优先新增独立 `xxx_utils.py`，并在本 README「已注册的 LLM 工具一览」中补充说明；同时提供命令入口与至少一个 LLM 工具（FunctionTool）封装。

### 配置统一（_conf_schema.json）

- 仅保留一个 `_conf_schema.json`，按模块分组：`train`、`sy`、`stock`、`weather`、`epic`、`jrys`、`ocr`、`qianfan_search`、`music`、邮件等。
- 字段使用模块前缀（如 `train_`、`weather_`），详见同目录 `_conf_schema.json`。
- 今日运势资源：将原 jrys 插件的 `backgroundFolder` 与 `font` 拷贝到 `astrbot_all_char/jrys_assets/` 下。

### 文档与更新规范

- 功能/配置/指令变更须同步更新本 README。
- 更新时建议注明：日期与版本、涉及模块、是否改动 `_conf_schema.json`、是否需要用户迁移数据或重配。

---

## 参考来源

- 记账：https://github.com/NONAME00X/astrbot_plugin_bookkeeping  
- 天气：https://github.com/ningyou8023/astrbot_plugin_nyweather  
- 运势：https://github.com/NINIYOYYO/astrbot_plugin_jrys  
- 点歌：https://github.com/Zhalslar/astrbot_plugin_music  
- 生图：https://github.com/muyouzhi6/astrbot_plugin_gitee_aiimg  
- 基金/股票分析整合来源：工作区内 `astrbot_plugin_fund_analyzer-master/` 目录及其自带 README/源码  
- 多智能体博弈灵感来源：https://github.com/HuaYaoAI/FinGenius  
