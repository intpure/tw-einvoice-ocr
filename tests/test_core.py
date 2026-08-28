"""Assert-based self-check，不用測試框架。`python tests/test_core.py` 直接跑。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tw_einvoice_ocr.qr import parse_einvoice
from tw_einvoice_ocr.draft import invoice_to_draft
from tw_einvoice_ocr import vendor_memory


def _build_sample_qr_text() -> str:
    inv_no = "AB12345678"
    roc_date = "1150801"          # 民國115年8月1日 = 2026-08-01
    rand = "1234"
    untaxed_hex = f"{100:08X}"
    total_hex = f"{105:08X}"
    buyer = "00000000"            # 無買方統編
    seller = "12345678"
    encrypted = "A" * 24
    head = inv_no + roc_date + rand + untaxed_hex + total_hex + buyer + seller + encrypted
    assert len(head) == 77
    return f"{head}:REC:1:茶葉蛋:2:15"


def test_parse_einvoice():
    inv = parse_einvoice(_build_sample_qr_text())
    assert inv["invoice_no"] == "AB12345678"
    assert inv["date"] == "2026-08-01"
    assert inv["amount_untaxed"] == 100
    assert inv["amount_incl_tax"] == 105
    assert inv["tax_amount"] == 5
    assert inv["buyer_tax_id"] == ""            # 全 0 視為無買方統編
    assert inv["seller_tax_id"] == "12345678"
    assert inv["items"] == [{"name": "茶葉蛋", "qty": 2, "unit_price": 15}]


def test_invoice_to_draft_defaults_no_guessing():
    inv = parse_einvoice(_build_sample_qr_text())
    draft = invoice_to_draft(inv)
    assert draft["counterparty"] == "賣方統編 12345678"  # 沒查到歷史紀錄，留統編不亂猜店名
    assert draft["category"] == ""                        # 沒查到歷史紀錄，留空不亂猜類別
    assert draft["status"] == "待審"
    assert draft["amount_incl_tax"] == 105


def test_invoice_to_draft_with_vendor_memory():
    records = [
        {"counterparty": "賣方統編 12345678", "category": "餐飲", "direction": "支出"},
        {"counterparty": "賣方統編 12345678", "category": "餐飲", "direction": "支出"},
        {"counterparty": "全家超商", "category": "日常雜支", "direction": "支出", "note": "賣方統編 12345678"},
    ]
    patterns = vendor_memory.build(records)
    hit = vendor_memory.lookup(patterns, tax_id="12345678")
    assert hit is not None
    assert hit["category"] == "餐飲"          # 多數決：2 筆餐飲 vs 1 筆日常雜支
    assert hit["counterparty"] == "全家超商"   # 唯一出現過的真實店名

    inv = parse_einvoice(_build_sample_qr_text())
    draft = invoice_to_draft(inv, counterparty=hit.get("counterparty"), category=hit.get("category"))
    assert draft["counterparty"] == "全家超商"
    assert draft["category"] == "餐飲"

    assert vendor_memory.lookup(patterns, tax_id="99999999") is None  # 沒看過的賣方不猜


def test_ocr_parse_receipt_text_no_crash_on_missing_tax_fields():
    # 這條是回歸測試：曾經因為 _extract_three_amounts 打錯字成 _extract_tax_amounts
    # 加上縮排錯誤，這個函式一被呼叫就 NameError/IndentationError 崩潰。
    from tw_einvoice_ocr import ocr  # 需要 pillow（core 依賴），regex 部分不需要 OCR extras
    result = ocr.parse_receipt_text("全家便利商店\n合計 145\n2026-08-01\n統編 12345678")
    assert result["date"] == "2026-08-01"
    assert result["amount_incl_tax"] == 145
    assert result["status"] == "待審"
    assert result["category"] == "日常雜支"  # 命中「全家」關鍵字


def run_all():
    tests = [v for k, v in globals().items() if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
        print(f"PASS {t.__name__}")
    print(f"{len(tests)}/{len(tests)} passed")


if __name__ == "__main__":
    run_all()
