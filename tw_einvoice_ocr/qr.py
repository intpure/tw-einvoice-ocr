"""台灣電子發票證明聯 QR code 解碼。

QR 內容是財政部規定的固定寬度欄位，解碼出來的是結構化資料，不需要用猜的，
信心值可視為 0.99。

左側主 QR 固定欄位：
  [0:10]  發票字軌號碼      [10:17] 開立日期(ROC YYYMMDD)  [17:21] 隨機碼
  [21:29] 銷售額(未稅,hex)  [29:37] 總計(含稅,hex)         [37:45] 買方統編
  [45:53] 賣方統編          [53:77] 加密驗證(AES/base64)
  之後以 ':' 分隔附加：二維條碼記錄資訊 / 中文編碼 / 品項總數 / (品名:數量:單價)...
金額用「總計(含稅)」。
"""
from __future__ import annotations

from PIL import Image
import zxingcpp


def decode_invoice_qr(image_path: str) -> dict | None:
    """讀圖片，找出台灣電子發票主 QR 並解碼。找不到（無 QR / 非發票）回 None。"""
    img = Image.open(image_path).convert("RGB")
    codes = zxingcpp.read_barcodes(img)
    main = None
    for c in codes:
        if c.format.name.upper().startswith("QRCODE") or "QR" in str(c.format):
            head = c.text.split(":", 1)[0]
            if len(head) >= 77 and head[:10].strip():
                main = c.text
                break
    if main is None:
        return None
    return parse_einvoice(main)


def parse_einvoice(qr_text: str) -> dict:
    """把電子發票 QR 的原始文字解析成結構化欄位。"""
    head = qr_text.split(":")[0]
    rest = qr_text.split(":")[1:]
    inv_no = head[0:10]
    roc = head[10:17]           # YYYMMDD (民國)
    rand = head[17:21]
    untaxed = int(head[21:29], 16)
    total = int(head[29:37], 16)
    buyer = head[37:45]
    seller = head[45:53]

    year = int(roc[0:3]) + 1911
    date = f"{year:04d}-{roc[3:5]}-{roc[5:7]}"

    # 品項：':' 後通常是 [記錄資訊, 中文編碼, 品項總數, (名:量:價)...]
    # 跳過遮罩記錄資訊(全 *)與純數字計數欄，只收真正的 (名:量:價) 三連。
    items = []
    i = 0
    while i + 2 < len(rest):
        name, qty, price = rest[i], rest[i + 1], rest[i + 2]
        is_real_name = bool(name) and not name.isdigit() and name.strip("*") != ""
        if is_real_name and qty.isdigit() and price.isdigit():
            items.append({"name": name, "qty": int(qty), "unit_price": int(price)})
            i += 3
        else:
            i += 1

    tax = total - untaxed
    return {
        "invoice_no": inv_no,
        "date": date,
        "random_code": rand,
        "amount_untaxed": untaxed,
        "amount_incl_tax": total,
        "tax_amount": tax,
        "buyer_tax_id": buyer if buyer.strip("0") else "",
        "seller_tax_id": seller,
        "items": items,
    }
