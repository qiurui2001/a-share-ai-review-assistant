# 每日复盘 Agent（Daily Market Review Agent）

一个基于 **CrewAI 多智能体协作** 的 A 股盘后复盘自动化系统：收盘后自动抓取行情与资讯、交叉核验数据、
生成带 K 线图的专业复盘报告，并邮件交付给客户。

> ⚠️ 本系统输出仅供研究参考，不构成任何投资建议。

---

## 1. 功能特性

- **多 Agent 流水线**：研究员 → 事实核查员 → 盘面分析师 → 专业投资顾问，各司其职、上下文隔离。
- **防幻觉设计**：独立「事实核查员」在分析前重新取数、逐项交叉验证，输出差异清单与修正后情报。
- **真实数据源**：腾讯行情、东方财富（延时/涨跌停/快讯）、必应搜索，全部免费公开接口。
- **可视化报告**：自动生成近三个月指数 K 线图（蜡烛图 + 均线 + 成交量），内嵌进报告与邮件。
- **规范化表格**：指数表、行业/概念板块的涨/跌/资金四榜均为规范 Markdown 表格，邮件内渲染为 HTML 表格。
- **邮件交付**：Markdown → HTML 渲染（含表格、图片 base64 内联），支持 QQ/163 等 SMTP。
- **健壮性**：数据接口重试与降级、LLM 空响应自动重试、交付与写作分离确保一定发送。

---

## 2. 系统架构

### 2.1 Agent 流水线

```
┌─────────────┐   ┌──────────────┐   ┌──────────────┐   ┌──────────────┐   ┌──────────┐
│  研究员     │ → │  事实核查员  │ → │  盘面分析师  │ → │  专业投资顾问│ → │  交付    │
│ researcher  │   │ fact_checker │   │  analyzer    │   │  writer      │   │ deliver  │
│ 温度 0.8    │   │ 温度 0.3     │   │ 温度 0.8     │   │ 温度 0.8     │   │          │
└─────────────┘   └──────────────┘   └──────────────┘   └──────────────┘   └──────────┘
   收集情报          独立复核/修正      量价/技术/情绪      撰写+配图         存档+发邮件
```

### 2.2 任务链（CrewAI `Process.sequential`）

| 任务 | Agent | 说明 | 落盘 |
|---|---|---|---|
| `researcher_task` | 研究员 | 收集大盘/板块/宽度/情绪/消息/外围 | `outputs/01_research_{date}.md` |
| `fact_check_task` | 事实核查员 | 只审研究员输出，重取数据交叉验证 | `outputs/01b_factcheck_{date}.md` |
| `analyzer_task` | 盘面分析师 | 基于修正后情报做深度分析 | `outputs/02_analysis_{date}.md` |
| `writer_task` | 撰稿人 | 撰写最终报告并插入 K 线图 | `outputs/03_report_{date}.md` |
| `deliver_task` | 撰稿人 | 存档 + 发送邮件（独立任务确保执行） | `outputs/04_delivery_{date}.md` |

- 下游任务通过 CrewAI 的 `context` 获取上游任务输出；写作与交付分离，避免「只写不交」。
- 最终报告同时归档到 `reports/每日复盘_{date}.md`，K 线图在 `reports/charts/`。

---

## 3. 目录结构

```
每日复盘agent/
├── main.py                       # 入口：CLI 参数、组装 Crew、kickoff
├── agents.py                     # 4 个 Agent + LLM 配置 + 空响应重试
├── tasks.py                      # 5 个串行任务
├── tools.py                      # 12 个工具 + 数据源解析 + 邮件渲染
├── requirements.txt
├── .env.example                  # 环境变量模板（复制为 .env）
├── .gitignore                    # 排除 .env / reports / outputs
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
| `LLM_MAX_TOKENS` | | `8192` | 单次输出上限 |
| `LLM_EMPTY_RETRY` | | `3` | LLM 空响应最大重试次数 |
| `LLM_RETRY_DELAY` | | `2` | 空响应退避基数（秒，线性退避） |
| `SMTP_HOST` / `SMTP_PORT` | | `smtp.qq.com` / `587` | SMTP 服务器 |
| `SMTP_USER` / `SMTP_PASS` | | — | 账号 / 授权码（非登录密码） |
| `FROM_EMAIL` | | =`SMTP_USER` | 发件人 |
| `REPORT_TO_EMAIL` | | — | 收件人（留空则只存档） |
| `AGENT_VERBOSE` | | `true` | 是否打印过程日志 |
| `HTTP_TIMEOUT` | | `20` | 数据接口超时（秒） |
| `REPORT_DIR` / `OUTPUT_DIR` | | `reports` / `outputs` | 产物目录 |

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

- **腾讯行情**（`qt.gtimg.cn` / `web.ifzq.gtimg.cn`）：指数/个股快照与 K 线，稳定、不限流。
- **东方财富延时行情**（`push2delay.eastmoney.com`）：盘后复盘足够，比主站 `push2` 稳定，规避限流。
- **东方财富涨跌停**（`push2ex.eastmoney.com`）、**快讯**（`np-listapi.eastmoney.com`）。
- 所有接口均带**重试**；失败返回可读提示，便于 Agent 降级处理。

### 6.2 邮件渲染

`_markdown_to_html()` 支持：标题、**表格（带边框样式）**、列表、引用、粗体、链接，
以及 **图片 base64 内联**（本地 K 线图转 data URI，保证邮件客户端可显示）。

---

## 7. 关键设计

1. **独立上下文**：每个 Agent 独立上下文窗口，核查、分析、写作互不污染。
2. **事实核查防幻觉**：核查员只依据工具原始返回逐项比对，输出「✅一致 / ❌错误 / ⚠️无法核验」清单与修正值，
   下游分析师以修正值为准。
3. **数据由代码产出**：板块表格、K 线图等由工具直接生成，LLM 只做组织与解读，降低编造空间。
4. **交付与写作分离**：`writer_task` 只产出正文，`deliver_task` 只负责存档发送，确保一定触发邮件。
5. **空响应自动重试**：在实例层给 `llm.call` 包裹重试，规避 DeepSeek 偶发空回复导致的任务中断
   （详见 `BUGFIX-LLM空响应重试.md`）。

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
