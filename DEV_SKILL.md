## astrbot_all_char 开发 Skill（给未来的自己和 AI 看）

这份文档是专门给维护 `astrbot_all_char` 的开发者和 AI 助手看的「开发 Skill」，用来提醒后续所有改动都遵守同一套规则，避免遗忘。

---

### 1. 集成目标与范围

- **统一插件**：把以下插件的功能整合进来：
  - 火车票：`astrbot_plugin_train`
  - 定时任务：`astrbot_plugin_sy`
  - 股票：`astrbot_plugin_stock`
  - 天气：`astrbot_plugin_nyweather_char`
  - Epic 免费游戏：`astrbot_plugin_Epicfell_char`
- 后续如有新的 char 系列插件，也可以继续往这个合集里加，但必须遵守本 Skill。

---

### 2. main.py 的硬性约束（当前以命令为主）

- `main.py` 只做三件事：
  1. 声明插件元信息（name、desc、help 等）；
  2. 注册各模块的命令入口（自然语言 Hook 为可选增强，不是当前必须项）；
  3. 做少量初始化（如加载配置、挂载 scheduler 等）。
- **禁止**在 `main.py` 中堆叠大段业务逻辑或复杂 if/else：
  - 所有真实逻辑必须拆到对应的 `*_utils.py` 或子包。

---

### 3. 模块划分与命名约定

- 每个原插件对应一个独立模块（可以是单文件 utils，也可以是子包）：
  - 火车：`train_utils.py`
  - 定时任务：`sy_scheduler_utils.py`
  - 股票：`stock_utils.py`
  - 天气：`weather_utils.py`
  - Epic：`epic_utils.py`
- 模块内部再按需要拆分函数即可，但对外只暴露**清晰的入口函数**，例如：
  - `handle_train_command(...)`
  - `handle_sy_reminder_command(...)`
  - `handle_stock_query(...)`
  - `handle_weather_command(...)`
  - `handle_epic_command(...)`

---搜索

### 4. 指令与自然语言不打架的规则

- **主命令前缀必须唯一**：
  - `/提醒`（简易提醒）、`/rmd`、`/rmdg`、`/股票`、`/stock`、`/天气`、`/nyweather`、`/epic`、`/Epic免费` 等都要列出来，新增前先检查。
- **自然语言 Hook 的处理顺序要小心**：
  - 自然语言触发后，如已成功处理，应主动返回并阻止消息继续流转到其他模块/LLM。
  - 如果多个模块都有可能命中同一条自然语言，优先考虑：
    - 是否可以通过**关键词组合**严格区分；
    - 或让某个模块在无法确定时「放行」，交给下一个模块尝试。

---

### 5. 配置：统一到一个 _conf_schema.json

- 所有配置项都集中在 `astrbot_all_char/_conf_schema.json` 中，**写法参考项目根目录的 `样例` 文件（更结构化、带滑块/选项提示）**：
  - 火车相关：以 `train_` 开头；
  - 定时任务相关：以 `sy_` 开头；
  - 股票相关：以 `stock_` 开头；
  - 天气相关：以 `weather_` 开头；
  - Epic 相关：以 `epic_` 开头；
  - 今日运势相关：以 `jrys_` 开头；
  - 以后如有记账类等，可按需增加 `book_` 前缀配置。
- 如果从原插件迁移配置：
  - **语义和默认值保持一致**，仅做前缀/命名上的统一；
  - 在 `description` 或 `hint` 中标明「来源：某某原插件」。
 - 为了在面板中更好用，数值类配置尽量补上 `slider` 段（`min/max/step`），布尔/枚举类配置补充 `options` 和清晰的 `hint`，写法风格对齐 `样例` 文件。

---

### 6. 文档与更新的强制流程

每次修改/新增以下内容时，**必须同步更新 `astrbot_all_char/README.md`（以及后续可能的 `CHANGELOG.md`）**：

- 新增或修改指令/别名；
- 新增或修改自然语言触发逻辑；
- 调整 `_conf_schema.json` 中的字段、默认值或含义；
- 重要的行为变化（例如：数据存储路径、定时任务触发策略变化等）。
- 每次任务执行完毕后，必须更新 `astrbot_all_char/已更新.md`，至少记录日期、改动点、涉及文件。

推荐的更新步骤：

1. 先改代码与 `_conf_schema.json`；
2. 立刻在 `README.md` 中补充/修正文档；
3. 若是破坏性变更，在文档中写清迁移方式或兼容策略。
4. 补写 `已更新.md` 本次执行记录（不可省略）。

---

### 7. 给 AI 助手的特别提示

当你（AI）在这个仓库里帮忙写代码时，请优先遵守以下顺序思考：

1. 先看 `astrbot_all_char/README.md` 和本 `DEV_SKILL.md`；
2. 再根据需要查看原插件目录下的 README 与 `_conf_schema.json`，理解原始行为；
3. 在 `astrbot_all_char` 中：
   - 把逻辑写到对应 `*_utils.py`；
   - 在 `main.py` 中只做路由/注册；
   - 更新 `_conf_schema.json` 与 `.md` 文档；
4. 如果新增功能会影响指令或自然语言触发，务必检查是否与现有内容冲突。
