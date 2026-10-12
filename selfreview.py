# -*- coding: utf-8 -*-
"""
selfreview.py —— 研究月报（自省）：让 AI 总结「我最近为什么犯错」
================================================================

（由美股版迁移而来）与每日复盘不同，这里做的是**元复盘**：不看今天盘面，而看**自己这段时间的战绩**。

数据来源全部是 `ledger.py` 的**确定性统计**：
- `get_error_analysis`        → 弱项、典型偏离、过度自信、冲击日敏感性、基准对比
- `get_research_track_record` → 按能力维度/体制/视野/置信度的命中率与 Wilson 区间
- `get_prediction_stats`      → 全时段/滚动命中率

LLM 只负责**归因与建议**，不得编造数字。输出 `reports/研究月报_{today}.md`。

用法：
    python main.py --self-review            # 近 30 天
    python main.py --self-review --days 90  # 近 90 天
"""

from __future__ import annotations

from datetime import datetime

from crewai import Agent, Crew, Process, Task

from agents import AGENT_VERBOSE, llm_precise
from ledger import (
    auto_verify_predictions,
    get_error_analysis,
    get_prediction_stats,
    get_research_track_record,
)
from rag import retrieve_past_predictions

review_director = Agent(
    role="研究总监（自省官）",
    goal=(
        "基于确定性的战绩与错误结构，客观总结本系统这段时间在哪些能力维度/市场体制上表现好或差、"
        "为什么反复犯错，并给出**可执行**的改进方向（调整预测形式、表态强度、关注点），为下一阶段校准。"
    ),
    backstory=(
        "你是研究团队里负责『元复盘』的总监。你只依据工具算出的客观统计说话，"
        "绝不替团队找台阶、也绝不编造数据。你相信：承认错误的系统才会进步，"
        "而『把话说得含糊』来掩盖错误是最可耻的。你的报告结论先行、直指问题、给出可落地的改进。"
    ),
    tools=[
        get_error_analysis,
        get_research_track_record,
        get_prediction_stats,
        auto_verify_predictions,
        retrieve_past_predictions,
    ],
    llm=llm_precise,  # 低温，求客观
    verbose=AGENT_VERBOSE,
    allow_delegation=False,
    max_iter=15,
    respect_context_window=True,
)

self_review_task = Task(
    description=(
        "今天是 {today}。请撰写一份《研究月报》——对最近 {days_back} 天本系统预测战绩的**自省**。\n\n"
        "步骤（必须调用工具，不能凭记忆）：\n"
        "1. get_error_analysis(days_back={days_back})：拿到**确定性错误结构**"
        "（弱项、典型偏离案例、过度自信、冲击日敏感性、与『永远看多』基准的对比）。\n"
        "2. get_research_track_record(days_back={days_back})：拿到按能力维度/市场体制/视野/置信度"
        "的命中率与 Wilson 置信区间。\n"
        "3. get_prediction_stats(window=5, days_back={days_back})：全时段与滚动命中率。\n"
        "4. 必要时用 auto_verify_predictions / retrieve_past_predictions 回看具体预测原文，理解当时依据。\n\n"
        "报告结构：\n"
        "# 研究月报 | {today}（近 {days_back} 天）\n"
        "## 一、战绩概览\n"
        "  总体命中率（含 Wilson 区间）与基准对比；一句话自我评价（诚实、不粉饰）。\n"
        "## 二、强项与弱项\n"
        "  按**能力维度**（方向/行业/情绪/事件/风险）与**市场体制**（趋势/震荡 × 波动档）列出命中率，"
        "标注样本量；明确哪些是强项、哪些是弱项。\n"
        "## 三、我为什么犯错\n"
        "  针对**弱项**，结合**典型偏离案例**做归因：是逻辑错（在震荡市误用趋势思维、情绪周期误判）、"
        "数据错、还是被外生冲击打断；总结**反复出现**的误判模式。\n"
        "## 四、置信度校准\n"
        "  高信心（≥80%）预测是否真的更准？若是过度自信，指出程度。\n"
        "## 五、下阶段改进（可执行）\n"
        "  给出 3-5 条具体、可落地的改进（如：震荡市方向判断降低表态强度、改用区间/条件式预测、"
        "补写证伪条件、在事件驱动日提高风险权重等），并说明每条的**验证方式**。\n\n"
        "写作要求：\n"
        "- **只引用工具给出的数字，不得编造**；数字与工具不一致视为错误。\n"
        "- 样本不足（可验证 < 3 或 Wilson 区间跨过 50%）时，明确写「样本不足，暂不下结论」。\n"
        "- 结论先行、直指问题，避免套话；重点用 **加粗**。\n"
        "- 文末附一句免责声明。"
    ),
    expected_output=(
        "一份《研究月报》MARKDOWN：含战绩概览、强弱项（分维度/体制）、犯错归因、"
        "置信度校准、可执行的改进清单，全部数据来自工具、不自造，且已完整结尾。"
    ),
    agent=review_director,
    output_file="reports/研究月报_{today}.md",
)


def run_self_review(days_back: int = 30, today: str = "") -> object:
    """运行研究月报（自省）。返回 CrewAI 的 kickoff 结果。"""
    today = today or datetime.now().strftime("%Y-%m-%d")
    crew = Crew(
        agents=[review_director],
        tasks=[self_review_task],
        process=Process.sequential,
        verbose=AGENT_VERBOSE,
        memory=False,
        max_rpm=30,
    )
    return crew.kickoff(inputs={"today": today, "days_back": days_back})


if __name__ == "__main__":
    print(run_self_review(days_back=30))
