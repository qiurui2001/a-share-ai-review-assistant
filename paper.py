# -*- coding: utf-8 -*-
"""
paper.py —— 模拟交易账户（Paper Trading）
==========================================

- 初始资金 10 万元（可用环境变量 PAPER_INITIAL_CASH 调整）；
- 可买卖 A 股个股与 ETF（腾讯行情代码，如 sh600519 / sz000001 / sh510300 / sz159915）；
- **收盘交易**：以最新收盘价成交（复盘在盘后进行）；
- 状态持久化到 `paper/`（portfolio.json 账户、trades.json 流水）；
- 输出为 Markdown 表格：时间 / 代码 / 名称 / 方向 / 价格 / 数量 / 金额 / 仓位% / 持有状态 / 盈亏 / 理由。

并发安全：CrewAI 可能在同一步并发调用多个工具，故所有「读-改-写」都用全局锁串行化，
避免状态被覆盖（曾出现买入两只、只落一只的竞态）。

说明：不含手续费与印花税（纯模拟）；A 股按 100 股整数倍成交。
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime
from pathlib import Path

from crewai.tools import tool

from tools import _tencent_quotes, _to_float

PAPER_DIR = Path(os.environ.get("PAPER_DIR", "paper"))
INITIAL_CASH = float(os.environ.get("PAPER_INITIAL_CASH", "100000") or "100000")
STATE_FILE = PAPER_DIR / "portfolio.json"
TRADES_FILE = PAPER_DIR / "trades.json"
EQUITY_FILE = PAPER_DIR / "equity.json"

# ── 交易纪律（硬约束）──
PAPER_MAX_POSITION = float(os.environ.get("PAPER_MAX_POSITION", "0.8") or "0.8")
PAPER_MAX_REBALANCE_PER_WEEK = int(os.environ.get("PAPER_MAX_REBALANCE_PER_WEEK", "2") or "2")
PAPER_COOLDOWN_DAYS = int(os.environ.get("PAPER_COOLDOWN_DAYS", "3") or "3")

_LOCK = threading.RLock()


# ──────────────────────────────────────────────────────────────────────────────
# 状态读写（仅在本模块内部调用，外部调用均需持有 _LOCK）
# ──────────────────────────────────────────────────────────────────────────────
def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _load() -> dict:
    PAPER_DIR.mkdir(parents=True, exist_ok=True)
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            pass
    return {"cash": INITIAL_CASH, "holdings": {}, "realized_pnl": 0.0}


def _save(state: dict) -> None:
    PAPER_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def _load_trades() -> list[dict]:
    if TRADES_FILE.exists():
        try:
            return json.loads(TRADES_FILE.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return []
    return []


def _save_trades(trades: list[dict]) -> None:
    PAPER_DIR.mkdir(parents=True, exist_ok=True)
    TRADES_FILE.write_text(json.dumps(trades, ensure_ascii=False, indent=2), encoding="utf-8")


def _load_equity() -> list[dict]:
    if EQUITY_FILE.exists():
        try:
            return json.loads(EQUITY_FILE.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return []
    return []


def _save_equity(records: list[dict]) -> None:
    PAPER_DIR.mkdir(parents=True, exist_ok=True)
    EQUITY_FILE.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")


def _record_equity(total: float, cash: float, market_value: float) -> list[dict]:
    """按日期 upsert 当日净值快照，返回按日期升序的净值序列。"""
    records = _load_equity()
    today = _today()
    row = {"date": today, "total": round(total, 2),
           "cash": round(cash, 2), "market_value": round(market_value, 2)}
    for i, r in enumerate(records):
        if r.get("date") == today:
            records[i] = row
            break
    else:
        records.append(row)
    records.sort(key=lambda r: r["date"])
    _save_equity(records)
    return records


def _equity_chart(records: list[dict]):
    """画净资产曲线 PNG，返回文件路径（失败返回 None）。"""
    if len(records) < 2:
        return None
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei", "DejaVu Sans"]
        plt.rcParams["axes.unicode_minus"] = False

        dates = [r["date"][5:] for r in records]  # MM-DD
        totals = [r["total"] for r in records]
        x = list(range(len(records)))
        last = totals[-1]
        ret = (last / INITIAL_CASH - 1) * 100

        fig, ax = plt.subplots(figsize=(8.5, 3.6))
        ax.axhline(INITIAL_CASH, color="#9e9e9e", linestyle="--", linewidth=1, label=f"初始 {INITIAL_CASH:,.0f}")
        color = "#d32f2f" if ret >= 0 else "#2e7d32"
        ax.plot(x, totals, color=color, linewidth=1.8, marker="o", markersize=3, label="总资产")
        ax.fill_between(x, totals, INITIAL_CASH, color=color, alpha=0.08)
        # 按数据区间缩放 y 轴，避免小涨小跌被画成陡峭大波动
        lo = min(totals + [INITIAL_CASH])
        hi = max(totals + [INITIAL_CASH])
        pad = max((hi - lo) * 0.3, INITIAL_CASH * 0.004)
        ax.set_ylim(lo - pad, hi + pad)
        ax.annotate(f"{last:,.0f}（{ret:+.2f}%）", xy=(x[-1], last),
                    xytext=(-8, 10), textcoords="offset points",
                    fontsize=10, color=color, fontweight="bold", ha="right")
        ax.set_title("模拟盘净资产曲线", fontsize=13, fontweight="bold")
        ax.set_ylabel("总资产（元）", fontsize=10)
        ax.grid(True, linestyle="--", alpha=0.25)
        ax.legend(loc="upper left", fontsize=9)
        for spine in ("top", "right"):
            ax.spines[spine].set_visible(False)
        step = max(1, len(dates) // 10)
        ax.set_xticks(x[::step])
        ax.set_xticklabels(dates[::step], fontsize=8)
        fig.tight_layout()
        path = PAPER_DIR / f"净资产曲线_{_today()}.png"
        fig.savefig(path, dpi=110, bbox_inches="tight", facecolor="white")
        plt.close(fig)
        return path
    except Exception:  # noqa: BLE001
        return None


def _quotes(codes: list[str]) -> dict:
    """批量取最新价，返回 {code: (name, price)}。"""
    out = {}
    if not codes:
        return out
    data = _tencent_quotes(codes)
    for code in codes:
        f = data.get(code)
        if f:
            out[code] = (f[1], _to_float(f[3]))
    return out


def _valuation(state: dict):
    """计算持仓明细、持仓市值、总资产。"""
    holdings = state.get("holdings", {})
    quotes = _quotes(list(holdings.keys()))
    rows = []
    market_value = 0.0
    for code, h in holdings.items():
        name_q, price = quotes.get(code, (h.get("name", code), None))
        price = price or h["cost"]
        value = h["shares"] * price
        market_value += value
        rows.append({
            "code": code,
            "name": h.get("name", name_q),
            "shares": h["shares"],
            "cost": h["cost"],
            "price": price,
            "value": value,
            "pnl": (price - h["cost"]) * h["shares"],
            "pnl_pct": (price / h["cost"] - 1) * 100 if h["cost"] else 0.0,
        })
    total = state.get("cash", 0.0) + market_value
    return rows, market_value, total


# ──────────────────────────────────────────────────────────────────────────────
# 内部实现（在 _LOCK 保护下执行）
# ──────────────────────────────────────────────────────────────────────────────
def _portfolio_text() -> str:
    state = _load()
    rows, mv, total = _valuation(state)
    realized = state.get("realized_pnl", 0.0)
    lines = [
        "# 模拟账户状态",
        "",
        f"- 初始资金：{INITIAL_CASH:,.2f} 元",
        f"- 现金：{state.get('cash', 0.0):,.2f} 元",
        f"- 持仓市值：{mv:,.2f} 元（仓位 {mv / total * 100:.1f}%）" if total else "- 持仓市值：0.00 元",
        f"- 总资产：{total:,.2f} 元",
        f"- 累计已实现盈亏：{realized:+,.2f} 元",
        f"- 总收益率：{(total / INITIAL_CASH - 1) * 100:+.2f}%",
        "",
    ]
    if rows:
        lines += ["| 代码 | 名称 | 持股 | 成本 | 现价 | 市值 | 浮动盈亏 | 盈亏% | 仓位% |",
                  "|---|---|---|---|---|---|---|---|---|"]
        for r in rows:
            lines.append(
                f"| {r['code']} | {r['name']} | {r['shares']} | {r['cost']:.3f} | {r['price']:.3f} "
                f"| {r['value']:,.2f} | {r['pnl']:+,.2f} | {r['pnl_pct']:+.2f}% "
                f"| {r['value'] / total * 100:.1f}% |"
            )
    else:
        lines.append("当前空仓。")
    return "\n".join(lines)


def _buy(code: str, amount: float, reason: str) -> str:
    state = _load()
    trades = _load_trades()
    today = datetime.now().date()

    # ── 买入额度：每周最多 N 个「买入日」（同一天多笔买入算一次；卖出不受限）──
    week_buy_dates = set()
    for t in trades:
        if t.get("action") != "买入":
            continue
        try:
            d = datetime.strptime(t["date"], "%Y-%m-%d").date()
        except Exception:  # noqa: BLE001
            continue
        if d.isocalendar()[:2] == today.isocalendar()[:2]:
            week_buy_dates.add(d)
    if today not in week_buy_dates and len(week_buy_dates) >= PAPER_MAX_REBALANCE_PER_WEEK:
        return (f"【纪律拦截·仅限买入】本周买入日已达 {len(week_buy_dates)} 个"
                f"（上限 {PAPER_MAX_REBALANCE_PER_WEEK}），本周不再开新仓；"
                f"**卖出/减持不受此限，可随时执行**。")

    # ── 反追涨冷静期：同一标的 X 日内不得重复买入；刚止损的 X 日内不得追回 ──
    for t in trades:
        if t.get("code") != code:
            continue
        try:
            d = datetime.strptime(t["date"], "%Y-%m-%d").date()
        except Exception:  # noqa: BLE001
            continue
        gap = (today - d).days
        if gap < 0 or gap > PAPER_COOLDOWN_DAYS:
            continue
        if t.get("action") == "买入":
            return f"【纪律拦截·仅限买入】{code} 在 {gap} 天内已买入过，禁止追涨加仓（冷静期 {PAPER_COOLDOWN_DAYS} 天）。"
        if t.get("action") == "卖出" and (t.get("realized_pnl") or 0) < 0:
            return f"【纪律拦截·仅限买入】{code} 在 {gap} 天内刚止损过，冷静期内不得追回。"

    q = _quotes([code])
    if code not in q or q[code][1] is None:
        return f"【失败】未能获取 {code} 行情，放弃买入。"
    name, price = q[code]
    cash = state.get("cash", 0.0)
    lots = int(amount / price / 100)
    lots = min(lots, int(cash / price / 100))
    if lots <= 0:
        return f"【放弃】可用现金 {cash:,.2f} 元不足以按 {price} 买入 100 股 {name}({code})。"
    shares = lots * 100
    cost = shares * price

    # ── 仓位上限硬拦截（默认 80%）──
    _, mv_now, total_now = _valuation(state)
    if total_now:
        pos_after = (mv_now + cost) / total_now
        if pos_after > PAPER_MAX_POSITION + 1e-9:
            return (f"【纪律拦截】买入后总仓位将达 {pos_after * 100:.1f}%，"
                    f"超过上限 {PAPER_MAX_POSITION * 100:.0f}%，拒绝执行。")

    holdings = state.setdefault("holdings", {})
    prev = holdings.get(code)
    if prev:
        total_shares = prev["shares"] + shares
        prev["cost"] = (prev["cost"] * prev["shares"] + cost) / total_shares
        prev["shares"] = total_shares
    else:
        holdings[code] = {"name": name, "shares": shares, "cost": price}
    state["cash"] = cash - cost

    _, _, total = _valuation(state)
    pos = holdings[code]["shares"] * price / total * 100 if total else 0.0
    trades.append({
        "date": _today(), "code": code, "name": name, "action": "买入",
        "price": round(price, 3), "shares": shares, "amount": round(cost, 2),
        "position": round(pos, 1), "reason": reason, "realized_pnl": None,
    })
    _save_trades(trades)
    _save(state)
    return (f"✅ 买入 {name}({code}) {shares} 股 @ {price:.3f}，金额 {cost:,.2f} 元，"
            f"仓位 {pos:.1f}%，剩余现金 {state['cash']:,.2f} 元。")


def _sell(code: str, ratio: float, reason: str) -> str:
    state = _load()
    holdings = state.get("holdings", {})
    h = holdings.get(code)
    if not h:
        return f"未持有 {code}，无法卖出。"
    q = _quotes([code])
    if code not in q or q[code][1] is None:
        return f"【失败】未能获取 {code} 行情，放弃卖出。"
    name, price = q[code]
    ratio = max(0.0, min(float(ratio), 1.0))
    shares = h["shares"] if ratio >= 0.999 else int(h["shares"] * ratio / 100) * 100
    if shares <= 0:
        return "卖出数量不足 100 股，放弃。"

    proceeds = shares * price
    realized = (price - h["cost"]) * shares
    h["shares"] -= shares
    state["cash"] = state.get("cash", 0.0) + proceeds
    state["realized_pnl"] = state.get("realized_pnl", 0.0) + realized
    status = "已清仓" if h["shares"] == 0 else "持有"
    if h["shares"] == 0:
        del holdings[code]

    _, _, total = _valuation(state)
    remaining = holdings.get(code, {}).get("shares", 0)
    pos = remaining * price / total * 100 if total else 0.0
    trades = _load_trades()
    trades.append({
        "date": _today(), "code": code, "name": name, "action": "卖出",
        "price": round(price, 3), "shares": shares, "amount": round(proceeds, 2),
        "position": round(pos, 1), "reason": reason, "realized_pnl": round(realized, 2),
    })
    _save_trades(trades)
    _save(state)
    return (f"✅ 卖出 {name}({code}) {shares} 股 @ {price:.3f}，回款 {proceeds:,.2f} 元，"
            f"本笔盈亏 {realized:+,.2f} 元，状态：{status}。")


def _report() -> str:
    state = _load()
    trades = _load_trades()
    rows, mv, total = _valuation(state)
    cash = state.get("cash", 0.0)
    realized = state.get("realized_pnl", 0.0)
    float_pnl = sum(r["pnl"] for r in rows)

    # 记录当日净值快照
    equity = _record_equity(total, cash, mv)
    cum_ret = (total / INITIAL_CASH - 1) * 100
    day_ret = 0.0
    if len(equity) >= 2 and equity[-2]["total"]:
        day_ret = (equity[-1]["total"] / equity[-2]["total"] - 1) * 100

    holding_codes = set(state.get("holdings", {}).keys())
    lines = [
        f"# 模拟交易报表 | {_today()}（收盘价成交）",
        "",
        f"> 初始资金 **{INITIAL_CASH:,.0f}** 元 ｜ 现金 **{cash:,.2f}** 元 ｜ "
        f"持仓市值 **{mv:,.2f}** 元 ｜ 总资产 **{total:,.2f}** 元",
        f"> 当日收益率 **{day_ret:+.2f}%** ｜ 累计收益率 **{cum_ret:+.2f}%** ｜ "
        f"已实现盈亏 **{realized:+,.2f}** 元 ｜ 浮动盈亏 **{float_pnl:+,.2f}** 元",
        "",
        "## 一、交易流水",
        "",
        "| 时间 | 代码 | 名称 | 方向 | 价格 | 数量 | 金额 | 仓位% | 持有状态 | 盈亏 | 理由 |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for t in trades:
        flag = "持有" if t["code"] in holding_codes else "已清仓"
        pnl = "—" if t.get("realized_pnl") is None else f"{t['realized_pnl']:+,.2f}"
        lines.append(
            f"| {t['date']} 15:00 | {t['code']} | {t['name']} | {t['action']} | {t['price']:.3f} "
            f"| {t['shares']} | {t['amount']:,.2f} | {t.get('position', 0):.1f}% | {flag} "
            f"| {pnl} | {t.get('reason', '')} |"
        )
    if not trades:
        lines.append("| — | — | — | — | — | — | — | — | — | — | 尚无交易 |")

    lines += ["", "## 二、当前持仓", ""]
    if rows:
        lines += ["| 代码 | 名称 | 持股 | 成本 | 现价 | 市值 | 浮动盈亏 | 盈亏% | 仓位% |",
                  "|---|---|---|---|---|---|---|---|---|"]
        for r in rows:
            lines.append(
                f"| {r['code']} | {r['name']} | {r['shares']} | {r['cost']:.3f} | {r['price']:.3f} "
                f"| {r['value']:,.2f} | {r['pnl']:+,.2f} | {r['pnl_pct']:+.2f}% "
                f"| {r['value'] / total * 100:.1f}% |"
            )
    else:
        lines.append("当前空仓。")

    cash_pct = f"{cash / total * 100:.1f}%" if total else "—"
    mv_pct = f"{mv / total * 100:.1f}%" if total else "—"
    lines += [
        "", "## 三、账户汇总", "",
        "| 项目 | 金额（元） | 占比 |",
        "|---|---|---|",
        f"| 初始资金 | {INITIAL_CASH:,.2f} | 100.0% |",
        f"| 现金 | {cash:,.2f} | {cash_pct} |",
        f"| 持仓市值 | {mv:,.2f} | {mv_pct} |",
        f"| **总资产** | **{total:,.2f}** | **100.0%** |",
        f"| 累计已实现盈亏 | {realized:+,.2f} | — |",
        f"| 浮动盈亏 | {float_pnl:+,.2f} | — |",
        f"| **当日收益率** | **{day_ret:+.2f}%** | — |",
        f"| **累计收益率** | **{cum_ret:+.2f}%** | — |",
    ]

    lines += ["", "## 四、净资产曲线与逐日收益", ""]
    chart = _equity_chart(equity)
    if chart:
        lines += [f"![模拟盘净资产曲线]({chart.resolve().as_posix()})", ""]
    else:
        lines += ["> 暂无足够数据绘制曲线（需至少 2 个交易日的净值快照）。", ""]
    lines += ["| 日期 | 总资产 | 当日收益率 | 累计收益率 | 仓位% |", "|---|---|---|---|---|"]
    prev_total = None
    for r in equity:
        dr = 0.0 if not prev_total else (r["total"] / prev_total - 1) * 100
        cr = (r["total"] / INITIAL_CASH - 1) * 100
        posw = r["market_value"] / r["total"] * 100 if r["total"] else 0.0
        lines.append(f"| {r['date']} | {r['total']:,.2f} | {dr:+.2f}% | {cr:+.2f}% | {posw:.1f}% |")
        prev_total = r["total"]
    lines += ["", f"> 净值快照数：{len(equity)} 个交易日；曲线文件：`净资产曲线_{_today()}.png`。"]

    md = "\n".join(lines)
    PAPER_DIR.mkdir(parents=True, exist_ok=True)
    (PAPER_DIR / f"交易_{_today()}.md").write_text(md, encoding="utf-8")
    return md


# ──────────────────────────────────────────────────────────────────────────────
# CrewAI 工具（对外，全部经 _LOCK 串行化）
# ──────────────────────────────────────────────────────────────────────────────
@tool
def get_quote(code: str) -> str:
    """获取个股或 ETF 的最新价格（收盘价）。

    Args:
        code: 腾讯行情代码，个股如 sh600519 / sz000001，ETF 如 sh510300 / sz159915 / sh588000。
    """
    q = _quotes([code])
    if code not in q or q[code][1] is None:
        return f"【失败】未能获取 {code} 的行情，请检查代码格式（如 sh600519 / sz159915）。"
    name, price = q[code]
    return f"{code} {name}：最新价 {price}"


@tool
def paper_portfolio() -> str:
    """查看模拟账户当前状态：现金、持仓（成本/现价/浮动盈亏/仓位）、总资产、累计收益。"""
    with _LOCK:
        return _portfolio_text()


@tool
def paper_buy(code: str, amount: float, reason: str = "") -> str:
    """以最新收盘价买入个股/ETF（按 100 股整数倍成交）。

    Args:
        code: 腾讯行情代码，如 sh600519 / sz159915。
        amount: 计划投入金额（元）。实际按能买到的整手数成交。
        reason: 买入理由（简短，一句话）。
    """
    with _LOCK:
        return _buy(code, amount, reason)


@tool
def paper_sell(code: str, ratio: float = 1.0, reason: str = "") -> str:
    """以最新收盘价卖出持仓的指定比例。

    Args:
        code: 腾讯行情代码。
        ratio: 卖出比例，1.0=全部清仓，0.5=卖出一半；默认 1.0。
        reason: 卖出理由（简短，一句话）。
    """
    with _LOCK:
        return _sell(code, ratio, reason)


@tool
def paper_report() -> str:
    """生成模拟交易报表（交易流水 + 当前持仓 + 账户汇总），保存到 paper/交易_{日期}.md 并返回表格。"""
    with _LOCK:
        return _report()


@tool
def paper_trade_history(days: int = 14) -> str:
    """查看最近 N 天的模拟交易流水（调仓/换手纪律自查用）。

    Args:
        days: 回看天数，默认 14 天。
    """
    with _LOCK:
        trades = _load_trades()
    today = datetime.now().date()
    rows = []
    for t in trades:
        try:
            d = datetime.strptime(t["date"], "%Y-%m-%d").date()
        except Exception:  # noqa: BLE001
            continue
        if 0 <= (today - d).days <= days:
            rows.append(t)
    if not rows:
        return f"最近 {days} 天无交易。"
    rows.sort(key=lambda x: x["date"])
    lines = [
        f"# 最近 {days} 天交易（共 {len(rows)} 笔）",
        "",
        f"> 纪律：每周最多 {PAPER_MAX_REBALANCE_PER_WEEK} 个买入日（**卖出/减持不受限**）；同一标的 {PAPER_COOLDOWN_DAYS} 天内不得重复买入、"
        f"止损后 {PAPER_COOLDOWN_DAYS} 天内不得追回（均只限制买入）；总仓位上限 {PAPER_MAX_POSITION * 100:.0f}%。",
        "",
        "| 日期 | 代码 | 名称 | 方向 | 价格 | 金额 | 已实现盈亏 | 理由 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for t in rows:
        pnl = "—" if t.get("realized_pnl") is None else f"{t['realized_pnl']:+,.2f}"
        lines.append(
            f"| {t['date']} | {t['code']} | {t['name']} | {t['action']} | {t['price']:.3f} "
            f"| {t['amount']:,.2f} | {pnl} | {t.get('reason', '')} |"
        )
    return "\n".join(lines)


PAPER_TOOLS = [get_quote, paper_portfolio, paper_buy, paper_sell, paper_report, paper_trade_history]


if __name__ == "__main__":
    print(paper_portfolio.run())
    print()
    print(paper_report.run())
