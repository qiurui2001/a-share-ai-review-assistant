# -*- coding: utf-8 -*-
"""
rag.py —— 往期复盘检索层（轻量 RAG）
=====================================

用途
----
每次复盘时，检索「往期复盘报告里对未来的预测」，交给校验 Agent 与今日真实走势逐条比对，
从而形成「预测 → 验证 → 纠偏」的闭环，减少口径漂移与重复犯错。

检索对象
--------
`reports/每日复盘_YYYY-MM-DD.md`（历史每日复盘报告，按 Markdown 标题切成 section 作为最小检索单元）。

检索算法（无外部依赖、可离线）
------------------------------
- 中文按 **字符 bigram** 切分 + 英文/数字 token（无需分词器）；
- **TF-IDF + 余弦相似度** 计算相关度；
- **时间衰减权重**：越近的报告权重越高；
- **预测偏置**：命中「预判/情景/概率/支撑/压力/操作策略…」的段落加权。

如需升级为向量检索：把 `_score` 的余弦部分换成 embedding 即可，接口不变。

用法（作为 CrewAI 工具）
------------------------
    from rag import retrieve_past_predictions
    retrieve_past_predictions.run(query="下一交易日 走势预判 情景 支撑压力", top_k=6)
"""

from __future__ import annotations

import math
import os
import re
from datetime import datetime
from pathlib import Path

from crewai.tools import tool

REPORT_DIR = Path(os.environ.get("REPORT_DIR", "reports"))
REPORT_GLOB = "每日复盘_*.md"

# 预测类关键词：命中越多，越可能是「对未来的预测」段落
PREDICTION_KEYWORDS = [
    "预判", "预测", "情景", "概率", "后市", "走势", "支撑", "压力", "点位",
    "操作策略", "仓位", "加仓", "减仓", "止损", "目标", "证伪", "次日", "未来",
    "乐观", "中性", "悲观",
]

_LATIN_RE = re.compile(r"[a-z0-9]+")
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
_DATE_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")
_HEAD_RE = re.compile(r"^(#{1,6})\s+(.*)$")


# ──────────────────────────────────────────────────────────────────────────────
# 文本处理
# ──────────────────────────────────────────────────────────────────────────────
def _tokenize(text: str) -> list[str]:
    """中文按字符 bigram，英文数字按词切分。"""
    low = text.lower()
    tokens = _LATIN_RE.findall(low)
    cjk = "".join(_CJK_RE.findall(low))
    tokens += [cjk[i:i + 2] for i in range(len(cjk) - 1)]
    return tokens


def _split_sections(md: str) -> list[tuple[str, str]]:
    """把 Markdown 按标题切成 (标题, 正文) 段落。"""
    sections: list[tuple[str, str]] = []
    heading = "（开篇）"
    buf: list[str] = []
    for line in md.splitlines():
        m = _HEAD_RE.match(line)
        if m:
            if buf:
                sections.append((heading, "\n".join(buf).strip()))
                buf = []
            heading = m.group(2).strip()
        else:
            buf.append(line)
    if buf:
        sections.append((heading, "\n".join(buf).strip()))
    return [(h, b) for h, b in sections if len(b) >= 20]


def _parse_report_date(path: Path) -> str | None:
    m = _DATE_RE.search(path.name)
    return f"{m.group(1)}-{m.group(2)}-{m.group(3)}" if m else None


# ──────────────────────────────────────────────────────────────────────────────
# 索引构建（进程内缓存）
# ──────────────────────────────────────────────────────────────────────────────
_INDEX: list[dict] | None = None


def _build_index() -> list[dict]:
    chunks: list[dict] = []
    if not REPORT_DIR.exists():
        return chunks
    for path in sorted(REPORT_DIR.glob(REPORT_GLOB)):
        doc_date = _parse_report_date(path)
        if not doc_date:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for heading, body in _split_sections(text):
            tokens = _tokenize(heading + "\n" + body)
            if len(tokens) < 8:
                continue
            hit = sum(1 for kw in PREDICTION_KEYWORDS if kw in (heading + body))
            chunks.append({
                "date": doc_date,
                "heading": heading,
                "text": body,
                "tokens": tokens,
                "predictive": hit >= 2,
            })
    return chunks


def _get_index() -> list[dict]:
    global _INDEX
    if _INDEX is None:
        _INDEX = _build_index()
    return _INDEX


# ──────────────────────────────────────────────────────────────────────────────
# 检索
# ──────────────────────────────────────────────────────────────────────────────
def _cosine_score(query_tokens: list[str], chunks: list[dict]) -> list[float]:
    """TF-IDF 余弦相似度（在给定子集上重算 idf）。"""
    n = len(chunks)
    df: dict[str, int] = {}
    for c in chunks:
        for tok in set(c["tokens"]):
            df[tok] = df.get(tok, 0) + 1

    def vector(tokens: list[str]) -> dict[str, float]:
        tf: dict[str, int] = {}
        for tok in tokens:
            tf[tok] = tf.get(tok, 0) + 1
        vec: dict[str, float] = {}
        for tok, f in tf.items():
            if tok in df:
                idf = math.log((n + 1) / (df[tok] + 1)) + 1.0
                vec[tok] = (f / len(tokens)) * idf
        norm = math.sqrt(sum(v * v for v in vec.values())) or 1.0
        return {k: v / norm for k, v in vec.items()}

    qv = vector(query_tokens)
    scores: list[float] = []
    for c in chunks:
        cv = vector(c["tokens"])
        if len(cv) < len(qv):
            small, big = cv, qv
        else:
            small, big = qv, cv
        scores.append(sum(v * big.get(k, 0.0) for k, v in small.items()))
    return scores


def _recency_weight(doc_date: str, ref_date: datetime) -> float:
    try:
        d = datetime.strptime(doc_date, "%Y-%m-%d")
    except ValueError:
        return 1.0
    age = (ref_date - d).days
    if age < 0:
        return 0.0
    return 0.5 + 0.5 * max(0.0, 1.0 - age / 120.0)  # 120 天内线性衰减，最低 0.5


@tool
def retrieve_past_predictions(query: str, top_k: int = 6,
                              days_back: int = 60, exclude_date: str = "") -> str:
    """检索往期每日复盘报告里「对未来的预测」相关段落，用于与今日实际走势做验证。

    Args:
        query: 检索意图，例如「下一交易日及未来1-2周 走势预判 情景 概率 支撑压力 操作策略」。
        top_k: 返回最相关的段落数，默认 6，范围 1-15。
        days_back: 只检索最近多少天内的报告，默认 60 天。
        exclude_date: 需要排除的日期（YYYY-MM-DD），一般填今天，避免检索到本次正在生成的报告。
    """
    chunks = _get_index()
    if not chunks:
        return (f"【无往期复盘】{REPORT_DIR.resolve()} 下没有可检索的历史复盘报告；"
                f"首次运行属正常，从第二次起即可回溯验证。")

    ref = datetime.now()
    pool: list[dict] = []
    for c in chunks:
        if exclude_date and c["date"] == exclude_date:
            continue
        try:
            age = (ref - datetime.strptime(c["date"], "%Y-%m-%d")).days
        except ValueError:
            age = 0
        if age < 0 or age > days_back:
            continue
        pool.append(c)

    if not pool:
        return f"【无往期复盘】最近 {days_back} 天内没有可检索的历史复盘报告。"

    top_k = max(1, min(int(top_k), 15))
    query_tokens = _tokenize(query)
    base = _cosine_score(query_tokens, pool)

    scored: list[tuple[float, dict]] = []
    for score, c in zip(base, pool):
        w = _recency_weight(c["date"], ref)
        bias = 1.6 if c["predictive"] else 1.0
        scored.append((score * w * bias, c))
    scored.sort(key=lambda x: x[0], reverse=True)
    top = [x for x in scored if x[0] > 0][:top_k] or scored[:top_k]

    lines = [f"# 往期预测检索结果（共命中 {len(top)} 段 / 候选 {len(pool)} 段）", ""]
    for rank, (score, c) in enumerate(top, 1):
        body = c["text"]
        if len(body) > 1500:
            body = body[:1500] + "\n…（节选）"
        lines.append(f"## {rank}. [{c['date']}] {c['heading']}（相关度 {score:.3f}）")
        lines.append("")
        lines.append(body)
        lines.append("")
    return "\n".join(lines)


def rebuild_index() -> int:
    """清空缓存并重建索引，返回段落数（供脚本/测试调用）。"""
    global _INDEX
    _INDEX = None
    return len(_get_index())


if __name__ == "__main__":
    print(f"索引段落数：{rebuild_index()}")
    print(retrieve_past_predictions.run(
        query="下一交易日 走势预判 情景 概率 支撑压力 操作策略 仓位",
        top_k=3, days_back=60,
    ))
