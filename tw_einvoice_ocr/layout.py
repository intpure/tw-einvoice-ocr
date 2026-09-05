"""版面結構感知的欄位擷取層。

參考 docling（https://github.com/docling-project/docling，IBM Research，MIT）
技術報告（arXiv:2408.09869）的架構原則：先用版面分析模型（DocLayNet）+ 表格結構
辨識模型（TableFormer）把文件重建成保留空間結構的表示（哪些字在同一行/同一格），
再從結構化表示抽欄位——而不是把整份文件拉平成一段連續文字後用正則掃全文。

後者正是 memory `aqua-ocr-vision-fallback-fix-2026-07-22.md` 記錄的健保繳費單
誤讀案例的根因：regex 在拉平文字裡抓到「看起來最近」但其實屬於別的表格列的數字。

這裡不引入 docling 本身（DocLayNet/TableFormer 是需要 GPU/CPU 推論的深度學習模型，
本機 GPU 僅 2GB VRAM，量級跟一支輕量 OCR fallback 層不成比例）。只借用它的原則，
用既有依賴 pytesseract 的 word-level bounding box 輸出（image_to_data）自己做
最小可用的行聚類（row clustering），零新依賴。

ponytail: 只做「同一視覺行」等級的結構（label 跟 value 在同一行），不做完整多欄
表格對齊（跨行找同一欄）——目前確認過的真實案例（收據/繳費單）都是「標籤: 數值」
單行並列格式，用不到欄位對齊。真的遇到需要跨行比欄位（例如品名/數量/單價/小計
四欄的明細表）才需要欄位聚類，屆時才是真正該考慮 docling/TableFormer 的時機。
"""
from __future__ import annotations

import re

Word = dict  # pytesseract image_to_data() 一列，至少含 text/left/top/width/height


def group_words_into_rows(words: list[Word], y_tolerance: float = 8.0) -> list[list[Word]]:
    """把逐字 word dict 依垂直座標聚成「視覺行」。

    pytesseract 原生的 line_num/block_num 在跨欄/跨表格時常常不可靠（同一物理行被
    切成多個 line_num，或不同行被併成一個），這裡改用座標幾何自己聚類，只信
    left/top/width/height 四個數字。

    回傳依 top 排序的行列表，行內依 left 排序（左到右）。
    """
    usable = [w for w in words if w.get("text", "").strip()]
    if not usable:
        return []

    def center_y(w: Word) -> float:
        return w["top"] + w["height"] / 2.0

    rows: list[list[Word]] = []
    for w in sorted(usable, key=center_y):
        target = None
        for row in rows:
            row_center = sum(center_y(x) for x in row) / len(row)
            if abs(center_y(w) - row_center) <= y_tolerance:
                target = row
                break
        if target is None:
            rows.append([w])
        else:
            target.append(w)

    for row in rows:
        row.sort(key=lambda w: w["left"])
    rows.sort(key=lambda row: sum(center_y(x) for x in row) / len(row))
    return rows


def row_text(row: list[Word]) -> str:
    """行內文字左到右接成一段（中間補空白，避免中英數字黏在一起）。"""
    return " ".join(w["text"] for w in row).strip()


def rows_to_text(rows: list[list[Word]]) -> str:
    """整份文件的行結構轉回一段多行文字（等效於 image_to_string 的輸出，
    但省掉重跑一次 OCR——行順序已經是幾何排序過的結果，比原生 reading order 更穩）。
    """
    return "\n".join(row_text(row) for row in rows)


def find_value_in_labeled_row(
    rows: list[list[Word]],
    label_keywords: tuple[str, ...],
    value_pattern: re.Pattern,
) -> str | None:
    """只在「含有 label 關鍵字的那一行」裡找 value_pattern，不會抓到別的行/別的
    表格列裡文字上比較接近、但其實不相關的數字——這是相對於全文 regex 掃描的
    核心差異，直接對應 docling 的「先建結構、再抽欄位」原則。
    """
    for row in rows:
        text = row_text(row)
        if any(kw in text for kw in label_keywords):
            m = value_pattern.search(text)
            if m:
                return m.group(0)
    return None


def _demo() -> None:
    """自我檢查：重現健保繳費單誤讀案例——同一份文件裡，決策所需的數字跟一個
    無關表格列裡「文字上看起來更近」的數字並存，驗證行聚類能把兩者分開。
    """
    # 模擬一份繳費單 OCR 輸出的 word list：兩個視覺行，y 座標分開。
    # 第一行：無關表格列，裡面有一個誘餌數字 999（label 沒出現在這一行）。
    # 第二行：真正的「應繳金額」標籤跟正確金額 350 同一行。
    words: list[Word] = [
        {"text": "備註", "left": 0, "top": 0, "width": 30, "height": 14},
        {"text": "999", "left": 40, "top": 0, "width": 30, "height": 14},  # 誘餌
        {"text": "應繳金額", "left": 0, "top": 30, "width": 60, "height": 14},
        {"text": "350", "left": 70, "top": 30, "width": 30, "height": 14},  # 正確值
    ]

    rows = group_words_into_rows(words)
    assert len(rows) == 2, f"應聚成兩行，實際 {len(rows)} 行"
    assert row_text(rows[0]) == "備註 999"
    assert row_text(rows[1]) == "應繳金額 350"

    amount_pattern = re.compile(r"\d+")
    value = find_value_in_labeled_row(rows, ("應繳金額",), amount_pattern)
    assert value == "350", f"row-scoped 應抓到 350，實際 {value}"

    # 對照：若不分行、直接對拉平文字全文掃描，會抓到「先出現」的誘餌數字 999，
    # 這正是真實案例裡 regex 抓錯表格列的重現。
    flat_text = rows_to_text(rows)
    naive_match = amount_pattern.search(flat_text)
    assert naive_match is not None and naive_match.group(0) == "999", (
        "此斷言驗證的是『不分行會抓錯』這個已知失敗模式本身，"
        "不是本模組的正確行為"
    )

    print("layout.py self-check 通過：row-scoped 抓到 350（正確），"
          "拉平全文抓到 999（重現已知誤判）")


if __name__ == "__main__":
    _demo()
