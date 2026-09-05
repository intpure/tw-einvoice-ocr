"""Assert-based self-check，不用測試框架。`python tests/test_layout.py` 直接跑。

驗證 layout.py 的行聚類 + ocr.py 的 row-scoped 欄位擷取，對照「OCR reading-order
判斷錯誤，把不同表格列的文字交錯輸出成同一段文字」這個真實已知失敗模式
（見 memory `aqua-ocr-vision-fallback-fix-2026-07-22.md` 記錄的健保繳費單誤讀案例）。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tw_einvoice_ocr import layout, ocr


def _build_bill_words():
    """兩個視覺行，座標分開足夠遠：
    第一行（不相關表格列）：本期已繳金額 120
    第二行（我們要的欄位）：應繳總計 480
    """
    return [
        {"text": "本期已繳金額", "left": 0, "top": 0, "width": 100, "height": 16},
        {"text": "120", "left": 110, "top": 0, "width": 30, "height": 16},
        {"text": "應繳總計", "left": 0, "top": 40, "width": 70, "height": 16},
        {"text": "480", "left": 80, "top": 40, "width": 30, "height": 16},
    ]


def test_row_clustering_keeps_rows_separate():
    rows = layout.group_words_into_rows(_build_bill_words())
    assert len(rows) == 2
    assert layout.row_text(rows[0]) == "本期已繳金額 120"
    assert layout.row_text(rows[1]) == "應繳總計 480"


def test_row_scoped_amount_gets_correct_row():
    rows = layout.group_words_into_rows(_build_bill_words())
    tax_inclusive, tax_exclusive, tax_amount = ocr._extract_three_amounts_rows(rows)
    assert tax_inclusive == 480, f"應抓到應繳總計480，實際 {tax_inclusive}"


def test_naive_flatten_reproduces_known_bug():
    """對照組：模擬 OCR reading-order 誤判，把「應繳總計」跟屬於別行的 120 併成
    同一段文字（label 緊接著錯誤的數字）。全文版 regex 會抓到 120，不是 480——
    這正是已知案例的失敗模式，驗證 row-scoped 版本存在的必要性，不是可有可無的重構。
    """
    garbled_text = "應繳總計 120 本期已繳金額 480"
    tax_inclusive, _, _ = ocr._extract_three_amounts(garbled_text)
    assert tax_inclusive == 120, (
        "此斷言重現已知失敗模式本身，不是本模組的正確行為；"
        f"實際 {tax_inclusive}"
    )


def test_parse_receipt_rows_schema_matches_flat_version():
    rows = layout.group_words_into_rows(_build_bill_words())
    draft = ocr.parse_receipt_rows(rows, source="test")
    flat_draft = ocr.parse_receipt_text(layout.rows_to_text(rows), source="test")
    assert set(draft.keys()) == set(flat_draft.keys()), "row-scoped 跟全文版輸出的 dict schema 必須一致"
    assert draft["amount_incl_tax"] == 480


if __name__ == "__main__":
    test_row_clustering_keeps_rows_separate()
    test_row_scoped_amount_gets_correct_row()
    test_naive_flatten_reproduces_known_bug()
    test_parse_receipt_rows_schema_matches_flat_version()
    print("test_layout.py 全部通過")
