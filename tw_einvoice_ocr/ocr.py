"""OCR 備援 — QR 解不到（紙本收據/截圖/非台灣電子發票）時的本地端純離線辨識。

不依賴任何雲端 API。需要安裝可選依賴：`pip install tw-einvoice-ocr[ocr]`
（Tesseract 5 本身要另外裝系統套件，含 chi_tra 繁中語言包）。

收據前處理管線（針對台灣熱感應收據優化）：
  1. CLAHE 局部對比增強（應對反光/低對比白色收據）
  2. 快速去噪（保留細字邊緣）
  3. Hough 直線旋轉校正（只校傾斜，不做透視 warp——白收據在淺色桌面對比不足，
     四角輪廓偵測不可靠）
  4. 自適應二值化（消除陰影/光照不均）

引擎：Tesseract 5 + 繁中語言包 (chi_tra+eng)；PDF 逐頁用 pymupdf 轉圖後 OCR。
回傳格式與 qr.py 的 to_draft 相容，confidence 反映 OCR 不確定性（0.5-0.85，
一律 status="待審"，需人工確認才能入帳）。
"""
from __future__ import annotations

import os
import re
import tempfile
import urllib.request
import urllib.error
from datetime import datetime

# ──────────────────────────────────────────────
# 正則：台灣收據 / 發票欄位
# ──────────────────────────────────────────────
_RE_INV_NO = re.compile(r'\b([A-Z]{2}[-]?\d{8})\b')
_RE_DATE_AD = re.compile(r'(\d{4})[-/](\d{1,2})[-/](\d{1,2})')
_RE_DATE_ROC = re.compile(r'(\d{3})年(\d{1,2})月(\d{1,2})日')
_RE_DATE_ROC2 = re.compile(r'(\d{3})[/-](\d{1,2})[/-](\d{1,2})')
# 優先抓含稅合計，再抓未稅/小計，最後抓 $ 符號
_RE_AMOUNT_TOTAL = re.compile(r'(?:總計|合計|含稅|消費金額)[^\d\n]*(\d[\d,]+)', re.IGNORECASE)
_RE_AMOUNT_UNTAXED = re.compile(r'(?:未稅|小計|營業額|銷售額)[^\d\n]*(\d[\d,]+)', re.IGNORECASE)
_RE_AMOUNT_TAX = re.compile(r'(?:營業稅|稅額|稅金)[^\d\n]*(\d[\d,]+)', re.IGNORECASE)
_RE_AMOUNT2 = re.compile(r'\$\s*(\d[\d,]+)')
_RE_TAX_ID = re.compile(r'(?:統編|统编|買方|买方|賣方|卖方)[^\d]*(\d{8})')
_PAYMENT_KW = {
    "信用卡": "信用卡", "刷卡": "信用卡",
    "現金": "現金", "cash": "現金",
    "line pay": "其他", "linepay": "其他",
    "街口": "其他", "悠遊": "其他",
    "台灣pay": "其他", "twpay": "其他",
    "轉帳": "銀行", "匯款": "銀行",
}
_CAT_KW = [
    (["加油", "油站", "中油", "台油", "全國加油"], "交通費"),
    (["計程車", "uber", "taxi", "mrt", "捷運", "高鐵", "台鐵", "公車", "停車"], "交通費"),
    (["超商", "7-11", "711", "全家", "萊爾富", "ok便利"], "日常雜支"),
    (["藥局", "藥妝", "屈臣氏", "康是美", "藥品"], "醫療費"),
    (["文具", "書局", "書店"], "辦公用品"),
    (["餐廳", "飲食", "咖啡", "茶", "飲料", "便當", "小吃", "麵", "飯", "食", "starbucks", "麥當勞", "肯德基"], "餐飲"),
]


# ──────────────────────────────────────────────
# 遠端來源下載（含 OneDrive 分享連結轉直連）
# ──────────────────────────────────────────────

def _onedrive_to_download_url(url: str) -> str:
    """1drv.ms / onedrive.live.com 分享連結 -> 直接下載 URL。
    若檔案設為「需要登入」而非「任何人皆可存取」，仍會回傳 HTML（403/401）。
    """
    if "1drv.ms" not in url and "onedrive.live.com" not in url:
        return url

    try:
        class _NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                return None

        opener = urllib.request.build_opener(_NoRedirect)
        try:
            opener.open(urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"}), timeout=10)
        except urllib.error.HTTPError as e:
            loc = e.headers.get("Location", "")
            if loc:
                sep = "&" if "?" in loc else "?"
                return loc + sep + "download=1"
    except Exception:
        pass

    sep = "&" if "?" in url else "?"
    return url + sep + "download=1"


def _download_to_tempfile(url: str) -> tuple[str, str]:
    """下載 URL -> 暫存檔，回傳 (path, content_type)。"""
    dl_url = _onedrive_to_download_url(url)
    headers = {"User-Agent": "Mozilla/5.0 (compatible; tw-einvoice-ocr/1.0)"}
    req = urllib.request.Request(dl_url, headers=headers)
    with urllib.request.urlopen(req, timeout=60) as resp:
        ctype = resp.headers.get("Content-Type", "application/octet-stream")
        data = resp.read()

    if "pdf" in ctype:
        suffix = ".pdf"
    elif "jpeg" in ctype or "jpg" in ctype:
        suffix = ".jpg"
    elif "png" in ctype:
        suffix = ".png"
    elif "webp" in ctype:
        suffix = ".webp"
    else:
        path_lower = dl_url.split("?")[0].lower()
        suffix = ".pdf" if path_lower.endswith(".pdf") else ".jpg"

    if "text/html" in ctype:
        raise RuntimeError(
            "無法直接下載此連結（需要登入或檔案未設為公開）。"
            "請改用本機檔案路徑，或確認分享設定為「任何人皆可存取」。"
        )

    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    tmp.write(data)
    tmp.close()
    return tmp.name, ctype


# ──────────────────────────────────────────────
# 收據前處理管線（OpenCV）
# ──────────────────────────────────────────────

def _deskew(gray):
    """Hough 直線偵測文字行角度做旋轉校正。只校傾斜，不做透視變換。"""
    import cv2
    import numpy as np

    edges = cv2.Canny(gray, 50, 150, apertureSize=3)
    lines = cv2.HoughLinesP(edges, 1, np.pi / 180, threshold=80,
                            minLineLength=50, maxLineGap=10)
    if lines is None:
        return gray

    angles = []
    for line in lines:
        x1, y1, x2, y2 = line[0]
        if x2 != x1:
            angle = np.degrees(np.arctan2(y2 - y1, x2 - x1))
            if abs(angle) < 45:
                angles.append(angle)

    if not angles:
        return gray

    median_angle = float(np.median(angles))
    if abs(median_angle) < 0.5:
        return gray

    h, w = gray.shape
    M = cv2.getRotationMatrix2D((w // 2, h // 2), median_angle, 1.0)
    return cv2.warpAffine(gray, M, (w, h),
                          flags=cv2.INTER_CUBIC,
                          borderMode=cv2.BORDER_REPLICATE)


def scan_document(pil_img):
    """收據專用前處理：CLAHE 對比增強 -> 去噪 -> 旋轉校正 -> 自適應二值化。
    輸入 PIL.Image，回傳前處理後的 PIL.Image（灰階）。
    """
    import cv2
    import numpy as np
    from PIL import Image

    gray = cv2.cvtColor(np.array(pil_img.convert("RGB")), cv2.COLOR_RGB2GRAY)

    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
    enhanced = clahe.apply(gray)

    denoised = cv2.fastNlMeansDenoising(enhanced, h=10,
                                        templateWindowSize=7,
                                        searchWindowSize=21)

    deskewed = _deskew(denoised)

    cleaned = cv2.adaptiveThreshold(
        deskewed, 255,
        cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
        cv2.THRESH_BINARY,
        blockSize=21, C=10
    )

    return Image.fromarray(cleaned)


def _preprocess(img):
    """圖片前處理主入口。優先走 OpenCV 掃描管線，未裝就退回 PIL 基本處理。"""
    try:
        return scan_document(img)
    except ImportError:
        pass
    except Exception:
        pass

    from PIL import ImageOps, ImageFilter
    if img.mode != "L":
        img = img.convert("L")
    img = img.filter(ImageFilter.SHARPEN)
    img = ImageOps.autocontrast(img, cutoff=2)
    return img


# ──────────────────────────────────────────────
# 本地 OCR（Tesseract）
# ──────────────────────────────────────────────

_TESS_LANG = "chi_tra+eng"
_TESS_CONFIG = "--psm 3 --oem 3"


def _tesseract_image(img) -> str:
    try:
        import pytesseract
    except ImportError:
        raise RuntimeError("需要 pytesseract：pip install tw-einvoice-ocr[ocr]")

    processed = _preprocess(img)
    text = pytesseract.image_to_string(processed, lang=_TESS_LANG, config=_TESS_CONFIG)
    return text.strip()


def ocr_image(image_path: str) -> str | None:
    from PIL import Image
    img = Image.open(image_path)
    text = _tesseract_image(img)
    return text or None


def ocr_pdf(pdf_path: str) -> str | None:
    """PDF -> 逐頁以 pymupdf 轉圖 -> Tesseract OCR -> 合併全文。每頁 2x 縮放確保解析度。"""
    try:
        import fitz
    except ImportError:
        raise RuntimeError("PDF OCR 需要 pymupdf：pip install tw-einvoice-ocr[ocr]")
    from PIL import Image
    import io

    doc = fitz.open(pdf_path)
    all_text: list[str] = []
    for page in doc:
        mat = fitz.Matrix(2, 2)
        pix = page.get_pixmap(matrix=mat)
        img = Image.open(io.BytesIO(pix.tobytes("png")))
        page_text = _tesseract_image(img)
        if page_text:
            all_text.append(page_text)
    doc.close()
    return "\n".join(all_text) if all_text else None


def ocr_url(url: str) -> str | None:
    """URL（含 OneDrive share link）-> 下載 -> 自動偵測 PDF/圖片 -> OCR 全文。"""
    tmp_path, ctype = _download_to_tempfile(url)
    try:
        if "pdf" in ctype or tmp_path.endswith(".pdf"):
            return ocr_pdf(tmp_path)
        else:
            return ocr_image(tmp_path)
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


# ──────────────────────────────────────────────
# 欄位解析
# ──────────────────────────────────────────────

def _roc_to_ad(roc_year: str, month: str, day: str) -> str:
    return f"{int(roc_year)+1911:04d}-{int(month):02d}-{int(day):02d}"


def _extract_date(text: str) -> tuple[str | None, float]:
    m = _RE_DATE_AD.search(text)
    if m:
        y, mo, d = m.groups()
        if 2000 <= int(y) <= 2099:
            return f"{int(y):04d}-{int(mo):02d}-{int(d):02d}", 0.9
    m = _RE_DATE_ROC.search(text)
    if m:
        return _roc_to_ad(*m.groups()), 0.85
    m = _RE_DATE_ROC2.search(text)
    if m:
        return _roc_to_ad(*m.groups()), 0.80
    return None, 0.0


def _extract_amount(text: str) -> tuple[int | None, float]:
    m = _RE_AMOUNT_TOTAL.search(text)
    if m:
        return int(m.group(1).replace(",", "")), 0.85
    m = _RE_AMOUNT_UNTAXED.search(text)
    if m:
        return int(m.group(1).replace(",", "")), 0.75
    m = _RE_AMOUNT2.search(text)
    if m:
        return int(m.group(1).replace(",", "")), 0.70
    nums = [int(n.replace(",", "")) for n in re.findall(r'\b(\d{2,6})\b', text)
            if 10 <= int(n.replace(",", "")) <= 999999]
    if nums:
        return max(nums), 0.5
    return None, 0.0


def _extract_three_amounts(text: str) -> tuple[int | None, int | None, int | None]:
    """從 OCR 文字擷取三欄金額：tax_inclusive(含稅)/tax_exclusive(未稅)/tax_amount(稅額)。
    策略：三欄都抓到就驗證 5% 稅率平衡；只抓到其中一欄就用 5% 稅率反推其餘兩欄。
    """
    tax_inclusive = tax_exclusive = tax_amount = None

    m = _RE_AMOUNT_TOTAL.search(text)
    if m:
        tax_inclusive = int(m.group(1).replace(",", ""))
    m = _RE_AMOUNT_UNTAXED.search(text)
    if m:
        tax_exclusive = int(m.group(1).replace(",", ""))
    m = _RE_AMOUNT_TAX.search(text)
    if m:
        tax_amount = int(m.group(1).replace(",", ""))

    if tax_inclusive is not None and tax_exclusive is not None and tax_amount is not None:
        expected_tax = round(tax_exclusive * 0.05)
        expected_inclusive = tax_exclusive + expected_tax
        if tax_amount == expected_tax and tax_inclusive == expected_inclusive:
            return tax_inclusive, tax_exclusive, tax_amount
        # 不平衡時優先信任含稅金額（收據最常見）
        tax_exclusive = round(tax_inclusive / 1.05)
        tax_amount = tax_inclusive - tax_exclusive
        return tax_inclusive, tax_exclusive, tax_amount

    if tax_inclusive is not None:
        tax_exclusive = round(tax_inclusive / 1.05)
        tax_amount = tax_inclusive - tax_exclusive
        return tax_inclusive, tax_exclusive, tax_amount

    if tax_exclusive is not None:
        tax_inclusive = round(tax_exclusive * 1.05)
        tax_amount = tax_inclusive - tax_exclusive
        return tax_inclusive, tax_exclusive, tax_amount

    if tax_amount is not None:
        tax_exclusive = round(tax_amount / 0.05)
        tax_inclusive = tax_exclusive + tax_amount
        return tax_inclusive, tax_exclusive, tax_amount

    amount, _ = _extract_amount(text)
    if amount is not None:
        tax_inclusive = amount
        tax_exclusive = round(amount / 1.05)
        tax_amount = tax_inclusive - tax_exclusive
        return tax_inclusive, tax_exclusive, tax_amount

    return None, None, None


def _extract_tax_id(text: str) -> tuple[str | None, str | None]:
    buyer = seller = None
    for m in _RE_TAX_ID.finditer(text):
        prefix = m.group(0)[:4]
        if any(k in prefix for k in ("買方", "买方")):
            buyer = m.group(1)
        elif any(k in prefix for k in ("賣方", "卖方")):
            seller = m.group(1)
        elif seller is None:
            seller = m.group(1)
    return buyer, seller


def _extract_payment(text: str) -> str:
    tl = text.lower()
    for kw, method in _PAYMENT_KW.items():
        if kw in tl:
            return method
    return "信用卡"


def _guess_category(text: str) -> str:
    tl = text.lower()
    for kws, cat in _CAT_KW:
        if any(k in tl for k in kws):
            return cat
    return "日常雜支"


def parse_receipt_text(text: str, source: str = "photo") -> dict:
    """從 OCR 全文解析台灣收據/發票欄位，回傳 draft-like dict（與 draft.invoice_to_draft
    輸出的欄位相容，confidence 較低、status 一律「待審」）。
    """
    inv_m = _RE_INV_NO.search(text)
    inv_no = inv_m.group(1) if inv_m else None

    date, date_conf = _extract_date(text)
    if date is None:
        date = datetime.now().strftime("%Y-%m-%d")
        date_conf = 0.3

    amount, amt_conf = _extract_amount(text)
    if amount is None:
        amount = 0
        amt_conf = 0.0

    buyer_id, seller_id = _extract_tax_id(text)
    payment = _extract_payment(text)
    category = _guess_category(text)

    if seller_id:
        counterparty = f"賣方統編 {seller_id}"
    else:
        first_line = text.strip().splitlines()[0][:20] if text.strip() else "未知"
        counterparty = first_line

    if buyer_id or seller_id:
        inv_status = "有(含統編)" if buyer_id else "有(無統編)"
    else:
        inv_status = "有(無統編)" if inv_no else "無"

    tax_inclusive, tax_exclusive, tax_amount = _extract_three_amounts(text)

    if tax_inclusive is None:
        tax_inclusive = amount if amount > 0 else 0
        tax_exclusive = round(tax_inclusive / 1.05) if tax_inclusive > 0 else 0
        tax_amount = tax_inclusive - tax_exclusive
        amt_conf = 0.3
    else:
        amt_conf = max(amt_conf, 0.7)

    confidence = round(min(date_conf, amt_conf if amt_conf > 0 else 0.5, 0.85), 2)

    note_parts = []
    if inv_no:
        note_parts.append(f"發票 {inv_no}")
    note = " ".join(note_parts)

    return {
        "date": date,
        "direction": "支出",
        "payment_method": payment,
        "counterparty": counterparty,
        "category": category,
        "tax_inclusive": tax_inclusive,
        "tax_exclusive": tax_exclusive,
        "tax_amount": tax_amount,
        "amount_incl_tax": tax_inclusive,
        "invoice_status": inv_status,
        "evidence_url": None,
        "note": note,
        "status": "待審",
        "confidence": confidence,
        "source": source,
        "_ocr_raw_snippet": text[:300].replace("\n", " "),
    }


def ocr_to_draft(source: str, draft_source_tag: str = "photo") -> dict | None:
    """主入口：接受本機路徑（圖片/PDF）或 URL（含 OneDrive 連結）。
    回傳 draft dict；OCR 失敗回 None。
    """
    if source.startswith("http://") or source.startswith("https://"):
        text = ocr_url(source)
    else:
        path_lower = source.lower()
        if path_lower.endswith(".pdf"):
            text = ocr_pdf(source)
        else:
            text = ocr_image(source)

    if not text:
        return None
    return parse_receipt_text(text, source=draft_source_tag)
