"""把 qr.py 的結構化發票資料，映射成標準交易草稿 dict（schema 見 schema.json）。"""
from __future__ import annotations


def invoice_to_draft(
    invoice: dict,
    counterparty: str | None = None,
    category: str | None = None,
    payment_method: str = "信用卡",
    direction: str = "支出",
) -> dict:
    """QR 解碼結果 -> 交易草稿。

    counterparty/category 留空時，呼叫端應先用 vendor_memory.lookup() 查歷史紀錄；
    查不到就留空，交給人工補——不要自己猜一個預設類別（曾經寫死猜「餐飲」，
    租金/交通等一律錯）。status 一律「待審」，寫入正式帳本前需要人工確認。
    """
    item_note = "、".join(f"{x['name']}x{x['qty']}" for x in invoice["items"]) if invoice["items"] else ""
    seller = invoice["seller_tax_id"]
    return {
        "date": invoice["date"],
        "direction": direction,
        "payment_method": payment_method,
        "counterparty": counterparty or f"賣方統編 {seller}",
        "category": category or "",
        "amount_incl_tax": invoice["amount_incl_tax"],
        "tax_exclusive": invoice["amount_untaxed"],
        "tax_amount": invoice["tax_amount"],
        "invoice_status": "有(含統編)" if invoice["buyer_tax_id"] else "有(無統編)",
        "evidence_url": None,
        "note": f"發票 {invoice['invoice_no']} 賣方統編{seller} {item_note}".strip(),
        "status": "待審",
        "confidence": 0.99,
        "source": "qr",
    }
