"""賣方類別學習表——用歷史交易紀錄學「這個賣方通常記哪個類別」，
取代寫死猜一個預設值（曾經固定猜「餐飲」，租金/交通等一律錯）。

不是機器學習模型，是多數決查表：給一批歷史交易 dict，依「賣方統編」
（從 counterparty/note 欄位的 "賣方統編XXXXXXXX" 樣式抽取）或「店名」分組，
統計歷史上最常見的類別/收支方向。

歷史紀錄從哪裡來（Google Sheet／資料庫／CSV）由呼叫端自己接，本模組只管統計與查詢。
"""
from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path
from typing import Iterable

_TAX_ID_RE = re.compile(r"賣方統編\s*(\d{8})")


def build(records: Iterable[dict]) -> dict:
    """records 每筆至少含 counterparty/category/direction，note 選填（用來抓賣方統編）。
    沒有 category 或 counterparty 的紀錄會被跳過（代表那筆本身還沒分類，不該拿來學）。
    回傳 {"by_tax_id": {...}, "by_name": {...}}，每個 entry 含 count/category/direction，
    有真實店名（非「賣方統編XXX」樣式）出現過才會多一個 counterparty 欄位。
    """
    by_tax_id: dict = {}
    by_name: dict = {}

    for r in records:
        counterparty = (r.get("counterparty") or "").strip()
        category = (r.get("category") or "").strip()
        direction = r.get("direction") or ""
        note = r.get("note") or ""
        if not counterparty or not category:
            continue

        m = _TAX_ID_RE.search(counterparty) or _TAX_ID_RE.search(note)
        if m:
            tax_id = m.group(1)
            slot = by_tax_id.setdefault(
                tax_id, {"category": Counter(), "counterparty": Counter(), "direction": Counter()}
            )
            slot["category"][category] += 1
            slot["direction"][direction] += 1
            if not _TAX_ID_RE.match(counterparty):
                slot["counterparty"][counterparty] += 1
        else:
            slot = by_name.setdefault(counterparty, {"category": Counter(), "direction": Counter()})
            slot["category"][category] += 1
            slot["direction"][direction] += 1

    return {"by_tax_id": _finalize(by_tax_id), "by_name": _finalize(by_name)}


def _finalize(buckets: dict) -> dict:
    out = {}
    for key, slot in buckets.items():
        entry = {"count": sum(slot["category"].values())}
        if slot["category"]:
            entry["category"] = slot["category"].most_common(1)[0][0]
        if slot.get("counterparty"):
            entry["counterparty"] = slot["counterparty"].most_common(1)[0][0]
        if slot["direction"]:
            entry["direction"] = slot["direction"].most_common(1)[0][0]
        out[key] = entry
    return out


def lookup(patterns: dict, tax_id: str | None = None, name: str | None = None) -> dict | None:
    """查 build() 的結果；tax_id 優先命中，其次店名。找不到回 None，不猜。"""
    if tax_id and tax_id in patterns.get("by_tax_id", {}):
        return patterns["by_tax_id"][tax_id]
    if name and name in patterns.get("by_name", {}):
        return patterns["by_name"][name]
    return None


def save_cache(patterns: dict, path: str | Path) -> None:
    Path(path).write_text(json.dumps(patterns, ensure_ascii=False, indent=2), encoding="utf-8")


def load_cache(path: str | Path) -> dict:
    p = Path(path)
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"by_tax_id": {}, "by_name": {}}
