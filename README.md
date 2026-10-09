# 每日复盘 Agent（Daily Market Review Agent）

一个基于 **CrewAI 多智能体协作** 的 A 股盘后复盘自动化系统：收盘后自动抓取行情与资讯、交叉核验数据、
生成带 K 线图的专业复盘报告，并邮件交付给客户。

> ⚠️ 本系统输出仅供研究参考，不构成任何投资建议。

---

## 1. 功能特性

- **多 Agent 流水线**：研究员 → 事实核查员 → 复盘校验员 → 盘面分析师 → **模拟交易员** → 专业投资顾问，各司其职、上下文隔离。
- **防幻觉设计**：独立「事实核查员」在分析前重新取数、逐项交叉验证，输出差异清单与修正后情报。
- **往期预测验证（RAG）**：检索历史复盘里「对未来的预测」，与今日真实走势逐条比对，形成「预测 → 验证 → 纠偏」闭环。
- **模拟交易**：内置 **10 万元**模拟盘，复盘时按**收盘价**自动买卖个股/ETF，输出「交易流水 + 持仓盈亏」表格（**不发邮件**）。
- **真实数据源**：腾讯行情、东方财富（延时/涨跌停/快讯）、必应搜索，全部免费公开接口。
- **可视化报告**：自动生成近三个月指数 K 线图（蜡烛图 + 均线 + 成交量），内嵌进报告与邮件。
- **规范化表格**：指数表、行业/概念板块的涨/跌/资金四榜均为规范 Markdown 表格，邮件内渲染为 HTML 表格。
- **邮件交付**：Markdown → HTML 渲染（含表格、图片 base64 内联），支持 QQ/163 等 SMTP。
- **健壮性**：数据接口重试与降级、LLM 空响应自动重试、交付与写作分离确保一定发送。

---

## 2. 系统架构

### 2.1 Agent 流水线

```
研究员(0.8) → 核查员(0.3) → 复盘校验员(0.3) → 分析师(0.8) → 模拟交易员(0.8) → 撰稿人(0.8) → 交付
  收集情报      独立复核/防幻觉   检索往期预测+命中率    量价/技术/情绪   10w模拟盘收盘交易   撰写+配图   存档+发邮件
```

### 2.2 任务链（CrewAI `Process.sequential`）

| 任务 | Agent | 说明 | 落盘 |
|---|---|---|---|
| `researcher_task` | 研究员 | 收集大盘/板块/宽度/情绪/消息/外围 | `outputs/01_research_{date}.md` |
| `fact_check_task` | 事实核查员 | 只审研究员输出，重取数据交叉验证 | `outputs/01b_factcheck_{date}.md` |
| `review_task` | 复盘校验员 | **RAG 检索往期预测**，与今日走势逐条验证 | `outputs/01c_review_{date}.md` |
| `analyzer_task` | 盘面分析师 | 基于修正情报 + 往期校准做深度分析 | `outputs/02_analysis_{date}.md` |
| `paper_trade_task` | 模拟交易员 | **10w 模拟盘收盘交易**，输出交易表（不发邮件） | `paper/交易_{date}.md` |
| `writer_task` | 撰稿人 | 撰写最终报告并插入 K 线图 | `outputs/03_report_{date}.md` |
| `deliver_task` | 撰稿人 | 存档 + 发送邮件（独立任务确保执行） | `outputs/04_delivery_{date}.md` |

- 下游任务通过 CrewAI 的 `context` 获取上游任务输出；写作与交付分离，避免「只写不交」。
- 最终报告同时归档到 `reports/每日复盘_{date}.md`，K 线图在 `reports/charts/`；模拟交易在 `paper/`。

---

## 3. 目录结构

```
每日复盘agent/
├── main.py                       # 入口：CLI 参数、组装 Crew、kickoff
├── agents.py                     # 6 个 Agent + LLM 配置 + 空响应重试
├── tasks.py                      # 7 个串行任务
├── tools.py                      # 12 个工具 + 数据源解析 + 邮件渲染
├── rag.py                        # 往期复盘检索（轻量 RAG：TF-IDF + 时间衰减 + 预测偏置）
├── ledger.py                     # 预测命中率滚动统计（确定性解析历史验证表）
├── paper.py                      # 模拟交易账户（10w 本金、收盘成交、状态持久化 + 表格输出）
├── requirements.txt
├── .env.example                  # 环境变量模板（复制为 .env）
├── .gitignore                    # 排除 .env / reports / outputs / paper
├── BUGFIX-LLM空响应重试.md        # 一次线上问题的复盘记录
├── reports/                      # 归档报告与图表（git 忽略）
└── outputs/                      # 各阶段中间产物（git 忽略）
```

---

## 4. 快速开始

### 4.1 安装

```bash
pip install -r requirements.txt
```

依赖：`crewai`、`requests`、`beautifulsoup4`、`lxml`、`python-dotenv`、`matplotlib`。

### 4.2 配置

复制 `.env.example` 为 `.env` 并填写：

```ini
DEEPSEEK_API_KEY=sk-你的密钥
DEEPSEEK_BASE_URL=https://api.deepseek.com/v1
DEEPSEEK_MODEL=deepseek-v4-flash
LLM_TEMPERATURE=0.8
LLM_MAX_TOKENS=8192

SMTP_HOST=smtp.qq.com
SMTP_PORT=587
SMTP_USER=your@qq.com
SMTP_PASS=你的SMTP授权码
FROM_EMAIL=your@qq.com
REPORT_TO_EMAIL=client@example.com

AGENT_VERBOSE=true
CREWAI_TRACING_ENABLED=false
LLM_EMPTY_RETRY=3
LLM_RETRY_DELAY=2
```

### 4.3 运行

```bash
python main.py                          # 复盘今天
python main.py --date 2026-09-11        # 指定日期
python main.py --to client@qq.com       # 指定收件邮箱
python main.py --quiet                  # 关闭过程日志
```

---

## 5. 配置项说明

| 变量 | 必填 | 默认 | 说明 |
|---|---|---|---|
| `DEEPSEEK_API_KEY` | ✅ | — | DeepSeek API Key |
| `DEEPSEEK_BASE_URL` | | `https://api.deepseek.com/v1` | OpenAI 兼容地址 |
| `DEEPSEEK_MODEL` | | `deepseek-v4-flash` | 模型名（内部加 `deepseek/` 前缀走原生 provider） |
| `LLM_TEMPERATURE` | | `0.8` | 创作类 Agent 温度 |
| `LLM_MAX_TOKENS` | | `16384` | 单次输出上限 |
| `LLM_EMPTY_RETRY` | | `3` | LLM 空响应最大重试次数 |
| `LLM_RETRY_DELAY` | | `2` | 空响应退避基数（秒，线性退避） |
| `SECTOR_CACHE_TTL` | | `600` | 板块资金接口进程内缓存（秒），防限流 |
| `SMTP_HOST` / `SMTP_PORT` | | `smtp.qq.com` / `587` | SMTP 服务器 |
| `SMTP_USER` / `SMTP_PASS` | | — | 账号 / 授权码（非登录密码） |
| `FROM_EMAIL` | | =`SMTP_USER` | 发件人 |
| `REPORT_TO_EMAIL` | | — | 收件人（留空则只存档） |
| `AGENT_VERBOSE` | | `true` | 是否打印过程日志 |
| `HTTP_TIMEOUT` | | `20` | 数据接口超时（秒） |
| `REPORT_DIR` / `OUTPUT_DIR` | | `reports` / `outputs` | 产物目录 |
| `PAPER_INITIAL_CASH` | | `100000` | 模拟盘初始资金（元） |
| `PAPER_DIR` | | `paper` | 模拟盘状态与报表目录 |

---

## 6. 工具体系（`tools.py`）

| 工具 | 作用 | 数据源 |
|---|---|---|
| `get_market_overview` | 八大指数收盘 + 两市成交额 | 腾讯行情 |
| `get_index_kline` | K 线 + MA5/10/20/60 + 区间高低 + 量能 | 腾讯行情 |
| `generate_index_charts` | 生成近三月指数 K 线 PNG（供报告/邮件） | 腾讯 + matplotlib |
| `get_sector_flow` | 行业/概念板块 涨幅/跌幅/净流入/净流出 四榜（Markdown 表） | 东方财富延时 |
| `get_market_breadth` | 全市场涨/跌/平/涨跌停家数 | 东方财富延时 |
| `get_limit_pool` | 涨停池 + 连板梯队 + 炸板率 + 跌停家数 | 东方财富 |
| `get_market_news` | 7×24 财经快讯 | 东方财富 |
| `get_global_markets` | 恒生/道指/纳指/标普/美元指数 | 腾讯行情 |
| `web_search` | 网页搜索（标题/摘要/链接） | 必应 |
| `fetch_webpage` | 抓取网页正文 | — |
| `save_report_file` | 保存/归档 Markdown（支持 `source_file`） | — |
| `send_email_report` | 渲染并发送 HTML 邮件（支持 `source_file`） | SMTP |

### 6.1 数据源与稳健性

- **腾讯行情**（`qt.gtimg.cn` / `web.ifzq.gtimg.cn`）：A股指数/个股/ETF 快照与 K 线，稳定、不限流。
- **新浪财经**（`hq.sinajs.cn`）：**国际市场兜底**——美元指数（`DINIW`）、布伦特原油（`hf_OIL`）、纽约黄金（`hf_GC`）；
  板块涨跌幅**备用源**（`newSinaHy.php` / `newFLJK.php`）。
- **东方财富涨跌停**（`push2ex.eastmoney.com`）、**快讯**（`np-listapi.eastmoney.com`）：稳定。
- **东方财富延时行情**（`push2delay.eastmoney.com`）：板块资金、涨跌家数走其 `clist/get`。
- ⚠️ **已知限制**：东财 `clist/get` 对**高频访问的 IP 会 WAF 拦截**（表现为 TCP `RemoteDisconnected`，换主机/参数/Cookie 均无效）。
  应对：① 板块加**进程内缓存**（`SECTOR_CACHE_TTL`）；② 板块限流时**自动降级到新浪`（仅涨跌幅+领涨股，无主力资金）；③ 涨跌家数暂无双源，缺失时**如实标注、不估算**。
- 所有接口均带**重试**；失败返回可读提示，便于 Agent 降级处理。
- 提示：`get_market_overview` 已返回全部 8 大指数收盘；报告中个别指数「近5日量能」缺失，是因为该字段由 `get_index_kline` 单独计算，属**采集范围**问题，非接口故障。

### 6.2 邮件渲染

`_markdown_to_html()` 支持：标题、**表格（带边框样式）**、列表、引用、粗体、链接，
以及 **图片 base64 内联**（本地 K 线图转 data URI，保证邮件客户端可显示）。

### 6.3 往期预测检索与命中率统计（RAG，`rag.py` / `ledger.py`）

| 工具 | 作用 |
|---|---|
| `retrieve_past_predictions` | 检索往期复盘报告里「对未来的预测」段落（`query / top_k / days_back / exclude_date`） |
| `get_prediction_stats` | 确定性解析历史复盘里的验证表，统计**全时段 + 近 N 份的滚动命中率** |

- **检索对象**：`reports/每日复盘_YYYY-MM-DD.md`，按 Markdown 标题切成 section 作为检索单元。
- **检索算法**：中文 **字符 bigram** 切分 + **TF-IDF 余弦相似度** + **时间衰减** + **预测词偏置**（命中「预判/情景/概率/支撑/压力/操作策略…」加权）。
- **命中率统计**：解析每份报告「二、往期预测验证」表的 ✅/⚠️/❌ 标记，以「原预测日期 + 归一化预测文本」去重，输出命中率与滚动命中率（纯确定性、不依赖 LLM）。
- **无外部依赖、可离线**；如需升级为向量检索，替换 `_score` 的相似度即可，工具接口不变。
- 由「复盘校验员」调用，产出《往期预测验证》并进入最终报告第二节。

#### 效果示例（取自 2026-10-08 报告，节选）

**滚动命中率**：可验证 36 条 —— ✅命中 19（52.8%）、⚠️部分命中 7、❌偏离 10；宽松命中率 72.2%。

| 报告日期 | 预测内容（含关键点位） | 实际走势 | 结论 | 归因 |
|---|---|---|---|---|
| 2026-10-03 | 上证**第一压力 3864（MA5）** | 今日最高 **3864.12**，收盘未站上 | ✅ 命中（差 0.12 点） | MA5 承压判断近乎零误差 |
| 2026-10-03 | 减仓信号：**创业板收盘破 3125 → 减至 2–3 成** | 创业板收 3036.66，明确破位 | ✅ 命中（信号生效） | 收盘口径纪律再次有效 |
| 2026-09-19 | 未来 1–2 周中枢：**震荡偏强** | 09-22 收 3952.13 → 10-08 收 3811.90（-3.55%） | ❌ 偏离 | 以放量上攻外推"偏强"，忽略量能硬约束 |

> 说明：同一预测可能在多份报告中出现，台账按「原预测日期 + 预测文本」去重，避免重复计数。

### 6.4 模拟交易（`paper.py`）

| 工具 | 作用 |
|---|---|
| `get_quote` | 取个股/ETF 最新价（收盘价） |
| `paper_portfolio` | 查看现金、持仓、浮动盈亏、总资产 |
| `paper_buy(code, amount, reason)` | 按收盘价买入（100 股整数倍） |
| `paper_sell(code, ratio, reason)` | 按比例卖出（`1.0`=清仓） |
| `paper_report` | 生成交易报表并写入 `paper/交易_{date}.md`（含账户汇总 / **净资产曲线** / 逐日收益） |

- 初始资金 **10 万元**；可买个股与 ETF；**不含手续费/印花税**（纯模拟）。
- 状态持久化在 `paper/portfolio.json`（账户）+ `paper/trades.json`（流水）+ `paper/equity.json`（每日净值）。
- **每日净值曲线**：每次生成报表时 upsert 当日净值快照，输出 `净资产曲线_{date}.png` 与逐日收益表（当日/累计收益率、仓位）。
- **并发安全**：所有「读-改-写」用全局锁串行化（CrewAI 可能并发调用工具，避免状态被覆盖）。
- 交易纪律（写在任务提示中）：持仓 2–4 只、单票 ≤30%、总仓位 ≤80%、浮亏 7–8% 或破位止损、理由 ≤20 字。
- 该任务**只输出表格与曲线、不发邮件**。

---

## 7. 关键设计

1. **独立上下文**：每个 Agent 独立上下文窗口，核查、分析、写作互不污染。
2. **事实核查防幻觉**：核查员只依据工具原始返回逐项比对，输出「✅一致 / ❌错误 / ⚠️无法核验」清单与修正值，
   下游分析师以修正值为准。
3. **数据由代码产出**：板块表格、K 线图等由工具直接生成，LLM 只做组织与解读，降低编造空间。
4. **交付与写作分离**：`writer_task` 只产出正文，`deliver_task` 只负责存档发送，确保一定触发邮件。
5. **空响应自动重试**：在实例层给 `llm.call` 包裹重试，规避 DeepSeek 偶发空回复导致的任务中断
   （详见 `BUGFIX-LLM空响应重试.md`）。
6. **预测闭环（RAG）**：每次复盘先检索往期报告里对未来的预测，用今日真实走势逐条验证并归因，
   校准本期判断，避免口径漂移与重复犯错。

---

## 8. 输出产物

```
reports/
├── 每日复盘_YYYY-MM-DD.md          # 最终归档报告
└── charts/
    ├── sh000001_YYYYMMDD.png       # 上证指数 K 线
    ├── sz399001_YYYYMMDD.png       # 深证成指
    ├── sz399006_YYYYMMDD.png       # 创业板指
    └── sh000300_YYYYMMDD.png       # 沪深300
outputs/                            # 各阶段中间产物
paper/                              # 模拟交易（git 忽略）
├── portfolio.json                  # 账户状态（现金/持仓）
├── trades.json                     # 交易流水
├── equity.json                     # 每日净值快照（净资产曲线数据源）
├── 净资产曲线_YYYY-MM-DD.png       # 净资产曲线图
└── 交易_YYYY-MM-DD.md              # 当日交易报表（表格 + 曲线 + 逐日收益）
```

---

## 9. 故障排查

| 现象 | 可能原因 | 处理 |
|---|---|---|
| `DEEPSEEK_API_KEY 未设置` | `.env` 缺失或未加载 | 检查 `.env`，确认在项目根目录 |
| `Invalid response from LLM call - None or empty` | DeepSeek 偶发空响应 | 已内置重试；可调大 `LLM_EMPTY_RETRY` |
| 邮件发送失败 | SMTP 未配置/授权码错误 | 检查 `SMTP_*`；QQ 邮箱需用「授权码」 |
| 板块/宽度数据失败 | 数据接口临时限流 | 重跑；工具内置重试与降级 |
| 报告无 K 线图 | 图表生成失败/图片路径失效 | 检查 `matplotlib` 与中文字体；查看 `reports/charts/` |

---

## 10. 安全说明

- `.env` 含密钥与 SMTP 授权码，已被 `.gitignore` 排除，**切勿提交**。
- `reports/`、`outputs/`、`__pycache__/` 亦被忽略。
- 请使用**最小权限**的邮箱授权码，并在泄露时及时更换。

---

## 11. 免责声明

本项目及其生成的报告仅供技术学习与研究参考，**不构成任何投资建议**。
据此操作，风险自负。
