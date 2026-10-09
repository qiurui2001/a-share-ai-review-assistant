# -*- coding: utf-8 -*-
"""
ledger.py —— 预测命中率滚动统计（确定性解析，不依赖 LLM）
==========================================================

思路
----
每份复盘报告的「二、往期预测验证」章节，都用固定列（报告日期/预测内容/实际走势/结论/归因）
写了一张验证表。本模块**直接解析这些表格**，汇总「命中 / 部分命中 / 偏离 / 无法验证」的
数量与命中率，并给出**近若干份报告的滚动命中率**，供复盘校验员引用、写入报告。

特点
----
- 完全确定性：正则解析 Markdown 表格 + 结论列里的 ✅/⚠️/❌ 标记；
- 去重：以「原预测日期 + 归一化预测文本」为键，避免同一预测被多份报告重复计数；
- 无外部依赖。
"""

from __future__ import annotations

import os
import re
from collections import Counter
from pathlib import Path

from crewai.tools import tool

REPORT_DIR = Path(os.environ.get("REPORT_DIR", "reports"))
REPORT_GLOB = "A股复盘_*.md"

_FULL_DATE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")
_SHORT_DATE = re.compile(r"\b(\d{2})-(\d{2})\b")
_PRED_KW = ("情景", "支撑", "压力", "仓位", "预判", "预测", "止损", "目标", "概率", "中枢", "信号")


def _norm_date(text: str) -> str | None:
    m = _FULL_DATE.search(text)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    m = _SHORT_DATE.search(text)
    if m:
        return m.group(0)  # MM-DD（同一年内足够）
    return None


def _iter_reports():
    if not REPORT_DIR.exists():
        return
    for path in sorted(REPORT_DIR.glob(REPORT_GLOB)):
        m = _FULL_DATE.search(path.name)
        if m:
            yield f"{m.group(1)}-{m.group(2)}-{m.group(3)}", path


def _verification_rows(text: str) -> list[list[str]]:
    """抽取「往期预测验证」章节里的表格数据行。"""
    rows: list[list[str]] = []
    in_sec = False
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("## "):
            in_sec = "往期预测验证" in line
            continue
        if not in_sec or not line.startswith("|"):
            continue
        if re.match(r"^\|[\s:|-]+\|?$", line):  # 分隔行
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if any(("报告日期" in c or "预测内容" in c) for c in cells):
            continue
        if len(cells) < 3:
            continue
        rows.append(cells)
    return rows


def _verdict(joined: str) -> str:
    if "无法验证" in joined or "无法核验" in joined:
        return "unverifiable"
    if "✅" in joined:
        return "hit"
    if "❌" in joined:
        return "miss"
    if "⚠" in joined:
        return "partial"
    return "unknown"


def _entry(report_date: str, cells: list[str]) -> dict:
    joined = " | ".join(cells)
    pred_date = ""
    for c in cells:
        d = _norm_date(c)
        if d:
            pred_date = d
            break
    pred_text = ""
    for c in cells:
        if any(k in c for k in _PRED_KW):
            pred_text = c
            break
    if not pred_text:
        pred_text = max(cells, key=len)
    return {
        "report_date": report_date,
        "pred_date": pred_date,
        "pred_text": pred_text,
        "verdict": _verdict(joined),
    }


def build_ledger() -> list[dict]:
    """扫描全部历史复盘，构建去重后的验证条目台账。"""
    seen: set = set()
    entries: list[dict] = []
    for report_date, path in _iter_reports():
        text = path.read_text(encoding="utf-8", errors="replace")
        for cells in _verification_rows(text):
            e = _entry(report_date, cells)
            if e["verdict"] == "unknown":
                continue
            key = (e["pred_date"], re.sub(r"[\s*`]+", "", e["pred_text"])[:50])
            if key in seen:
                continue
            seen.add(key)
            entries.append(e)
    return entries


def _rates(entries: list[dict]) -> dict:
    c = Counter(e["verdict"] for e in entries)
    total = len(entries)
    verified = c["hit"] + c["partial"] + c["miss"]
    hit_rate = (c["hit"] / verified * 100) if verified else 0.0
    loose = ((c["hit"] + c["partial"]) / verified * 100) if verified else 0.0
    return {"total": total, "hit": c["hit"], "partial": c["partial"],
            "miss": c["miss"], "unverifiable": c["unverifiable"],
            "verified": verified, "hit_rate": hit_rate, "loose_rate": loose}


def compute_stats(window: int = 5) -> dict:
    entries = build_ledger()
    all_time = _rates(entries)
    dates = sorted({e["report_date"] for e in entries})
    recent_dates = set(dates[-window:]) if dates else set()
    rolling = _rates([e for e in entries if e["report_date"] in recent_dates])
    return {"all_time": all_time, "rolling": rolling,
            "reports": len(dates), "window": window,
            "recent_dates": sorted(recent_dates)}


def _fmt(title: str, r: dict) -> list[str]:
    return [
        f"**{title}**：可验证 {r['verified']} 条 —— ✅命中 {r['hit']}（{r['hit_rate']:.1f}%）、"
        f"⚠️部分命中 {r['partial']}、❌偏离 {r['miss']}；"
        f"**宽松命中率（命中+部分）{r['loose_rate']:.1f}%**"
        + (f"；另有无需计入的「无法验证」{r['unverifiable']} 条" if r["unverifiable"] else ""),
    ]


@tool
def get_prediction_stats(window: int = 5) -> str:
    """统计历史复盘「往期预测验证」的命中率：全时段 + 近 window 份报告的滚动命中率。

    数据来源为历史复盘报告里固定格式的验证表，纯确定性解析，可用于记录预测准确度的长期趋势。

    Args:
        window: 滚动窗口（最近多少份含验证的复盘），默认 5。
    """
    stats = compute_stats(window=max(1, int(window)))
    a, r = stats["all_time"], stats["rolling"]
    if a["total"] == 0:
        return "【无数据】未在历史复盘中找到「往期预测验证」表格（可能尚未产生带验证章节的报告）。"

    lines = [
        "# 预测命中率统计（滚动）",
        "",
        f"- 覆盖含验证报告的份数：**{stats['reports']}** 份",
        f"- 累计验证条目（去重后）：**{a['total']}** 条",
        "",
    ]
    lines += _fmt("全时段", a)
    lines += _fmt(f"近 {stats['window']} 份（{('、'.join(stats['recent_dates'])) or '—'}）", r)
    return "\n".join(lines)


if __name__ == "__main__":
    print(get_prediction_stats.run(window=5))
