# -*- coding: utf-8 -*-
"""
ledger.py —— 预测的「确定性」自动核验、命中率统计与错误结构分析
================================================================

（由美股版迁移而来，护城河同源）核心思想：把「判对判错」从**模型的主观叙述**里剥离出来，
交给**价格数据确定性计算**：

1. 从往期《A股复盘》报告的「后市预判 / 操作策略」章节抽取可证伪的预测要素：
   - **点位类**：压力 / 支撑 / 突破 / 跌破 / 关键位（含具体数值）；
   - **方向类**：看多 / 看空 / 震荡。
2. 用真实日 K（腾讯行情，指数如 sh000001）在「预测日 → 最新交易日」窗口内按固定规则判定：
   - 压力/目标位：期间**最高价**是否触及；支撑位：**最低价**是否触及；
   - 突破/跌破位：期间**收盘价**是否突破/跌破；关键位：区间是否穿过；
   - **方向：按波动率归一化**——只有区间涨跌幅超过「日常噪音」才算兑现，噪音带内记为「未定（不计分）」。
3. 统计：按视野分桶、Wilson 置信区间、Brier 概率校准、剔除冲击日对照，
   并按**能力维度 / 市场体制 / 置信度**给出 Track Record 与**错误结构分析**（供「研究月报/自省」）。

模型只负责**归因**，不负责判分——判分口径固定、可复现、可审计。
评分规则变更**只对未来生效**，绝不回改历史判分。
"""

from __future__ import annotations

import math
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

from crewai.tools import tool

from tools import _tencent_kline, _to_float

REPORT_DIR = Path(os.environ.get("REPORT_DIR", "reports"))
REPORT_GLOB = "A股复盘_*.md"

# ── 判分口径（可配置；修改视为版本变更，勿回改历史） ──
VOL_MULT = float(os.environ.get("PRED_VOL_MULT", "1.0") or "1.0")        # 方向噪音带 = VOL_MULT × 日波动 × √N
SHOCK_MULT = float(os.environ.get("PRED_SHOCK_MULT", "2.5") or "2.5")   # 单日 |涨跌| > SHOCK_MULT × 日波动 → 冲击日
VOL_LOOKBACK = int(os.environ.get("PRED_VOL_LOOKBACK", "20") or "20")   # 日波动估计窗口
MIN_LEVEL = float(os.environ.get("PRED_MIN_LEVEL", "100") or "100")
MAX_LEVEL = float(os.environ.get("PRED_MAX_LEVEL", "100000") or "100000")

_FULL_DATE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")
_NUM_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")
_HEAD_RE = re.compile(r"^(#{1,6})\s+(.*)$")

_VERDICTS = ("hit", "partial", "miss")
_VERDICT_ICON = {"hit": "✅命中", "partial": "⚠️部分命中", "miss": "❌偏离", "unverifiable": "无法验证"}


# ──────────────────────────────────────────────────────────────────────────────
# 一、标的识别（A股主要指数）
# ──────────────────────────────────────────────────────────────────────────────
_A_INDEX_RULES = [
    (("创业板", "399006"), "创业板指", "sz399006"),
    (("科创50", "科创板", "科创"), "科创50", "sh000688"),
    (("沪深300", "沪深"), "沪深300", "sh000300"),
    (("中证500", "中证1000", "中证"), "中证500", "sh000905"),
    (("上证50",), "上证50", "sh000016"),
    (("深证成指", "深成指", "深证"), "深证成指", "sz399001"),
    (("北证", "北交所"), "北证50", "bj899050"),
    (("上证", "大盘", "沪指", "000001"), "上证指数", "sh000001"),
]
_DEFAULT_INSTRUMENT = ("上证指数", "sh000001")

# 板块/题材提及 → 归到「上证指数」判分（A股复盘以大盘点位为主）
_SECTOR_INSTRUMENT: list = []


def _instrument(text: str) -> tuple[str, str]:
    low = text.lower()
    for keywords, disp, symbol in _A_INDEX_RULES:
        if any(kw in low for kw in keywords):
            return disp, symbol
    return _DEFAULT_INSTRUMENT


# ──────────────────────────────────────────────────────────────────────────────
# 二、预测要素抽取
# ──────────────────────────────────────────────────────────────────────────────
_LEVEL_KIND = {
    "压力": "resistance", "阻力": "resistance", "目标位": "resistance",
    "上看": "resistance", "目标": "resistance",
    "支撑": "support", "下看": "support", "下方": "support", "守住": "support",
    "突破": "break_up", "站上": "break_up", "收复": "break_up", "上破": "break_up", "站稳": "break_up",
    "跌破": "break_down", "失守": "break_down", "下破": "break_down",
    "关口": "level", "中枢": "level", "点位": "level",
}
_KIND_LABEL = {
    "resistance": "压力/目标位", "support": "支撑位",
    "break_up": "突破位", "break_down": "跌破位", "level": "关键位",
}

_BULL = ("看涨", "看多", "偏多", "偏强", "上攻", "上涨", "反弹", "做多", "走高", "向上", "多头", "冲高")
_BEAR = ("看跌", "看空", "偏空", "偏弱", "下探", "下跌", "回调", "杀跌", "做空", "走低", "向下", "空头", "回落")
_RANGE = ("震荡", "横盘", "区间", "中性", "整理", "盘整")

# ── 能力维度关键词（优先级：风险 > 事件 > 行业 > 情绪 > 方向） ──
_CAP_RISK = ("风险", "回撤", "止损", "警惕", "谨防", "见顶", "破位", "杀跌风险", "高位风险", "下跌风险", "退潮风险")
_CAP_EVENT = ("财报", "业绩", "cpi", "ppi", "pmi", "gdp", "央行", "降准", "降息", "加息", "政策",
              "会议", "国常会", "证监会", "美联储", "非农", "催化", "事件", "经济数据", "利好", "利空")
_CAP_SECTOR = ("板块", "行业", "轮动", "领涨", "领跌", "主线", "题材", "概念", "半导体", "芯片",
               "新能源", "光伏", "医药", "券商", "银行", "地产", "军工", "算力", "消费", "ai")
_CAP_SENTIMENT = ("涨停", "跌停", "连板", "炸板", "情绪", "冰点", "修复", "高潮", "退潮", "封板", "打板", "高度")

# ── 表态置信度（百分比）与体制阈值 ──
_CONF_RE = re.compile(r"(\d{1,3})\s*%")
REGIME_TREND_GAP = float(os.environ.get("REGIME_TREND_GAP", "0.8") or "0.8")
REGIME_VOL_LOW = float(os.environ.get("REGIME_VOL_LOW", "0.8") or "0.8")
REGIME_VOL_HIGH = float(os.environ.get("REGIME_VOL_HIGH", "1.5") or "1.5")


def _capability(text: str) -> str:
    low = text.lower()
    if any(k in low for k in _CAP_RISK):
        return "风险"
    if any(k in low for k in _CAP_EVENT):
        return "事件"
    if any(k in low for k in _CAP_SECTOR):
        return "行业"
    if any(k in low for k in _CAP_SENTIMENT):
        return "情绪"
    return "方向"


def extract_confidence(text: str) -> Optional[float]:
    vals = [float(m.group(1)) for m in _CONF_RE.finditer(text)]
    vals = [v for v in vals if 0 < v <= 100]
    return max(vals) / 100 if vals else None


def _conf_bucket(c: Optional[float]) -> str:
    if c is None:
        return "未标注"
    if c < 0.5:
        return "<50%"
    if c < 0.65:
        return "50-65%"
    if c < 0.8:
        return "65-80%"
    return "≥80%"


def _regime(bars: list[list], date: str) -> str:
    """给「做出预测时」的市场环境打体制标签：趋势/震荡 × 波动档。"""
    hist = [b for b in bars if b[0] <= date]
    closes = [_to_float(b[2]) for b in hist if _to_float(b[2]) is not None]
    if len(closes) < 30:
        return "数据不足"
    ma10, ma30 = sum(closes[-10:]) / 10, sum(closes[-30:]) / 30
    gap = (ma10 / ma30 - 1) * 100 if ma30 else 0.0
    trend = "趋势上行" if gap > REGIME_TREND_GAP else ("趋势下行" if gap < -REGIME_TREND_GAP else "震荡")
    vol = _daily_vol(hist)
    if vol is None:
        volb = "波动未知"
    else:
        volb = "低波动" if vol < REGIME_VOL_LOW else ("中波动" if vol < REGIME_VOL_HIGH else "高波动")
    return f"{trend} · {volb}"


def _strip_noise(text: str) -> str:
    t = re.sub(r"\d{4}-\d{1,2}-\d{1,2}", " ", text)
    t = re.sub(r"\d{1,2}-\d{1,2}", " ", t)
    t = re.sub(r"\d+(?:\.\d+)?\s*%", " ", t)
    return t


def _classify_level(before: str) -> Optional[str]:
    best_kind, best_pos = None, -1
    for kw, kind in _LEVEL_KIND.items():
        pos = before.rfind(kw)
        if pos > best_pos:
            best_pos, best_kind = pos, kind
    return best_kind


def _direction(text: str):
    for kw in _BULL:
        if kw in text:
            return "bull", kw
    for kw in _BEAR:
        if kw in text:
            return "bear", kw
    for kw in _RANGE:
        if kw in text:
            return "range", kw
    return None, None


def extract_rules(text: str) -> list[dict]:
    rules: list[dict] = []
    direction, kw = _direction(text)
    if direction:
        label = {"bull": "看多", "bear": "看空", "range": "震荡"}[direction]
        rules.append({"kind": "direction", "dir": direction, "desc": f"{label}（{kw}）"})

    clean = _strip_noise(text)
    seen: set = set()
    for m in _NUM_RE.finditer(clean):
        try:
            num = float(m.group().replace(",", ""))
        except ValueError:
            continue
        if not (MIN_LEVEL <= num <= MAX_LEVEL):
            continue
        before = clean[max(0, m.start() - 18):m.start()]
        kind = _classify_level(before)
        if not kind or (kind, num) in seen:
            continue
        seen.add((kind, num))
        rules.append({"kind": kind, "level": num, "desc": f"{_KIND_LABEL[kind]} {num:g}"})
    return rules


def _horizon(text: str) -> int:
    if any(k in text for k in ("次日", "明日", "明天", "下一个交易日", "周二", "周一", "周三", "周四", "周五")):
        return 1
    if any(k in text for k in ("1-2周", "1～2周", "两周", "未来一周", "下周", "本月", "未来1-2周")):
        return 10
    return 5


def _horizon_label(h: int) -> str:
    return "次日" if h == 1 else ("1-2周" if h == 10 else "约1周")


def _split_chunks(text: str) -> list[str]:
    text = re.sub(r"[#>*`|]", " ", text)
    parts = re.split(r"[。！？；;\n]+", text)
    return [re.sub(r"^[-•\s]+", "", p).strip() for p in parts if p.strip()]


# ──────────────────────────────────────────────────────────────────────────────
# 三、情景概率抽取（用于 Brier 校准）
# ──────────────────────────────────────────────────────────────────────────────
_SCEN = [("乐观", "bull"), ("中性", "range"), ("悲观", "bear")]


def extract_scenarios(text: str) -> list[dict]:
    out = []
    for label, dflt in _SCEN:
        idx = text.find(label)
        if idx < 0:
            continue
        seg = text[idx:idx + 80]
        pm = re.search(r"(\d{1,3})\s*%", seg)
        prob = float(pm.group(1)) / 100 if pm else None
        d, _ = _direction(seg)
        out.append({"label": label, "prob": prob, "direction": d or dflt})
    return out


# ──────────────────────────────────────────────────────────────────────────────
# 四、报告解析
# ──────────────────────────────────────────────────────────────────────────────
_PRED_SECTION_RE = re.compile(r"(预判|后市|展望|操作策略|情景|预测|应对)")


def _iter_reports():
    if not REPORT_DIR.exists():
        return
    for path in sorted(REPORT_DIR.glob(REPORT_GLOB)):
        m = _FULL_DATE.search(path.name)
        if m:
            yield f"{m.group(1)}-{m.group(2)}-{m.group(3)}", path


def _split_sections(md: str) -> list[tuple[str, str]]:
    sections: list[tuple[str, str]] = []
    heading, buf = "（开篇）", []
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
    return sections


def _prediction_sections(md: str) -> list[tuple[str, str]]:
    return [(h, b) for h, b in _split_sections(md)
            if _PRED_SECTION_RE.search(h) and "验证" not in h]


def _items_from_text(text: str, report_date: str) -> list[dict]:
    items: list[dict] = []
    for _head, body in _prediction_sections(text):
        for chunk in _split_chunks(body):
            if len(chunk) < 6 or len(chunk) > 300:
                continue
            rules = extract_rules(chunk)
            if not rules:
                continue
            disp, symbol = _instrument(chunk)
            items.append({"report_date": report_date, "text": chunk,
                          "instrument": disp, "symbol": symbol, "rules": rules,
                          "capability": _capability(chunk),
                          "confidence": extract_confidence(chunk)})
    return items


def build_items(days_back: int = 30, exclude_date: str = "") -> list[dict]:
    ref = datetime.now()
    items: list[dict] = []
    for report_date, path in _iter_reports():
        if exclude_date and report_date == exclude_date:
            continue
        try:
            age = (ref - datetime.strptime(report_date, "%Y-%m-%d")).days
        except ValueError:
            age = 0
        if age < 0 or age > days_back:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        items.extend(_items_from_text(text, report_date))
    return items


def build_items_for_date(date: str) -> list[dict]:
    """只读取 `A股复盘_{date}.md` 抽取预测（供台账提交/校验用）。"""
    path = REPORT_DIR / f"A股复盘_{date}.md"
    if not path.exists():
        return []
    return _items_from_text(path.read_text(encoding="utf-8", errors="replace"), date)


# ──────────────────────────────────────────────────────────────────────────────
# 五、确定性判分（波动率归一化 + 冲击日）
# ──────────────────────────────────────────────────────────────────────────────
def _bars(symbol: str, count: int = 130) -> list[list]:
    return _tencent_kline(symbol, period="day", count=count)


def _daily_vol(bars: list[list], lookback: int = VOL_LOOKBACK) -> Optional[float]:
    closes = [_to_float(b[2]) for b in bars]
    closes = [c for c in closes if c]
    if len(closes) < 3:
        return None
    rets = [(closes[i] / closes[i - 1] - 1) * 100 for i in range(1, len(closes)) if closes[i - 1]]
    rets = rets[-lookback:]
    if len(rets) < 3:
        return None
    mean = sum(rets) / len(rets)
    return math.sqrt(sum((r - mean) ** 2 for r in rets) / len(rets))


def _shock_dates(bars: list[list], vol: Optional[float]) -> set:
    shock: set = set()
    if not vol:
        return shock
    for i in range(1, len(bars)):
        c0, c1 = _to_float(bars[i - 1][2]), _to_float(bars[i][2])
        if c0 and c1 and abs((c1 / c0 - 1) * 100) > SHOCK_MULT * vol:
            shock.add(bars[i][0])
    return shock


def _noise_band(vol: Optional[float], n: int) -> Optional[float]:
    if not vol:
        return None
    return VOL_MULT * vol * math.sqrt(max(1, n))


def _score_rules(rules: list[dict], bars: list[list], vol: Optional[float],
                 base_close: Optional[float] = None):
    highs = [_to_float(b[3]) for b in bars if _to_float(b[3]) is not None]
    lows = [_to_float(b[4]) for b in bars if _to_float(b[4]) is not None]
    closes = [_to_float(b[2]) for b in bars if _to_float(b[2]) is not None]
    if not closes:
        return None
    hi, lo = max(highs), min(lows)
    start = base_close if base_close else closes[0]
    ret = (closes[-1] / start - 1) * 100 if start else 0.0
    band = _noise_band(vol, len(bars))
    bull_th = band if band is not None else 0.5
    range_th = band if band is not None else 2.0

    details = []
    for r in rules:
        kind = r["kind"]
        if kind == "direction":
            d = r["dir"]
            if d == "bull":
                hit = True if ret >= bull_th else (False if ret <= -bull_th else None)
            elif d == "bear":
                hit = True if ret <= -bull_th else (False if ret >= bull_th else None)
            else:
                hit = abs(ret) <= range_th
            extra = "" if band is None else f"（噪音带 ±{band:.2f}%）"
            actual = f"区间涨跌 {ret:+.2f}%{extra}"
        else:
            level = r["level"]
            if kind == "resistance":
                hit, actual = hi >= level, f"期间最高 {hi:.2f}"
            elif kind == "support":
                hit, actual = lo <= level, f"期间最低 {lo:.2f}"
            elif kind == "break_up":
                hit, actual = max(closes) >= level, f"最高收盘 {max(closes):.2f}"
            elif kind == "break_down":
                hit, actual = min(closes) <= level, f"最低收盘 {min(closes):.2f}"
            else:
                hit, actual = (hi >= level >= lo), f"区间 {lo:.2f}~{hi:.2f}"
        details.append({"desc": r["desc"], "hit": hit, "actual": actual})
    return details, hi, lo, ret


def _aggregate(details: list[dict]) -> tuple[str, int, int]:
    definite = [d for d in details if d["hit"] is not None]
    if not definite:
        return "unverifiable", 0, 0
    hit_n = sum(1 for d in definite if d["hit"])
    verdict = "hit" if hit_n == len(definite) else ("miss" if hit_n == 0 else "partial")
    return verdict, hit_n, len(definite)


def _base_close(bars: list[list], window: list[list]) -> Optional[float]:
    if not window:
        return None
    idx = next((i for i, b in enumerate(bars) if b[0] == window[0][0]), 0)
    return _to_float(bars[idx - 1][2]) if idx > 0 else None


def score_item(item: dict, cache: dict) -> dict:
    symbol = item["symbol"]
    bars = cache.get(symbol)
    if bars is None:
        bars = _bars(symbol, 130)
        cache[symbol] = bars
    regime = _regime(bars, item["report_date"]) if bars else "数据不足"
    if not bars:
        return {**item, "verdict": "unverifiable", "verdict_ex_shock": "unverifiable",
                "rules_detail": [], "ret": None, "range_text": "无行情",
                "horizon": _horizon_label(_horizon(item["text"])), "shock_days": [], "regime": regime}

    vol = _daily_vol(bars)
    shock = _shock_dates(bars, vol)
    h = _horizon(item["text"])
    latest = bars[-1][0]
    window = [b for b in bars if item["report_date"] < b[0] <= latest][:h]

    if not window:
        return {**item, "verdict": "unverifiable", "verdict_ex_shock": "unverifiable",
                "rules_detail": [], "ret": None, "range_text": "无后续行情",
                "horizon": _horizon_label(h), "shock_days": [], "regime": regime}

    scored = _score_rules(item["rules"], window, vol, _base_close(bars, window))
    verdict, hit_n, total = _aggregate(scored[0]) if scored else ("unverifiable", 0, 0)
    hi, lo, ret = (scored[1], scored[2], scored[3]) if scored else (None, None, None)

    ex_bars = [b for b in window if b[0] not in shock]
    verdict_ex = "unverifiable"
    if ex_bars:
        scored_ex = _score_rules(item["rules"], ex_bars, vol, _base_close(bars, window))
        verdict_ex, _, _ = _aggregate(scored_ex[0]) if scored_ex else ("unverifiable", 0, 0)

    window_shock = sorted(b[0] for b in window if b[0] in shock)
    return {
        **item,
        "verdict": verdict, "verdict_ex_shock": verdict_ex,
        "rules_detail": scored[0] if scored else [],
        "hit_n": hit_n, "total": total, "ret": round(ret, 2) if ret is not None else None,
        "range_text": f"{window[0][0]}~{window[-1][0]}（{lo:.0f}~{hi:.0f}）" if lo is not None else "—",
        "horizon": _horizon_label(h), "shock_days": window_shock, "regime": regime,
    }


def score_all(days_back: int = 90, exclude_date: str = "") -> list[dict]:
    items = build_items(days_back=days_back, exclude_date=exclude_date)
    cache: dict = {}
    return [score_item(it, cache) for it in items]


# ──────────────────────────────────────────────────────────────────────────────
# 六、统计工具
# ──────────────────────────────────────────────────────────────────────────────
def _wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (0.0, 0.0)
    p = k / n
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt((p * (1 - p) + z * z / (4 * n)) / n) / denom
    return (max(0.0, center - half) * 100, min(1.0, center + half) * 100)


def _rates(verdicts: list[str]) -> dict:
    v = [x for x in verdicts if x in _VERDICTS]
    c = {k: v.count(k) for k in _VERDICTS}
    n = len(v)
    lo, hi = _wilson(c["hit"], n)
    return {
        "verified": n, "hit": c["hit"], "partial": c["partial"], "miss": c["miss"],
        "hit_rate": (c["hit"] / n * 100) if n else 0.0,
        "loose_rate": ((c["hit"] + c["partial"]) / n * 100) if n else 0.0,
        "ci": (lo, hi),
    }


def _fmt_line(title: str, r: dict) -> str:
    if r["verified"] == 0:
        return f"- {title}：可验证 0 条"
    return (f"- {title}：可验证 {r['verified']} 条 —— ✅命中 {r['hit']}（{r['hit_rate']:.1f}%，"
            f"Wilson 95% [{r['ci'][0]:.0f}, {r['ci'][1]:.0f}]）、⚠️部分 {r['partial']}、❌偏离 {r['miss']}；"
            f"宽松 {r['loose_rate']:.1f}%")


def _group_table(rows: list[dict], keyfunc, title: str, min_sample: int = 1) -> list[str]:
    groups: dict = {}
    for s in rows:
        groups.setdefault(keyfunc(s), []).append(s["verdict"])
    stats = []
    for k, verdicts in groups.items():
        r = _rates(verdicts)
        if r["verified"] >= min_sample:
            stats.append((k, r))
    stats.sort(key=lambda x: -x[1]["verified"])

    out = [f"## {title}", "", "| 分类 | 可验证 | 命中率 | Wilson 95% | 宽松 |", "|---|---|---|---|---|"]
    if stats:
        for k, r in stats:
            out.append(f"| {k} | {r['verified']} | {r['hit_rate']:.1f}% "
                       f"| [{r['ci'][0]:.0f}, {r['ci'][1]:.0f}] | {r['loose_rate']:.1f}% |")
    else:
        out.append("| （样本不足，暂不统计） | 0 | — | — | — |")
    return out


def _collect_brier(days_back: int, cache: dict) -> list[float]:
    ref = datetime.now()
    scores: list[float] = []
    for report_date, path in _iter_reports():
        try:
            age = (ref - datetime.strptime(report_date, "%Y-%m-%d")).days
        except ValueError:
            age = 0
        if age < 0 or age > days_back:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for _head, body in _prediction_sections(text):
            scen = [s for s in extract_scenarios(body) if s["prob"] is not None]
            if len(scen) < 2:
                continue
            symbol = _instrument(body)[1]
            bars = cache.get(symbol) or _bars(symbol, 130)
            cache[symbol] = bars
            if not bars:
                continue
            vol = _daily_vol(bars)
            h = _horizon(body)
            window = [b for b in bars if report_date < b[0] <= bars[-1][0]][:h]
            if not window:
                continue
            closes = [_to_float(b[2]) for b in window if _to_float(b[2]) is not None]
            start = _base_close(bars, window) or (closes[0] if closes else None)
            if not closes or not start:
                continue
            ret = (closes[-1] / start - 1) * 100
            band = _noise_band(vol, len(window))
            th = band if band is not None else 0.5
            realized = "bull" if ret >= th else ("bear" if ret <= -th else "range")
            brier = sum((s["prob"] - (1.0 if s["direction"] == realized else 0.0)) ** 2 for s in scen)
            scores.append(brier)
    return scores


# ──────────────────────────────────────────────────────────────────────────────
# 工具 1：往期预测自动核验（确定性，逐条判分）
# ──────────────────────────────────────────────────────────────────────────────
@tool
def auto_verify_predictions(days_back: int = 30, max_items: int = 12,
                            exclude_date: str = "") -> str:
    """从往期《A股复盘》报告抽取「对未来的预测」，用真实日 K（腾讯指数行情）**确定性自动判分**
    （压力/支撑/突破/跌破位是否触及；方向按波动率归一化，落在噪音带内记为「未定」），
    输出逐条核验表。复盘校验员应**以本工具结果为准**，只负责补充「归因」。

    Args:
        days_back: 只核验最近多少天内的报告，默认 30。
        max_items: 最多返回多少条预测，默认 12（按报告日期由近及远）。
        exclude_date: 需排除的报告日期（如今天正在生成的报告）。
    """
    items = build_items(days_back=days_back, exclude_date=exclude_date)
    if not items:
        return (f"【无往期预测】{REPORT_DIR.resolve()} 下最近 {days_back} 天内没有可核验的"
                f"『后市预判/操作策略』预测；首次运行属正常。")

    cache: dict = {}
    scored = [score_item(it, cache) for it in items]
    scored.sort(key=lambda x: x["report_date"], reverse=True)
    scored = scored[:max(1, int(max_items))]

    def esc(s: str) -> str:
        return s.replace("|", "/").replace("\n", " ")

    def icon(hit) -> str:
        return "—" if hit is None else ("✅" if hit else "❌")

    lines = [
        f"# 往期预测自动核验（确定性，共 {len(scored)} 条）",
        "",
        "> 判分口径：压力/目标位=期间最高价是否触及；支撑位=期间最低价是否触及；"
        "突破/跌破位=期间收盘价是否突破/跌破；关键位=区间是否穿过；"
        f"方向=区间涨跌是否超过噪音带（{VOL_MULT:g}×日波动×√N，带内记为「未定」不计分）。",
        "",
        "| 报告日期 | 视野 | 标的 | 预测内容 | 实际走势（核验区间） | 结论 | 规则命中明细 |",
        "|---|---|---|---|---|---|---|",
    ]
    for s in scored:
        detail = "；".join(
            f"{d['desc']}:{icon(d['hit'])}{d['actual']}" for d in s["rules_detail"]
        ) or "（无可判分规则）"
        note = f"（冲击日 {'、'.join(s['shock_days'])}）" if s["shock_days"] else ""
        lines.append(
            f"| {s['report_date']} | {s['horizon']} | {s['instrument']} | {esc(s['text'])[:80]} "
            f"| {s['range_text']} | {_VERDICT_ICON.get(s['verdict'], s['verdict'])} | {esc(detail)}{note} |"
        )

    r = _rates([s["verdict"] for s in scored])
    lines += ["", _fmt_line("本次自动核验", r)]
    return "\n".join(lines)


# ──────────────────────────────────────────────────────────────────────────────
# 工具 2：命中率统计（按视野分桶 + Wilson 区间 + 冲击日对照 + Brier）
# ──────────────────────────────────────────────────────────────────────────────
@tool
def get_prediction_stats(window: int = 5, days_back: int = 90) -> str:
    """统计往期预测的命中率（**以确定性自动核验为准**）：全时段 / 按视野分桶，
    各带 Wilson 95% 置信区间；并给出「剔除冲击日」对照与情景概率 Brier 校准分。

    Args:
        window: 滚动窗口（最近多少份含预测的报告），默认 5。
        days_back: 统计覆盖最近多少天内的报告，默认 90。
    """
    scored = score_all(days_back=days_back)
    valid = [s for s in scored if s["verdict"] in _VERDICTS]
    if not valid:
        return "【无数据】未在历史复盘中找到可核验的预测，或尚无可用的历史复盘报告。"

    dates = sorted({s["report_date"] for s in valid})
    recent = set(dates[-window:]) if dates else set()

    lines = [
        "# 预测命中率统计（确定性自动核验）",
        "",
        f"- 覆盖含预测报告的份数：**{len(dates)}** 份",
        f"- 累计可核验预测：**{len(valid)}** 条",
        "",
        "## 全时段",
        _fmt_line("原始", _rates([s["verdict"] for s in scored])),
        _fmt_line("剔除冲击日", _rates([s["verdict_ex_shock"] for s in scored])),
    ]
    shock_n = sum(len(s["shock_days"]) for s in scored)
    lines.append(f"- 冲击日合计 {shock_n} 个（单日 |涨跌| > {SHOCK_MULT:g}×日波动）")

    lines += ["", "## 按视野分桶"]
    for label in ("次日", "约1周", "1-2周"):
        bucket = [s for s in valid if s["horizon"] == label]
        lines.append(_fmt_line(label, _rates([s["verdict"] for s in bucket])))

    if recent:
        lines += ["", f"## 近 {window} 份（{('、'.join(sorted(recent))) or '—'}）",
                  _fmt_line("滚动", _rates([s["verdict"] for s in scored if s["report_date"] in recent]))]

    cache: dict = {}
    brier = _collect_brier(days_back, cache)
    if brier:
        avg = sum(brier) / len(brier)
        baseline = 2 / 3
        lines += ["", "## 情景概率校准（Brier，越低越好）",
                  f"- 平均 Brier **{avg:.3f}**（均匀基准 {baseline:.3f}，共 {len(brier)} 组）："
                  f"{'优于' if avg < baseline else '劣于'}均匀先验。"]
    lines += ["", "> 判分口径变更只对**未来**生效，不回改历史（保证台账可审计）。"]
    return "\n".join(lines)


# ──────────────────────────────────────────────────────────────────────────────
# 工具 3：AI Research Track Record 仪表盘（能力维度 / 体制 / 置信度校准）
# ──────────────────────────────────────────────────────────────────────────────
@tool
def get_research_track_record(days_back: int = 180, min_sample: int = 1) -> str:
    """输出「AI Research Track Record」仪表盘：按**能力维度**（方向/行业/情绪/事件/风险）、
    **市场体制**（趋势/震荡 × 波动档）、**视野**、**置信度分档**分别统计命中率与 Wilson 置信区间。
    用于长期评估强项与弱项，并做置信度校准。

    Args:
        days_back: 覆盖最近多少天内的报告，默认 180。
        min_sample: 每个分类至少多少条样本才展示，默认 1。
    """
    scored = [s for s in score_all(days_back=days_back) if s["verdict"] in _VERDICTS]
    if not scored:
        return "【无数据】暂无可核验的预测记录；先积累几天历史复盘即可形成 Track Record。"

    overall = _rates([s["verdict"] for s in scored])
    dates = sorted({s["report_date"] for s in scored})
    lines = [
        "# AI Research Track Record",
        "",
        f"- 观测窗口：{dates[0]} ~ {dates[-1]}（{len(dates)} 个交易日）",
        f"- 可核验预测：**{overall['verified']}** 条",
        f"- **总体命中率：{overall['hit_rate']:.1f}%**"
        f"（Wilson 95% [{overall['ci'][0]:.0f}, {overall['ci'][1]:.0f}]），"
        f"宽松（命中+部分）{overall['loose_rate']:.1f}%",
        "",
    ]
    lines += _group_table(scored, lambda s: s["capability"], "按能力维度", min_sample)
    lines += [""]
    lines += _group_table(scored, lambda s: s["regime"], "按市场体制（做出预测时）", min_sample)
    lines += [""]
    lines += _group_table(scored, lambda s: s["horizon"], "按视野", min_sample)
    lines += [""]

    calib = [s for s in scored if s.get("confidence") is not None]
    if calib:
        lines += _group_table(calib, lambda s: _conf_bucket(s["confidence"]), "按表态置信度（校准）", min_sample)
        high = _rates([s["verdict"] for s in calib if s["confidence"] >= 0.8])
        if high["verified"] >= 3 and high["hit_rate"] < overall["hit_rate"]:
            lines += ["", f"> ⚠️ 高信心（≥80%）预测命中率 {high['hit_rate']:.1f}% 低于总体 "
                          f"{overall['hit_rate']:.1f}%，存在**过度自信**：建议下调表态强度或补写证伪条件。"]
    else:
        lines += ["## 按表态置信度（校准）", "",
                  "（预测未标注概率，暂无法校准；建议在后市预判中写明情景概率，如「概率60%」。）"]
    lines += ["", "> 判分确定性、按视野/体制/维度/置信度分桶、带 Wilson 区间；样本少时勿下结论。"]
    return "\n".join(lines)


# ──────────────────────────────────────────────────────────────────────────────
# 工具 4：错误结构分析（确定性，供「研究月报/自省」引用）
# ──────────────────────────────────────────────────────────────────────────────
@tool
def get_error_analysis(days_back: int = 30, top_n: int = 8) -> str:
    """对最近一段时间的预测做**确定性错误结构分析**：找出低命中率的弱项
    （按能力维度/市场体制/视野/置信度）、列出典型偏离案例、检查过度自信与冲击日敏感性、
    并与「永远看多」基准对比。仅输出算出来的结构，供「研究月报/自省」做归因。

    Args:
        days_back: 分析覆盖最近多少天内的报告，默认 30。
        top_n: 最多列出多少条典型偏离案例，默认 8。
    """
    scored = [s for s in score_all(days_back=days_back) if s["verdict"] in _VERDICTS]
    if not scored:
        return "【无数据】暂无可分析的预测记录；先积累几天历史复盘。"

    overall = _rates([s["verdict"] for s in scored])
    lines = [
        f"# 错误结构分析（近 {days_back} 天）",
        "",
        f"- 可核验预测 {overall['verified']} 条，命中率 {overall['hit_rate']:.1f}%"
        f"（Wilson [{overall['ci'][0]:.0f}, {overall['ci'][1]:.0f}]），宽松 {overall['loose_rate']:.1f}%",
    ]
    rets = [s["ret"] for s in scored if s.get("ret") is not None]
    if rets:
        up = sum(1 for r in rets if r > 0) / len(rets) * 100
        lines.append(f"- 基准：这些窗口内上涨占比 {up:.1f}%（≈『永远看多』命中率）；"
                     f"系统{'跑赢' if overall['hit_rate'] >= up else '跑输'}该基准。")

    weak = []
    for label, keyf in (
        ("能力维度", lambda s: s["capability"]),
        ("市场体制", lambda s: s["regime"]),
        ("视野", lambda s: s["horizon"]),
        ("置信度", lambda s: _conf_bucket(s.get("confidence"))),
    ):
        groups: dict = {}
        for s in scored:
            groups.setdefault(keyf(s), []).append(s["verdict"])
        for k, v in groups.items():
            r = _rates(v)
            if r["verified"] >= 3 and r["hit_rate"] < overall["hit_rate"] - 10:
                weak.append((label, k, r["verified"], r["hit_rate"]))
    lines += ["", "## 弱项（样本≥3 且命中率明显低于总体）"]
    if weak:
        for label, k, n, hr in sorted(weak, key=lambda x: x[3]):
            lines.append(f"- 【{label}】{k}：{n} 条，命中率 {hr:.1f}%（总体 {overall['hit_rate']:.1f}%）")
    else:
        lines.append("- 暂无样本充足（≥3）且明显偏低的弱项。")

    calib = [s for s in scored if s.get("confidence") is not None]
    if calib:
        high = _rates([s["verdict"] for s in calib if s["confidence"] >= 0.8])
        if high["verified"] >= 3:
            lines.append(f"- 高信心（≥80%）：{high['verified']} 条，命中率 {high['hit_rate']:.1f}%"
                         + ("　→ ⚠️ 过度自信" if high["hit_rate"] < overall["hit_rate"] else "　→ 校准良好"))

    shock = [s for s in scored if s["shock_days"]]
    nonshock = [s for s in scored if not s["shock_days"]]
    if shock and nonshock:
        sm = sum(1 for s in shock if s["verdict"] == "miss")
        lines.append(f"- 冲击日：{len(shock)} 条预测的窗口含冲击日，其中完全偏离 {sm} 条；"
                     f"非冲击日 {len(nonshock)} 条。")

    misses = [s for s in scored if s["verdict"] == "miss"][:max(1, int(top_n))]
    lines += ["", "## 典型偏离案例"]
    if misses:
        for s in misses:
            fails = "；".join(f"{d['desc']}（{d['actual']}）" for d in s["rules_detail"] if d["hit"] is False)
            lines.append(f"- [{s['report_date']}·{s['capability']}·{s['horizon']}·{s['regime']}] "
                         f"{s['text'][:60]} → {fails or '—'}")
    else:
        lines.append("- 无完全偏离案例。")

    lines += ["", "> 以上为**确定性统计**；请据此撰写归因，不得编造数据。"]
    return "\n".join(lines)


if __name__ == "__main__":
    print(auto_verify_predictions.run(days_back=60))
    print()
    print(get_prediction_stats.run(window=5))
    print()
    print(get_research_track_record.run(days_back=180))
    print()
    print(get_error_analysis.run(days_back=30))
