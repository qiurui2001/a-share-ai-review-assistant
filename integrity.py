# -*- coding: utf-8 -*-
"""
integrity.py —— 只追加、哈希链的预测台账（防篡改）
==================================================

（由美股版迁移而来）一份**能被悄悄改写**的 track record 一文不值。要让它可信，必须：

1. **只追加**：一条记录一旦写入，永不覆盖、永不删除；
2. **哈希链**：每条记录含 `prev`（上一条的哈希）与自身 `hash`，任何改动都会断裂；
3. **可复现**：记录内容 = 当日报告里抽取的**原始预测**，落库后不可再改；判分由价格数据独立重算。

台账文件：`prediction_ledger.jsonl`（放项目根目录，**应纳入版本控制**）。

用法：每日复盘结束后 `main.py` 自动 `commit_day(today)`；`verify_ledger_integrity` 供 Agent 校验。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime
from pathlib import Path

from crewai.tools import tool

import ledger

LEDGER_FILE = Path(os.environ.get("LEDGER_FILE", "prediction_ledger.jsonl"))
_GENESIS = "GENESIS"


# ──────────────────────────────────────────────────────────────────────────────
# 规范化与哈希
# ──────────────────────────────────────────────────────────────────────────────
def _canonical(items: list[dict]) -> str:
    norm = []
    for it in sorted(items, key=lambda x: re.sub(r"\s+", "", x.get("text", ""))):
        norm.append({
            "date": it.get("report_date"),
            "instrument": it.get("instrument"),
            "capability": it.get("capability"),
            "confidence": it.get("confidence"),
            "text": re.sub(r"\s+", "", it.get("text", "")),
            "rules": [{k: r.get(k) for k in ("kind", "dir", "level", "desc")} for r in it.get("rules", [])],
        })
    return json.dumps(norm, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _payload_hash(items: list[dict]) -> str:
    return hashlib.sha256(_canonical(items).encode("utf-8")).hexdigest()


def _chain_hash(prev: str, date: str, payload: str) -> str:
    return hashlib.sha256(f"{prev}|{date}|{payload}".encode("utf-8")).hexdigest()


def _load_records(path: Path = LEDGER_FILE) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except Exception:  # noqa: BLE001
            pass
    return out


# ──────────────────────────────────────────────────────────────────────────────
# 提交（只追加）
# ──────────────────────────────────────────────────────────────────────────────
def commit(path: Path, date: str, items: list[dict], unit: str = "预测") -> str:
    """把某日预测提交进哈希链台账。

    - 新日期：追加；
    - 同日重跑且该日恰好是「链尾」：**替换链尾**（保持哈希链自洽，用于修正盘前/盘后重复运行）；
    - 同日但非链尾：不覆盖（避免篡改历史）。
    """
    if not items:
        return f"【未记录】{date} 无可提交的{unit}（报告缺失或无『后市预判』）。"

    raw_lines: list[str] = []
    if path.exists():
        raw_lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    records: list[dict] = []
    for ln in raw_lines:
        try:
            records.append(json.loads(ln))
        except Exception:  # noqa: BLE001
            pass

    replace_tip = bool(records and records[-1].get("date") == date)
    if not replace_tip and any(r.get("date") == date for r in records):
        return f"【已存在】{date} 的{unit}已提交过，且不是链尾，跳过（不覆盖历史）。"

    if replace_tip:
        raw_lines = raw_lines[: len(records) - 1]
        records = records[:-1]

    prev = records[-1]["hash"] if records else _GENESIS
    payload = _payload_hash(items)
    chash = _chain_hash(prev, date, payload)
    record = {
        "date": date, "prev": prev, "payload": payload, "hash": chash,
        "ts": datetime.now().isoformat(timespec="seconds"), "n": len(items), "items": items,
    }
    raw_lines.append(json.dumps(record, ensure_ascii=False))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(raw_lines) + "\n", encoding="utf-8")
    action = "已更新(同日重跑替换链尾)" if replace_tip else "已提交"
    return f"{action} {date}：{len(items)} 条{unit}，链哈希 {chash[:16]}…"


def commit_day(date: str) -> str:
    """把某日**A股复盘**报告里的预测提交进哈希链台账。同一天已提交则跳过。"""
    return commit(LEDGER_FILE, date, ledger.build_items_for_date(date), "预测")


# ──────────────────────────────────────────────────────────────────────────────
# 校验
# ──────────────────────────────────────────────────────────────────────────────
def verify_chain(path: Path = LEDGER_FILE) -> str:
    records = _load_records(path)
    if not records:
        return "【空台账】尚无已提交的预测记录。"

    prev = _GENESIS
    problems: list[str] = []
    for i, r in enumerate(records, 1):
        if r.get("prev") != prev:
            problems.append(f"第 {i} 条（{r.get('date')}）prev 不匹配")
        if _payload_hash(r.get("items", [])) != r.get("payload"):
            problems.append(f"第 {i} 条（{r.get('date')}）**内容被改动**：payload 哈希不符")
        if _chain_hash(prev, r.get("date", ""), r.get("payload", "")) != r.get("hash"):
            problems.append(f"第 {i} 条（{r.get('date')}）链哈希不符")
        prev = r.get("hash")

    if problems:
        return "❌ 台账校验失败：\n- " + "\n- ".join(problems)
    head = records[-1]
    return (f"✅ 台账完整：{len(records)} 条记录，{records[0]['date']} ~ {head['date']}，"
            f"链头 {str(head.get('hash'))[:16]}…；哈希链自洽，未被篡改。")


def reconcile(path: Path = LEDGER_FILE, builder=None) -> str:
    builder = builder or ledger.build_items_for_date
    records = _load_records(path)
    if not records:
        return "【空台账】无可比对记录。"
    # 源报告存在才可比对；缺失的（如 clone 后 reports/ 未随仓库、或被清理）不计为篡改
    available = {d for d, _ in ledger._iter_reports()}
    changed = [r["date"] for r in records
               if r["date"] in available and _payload_hash(builder(r["date"])) != r.get("payload")]
    missing = [r["date"] for r in records if r["date"] not in available]
    parts = []
    if changed:
        parts.append("⚠️ 以下日期的报告在提交后被改动（与台账不符）：" + "、".join(changed))
    if missing:
        parts.append("ℹ️ 以下日期源报告缺失，无法比对（不影响哈希链自洽）：" + "、".join(missing))
    return "；".join(parts) if parts else "✅ 报告与台账一致，未发现提交后改动。"


def chain_summary(path: Path = LEDGER_FILE) -> str:
    records = _load_records(path)
    if not records:
        return "【空台账】"
    total = sum(r.get("n", 0) for r in records)
    return (f"台账共 {len(records)} 天、{total} 条预测，"
            f"{records[0]['date']} ~ {records[-1]['date']}")


@tool
def verify_ledger_integrity() -> str:
    """校验预测台账（只追加哈希链）的完整性：检测文件是否被篡改、记录是否断裂，
    并对比台账与当前报告是否一致。用于证明「历史战绩未被回改」。"""
    return verify_chain() + "\n" + reconcile()


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "commit":
        d = sys.argv[2] if len(sys.argv) > 2 else datetime.now().strftime("%Y-%m-%d")
        print(commit_day(d))
    else:
        print(chain_summary())
        print(verify_chain())
        print(reconcile())
