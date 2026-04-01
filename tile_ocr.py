"""
Tile OCR Helpers - OCR analysis of map tiles.
"""
import re

from config import HAS_CV2

if HAS_CV2:
    import cv2


def _tile_extract_name(raw: str) -> str:
    import re
    patterns = [
        r'Lv\.?\s*\d+\s+[\w][\w\s]+(?:Plot|Mine|Camp|Fort|Tower|Mill|Farm)',
        r'Wasteland',
        r'Sawmill',
    ]
    for pat in patterns:
        m = re.search(pat, raw, re.IGNORECASE)
        if m:
            return m.group(0).strip()
    for line in raw.splitlines():
        line = line.strip()
        if len(line) < 3:
            continue
        if re.search(r'\(\d+,\d+\)', line):
            continue
        if len(re.findall(r'[a-zA-Z]', line)) >= 4:
            clean = re.sub(r'[^\w\s\.\-/+]', '', line).strip()
            if len(clean) >= 4:
                return clean
    return ''


def _tile_ocr_region(img, y1p, y2p, x1p=0.15, x2p=0.90, scale=3, psm=6):
    try:
        import cv2, pytesseract
    except ImportError:
        return ''
    h, w = img.shape[:2]
    crop = img[int(h * y1p):int(h * y2p), int(w * x1p):int(w * x2p)]
    if crop.shape[0] < 4 or crop.shape[1] < 4:
        return ''
    up = cv2.resize(crop, (crop.shape[1] * scale, crop.shape[0] * scale),
                    interpolation=cv2.INTER_CUBIC)
    gray = cv2.cvtColor(up, cv2.COLOR_BGR2GRAY)
    _, th = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    return pytesseract.image_to_string(th, config=f'--psm {psm}')


def _tile_ocr_buttons(img) -> str:
    try:
        import cv2
    except ImportError:
        return ''
    fh, fw = img.shape[:2]
    roi_y1 = int(fh * 0.25)
    roi_y2 = int(fh * 0.92)
    roi_x1 = int(fw * 0.20)
    roi = img[roi_y1:roi_y2, roi_x1:]
    gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
    _, white = cv2.threshold(gray, 220, 255, cv2.THRESH_BINARY)
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (20, 4))
    white = cv2.morphologyEx(white, cv2.MORPH_CLOSE, kernel)
    cnts, _ = cv2.findContours(white, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    texts = []
    for c in cnts:
        bx, by, bw, bh = cv2.boundingRect(c)
        if bw * bh < 5000 or bw < 100 or bh < 30 or bw / max(bh, 1) < 1.2:
            continue
        crop = roi[max(0, by - 4):by + bh + 4, bx:bx + bw]
        t = _tile_ocr_region(crop, 0.0, 1.0, 0.0, 1.0, scale=3, psm=7)
        t = t.strip().replace('\n', ' ')
        if t:
            texts.append(t)
    return '\n'.join(texts)


def _classify_tile_from_text(text_title: str, text_btn: str = ''):
    import re
    text_all_low = (text_title + '\n' + text_btn).lower()
    tile_name = _tile_extract_name(text_title)
    if 'march' in text_all_low:
        return 'ally', tile_name
    if 'info' in text_all_low and 'conquer' in text_all_low:
        return 'enemy_city', tile_name
    if 'conquer' in text_all_low and 'enter' not in text_all_low:
        return 'enemy', tile_name
    terrain_kws = ['lake', 'hills', 'mountains', 'bridge', 'river']
    if any(k in text_all_low for k in terrain_kws):
        return 'terrain', tile_name
    if 'wasteland' in text_title.lower():
        return 'lv1', tile_name
    for lv in [5, 4, 3, 2]:
        if re.search(rf'lv\.?{lv}\b', text_title, re.IGNORECASE):
            return f'lv{lv}', tile_name
    for lv in [5, 4, 3, 2]:
        if re.search(rf'lv\.?{lv}\b', text_btn, re.IGNORECASE):
            return f'lv{lv}', tile_name
    return '?', tile_name


def analyze_tile_image(img):
    text_title = _tile_ocr_region(img, 0.25, 0.62)
    text_btn = _tile_ocr_buttons(img)
    label, tile_name = _classify_tile_from_text(text_title, text_btn)
    return {
        'label': label,
        'name': tile_name,
        'raw_title': text_title.strip(),
        'raw_btn': text_btn.strip(),
    }


