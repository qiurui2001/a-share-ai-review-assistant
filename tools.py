# -*- coding: utf-8 -*-
"""
tools.py —— 每日盘后复盘 Agent 的工具集
===========================================

设计原则
--------
1. 只做「数据获取 + 交付」这类确定性动作，把「分析、判断、写作」留给 LLM。
2. 每个工具都做了重试与降级，返回**纯文本**，方便直接塞进 Agent 上下文。
3. 盘后复盘用不到毫秒级实时行情，因此优先使用稳定、不限流的公开数据源：
   - 腾讯行情 (qt.gtimg.cn)        → 指数/个股实时快照、K 线
   - 东方财富延时接口 (push2delay) → 板块涨跌、主力资金、涨跌家数
   - 东方财富资讯 (np-listapi)     → 7x24 快讯、财经资讯
   - 东方财富涨停池 (push2ex)      → 涨停/跌停梯队、连板高度、炸板率
   - 必应搜索 (cn.bing.com)        → 补充新闻/事件检索
   - SMTP                          → 邮件交付

所有数据仅供研究参考，不构成投资建议。
"""

from __future__ import annotations

import json
import base64
import math
import os
import re
import smtplib
import time
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Optional

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from crewai.tools import tool

# ──────────────────────────────────────────────────────────────────────────────
# 环境配置
# ──────────────────────────────────────────────────────────────────────────────
load_dotenv()

REPORT_DIR = Path(os.environ.get("REPORT_DIR", "reports"))
OUTPUT_DIR = Path(os.environ.get("OUTPUT_DIR", "outputs"))

SMTP_HOST = os.environ.get("SMTP_HOST", "")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587") or "587")
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASS = os.environ.get("SMTP_PASS", "")
FROM_EMAIL = os.environ.get("FROM_EMAIL", "") or SMTP_USER
REPORT_TO_EMAIL = os.environ.get("REPORT_TO_EMAIL", "")

HTTP_TIMEOUT = int(os.environ.get("HTTP_TIMEOUT", "20") or "20")

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
})

# 东方财富延时行情（盘后复盘足够用，且比 push2 主站稳定）
EM_DELAY_HOST = "push2delay.eastmoney.com"
EM_ZT_HOST = "push2ex.eastmoney.com"
EM_NEWS_URL = "https://np-listapi.eastmoney.com/comm/web/getFastNewsList"

# Tencent 行情代码
A_INDEX_CODES = {
    "上证指数": "sh000001",
    "深证成指": "sz399001",
    "创业板指": "sz399006",
    "科创50": "sh000688",
    "沪深300": "sh000300",
    "中证500": "sh000905",
    "上证50": "sh000016",
    "北证50": "bj899050",
}
GLOBAL_CODES = {
    "恒生指数": "hkHSI",
    "道琼斯": "usDJI",
    "纳斯达克": "usIXIC",
    "标普500": "usINX",
}


# ──────────────────────────────────────────────────────────────────────────────
# 通用 HTTP / 格式化工具
# ──────────────────────────────────────────────────────────────────────────────
def _http_get(url: str, params: Optional[dict] = None, retries: int = 3,
              timeout: int = HTTP_TIMEOUT, encoding: Optional[str] = None,
              headers: Optional[dict] = None):
    """带重试的 GET，失败返回 None。"""
    last_err = "unknown"
    for attempt in range(retries):
        try:
            resp = SESSION.get(url, params=params, timeout=timeout, headers=headers)
            if resp.status_code == 200:
                if encoding:
                    resp.encoding = encoding
                return resp
            last_err = f"HTTP {resp.status_code}"
        except Exception as exc:  # noqa: BLE001
            last_err = str(exc)[:120]
        time.sleep(1.2 * (attempt + 1))
    return None


def _to_float(value, default=None):
    try:
        f = float(value)
        if math.isnan(f):
            return default
        return f
    except (TypeError, ValueError):
        return default


def _pct(value) -> str:
    f = _to_float(value)
    return "—" if f is None else f"{f:+.2f}%"


def _yi(value) -> str:
    """元 → 亿元"""
    f = _to_float(value)
    return "—" if f is None else f"{f / 1e8:,.2f}亿"


# ──────────────────────────────────────────────────────────────────────────────
# 腾讯行情解析
# ──────────────────────────────────────────────────────────────────────────────
def _tencent_quotes(codes: list[str]) -> dict[str, list[str]]:
    """批量获取腾讯行情快照，返回 {code: fields[]}。"""
    url = "https://qt.gtimg.cn/q=" + ",".join(codes)
    resp = _http_get(url, encoding="gbk", retries=3)
    if resp is None:
        return {}
    result: dict[str, list[str]] = {}
    for chunk in resp.text.split(";"):
        chunk = chunk.strip()
        if not chunk.startswith("v_"):
            continue
        key, _, payload = chunk.partition("=")
        code = key[2:]
        fields = payload.strip().strip('"').split("~")
        if len(fields) > 35:
            result[code] = fields
    return result


def _quote_line(name: str, fields: list[str], show_amount: bool = True) -> str:
    """把腾讯字段格式化为一行摘要。"""
    price = _to_float(fields[3])
    change = _to_float(fields[31]) if len(fields) > 31 else None
    pct = _to_float(fields[32]) if len(fields) > 32 else None
    high = _to_float(fields[33]) if len(fields) > 33 else None
    low = _to_float(fields[34]) if len(fields) > 34 else None
    amount_wan = _to_float(fields[37]) if len(fields) > 37 else None
    high_low = ""
    if high is not None and low is not None and price:
        high_low = f"，振幅{(high - low) / price * 100:.2f}%"
    amount = f"，成交额{amount_wan / 1e4:,.1f}亿" if (show_amount and amount_wan) else ""
    pct_text = f"{pct:+.2f}%" if pct is not None else "—"
    chg_text = f"{change:+.2f}" if change is not None else "—"
    return (
        f"{name}: {price if price is not None else '—'}"
        f"（{chg_text}，{pct_text}）"
        f"{amount}{high_low}"
    )


def _tencent_kline(code: str, period: str = "day", count: int = 60) -> list[list]:
    """
    获取腾讯 K 线，返回 [[date, open, close, high, low, volume], ...]。
    period: day / week / month
    """
    url = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
    params = {"param": f"{code},{period},,,{count},qfq"}
    resp = _http_get(url, params=params, retries=3)
    if resp is None:
        return []
    try:
        data = resp.json().get("data", {}).get(code, {})
    except Exception:  # noqa: BLE001
        return []
    for key in (f"qfq{period}", period):
        if key in data and data[key]:
            return data[key]
    return []


def _ma(closes: list[float], n: int) -> Optional[float]:
    if len(closes) < n:
        return None
    return sum(closes[-n:]) / n


def _market_trade_date() -> str:
    """从腾讯上证指数时间戳推断最近交易日 YYYYMMDD。"""
    q = _tencent_quotes(["sh000001"])
    fields = q.get("sh000001")
    if fields and len(fields) > 30 and fields[30]:
        ts = re.sub(r"\D", "", fields[30])
        if len(ts) >= 8:
            return ts[:8]
    today = datetime.now()
    for _ in range(10):
        if today.weekday() < 5:
            return today.strftime("%Y%m%d")
        today -= timedelta(days=1)
    return datetime.now().strftime("%Y%m%d")


# ──────────────────────────────────────────────────────────────────────────────
# 东方财富延时接口
# ──────────────────────────────────────────────────────────────────────────────
def _em_clist_page(fs: str, fields: str, pz: int = 100, fid: str = "f3",
                   po: int = 1, pn: int = 1) -> tuple[list[dict], Optional[int]]:
    """东方财富列表接口（单页），返回 (diff 列表, 总条数)。"""
    url = f"https://{EM_DELAY_HOST}/api/qt/clist/get"
    params = {
        "pn": pn, "pz": pz, "po": po, "np": 1,
        "fltt": 2, "invt": 2, "fid": fid, "fs": fs, "fields": fields,
    }
    resp = _http_get(url, params=params, retries=3)
    if resp is None:
        return [], None
    try:
        data = resp.json().get("data") or {}
        return data.get("diff") or [], data.get("total")
    except Exception:  # noqa: BLE001
        return [], None


def _em_clist(fs: str, fields: str, pz: int = 500, fid: str = "f3",
              po: int = 1, pn: int = 1) -> list[dict]:
    """东方财富列表接口（取一页），返回 diff 列表。"""
    rows, _ = _em_clist_page(fs, fields, pz=pz, fid=fid, po=po, pn=pn)
    return rows


# ══════════════════════════════════════════════════════════════════════════════
# 工具 1：大盘指数总览
# ══════════════════════════════════════════════════════════════════════════════
@tool
def get_market_overview() -> str:
    """获取 A 股主要指数收盘快照（上证、深证、创业板、科创50、沪深300、中证500、上证50、北证50），
    并汇总两市成交额。适合每天盘后复盘的第一步，用来判断大盘温度与风格。"""
    quotes = _tencent_quotes(list(A_INDEX_CODES.values()))
    if not quotes:
        return "【数据获取失败】腾讯行情接口暂时不可用，请稍后重试或改用其他数据源。"

    lines = [f"# A股收盘总览（{_market_trade_date()}）", ""]
    total_amount_yi = 0.0
    for name, code in A_INDEX_CODES.items():
        fields = quotes.get(code)
        if not fields:
            continue
        lines.append("- " + _quote_line(name, fields))
        if code in ("sh000001", "sz399001"):
            amount_wan = _to_float(fields[37]) if len(fields) > 37 else None
            if amount_wan:
                total_amount_yi += amount_wan / 1e4
    if total_amount_yi:
        lines.append("")
        lines.append(f"**两市成交额合计约 {total_amount_yi:,.1f} 亿元**")
    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════════════════
# 工具 2：指数 / 个股 K 线 + 均线结构
# ══════════════════════════════════════════════════════════════════════════════
@tool
def get_index_kline(code: str = "sh000001", period: str = "day", count: int = 60) -> str:
    """获取指数或个股的 K 线，并计算均线、区间高低点、近 5 日量能变化等结构指标。

    Args:
        code: 腾讯行情代码。指数如 sh000001(上证) / sz399006(创业板) / sh000300(沪深300)；
              个股如 sh600519 / sz000001 / bj430047。
        period: K 线周期，day / week / month，默认 day。
        count: 取最近多少根 K 线，默认 60。
    """
    count = max(10, min(int(count), 250))
    bars = _tencent_kline(code, period=period, count=count)
    if not bars:
        return f"【数据获取失败】未能获取 {code} 的 K 线，请检查代码是否正确。"

    closes = [_to_float(b[2]) for b in bars if _to_float(b[2]) is not None]
    volumes = [_to_float(b[5]) for b in bars if len(b) > 5 and _to_float(b[5]) is not None]
    latest = bars[-1]

    ma5, ma10, ma20, ma60 = (_ma(closes, 5), _ma(closes, 10), _ma(closes, 20), _ma(closes, 60))
    recent = bars[-20:]
    high20 = max((_to_float(b[3]) for b in recent if _to_float(b[3]) is not None), default=None)
    low20 = min((_to_float(b[4]) for b in recent if _to_float(b[4]) is not None), default=None)

    vol_note = "—"
    if len(volumes) >= 6:
        avg5 = sum(volumes[-5:]) / 5
        prev5 = sum(volumes[-6:-1]) / 5
        if prev5:
            vol_note = f"近5日均量较前5日 {(avg5 / prev5 - 1) * 100:+.1f}%"

    def _chg(n):
        if len(closes) > n and closes[-1 - n]:
            return f"{(closes[-1] / closes[-1 - n] - 1) * 100:+.2f}%"
        return "—"

    ma_text = " / ".join(
        f"{label}={val:.2f}" if val else f"{label}=—"
        for label, val in (("MA5", ma5), ("MA10", ma10), ("MA20", ma20), ("MA60", ma60))
    )

    lines = [
        f"# {code} K线结构（{period}，共{len(bars)}根，数据截至 {latest[0]}）",
        "",
        f"- 最新收盘：{latest[2]}（开{latest[1]} 高{latest[3]} 低{latest[4]}）",
        f"- 均线：{ma_text}",
        f"- 近20日区间：高点 {high20} / 低点 {low20}",
        f"- 阶段涨跌：近5日 {_chg(5)} / 近20日 {_chg(20)}",
        f"- 量能：{vol_note}",
        "",
        "最近 10 根 K 线（日期 开 收 高 低 量）：",
    ]
    for b in bars[-10:]:
        lines.append("  " + "  ".join(str(x) for x in b[:6]))
    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════════════════
# 工具 2b：生成 K 线图（近三个月），用于报告/邮件直观展示
# ══════════════════════════════════════════════════════════════════════════════
def _make_kline_chart(code: str, name: str, days: int, out_dir: Path):
    """用 matplotlib 画一张蜡烛图（价格 + 成交量 + MA5/MA20），返回 (PNG 路径, K线根数)。"""
    bars = _tencent_kline(code, period="day", count=days)
    if len(bars) < 5:
        return None

    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.patches import Rectangle

    plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei", "DejaVu Sans"]
    plt.rcParams["axes.unicode_minus"] = False

    dates = [b[0] for b in bars]
    opens = [_to_float(b[1], 0) or 0 for b in bars]
    closes = [_to_float(b[2], 0) or 0 for b in bars]
    highs = [_to_float(b[3], 0) or 0 for b in bars]
    lows = [_to_float(b[4], 0) or 0 for b in bars]
    vols = [_to_float(b[5], 0) or 0 for b in bars]
    x = list(range(len(bars)))

    def ma(vals, n):
        out = []
        for i in range(len(vals)):
            out.append(sum(vals[i - n + 1:i + 1]) / n if i >= n - 1 else None)
        return out

    up_color, down_color = "#d32f2f", "#2e7d32"  # 红涨绿跌

    fig, (ax, axv) = plt.subplots(
        2, 1, figsize=(9, 5.2), sharex=True,
        gridspec_kw={"height_ratios": [3, 1], "hspace": 0.06},
    )

    for i, (o, c, h, l) in enumerate(zip(opens, closes, highs, lows)):
        color = up_color if c >= o else down_color
        ax.vlines(i, l, h, color=color, linewidth=0.9, zorder=2)
        body_low = min(o, c)
        body_h = abs(c - o) or (max(h - l, 0.01) * 0.01)
        ax.add_patch(Rectangle((i - 0.32, body_low), 0.64, body_h,
                               facecolor=color, edgecolor=color, zorder=3))

    for n, col in ((5, "#ff9800"), (20, "#1e88e5")):
        series = ma(closes, n)
        ax.plot(x, series, color=col, linewidth=1.1, label=f"MA{n}", zorder=4)

    ax.set_title(f"{name} 日K线（近 {len(bars)} 个交易日）", fontsize=13, fontweight="bold")
    ax.set_ylabel("点位", fontsize=10)
    ax.grid(True, linestyle="--", alpha=0.25)
    ax.legend(loc="upper left", fontsize=9)
    for spine in ("top", "right"):
        ax.spines[spine].set_visible(False)

    vol_colors = [up_color if c >= o else down_color for o, c in zip(opens, closes)]
    axv.bar(x, vols, color=vol_colors, width=0.7, alpha=0.75)
    axv.set_ylabel("成交量", fontsize=9)
    axv.grid(True, linestyle="--", alpha=0.2)
    for spine in ("top", "right"):
        axv.spines[spine].set_visible(False)

    step = max(1, len(dates) // 8)
    ticks = x[::step]
    axv.set_xticks(ticks)
    axv.set_xticklabels([dates[i][5:] for i in ticks], fontsize=8, rotation=0)

    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = re.sub(r"\D", "", dates[-1]) or datetime.now().strftime("%Y%m%d")
    path = out_dir / f"{code}_{stamp}.png"
    fig.savefig(path, dpi=110, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    return path, len(bars)


@tool
def generate_index_charts(codes: str = "", days: int = 60) -> str:
    """生成主要指数的日 K 线图（默认近 60 个交易日≈三个月），并把可直接插入报告的 Markdown 图片行返回。
    用于在复盘报告第一节后面直观展示大盘走势。

    Args:
        codes: 逗号分隔的腾讯指数代码，例如 "sh000001,sz399006"；留空默认生成
               上证指数、深证成指、创业板指、沪深300。
        days: 取最近多少个交易日，默认 60（约三个月），范围 20-120。
    """
    days = max(20, min(int(days), 120))
    default_codes = ["sh000001", "sz399001", "sz399006", "sh000300"]
    code_list = [c.strip() for c in codes.split(",") if c.strip()] or default_codes
    name_map = {v: k for k, v in A_INDEX_CODES.items()}

    chart_dir = REPORT_DIR / "charts"
    lines: list[str] = []
    for code in code_list:
        name = name_map.get(code, code)
        try:
            result = _make_kline_chart(code, name, days, chart_dir)
        except Exception as exc:  # noqa: BLE001
            lines.append(f"（{name} 图表生成失败：{exc}）")
            continue
        if result:
            path, n = result
            lines.append(f"![{name}近{n}日K线]({path.resolve().as_posix()})")
        else:
            lines.append(f"（{name} 数据不足，未生成图表）")

    if not lines:
        return "【生成失败】未能生成任何指数 K 线图。"
    return (
        f"已生成 {len(lines)} 张指数 K 线图（近 {days} 个交易日）。"
        "请把下面每一行 **原样** 插入报告第一节（大盘表格）之后：\n\n"
        + "\n\n".join(lines)
    )


# ══════════════════════════════════════════════════════════════════════════════
# 工具 3：板块涨跌与主力资金
# ══════════════════════════════════════════════════════════════════════════════
# 进程内缓存：同一 board 在 TTL 内只真正请求一次（每 board 4 个请求），
# 避免多 Agent（研究员/核查/分析/撰稿）反复调用把 push2delay 打到限流。
_SECTOR_CACHE: dict = {}
SECTOR_CACHE_TTL = int(os.environ.get("SECTOR_CACHE_TTL", "600") or "600")


def _sector_lists(board: str) -> dict:
    """获取并缓存某 board 的四榜原始列表（每榜 10 条），TTL 内复用。"""
    now = time.time()
    hit = _SECTOR_CACHE.get(board)
    if hit and now - hit[0] < SECTOR_CACHE_TTL:
        return hit[1]

    fs = "m:90+t:3" if board == "concept" else "m:90+t:2"
    fields = "f12,f14,f3,f62,f104,f105,f128,f136,f140"
    data = {
        "gainers": _em_clist(fs, fields, pz=10, fid="f3", po=1),
        "losers": _em_clist(fs, fields, pz=10, fid="f3", po=0),
        "inflow": _em_clist(fs, fields, pz=10, fid="f62", po=1),
        "outflow": _em_clist(fs, fields, pz=10, fid="f62", po=0),
    }
    if any(data.values()):
        _SECTOR_CACHE[board] = (now, data)
    return data


def _sina_sector(board: str) -> dict:
    """备用源：新浪板块涨跌幅排行（无主力资金/涨跌家数）。返回 {gainers, losers}。"""
    url = ("https://vip.stock.finance.sina.com.cn/q/view/newFLJK.php?param=class"
           if board == "concept"
           else "https://vip.stock.finance.sina.com.cn/q/view/newSinaHy.php")
    resp = _http_get(url, retries=2, encoding="gbk")
    if resp is None:
        return {}
    m = re.search(r"=\s*(\{.*\})", resp.text, re.DOTALL)
    if not m:
        return {}
    try:
        obj = json.loads(m.group(1))
    except Exception:  # noqa: BLE001
        return {}
    rows = []
    for v in obj.values():
        parts = str(v).split(",")
        if len(parts) < 13:
            continue
        rows.append({
            "name": parts[1],
            "change": _to_float(parts[5]),
            "lead_name": parts[12],
            "lead_change": _to_float(parts[9]),
        })
    if not rows:
        return {}
    return {
        "gainers": sorted(rows, key=lambda r: (r["change"] if r["change"] is not None else -1e9), reverse=True),
        "losers": sorted(rows, key=lambda r: (r["change"] if r["change"] is not None else 1e9)),
    }


@tool
def get_sector_flow(board: str = "industry", top_n: int = 10) -> str:
    """获取行业板块或概念板块的涨跌幅排行与主力资金净流入排行，用于判断当日市场主线。

    Args:
        board: industry(行业板块) 或 concept(概念板块)，默认 industry。
        top_n: 每个榜单返回的条数，默认 10。
    """
    top_n = max(3, min(int(top_n), 20))
    board_name = "概念板块" if board == "concept" else "行业板块"

    # 每个 board 只取一次（缓存），各 Agent 共享，避免限流
    data = _sector_lists(board)
    gainers = data["gainers"][:top_n]
    losers = data["losers"][:top_n]
    inflow = data["inflow"][:top_n]
    outflow = data["outflow"][:top_n]
    if not any((gainers, losers, inflow, outflow)):
        # 东方财富限流时，降级到新浪板块涨跌幅（无主力资金，如实标注）
        sina = _sina_sector(board)
        if sina.get("gainers"):
            lines = [
                f"# {board_name}涨跌幅排行（新浪备用源，主力资金不可用）",
                "",
                "> 东方财富资金接口限流，以下为**新浪板块降级数据**：仅有涨跌幅与领涨股，"
                "**无主力净额、无涨跌家数**（以 — 表示），请据此谨慎使用。",
                "",
            ]
            for title, group in (("涨幅榜", sina["gainers"][:top_n]),
                                 ("跌幅榜", sina["losers"][:top_n])):
                lines.append(f"## {title}")
                lines.append("")
                lines.append("| 板块 | 涨跌幅 | 领涨股 |")
                lines.append("|---|---|---|")
                for r in group:
                    lines.append(
                        f"| {r['name']} | {_pct(r['change'])} "
                        f"| {r['lead_name']}({_pct(r['lead_change'])}) |"
                    )
                lines.append("")
            return "\n".join(lines)
        return f"【数据获取失败】未能获取{board_name}数据，接口可能临时限流，请重试。"

    def table(group: list[dict]) -> list[str]:
        """把一个榜单渲染成规范 Markdown 表格。"""
        rows = [
            "| 板块 | 涨跌幅 | 主力净额 | 涨/跌家数 | 领涨股 |",
            "|---|---|---|---|---|",
        ]
        for r in group:
            rows.append(
                f"| {r.get('f14','')} | {_pct(r.get('f3'))} | {_yi(r.get('f62'))} "
                f"| {r.get('f104','—')}/{r.get('f105','—')} "
                f"| {r.get('f128','—')}({_pct(r.get('f136'))}) |"
            )
        return rows

    lines = [f"# {board_name}资金与涨跌（每榜前{top_n}）", ""]
    for title, group in (
        ("涨幅榜", gainers), ("跌幅榜", losers),
        ("主力净流入榜", inflow), ("主力净流出榜", outflow),
    ):
        lines.append(f"## {title}")
        lines.append("")
        lines.extend(table(group))
        lines.append("")
    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════════════════
# 工具 4：市场涨跌家数（市场宽度）
# ══════════════════════════════════════════════════════════════════════════════
@tool
def get_market_breadth() -> str:
    """统计全市场（沪深京 A 股）上涨、下跌、平盘家数以及涨跌分布，衡量市场宽度与赚钱效应。"""
    fs = "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048"
    rows: list[dict] = []
    total: Optional[int] = None
    for page in range(1, 81):
        diff, page_total = _em_clist_page(fs, "f3", pz=100, pn=page)
        if total is None:
            total = page_total
        if not diff:
            break
        rows.extend(diff)
        if total and len(rows) >= total:
            break
        time.sleep(0.08)

    if not rows:
        return "【数据获取失败】未能获取全市场涨跌家数，接口可能临时限流，请重试。"

    ups = sum(1 for r in rows if _to_float(r.get("f3"), 0) > 0)
    downs = sum(1 for r in rows if _to_float(r.get("f3"), 0) < 0)
    flats = len(rows) - ups - downs
    limit_up = sum(1 for r in rows if _to_float(r.get("f3"), 0) >= 9.8)
    limit_down = sum(1 for r in rows if _to_float(r.get("f3"), 0) <= -9.8)

    ratio_text = f"{ups / downs:.2f}" if downs else "∞"
    if ups > downs * 2:
        mood = "普涨、赚钱效应强"
    elif downs > ups * 2:
        mood = "普跌、亏钱效应明显"
    else:
        mood = "涨跌互现、结构分化"

    complete = "（全样本）" if (total and len(rows) >= total) else f"（抽样 {len(rows)}/{total}）"
    return "\n".join([
        f"# 市场宽度{complete}",
        "",
        f"- 上涨：{ups} 家，下跌：{downs} 家，平盘：{flats} 家",
        f"- 涨跌比：{ratio_text}",
        f"- 涨幅≥9.8%（近似涨停）：{limit_up} 家；跌幅≤-9.8%（近似跌停）：{limit_down} 家",
        f"- 市场情绪：{mood}",
    ])


# ══════════════════════════════════════════════════════════════════════════════
# 工具 5：涨停 / 连板梯队
# ══════════════════════════════════════════════════════════════════════════════
@tool
def get_limit_pool(date: str = "") -> str:
    """获取指定交易日的涨停池、跌停家数与连板梯队（最高板、封单、炸板率），用于判断短线情绪周期。

    Args:
        date: 交易日，格式 YYYYMMDD，留空则自动取最近交易日。
    """
    trade_date = date.strip() if date and date.strip() else _market_trade_date()

    def _pool(topic: str, sort: str = "fbt:asc") -> Optional[dict]:
        url = f"https://{EM_ZT_HOST}/{topic}"
        params = {
            "ut": "7eea3edcaed734bea9cbfc24409ed989",
            "dpt": "wz.ztzt", "Pageindex": "0", "pagesize": "300",
            "sort": sort, "date": trade_date,
        }
        resp = _http_get(url, params=params, retries=2)
        if resp is None:
            return None
        try:
            return resp.json()
        except Exception:  # noqa: BLE001
            return None

    data = _pool("getTopicZTPool")
    if not data or data.get("rc") != 0 or not data.get("data"):
        return (f"【暂无数据】{trade_date} 未取得涨停池（可能是非交易日或接口限流），"
                f"请确认日期后重试。")

    pool = data["data"].get("pool") or []
    qdate = data["data"].get("qdate", trade_date)
    if not pool:
        return f"{qdate} 无涨停个股（或数据未更新）。"

    def num(row, key):
        return _to_float(row.get(key), 0.0)

    ladder: dict[int, list[dict]] = {}
    for row in pool:
        ladder.setdefault(int(row.get("lbc", 1) or 1), []).append(row)

    lines = [f"# 涨停池（{qdate}，共 {len(pool)} 家）", ""]
    for level in sorted(ladder, reverse=True):
        names = "、".join(f"{r.get('n','')}({r.get('hybk','')})" for r in ladder[level])
        lines.append(f"- {level}板（{len(ladder[level])}家）：{names}")

    def fmt(row):
        fbt = str(row.get("fbt", "")).zfill(6)
        return (f"  {row.get('n','')}({row.get('c','')}) "
                f"涨{_to_float(row.get('zdp'),0):.2f}% "
                f"{row.get('lbc',1)}板 换手{_to_float(row.get('hs'),0):.1f}% "
                f"封单{_yi(row.get('fund'))} 首封{fbt[:2]}:{fbt[2:4]} "
                f"炸板{row.get('zbc',0)}次 [{row.get('hybk','')}]")

    top = sorted(pool, key=lambda r: (num(r, "lbc"), num(r, "fund")), reverse=True)[:15]
    lines.append("")
    lines.append("## 代表性涨停股（按连板数、封单排序）")
    lines.extend(fmt(r) for r in top)

    dt = _pool("getTopicDTPool", sort="fund:asc")
    dt_count = 0
    if dt and dt.get("rc") == 0 and dt.get("data"):
        dt_count = dt["data"].get("tc", 0) or len(dt["data"].get("pool") or [])
    zb = _pool("getTopicZBPool")
    zb_count = 0
    if zb and zb.get("rc") == 0 and zb.get("data"):
        zb_count = zb["data"].get("tc", 0) or len(zb["data"].get("pool") or [])
    total_attempt = len(pool) + zb_count
    rate = (zb_count / total_attempt * 100) if total_attempt else 0

    lines += [
        "",
        "## 情绪指标",
        f"- 跌停家数：{dt_count}",
        f"- 炸板家数：{zb_count}，炸板率约 {rate:.1f}%",
    ]
    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════════════════
# 工具 6：7x24 财经资讯
# ══════════════════════════════════════════════════════════════════════════════
@tool
def get_market_news(count: int = 20) -> str:
    """获取最新 7x24 全球财经快讯（宏观、政策、产业、公司要闻），用于收集当日消息面。

    Args:
        count: 返回条数，默认 20，最多 50。
    """
    count = max(5, min(int(count), 50))
    params = {
        "client": "web", "biz": "web_724", "fastColumn": "102",
        "sortEnd": "", "pageSize": str(count), "req_trace": str(int(time.time() * 1000)),
    }
    resp = _http_get(EM_NEWS_URL, params=params, retries=3)
    if resp is None:
        return "【数据获取失败】未能获取财经快讯，接口可能临时限流，请重试。"
    try:
        news = (resp.json().get("data") or {}).get("fastNewsList") or []
    except Exception:  # noqa: BLE001
        return "【数据解析失败】快讯返回格式异常。"
    if not news:
        return "暂无最新快讯。"

    lines = ["# 最新财经快讯", ""]
    for item in news:
        title = item.get("title") or item.get("summary", "")[:40]
        summary = item.get("summary", "")
        show_time = item.get("showTime", "")
        lines.append(f"- [{show_time}] {title}")
        if summary and summary != title:
            lines.append(f"    {summary}")
    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════════════════
# 工具 7：国际市场
# ══════════════════════════════════════════════════════════════════════════════
# 新浪兜底代码：腾讯没有美元指数（usDXY 无效），用新浪 DINIW；顺带补原油/黄金
SINA_GLOBAL_CODES = [
    ("美元指数", "DINIW", "fx"),
    ("布伦特原油", "hf_OIL", "hf"),
    ("纽约黄金", "hf_GC", "hf"),
]


def _sina_globals() -> list[str]:
    """新浪国际行情兜底：美元指数 / 布伦特原油 / 纽约黄金。"""
    url = "https://hq.sinajs.cn/list=" + ",".join(c for _, c, _ in SINA_GLOBAL_CODES)
    resp = _http_get(url, retries=2, encoding="gbk",
                     headers={"Referer": "https://finance.sina.com.cn/"})
    if resp is None:
        return []
    data: dict[str, list[str]] = {}
    for line in resp.text.splitlines():
        m = re.match(r'var hq_str_(\w+)="(.*)";', line.strip())
        if m:
            data[m.group(1)] = m.group(2).split(",")
    out: list[str] = []
    for name, code, kind in SINA_GLOBAL_CODES:
        f = data.get(code)
        if not f or len(f) < 8:
            continue
        if kind == "fx":       # time,price,...,prev,...  → price=f[1], prev=f[3]
            price, prev = _to_float(f[1]), _to_float(f[3])
        else:                  # hf: price=f[0], ..., prev=f[7]
            price, prev = _to_float(f[0]), _to_float(f[7])
        if price is None:
            continue
        pct = (price / prev - 1) * 100 if (prev and price) else None
        pct_text = f"（{pct:+.2f}%）" if pct is not None else ""
        out.append(f"{name}(新浪): {price}{pct_text}")
    return out


@tool
def get_global_markets() -> str:
    """获取隔夜国际主要市场行情（恒生、道琼斯、纳斯达克、标普500、美元指数、原油、黄金），用于研判外部环境。"""
    quotes = _tencent_quotes(list(GLOBAL_CODES.values()))
    lines = ["# 国际主要市场", ""]
    for name, code in GLOBAL_CODES.items():
        fields = quotes.get(code)
        if fields:
            lines.append("- " + _quote_line(name, fields, show_amount=False))
    # 腾讯缺失项（美元指数等）用新浪兜底
    extra = _sina_globals()
    if extra:
        lines.append("")
        lines.append("（新浪补充）")
        lines += [f"- {x}" for x in extra]
    return "\n".join(lines) if len(lines) > 2 else "【数据获取失败】未能获取国际市场行情。"


# ══════════════════════════════════════════════════════════════════════════════
# 工具 8：必应网页搜索
# ══════════════════════════════════════════════════════════════════════════════
@tool
def web_search(query: str, count: int = 8) -> str:
    """使用必应搜索互联网，返回标题、摘要与链接，用于补充同花顺/雪球/东方财富之外的新闻与事件。

    Args:
        query: 搜索关键词，例如「A股 今日复盘 大盘 大跌原因」。
        count: 返回结果条数，默认 8。
    """
    count = max(3, min(int(count), 15))
    resp = _http_get("https://cn.bing.com/search", params={"q": query, "count": count}, retries=3)
    if resp is None:
        return "【搜索失败】必应暂时不可用，请稍后重试或换用财经快讯工具。"
    soup = BeautifulSoup(resp.text, "html.parser")
    items = soup.select("li.b_algo")
    if not items:
        return f"未搜索到「{query}」的相关结果。"

    lines = [f"# 搜索：{query}", ""]
    for it in items[:count]:
        a = it.select_one("h2 a")
        p = it.select_one("p")
        if not a:
            continue
        title = a.get_text(strip=True)
        href = a.get("href", "")
        snippet = p.get_text(strip=True) if p else ""
        lines.append(f"- {title}")
        lines.append(f"  链接：{href}")
        if snippet:
            lines.append(f"  摘要：{snippet}")
    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════════════════
# 工具 9：网页正文抓取
# ══════════════════════════════════════════════════════════════════════════════
@tool
def fetch_webpage(url: str, max_chars: int = 4000) -> str:
    """抓取指定网页的标题与正文文本，用于读取新闻、研报、公告等具体内容。

    Args:
        url: 完整网址，例如雪球/同花顺/东方财富的文章链接。
        max_chars: 正文最大字符数，默认 4000。
    """
    max_chars = max(500, min(int(max_chars), 12000))
    resp = _http_get(url, retries=2, encoding=None)
    if resp is None:
        return f"【抓取失败】无法访问 {url}。"
    resp.encoding = resp.apparent_encoding or "utf-8"
    soup = BeautifulSoup(resp.text, "html.parser")
    title = soup.title.get_text(strip=True) if soup.title else url
    for tag in soup(["script", "style", "nav", "footer", "header", "noscript", "iframe"]):
        tag.decompose()

    selectors = ["article", ".article-content", ".newsContent", "#ContentBody",
                 ".detail-text", ".report-content", "div[class*='content']", "main"]
    content = ""
    for sel in selectors:
        elems = soup.select(sel)
        if elems:
            content = "\n".join(e.get_text(strip=True, separator="\n") for e in elems)
            if len(content) >= 100:
                break
    if len(content) < 50:
        content = soup.get_text(separator="\n", strip=True)
    content = re.sub(r"\n{3,}", "\n\n", content).strip()
    if len(content) > max_chars:
        content = content[:max_chars] + "\n…（已截断）"
    return f"# {title}\n\n{content}"


# ══════════════════════════════════════════════════════════════════════════════
# 工具 10：保存复盘文件
# ══════════════════════════════════════════════════════════════════════════════
@tool
def save_report_file(filename: str, content: str = "", source_file: str = "") -> str:
    """将复盘内容保存为 Markdown 文件到 reports/ 目录，便于归档与二次编辑。

    Args:
        filename: 文件名（不含路径），例如「复盘_20260911.md」。
        content: Markdown 正文内容。若留空则从 source_file 读取。
        source_file: 已有的 Markdown 文件路径，当 content 为空时从此文件读取并归档。
    """
    if not content.strip() and source_file.strip():
        src = Path(source_file.strip())
        if not src.is_absolute():
            src = (Path.cwd() / src).resolve()
        if not src.exists():
            return f"【保存失败】找不到 source_file：{src}"
        content = src.read_text(encoding="utf-8")
    if not content.strip():
        return "【保存失败】内容为空（content 与 source_file 至少提供一个）。"

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[\\/:*?\"<>|]", "_", filename).strip() or "report.md"
    if not safe.endswith(".md"):
        safe += ".md"
    path = REPORT_DIR / safe
    path.write_text(content, encoding="utf-8")
    return f"已保存：{path.resolve()}"


# ══════════════════════════════════════════════════════════════════════════════
# 工具 11：发送复盘邮件
# ══════════════════════════════════════════════════════════════════════════════
def _markdown_to_html(md: str) -> str:
    """Markdown → HTML（邮件渲染用），支持标题、表格、图片、列表、引用、粗体、链接。"""
    lines = md.split("\n")
    html: list[str] = []
    in_list = False
    i, n = 0, len(lines)
    while i < n:
        line = lines[i].rstrip()
        # 表格：当前行以 | 开头，且下一行是分隔行
        if line.lstrip().startswith("|") and i + 1 < n and \
                re.match(r"^\s*\|?[\s:|-]+\|?\s*$", lines[i + 1]) and "-" in lines[i + 1]:
            _close_list(html, in_list); in_list = False
            table_html, i = _render_table(lines, i)
            html.append(table_html)
            continue
        if line.startswith("### "):
            _close_list(html, in_list); in_list = False
            html.append(f"<h3>{_inline(line[4:])}</h3>")
        elif line.startswith("## "):
            _close_list(html, in_list); in_list = False
            html.append(f"<h2>{_inline(line[3:])}</h2>")
        elif line.startswith("# "):
            _close_list(html, in_list); in_list = False
            html.append(f"<h1>{_inline(line[2:])}</h1>")
        elif line.strip() in ("---", "***"):
            _close_list(html, in_list); in_list = False
            html.append("<hr/>")
        elif re.match(r"^\s*[-*] ", line):
            if not in_list:
                html.append("<ul>")
                in_list = True
            html.append(f"<li>{_inline(re.sub(r'^\s*[-*] ', '', line))}</li>")
        elif re.match(r"^\s*\d+\. ", line):
            if not in_list:
                html.append("<ol>")
                in_list = True
            html.append(f"<li>{_inline(re.sub(r'^\s*\d+\. ', '', line))}</li>")
        elif line.startswith(">"):
            _close_list(html, in_list); in_list = False
            html.append(f"<blockquote>{_inline(line.lstrip('> '))}</blockquote>")
        elif line.strip() == "":
            _close_list(html, in_list); in_list = False
        else:
            _close_list(html, in_list); in_list = False
            html.append(f"<p>{_inline(line)}</p>")
        i += 1
    _close_list(html, in_list)
    return "\n".join(html)


def _split_row(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def _render_table(lines: list[str], start: int) -> tuple[str, int]:
    """把连续的 Markdown 表格行渲染为带样式的 <table>，返回 (html, 下一行索引)。"""
    header = _split_row(lines[start])
    i = start + 2  # 跳过表头与分隔行
    body: list[list[str]] = []
    while i < len(lines) and lines[i].lstrip().startswith("|"):
        body.append(_split_row(lines[i]))
        i += 1

    th_style = ("padding:7px 9px;background:#eef2ff;color:#3f51b5;font-weight:600;"
                "border:1px solid #dfe3ee;text-align:left;white-space:nowrap")
    td_style = "padding:7px 9px;border:1px solid #e6e9f0;vertical-align:top"
    head = "".join(f'<th style="{th_style}">{_inline(c)}</th>' for c in header)
    rows = []
    for row in body:
        cells = "".join(f'<td style="{td_style}">{_inline(c)}</td>' for c in row)
        rows.append(f"<tr>{cells}</tr>")
    table = (
        '<table style="width:100%;border-collapse:collapse;font-size:13px;'
        'margin:10px 0;color:#333">'
        f"<thead><tr>{head}</tr></thead><tbody>{''.join(rows)}</tbody></table>"
    )
    return table, i


def _close_list(html: list[str], in_list: bool):
    if in_list:
        html.append("</ul>")


def _img_tag(alt: str, src: str) -> str:
    """图片：本地文件转 base64 内联（邮件里必然显示），远程链接直接引用。"""
    src = src.strip()
    style = "max-width:100%;height:auto;border:1px solid #e6e9f0;border-radius:6px;margin:8px 0"
    if src.lower().startswith(("http://", "https://")):
        return f'<img src="{src}" alt="{alt}" style="{style}">'
    path = Path(src)
    if not path.is_absolute():
        path = (Path.cwd() / path).resolve()
    if not path.exists():
        return f'<span style="color:#c0392b">[图片缺失: {alt or src}]</span>'
    ext = path.suffix.lower().lstrip(".")
    mime = "image/jpeg" if ext in ("jpg", "jpeg") else f"image/{ext or 'png'}"
    try:
        b64 = base64.b64encode(path.read_bytes()).decode("ascii")
    except Exception:  # noqa: BLE001
        return f'<span style="color:#c0392b">[图片读取失败: {alt or src}]</span>'
    return f'<img src="data:{mime};base64,{b64}" alt="{alt}" style="{style}">'


def _inline(text: str) -> str:
    text = re.sub(r"!\[(.*?)\]\((.+?)\)", lambda m: _img_tag(m.group(1), m.group(2)), text)
    text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"\[(.+?)\]\((.+?)\)", r'<a href="\2">\1</a>', text)
    return text


def _render_email_html(title: str, body_html: str) -> str:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return f"""\
<!DOCTYPE html>
<html lang="zh-CN">
<head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:12px;background:#f2f4f7;font-family:-apple-system,
 BlinkMacSystemFont,'PingFang SC','Microsoft YaHei',sans-serif">
<div style="max-width:680px;margin:0 auto;background:#fff;border-radius:10px;
 overflow:hidden;box-shadow:0 1px 6px rgba(0,0,0,0.08)">
 <div style="background:linear-gradient(135deg,#1f6feb,#6f42c1);padding:22px 20px;color:#fff">
  <h1 style="margin:0;font-size:20px">{title}</h1>
  <p style="margin:6px 0 0;font-size:13px;opacity:.85">生成时间：{now}</p>
 </div>
 <div style="padding:20px;color:#333;font-size:14px;line-height:1.75">
  {body_html}
 </div>
 <div style="text-align:center;padding:14px;color:#aaa;font-size:11px;border-top:1px solid #eee">
  本报告由 AI Agent 自动生成，仅供研究参考，不构成投资建议
 </div>
</div>
</body></html>"""


@tool
def send_email_report(subject: str, content: str = "", to_email: str = "",
                      source_file: str = "") -> str:
    """将复盘报告（Markdown 格式）渲染为 HTML 邮件并发送给客户；若未配置 SMTP 则保存为 HTML 文件。

    Args:
        subject: 邮件主题，例如「每日复盘 | 2026-09-11 大盘回顾与策略」。
        content: 复盘报告正文（Markdown）。若留空则从 source_file 读取。
        to_email: 收件邮箱，留空则使用环境变量 REPORT_TO_EMAIL。
        source_file: 报告文件路径（Markdown），当 content 为空时从此文件读取，
                     推荐先 save_report_file 再用它发送，避免超长文本通过参数传递。
    """
    if not content.strip() and source_file.strip():
        path = Path(source_file.strip())
        if not path.is_absolute():
            path = (Path.cwd() / path).resolve()
        if not path.exists():
            return f"【发送失败】找不到 source_file：{path}"
        content = path.read_text(encoding="utf-8")
    if not content.strip():
        return "【发送失败】邮件正文为空（content 与 source_file 至少提供一个）。"

    recipient = (to_email or os.environ.get("REPORT_TO_EMAIL", "") or REPORT_TO_EMAIL).strip()
    body_html = _markdown_to_html(content)
    html = _render_email_html(subject, body_html)

    # 未配置 SMTP：落盘为 HTML，方便手动查看
    if not all([SMTP_HOST, SMTP_USER, SMTP_PASS]):
        REPORT_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d")
        path = REPORT_DIR / f"复盘邮件_{stamp}.html"
        path.write_text(html, encoding="utf-8")
        return (f"未配置 SMTP，已改为保存 HTML 文件：{path.resolve()}。"
                f"如需自动发送，请在 .env 配置 SMTP_HOST/USER/PASS。")

    if not recipient or "@" not in recipient:
        return "【发送失败】收件邮箱为空或格式不正确，请检查 to_email 或 REPORT_TO_EMAIL。"

    msg = MIMEMultipart("alternative")
    msg["From"] = FROM_EMAIL
    msg["To"] = recipient
    msg["Subject"] = subject
    msg.attach(MIMEText(content, "plain", "utf-8"))
    msg.attach(MIMEText(html, "html", "utf-8"))

    server = None
    try:
        if SMTP_PORT == 465:
            server = smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=HTTP_TIMEOUT)
        else:
            server = smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=HTTP_TIMEOUT)
            server.ehlo()
            server.starttls()
            server.ehlo()
        server.login(SMTP_USER, SMTP_PASS)
        server.sendmail(FROM_EMAIL, [recipient], msg.as_string())
        return f"邮件已发送至 {recipient}。"
    except Exception as exc:  # noqa: BLE001
        return f"【邮件发送失败】{exc}"
    finally:
        if server:
            try:
                server.quit()
            except Exception:  # noqa: BLE001
                pass


# 供 agents.py 统一引用
ALL_TOOLS = [
    get_market_overview,
    get_index_kline,
    generate_index_charts,
    get_sector_flow,
    get_market_breadth,
    get_limit_pool,
    get_market_news,
    get_global_markets,
    web_search,
    fetch_webpage,
    save_report_file,
    send_email_report,
]


if __name__ == "__main__":
    # 手动冒烟测试：python tools.py
    print(get_market_overview.run())
    print()
    print(get_index_kline.run(code="sh000001", period="day", count=30))
    print()
    print(get_sector_flow.run(board="industry", top_n=5))
