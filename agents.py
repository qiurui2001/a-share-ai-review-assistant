# -*- coding: utf-8 -*-
"""
agents.py —— 每日盘后复盘 Multi-Agent 团队
============================================

六个角色，串行协作：

    研究员(信息检索) → 核查员(事实核验/防幻觉) → 复盘校验员(往期预测验证/RAG)
    → 分析师(盘面分析) → 模拟交易员(10w模拟盘/收盘交易) → 撰稿人(交付)

设计要点
--------
- 统一由 DeepSeek 驱动；创作类角色温度 0.8，核验类角色用低温 0.3 求稳。
- 每个 Agent 拥有**独立的上下文**，避免互相污染。
- 工具只绑定各自岗位真正用得上的，降低误调用概率。
"""

from __future__ import annotations

import os
import time

from crewai import Agent, LLM
from dotenv import load_dotenv

from rag import retrieve_past_predictions
from ledger import get_prediction_stats
from paper import get_quote, paper_buy, paper_portfolio, paper_report, paper_sell
from tools import (
    fetch_webpage,
    generate_index_charts,
    get_global_markets,
    get_index_kline,
    get_limit_pool,
    get_market_breadth,
    get_market_news,
    get_market_overview,
    get_sector_flow,
    save_report_file,
    send_email_report,
    web_search,
)

load_dotenv()

# ──────────────────────────────────────────────────────────────────────────────
# LLM 配置（DeepSeek，OpenAI 兼容协议）
# ──────────────────────────────────────────────────────────────────────────────
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
DEEPSEEK_BASE_URL = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1")
DEEPSEEK_MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-flash")
LLM_TEMPERATURE = float(os.environ.get("LLM_TEMPERATURE", "0.8") or "0.8")
LLM_MAX_TOKENS = int(os.environ.get("LLM_MAX_TOKENS", "16384") or "16384")
AGENT_VERBOSE = os.environ.get("AGENT_VERBOSE", "true").lower() in ("1", "true", "yes")

# CrewAI 原生支持 deepseek 提供方（OpenAI 兼容），用 "deepseek/" 前缀即可，
# 无需额外安装 litellm。base_url 与 api_key 会被透传到 DeepSeek 接口。
_llm_model = DEEPSEEK_MODEL if "/" in DEEPSEEK_MODEL else f"deepseek/{DEEPSEEK_MODEL}"

llm = LLM(
    model=_llm_model,
    api_key=DEEPSEEK_API_KEY,
    base_url=DEEPSEEK_BASE_URL,
    temperature=LLM_TEMPERATURE,
    max_tokens=LLM_MAX_TOKENS,
)

# 部分 Agent 需要更冷静的判断（如数值校验），可另建低温模型
llm_precise = LLM(
    model=_llm_model,
    api_key=DEEPSEEK_API_KEY,
    base_url=DEEPSEEK_BASE_URL,
    temperature=0.3,
    max_tokens=LLM_MAX_TOKENS,
)


# ──────────────────────────────────────────────────────────────────────────────
# 空响应自动重试（防 DeepSeek 偶发空 message）
# ──────────────────────────────────────────────────────────────────────────────
# 背景：DeepSeek 长上下文的调用偶发返回空 assistant 消息（content 为空且无 tool_calls），
# CrewAI 会直接抛 "Invalid response from LLM call - None or empty." 中断任务。
# 这里不改 CrewAI 源码，只在实例层给 call 包一层：返回空则退避重试。
# 注意：LLM 调用本身无副作用（工具执行由 executor 负责），重试是安全的。
EMPTY_RETRY_TIMES = int(os.environ.get("LLM_EMPTY_RETRY", "3") or "3")
EMPTY_RETRY_DELAY = float(os.environ.get("LLM_RETRY_DELAY", "2") or "2")


def with_empty_retry(model, retries: int = EMPTY_RETRY_TIMES,
                     delay: float = EMPTY_RETRY_DELAY):
    """给 LLM 实例的 call 方法加「空响应重试」，返回空则退避重试 retries 次。"""
    original_call = model.call

    def call_with_retry(*args, **kwargs):
        last = None
        for attempt in range(1, retries + 1):
            last = original_call(*args, **kwargs)
            if last:  # 非空即成功（含正常文本或 tool_calls 列表）
                return last
            if attempt < retries:
                time.sleep(delay * attempt)  # 线性退避：2s, 4s, ...
        return last

    # 用 object.__setattr__ 绕过 pydantic 的字段校验，把函数写进实例 __dict__
    object.__setattr__(model, "call", call_with_retry)
    return model


llm = with_empty_retry(llm)
llm_precise = with_empty_retry(llm_precise)


# ══════════════════════════════════════════════════════════════════════════════
# Agent 1：研究员（信息检索专家）
# ══════════════════════════════════════════════════════════════════════════════
researcher = Agent(
    role="信息检索专家",
    goal=(
        "每天收盘后，快速、全面、准确地收集当日市场信息：大盘指数与量能、"
        "行业与概念板块的资金流向、市场宽度与情绪、财经与政策快讯、国际市场与外围形势，"
        "并整理成一份结构化的原始情报，为后续分析提供充足弹药。"
    ),
    backstory=(
        "你是一名有十年经验的全职个人投资者，只做波段、不做短线。你每天收盘后的第一件事就是"
        "把今天发生的一切翻个底朝天：指数怎么走、钱去了哪些板块、谁在涨谁在跌、政策说了什么、"
        "外盘什么脸色。你信奉「信息不完整就不下结论」，宁可多查三遍，也不肯漏掉关键利空或利好。"
        "你的输出追求客观、带数据、分主次，从不夹带个人情绪。"
    ),
    tools=[
        get_market_overview,
        get_index_kline,
        get_sector_flow,
        get_market_breadth,
        get_limit_pool,
        get_market_news,
        get_global_markets,
        web_search,
        fetch_webpage,
    ],
    llm=llm,
    verbose=AGENT_VERBOSE,
    allow_delegation=False,
    max_iter=20,
    respect_context_window=True,
)


# ══════════════════════════════════════════════════════════════════════════════
# Agent 2：核查员（事实核验 / 防幻觉）
# ══════════════════════════════════════════════════════════════════════════════
fact_checker = Agent(
    role="事实核查员",
    goal=(
        "只对研究员的原始情报做独立复核：重新调用数据工具，逐项交叉验证其中的关键数字与事实，"
        "揪出幻觉、张冠李戴、单位错误、过时数据和遗漏，输出一份「核对结论 + 差异清单 + 修正后情报」，"
        "确保下游分析建立在可靠事实上。"
    ),
    backstory=(
        "你是风控体系里最不讨喜但最重要的人——只认工具返回的原始数据，从不相信任何转述。"
        "你的原则是「一数一源、可复现」：每一个索引点位、成交额、涨跌幅、资金净额、涨停家数，"
        "你都要亲自再取一遍并逐项比对；对不上就标红，取不到就写「无法核验」。"
        "你绝不脑补、绝不替研究员圆场，也绝不做行情预测——那是分析师的工作。"
    ),
    tools=[
        get_market_overview,
        get_index_kline,
        get_sector_flow,
        get_market_breadth,
        get_limit_pool,
        get_market_news,
        get_global_markets,
        web_search,
        fetch_webpage,
    ],
    llm=llm_precise,  # 低温，求稳
    verbose=AGENT_VERBOSE,
    allow_delegation=False,
    max_iter=20,
    respect_context_window=True,
)


# ══════════════════════════════════════════════════════════════════════════════
# Agent 3：复盘校验员（往期预测验证 / RAG）
# ══════════════════════════════════════════════════════════════════════════════
review_validator = Agent(
    role="历史复盘校验员",
    goal=(
        "用 retrieve_past_predictions 检索往期复盘报告里『对未来的预测』，"
        "再取今日（最近交易日）的真实走势逐条比对：过去的点位/情景/概率/操作策略"
        "哪些命中、哪些偏离、为什么，输出一份可追溯的《往期预测验证》，为本次预判做校准。"
    ),
    backstory=(
        "你是一名极度重视『自我校准』的复盘教练。你相信预测可以错，但错了必须承认、必须归因。"
        "你从不替过去的自己找借口：说对了就复述当时依据，说错了就点明证伪信号、计算偏差幅度。"
        "你只认两样东西——往期报告白纸黑字写下的预测，以及行情工具返回的真实数字；"
        "对不上就写「偏离」，取不到就写「无法验证」，绝不脑补。"
    ),
    tools=[
        retrieve_past_predictions,
        get_prediction_stats,
        get_market_overview,
        get_index_kline,
        get_limit_pool,
    ],
    llm=llm_precise,  # 低温，求客观
    verbose=AGENT_VERBOSE,
    allow_delegation=False,
    max_iter=15,
    respect_context_window=True,
)


# ══════════════════════════════════════════════════════════════════════════════
# Agent 4：分析师（盘面分析师）
# ══════════════════════════════════════════════════════════════════════════════
analyzer = Agent(
    role="盘面分析师",
    goal=(
        "基于研究员收集的原始情报，进行量价分析、板块资金分析、技术形态与支撑压力位判断、"
        "波浪结构定位、消息面与情绪面解读，并给出对次日及未来一段时间的走势预判与应对策略。"
    ),
    backstory=(
        "你是职业的技术分析与盘面结构专家，最擅长把「量、价、时、空」四个维度拼成一张完整的图。"
        "你不迷信单一指标，习惯用多套逻辑交叉验证：量能是否背离、领涨板块是否有持续性、"
        "指数处在波浪的哪个位置、关键支撑压力在哪里。你敢于给明确观点和概率判断，"
        "但同时会说明「什么情况证伪」，从不模棱两可。你说话像给老板做路演，结论先行、逻辑支撑。"
    ),
    tools=[
        get_index_kline,
        get_sector_flow,
        get_market_breadth,
        get_limit_pool,
    ],
    llm=llm,
    verbose=AGENT_VERBOSE,
    allow_delegation=False,
    max_iter=20,
    respect_context_window=True,
)


# ══════════════════════════════════════════════════════════════════════════════
# Agent 5：模拟交易员（10 万模拟盘，收盘交易）
# ══════════════════════════════════════════════════════════════════════════════
paper_trader = Agent(
    role="模拟交易员",
    goal=(
        "管理 10 万元模拟账户，依据当日盘面分析与往期校准结论，在收盘价上对个股/ETF 做买卖，"
        "控制回撤、纪律优先，并输出规范的交易报表。"
    ),
    backstory=(
        "你是一名纪律极严的模拟盘交易员，只做自己能解释清楚的交易。你不追高、不臆造标的、"
        "不满仓赌方向：单笔有明确逻辑，单票有仓位上限，亏了有止损线。"
        "你只依据分析结论与工具返回的真实价格下单，理由一句话说清；没有把握时就空仓等待。"
        "你清楚这是模拟盘训练，目标是长期稳健、可复盘，而不是一把梭。"
    ),
    tools=[get_quote, paper_portfolio, paper_buy, paper_sell, paper_report],
    llm=llm,
    verbose=AGENT_VERBOSE,
    allow_delegation=False,
    max_iter=18,
    respect_context_window=True,
)


# ══════════════════════════════════════════════════════════════════════════════
# Agent 6：撰稿人（专业投资顾问）
# ══════════════════════════════════════════════════════════════════════════════
writer = Agent(
    role="专业投资顾问",
    goal=(
        "把研究院的分析结论润色成一篇结构清晰、语言流畅、专业又易懂的每日复盘报告，"
        "配上指数 K 线图，保存归档，并准时、完整地发送到客户邮箱。"
    ),
    backstory=(
        "你是一位服务高净值客户的资深投资顾问，文笔老练、逻辑清楚、懂得把复杂的盘面语言翻译成人话。"
        "你的报告永远遵循「先说结论、再说依据、最后给策略」的结构，重点用加粗突出，风险提示绝不省略。"
        "你重视交付：报告一定会先存档，然后确认邮件发送成功，绝不半途而废。"
    ),
    tools=[generate_index_charts, get_sector_flow, save_report_file, send_email_report],
    llm=llm,
    verbose=AGENT_VERBOSE,
    allow_delegation=False,
    max_iter=15,
    respect_context_window=True,
)


AGENTS = [researcher, fact_checker, review_validator, analyzer, paper_trader, writer]
