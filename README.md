# tw-einvoice-ocr

台灣電子發票／收據結構化辨識引擎。QR 解碼優先（結構化、信心值 0.99），
QR 解不到才退本地 OCR 備援（Tesseract，離線、不依賴雲端 API）。

從 Intpure Fin 財務助理（Hermes bot）拆出，移除了 Telegram／Google Sheets／
Hermes 的綁定，只留辨識引擎本身，讓其他專案可以自己接資料庫/介面。

## 安裝

```bash
pip install -e .           # 只要 QR 解碼（pillow + zxing-cpp）
pip install -e .[ocr]      # 再加 OCR 備援（pytesseract + pymupdf + opencv）
```

OCR 備援另需系統安裝 Tesseract 5 本體＋繁中語言包（`chi_tra`），pip 裝的
`pytesseract` 只是 Python 綁定，不含引擎本身。

## 用法

### 1. QR 解碼（主線）

```python
from tw_einvoice_ocr import decode_invoice_qr, invoice_to_draft

invoice = decode_invoice_qr("receipt.jpg")
if invoice is None:
    # 沒解到 QR，改走 OCR 備援（見下）
    ...
else:
    draft = invoice_to_draft(invoice)  # -> 交易草稿 dict，status="待審"
```

### 2. OCR 備援（QR 解不到時）

```python
from tw_einvoice_ocr import ocr

draft = ocr.ocr_to_draft("receipt.jpg")   # 或傳 URL（含 OneDrive 分享連結）
# draft 為 None 代表 OCR 也讀不出文字
```

`ocr_to_draft()` 內部優先用**版面結構感知**（`layout.py`）解析金額/日期：先用
Tesseract 的逐字座標（`image_to_data`）把畫面聚類成「視覺行」，只在同一行內找
標籤跟數值，不會被別的表格列裡文字上比較接近、但其實不相關的數字帶偏——這是
參考 [docling](https://github.com/docling-project/docling)（IBM Research，
MIT，見借用紀錄.md 082）「先建結構、再抽欄位」的架構原則做的輕量版，不引入
docling 本身（其 DocLayNet/TableFormer 是需要 GPU/CPU 推論的深度學習模型，跟
一支輕量 fallback 層不成比例），只借用原則、用既有依賴自己刻。行聚類抓不到任何
金額時，才退回原本的全文正則（`parse_receipt_text`）當最後防線。

### 3. 賣方類別學習（多數決查表，非 ML）

從你自己的歷史交易紀錄（哪裡來、怎麼存都可以，這裡只吃一個 dict list）學「這個
賣方通常記哪個類別」，取代寫死猜一個預設值：

```python
from tw_einvoice_ocr import vendor_memory

patterns = vendor_memory.build(your_historical_records)  # [{"counterparty","category","direction","note"}]
vendor_memory.save_cache(patterns, "vendor_patterns.json")

hit = vendor_memory.lookup(patterns, tax_id=invoice["seller_tax_id"])
draft = invoice_to_draft(invoice, counterparty=hit and hit.get("counterparty"),
                          category=hit and hit.get("category"))
```

查不到的賣方，`category` 留空——不自動猜一個錯的類別，交給人工補。

## 草稿 schema

`invoice_to_draft()` / `ocr.parse_receipt_text()` 回傳的欄位一致：

| 欄位 | 型別 | 說明 |
|---|---|---|
| `date` | str | `YYYY-MM-DD` |
| `direction` | str | `收入` / `支出` |
| `payment_method` | str | 自由文字，QR 路徑預設「信用卡」 |
| `counterparty` | str | 店名或「賣方統編 XXXXXXXX」 |
| `category` | str | 自由文字，查不到歷史紀錄時留空 |
| `amount_incl_tax` | int | 含稅金額 |
| `invoice_status` | str | `有(含統編)` / `有(無統編)` / `無` |
| `evidence_url` | str\|None | 憑證圖片連結，由呼叫端補 |
| `note` | str | 自由文字 |
| `status` | str | 一律 `待審`，寫入正式帳本前需人工確認 |
| `confidence` | float | QR=0.99，OCR=0.5–0.85 |
| `source` | str | `qr` / 呼叫端自訂（OCR 預設 `photo`） |

金額與帳本寫入的紅線：本套件只負責「辨識出草稿」，**不做任何寫入動作**，也不
判斷是否要自動入帳——這是刻意留給呼叫端的，任何金額動作都應該有一道人工確認
關卡才寫入正式帳本。

## 測試

```bash
python tests/test_core.py
python tests/test_layout.py
```

無框架、assert-based。`test_core.py` 涵蓋 QR 解碼、草稿映射、賣方學習查表、
OCR 欄位解析；`test_layout.py` 涵蓋行聚類與 row-scoped 欄位擷取，並用一組對照
測試重現「OCR reading-order 誤判時全文正則會抓錯表格列」的已知失敗模式，證明
row-scoped 版本的必要性。兩者皆純資料/字串邏輯，不需要真的裝 Tesseract 引擎
就能跑；`ocr_to_draft()` 實際呼叫 Tesseract 的路徑（`_tesseract_words`/
`_tesseract_image`）需要系統裝好 Tesseract 5 才能端到端驗證。

## 已知限制

- QR 解碼只認台灣財政部電子發票證明聯格式，其他國家/其他格式的收據 QR 不支援。
- OCR 備援是規則式（正則+關鍵字+幾何行聚類），非通用 LLM 視覺辨識或深度學習
  版面模型，遇到排版特殊的收據信心值會偏低，設計上就是要讓人工確認，不是要
  做到全自動；行聚類目前只做「同一視覺行」等級的結構，不做跨行欄位對齊
  （多欄明細表的品名/數量/單價/小計四欄對齊），真的遇到這類需求才是該考慮
  導入 docling 本身的時機（見 layout.py 檔頭註解）。
- 賣方類別學習是多數決查表，不是機器學習模型，冷啟動（沒有歷史紀錄）時一律
  留空給人工補。
