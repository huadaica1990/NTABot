"""
Detector - UI element detection via template matching and color analysis.
"""
import os
import re
import time
import numpy as np
from typing import Optional, List, Callable

from config import HAS_CV2, ASSETS_DIR

if HAS_CV2:
    import cv2


class Detector:
    def __init__(self, assets_dir: str, log: Callable):
        self.assets_dir = assets_dir
        self.log        = log
        self.templates  = {}
        self.reload()

    def reload(self):
        self.templates = {}
        if not HAS_CV2 or not os.path.isdir(self.assets_dir):
            return
        n = 0
        for f in os.listdir(self.assets_dir):
            if f.lower().endswith(".png"):
                img = cv2.imread(os.path.join(self.assets_dir, f))
                if img is not None:
                    self.templates[f[:-4].lower()] = img
                    n += 1
        if n: self.log(f"[Det] {n} templates")

    def _load_atk_templates(self):
        """Load multi-frame ATK templates tu assets/.
        Ho tro: btn_atk.png, btn_atk_0.png, btn_atk_1.png, ...
        """
        loaded = 0
        # Frame co so
        base = os.path.join(self.assets_dir, "btn_atk.png")
        img = cv2.imread(base) if os.path.exists(base) else None
        if img is not None:
            self.templates["btn_atk_f0"] = img
            loaded += 1
        # Frame danh so: btn_atk_0.png, btn_atk_1.png, ...
        i = 0
        while True:
            path = os.path.join(self.assets_dir, f"btn_atk_{i}.png")
            if not os.path.exists(path): break
            img = cv2.imread(path)
            if img is not None:
                self.templates[f"btn_atk_f{i+1}"] = img
                loaded += 1
            i += 1
        self._atk_templates_loaded = True
        if loaded:
            self.log(f"[Det] btn_atk: {loaded} frame(s) loaded")
        else:
            self.log(f"[Det] btn_atk: khong co file nao trong assets/")

    def find_atk_btn(self, screen, thr=0.65):
        """Tim icon ATK - thu tat ca frame template."""
        if not getattr(self, "_atk_templates_loaded", False):
            self._load_atk_templates()
        keys = [k for k in self.templates if k.startswith("btn_atk_f")]
        for key in keys:
            for t in [thr, thr - 0.10]:
                p = self._match(screen, key, t)
                if p:
                    self.log(f"[Det] ATK match: {key} thr={t:.2f} pos={p}")
                    return p
        return None

    def _match(self, screen, key, thr=0.72):
        if not HAS_CV2 or screen is None: return None
        if key not in self.templates: return None
        t = self.templates[key]
        th, tw = t.shape[:2]
        sh, sw = screen.shape[:2]
        if tw >= sw or th >= sh: return None
        res = cv2.matchTemplate(screen, t, cv2.TM_CCOEFF_NORMED)
        _, mv, _, ml = cv2.minMaxLoc(res)
        if mv >= thr:
            return (ml[0]+tw//2, ml[1]+th//2)
        return None

    def find_capture_btn(self, screen):
        """
        Tim nut Chiem / Conquer.
        Nut nay la hop trang/kem co chu 'Chiem' hoac 'Conquer' + icon X do.
        Tim o nua phai man hinh, phan giua-duoi.
        """
        for thr in [0.85, 0.75, 0.65]:
            p = self._match(screen, "btn_capture", thr)
            if p: return p
        if not HAS_CV2 or screen is None: return None
        h, w = screen.shape[:2]

        # --- Pass 1: icon X do (mau do tuoi BGR) ---
        lo_r = np.array([0,  30, 160], np.uint8)
        hi_r = np.array([80, 110, 255], np.uint8)
        mask_r = cv2.inRange(screen, lo_r, hi_r)
        cnts_r, _ = cv2.findContours(mask_r, cv2.RETR_EXTERNAL,
                                      cv2.CHAIN_APPROX_SIMPLE)
        best_a, best_p = 0, None
        for c in cnts_r:
            a = cv2.contourArea(c)
            if a > 200 and a > best_a:
                M = cv2.moments(c)
                if M["m00"] > 0:
                    cx = int(M["m10"]/M["m00"])
                    cy = int(M["m01"]/M["m00"])
                    # Phai nam o nua phai man hinh
                    if cx > w * 0.45:
                        best_a = a; best_p = (cx, cy)
        if best_p:
            # Shift sang phai de tap vao giua nut (icon nam ben trai)
            return (min(best_p[0] + 60, w - 10), best_p[1])

        # --- Pass 2: nut trang (Conquer/Chiem) - hop kem vien tron ---
        # Tim vung trang lon o nua phai, nua duoi man hinh
        roi_x0 = w // 2
        roi = screen[int(h*0.3):int(h*0.85), roi_x0:]
        lo_w = np.array([210, 210, 195], np.uint8)
        hi_w = np.array([255, 255, 240], np.uint8)
        mask_w = cv2.inRange(roi, lo_w, hi_w)
        # Dilate de lien ket cac phan
        kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (15, 8))
        mask_w = cv2.dilate(mask_w, kernel)
        cnts_w, _ = cv2.findContours(mask_w, cv2.RETR_EXTERNAL,
                                      cv2.CHAIN_APPROX_SIMPLE)
        btn_rects = []
        for c in cnts_w:
            a = cv2.contourArea(c)
            x, y, bw, bh = cv2.boundingRect(c)
            # Nut phai co ti le ngang (rong > cao) va du lon
            if a > 1500 and bw > bh * 1.5 and bw > 60:
                btn_rects.append((y, x, bw, bh))  # sort by y
        if btn_rects:
            # Lay nut thap nhat (Conquer/Chiem thuong o duoi Enter)
            btn_rects.sort(key=lambda r: r[0], reverse=True)
            y, x, bw, bh = btn_rects[0]
            cx = roi_x0 + x + bw // 2
            cy = int(h*0.3) + y + bh // 2
            return (cx, cy)

        return None

    def find_build_btn(self, screen):
        """
        Tim nut Build bang template matching (assets/btn_build.png).
        Logic giong find_capture_btn: thu nhieu threshold giam dan.
        """
        for thr in [0.85, 0.75, 0.65]:
            p = self._match(screen, "btn_build", thr)
            if p: return p
        return None

    def find_ok_btn(self, screen):
        # Vi tri co dinh cua nut OK
        return (342, 1045)

    def has_capture_popup(self, screen) -> bool:
        return self.find_capture_btn(screen) is not None

    def is_troop_screen(self, screen) -> bool:
        p = self.find_ok_btn(screen)
        if p is None: return False
        if screen is None: return False
        return p[1] > screen.shape[0] * 0.5

    def popup_closed(self, screen) -> bool:
        return not self.has_capture_popup(screen)

    def _find_bottom_right_action_btn(self, screen):
        """
        Tim nut action vang lon o goc duoi-phai popup map.
        Ho tro ca giao dien 'Xem' va 'View'.
        """
        if not HAS_CV2 or screen is None:
            return None
        h, w = screen.shape[:2]
        y0, y1 = int(h * 0.82), min(h, int(h * 0.985))
        x0, x1 = int(w * 0.68), w
        roi = screen[y0:y1, x0:x1]
        if roi.size == 0:
            return None

        hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
        mask = cv2.inRange(hsv, np.array([10, 40, 150], np.uint8),
                                np.array([40, 255, 255], np.uint8))
        kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (9, 9))
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel)
        cnts, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        best = None
        best_score = 0.0
        for c in cnts:
            a = cv2.contourArea(c)
            x, y, bw, bh = cv2.boundingRect(c)
            if a < 2500 or bw < 90 or bh < 40:
                continue
            if x + bw < int(roi.shape[1] * 0.55):
                continue
            score = float(a) + bw * 8 + bh * 4
            if score > best_score:
                best_score = score
                best = (x0 + x + bw // 2, y0 + y + bh // 2)
        return best

    def find_xem_btn(self, screen):
        """Tim nut Xem / View (vang/cam) o cuoi man hinh."""
        for thr in [0.85, 0.75, 0.65]:
            p = self._match(screen, "btn_xem", thr)
            if p: return p
        if not HAS_CV2 or screen is None: return None
        h, w = screen.shape[:2]
        lo = np.array([25, 145, 175], np.uint8)
        hi = np.array([150, 235, 255], np.uint8)
        roi  = screen[int(h*0.82):, :]
        mask = cv2.inRange(roi, lo, hi)
        kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (7, 7))
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel)
        cnts, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL,
                                    cv2.CHAIN_APPROX_SIMPLE)
        best_a, best_p = 0, None
        for c in cnts:
            a = cv2.contourArea(c)
            x, y, bw, bh = cv2.boundingRect(c)
            if a > 1800 and bw > 70 and bh > 30 and (x + bw//2) > int(w * 0.60):
                if a > best_a:
                    best_a = a
                    best_p = (x + bw // 2, y + bh // 2 + int(h*0.82))
        if best_p:
            return best_p
        return self._find_bottom_right_action_btn(screen)


    def find_hanh_quan_btn(self, screen):
        """Tim nut Hanh Quan (vang/nau vang) xuat hien sau khi tap vao o tren ban do."""
        for thr in [0.85, 0.75, 0.65]:
            p = self._match(screen, "btn_hanh_quan", thr)
            if p: return p
        if not HAS_CV2 or screen is None: return None
        h, w = screen.shape[:2]
        # Nut Hanh Quan: mau vang nhat BGR ~ [30,160,200] den [120,215,255]
        lo = np.array([30,  155, 185], np.uint8)
        hi = np.array([130, 220, 255], np.uint8)
        # Tim o nua duoi man hinh (thuong xuat hien giua-duoi)
        roi  = screen[int(h*0.55):, :]
        mask = cv2.inRange(roi, lo, hi)
        cnts, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL,
                                    cv2.CHAIN_APPROX_SIMPLE)
        best_a, best_p = 0, None
        for c in cnts:
            a = cv2.contourArea(c)
            if a > 1200 and a > best_a:
                best_a = a
                M = cv2.moments(c)
                if M["m00"] > 0:
                    best_p = (int(M["m10"]/M["m00"]),
                              int(M["m01"]/M["m00"]) + int(h*0.55))
        return best_p

    def has_hanh_quan_btn(self, screen) -> bool:
        return self.find_hanh_quan_btn(screen) is not None


    def has_error_popup(self, screen) -> bool:
        """
        Kiem tra co popup loi (hop trang vien tron, chu den) khong.
        Vi du: 'Chi co the chiem manh dat lien ke'.
        Popup nay thuong xuat hien giua man hinh, nen vung nen la mau trang.
        """
        if not HAS_CV2 or screen is None: return False
        h, w = screen.shape[:2]
        # Scan vung giua man hinh (30%-70% cao, 10%-90% ngang)
        y0 = int(h * 0.30); y1 = int(h * 0.65)
        x0 = int(w * 0.10); x1 = int(w * 0.90)
        roi = screen[y0:y1, x0:x1]
        # Popup loi: nen trang (R>220, G>220, B>220) chiem nhieu
        lo = np.array([210, 210, 210], np.uint8)
        hi = np.array([255, 255, 255], np.uint8)
        mask  = cv2.inRange(roi, lo, hi)
        white_ratio = mask.sum() / 255 / (roi.shape[0] * roi.shape[1])
        return white_ratio > 0.35  # >35% la trang -> co popup

    def dismiss_error_popup(self, screen):
        """Tra ve toa do de tap dong popup loi (vung giua-duoi popup)."""
        if screen is None: return None
        h, w = screen.shape[:2]
        # Tap vao giua man hinh de dong
        return (w // 2, int(h * 0.55))

    def has_captcha_popup(self, screen) -> bool:
        """
        Kiem tra co popup CAPTCHA 'Random Test' khong.
        Popup hien giua man hinh voi text 'Random Test'.
        Dung OCR de phat hien.
        """
        if not HAS_CV2 or screen is None: return False
        try:
            import pytesseract
            h, w = screen.shape[:2]
            # Crop vung giua-tren man hinh (20%-55% cao, 10%-90% ngang)
            y0, y1 = int(h * 0.15), int(h * 0.55)
            x0, x1 = int(w * 0.10), int(w * 0.90)
            roi = screen[y0:y1, x0:x1]
            # Scale up + grayscale + threshold cho OCR tot hon
            roi2 = cv2.resize(roi, (roi.shape[1]*2, roi.shape[0]*2),
                              interpolation=cv2.INTER_CUBIC)
            gray = cv2.cvtColor(roi2, cv2.COLOR_BGR2GRAY)
            _, th = cv2.threshold(gray, 0, 255,
                                  cv2.THRESH_BINARY + cv2.THRESH_OTSU)
            text = pytesseract.image_to_string(th, config="--psm 6")
            text_lower = text.lower()
            # Tim keywords cua CAPTCHA popup
            captcha_kw = ["random test", "please select", "from the pictures",
                          "countdown"]
            found = sum(1 for kw in captcha_kw if kw in text_lower)
            return found >= 2  # Tim thay it nhat 2 keywords
        except Exception:
            return False
    def find_xy_fields(self, screen):
        """
        Tra ve (x_field, y_field) ADB pixel khi popup ban do mo.
        Scan vung duoi cung tim 2 o trang (input fields) truoc nut Xem.
        """
        if not HAS_CV2 or screen is None: return None, None
        h, w = screen.shape[:2]
        xem = self.find_xem_btn(screen)
        if xem is None: return None, None
        xem_x, xem_y = xem

        # Scan hang ngang tai xem_y, tim cac vung trang (o nhap)
        bar_y = xem_y
        row   = screen[bar_y, :]
        in_w  = False; start = 0; fields = []
        for x in range(0, xem_x - 20):
            px = row[x]
            is_w = (int(px[0]) > 195 and int(px[1]) > 195
                    and int(px[2]) > 195)
            if is_w and not in_w:
                in_w = True; start = x
            elif not is_w and in_w:
                in_w = False
                if x - start > 25:   # o nhap phai rong
                    fields.append((start + x) // 2)
        if len(fields) >= 2:
            return (fields[0], bar_y), (fields[1], bar_y)
        elif len(fields) == 1:
            # Chi tim duoc 1: gia su X field, Y = Xem - 130
            return (fields[0], bar_y), (xem_x - 130, bar_y)
        return None, None
    def _is_territory_map_popup(self, screen) -> bool:
        """
        Territory map: chi detect bang legend tren cung.
        """
        if not HAS_CV2 or screen is None:
            return False
        h, w = screen.shape[:2]

        txt_top = self._ocr_text_roi(
            screen,
            int(w * 0.02), int(h * 0.14),
            int(w * 0.98), int(h * 0.24),
            psm=6, scale=3
        )
        if not txt_top:
            return False

        txt_norm = re.sub(r"[^a-z]+", " ", txt_top.lower()).strip()
        phrases = ["our territory", "allied territory", "other players"]
        phrase_hits = sum(1 for p in phrases if p in txt_norm)

        token_hits = sum(
            1 for kw in ["our", "territory", "allied", "other", "players"]
            if kw in txt_norm
        )

        if phrase_hits >= 2:
            return True
        if token_hits >= 4 and sum(1 for kw in ["our", "allied", "other", "players"] if kw in txt_norm) >= 3:
            return True
        return False
        
    def has_nav_popup(self, screen) -> bool:
        """Kiem tra popup ban do territory map da mo chua."""
        return self._is_territory_map_popup(screen)

    def detect_checkboxes(self, screen) -> List[int]:
        """Y-positions của các checkbox hiển thị."""
        if not HAS_CV2 or screen is None: return []
        h, w = screen.shape[:2]
        x0 = int(w * 0.080)
        x1 = int(w * 0.120)
        lo = np.array([100, 125, 148], np.uint8)
        hi = np.array([148, 172, 198], np.uint8)
        roi  = screen[:, x0:x1]
        mask = cv2.inRange(roi, lo, hi)
        proj = (mask.sum(axis=1) / 255).astype(float)
        roi_w = x1 - x0
        P_THR = int(roi_w * 0.35)
        H_THR = int(roi_w * 0.45)
        y_min = int(h * 0.22)
        y_max = int(h * 0.88)
        peaks = []
        in_r, start = False, 0
        for y in range(y_min, y_max):
            v = proj[y]
            if v >= P_THR and not in_r:
                in_r, start = True, y
            elif v < P_THR and in_r:
                in_r = False
                if float(proj[start:y].max()) >= H_THR:
                    peaks.append((start + y) // 2)
        ys = []
        for cy in peaks:
            if not ys or cy - ys[-1] > 35:
                ys.append(cy)
            else:
                ys[-1] = (ys[-1] + cy) // 2
        return ys

    def checkbox_x(self, screen) -> int:
        if screen is None: return 71
        return int(screen.shape[1] * 0.100)

    def is_checked(self, screen, cb_x: int, cb_y: int) -> bool:
        """
        Kiem tra checkbox tai (cb_x, cb_y) da duoc check chua.
        Checked = co dau check mau xanh la ben trong o vuong.
        """
        if not HAS_CV2 or screen is None: return False
        h, w = screen.shape[:2]
        x0 = max(0, cb_x - 7); x1 = min(w, cb_x + 7)
        y0 = max(0, cb_y - 7); y1 = min(h, cb_y + 7)
        region = screen[y0:y1, x0:x1]
        if region.size == 0: return False
        # Mau xanh la: G cao, B va R thap
        lo = np.array([20,  100,  20], np.uint8)
        hi = np.array([130, 230, 130], np.uint8)
        green_px = int(cv2.inRange(region, lo, hi).sum() / 255)
        return green_px >= 4

    def read_troop_name(self, screen, cb_x: int, cb_y: int) -> str:
        """
        OCR doc ten linh tai hang co checkbox o cb_y.
        Do checkbox che khuat 1-2 ky tu dau, tra ve partial text.
        Dung ket hop fuzzy_match_name de tim ten dung.
        """
        if not HAS_CV2 or screen is None: return ""
        try:
            import pytesseract
            from PIL import Image as PILImage
        except ImportError:
            return ""
        import re
        h, w = screen.shape[:2]
        # Name region: sau checkbox (~11%), truoc location text (~50%)
        nx0 = int(w * 0.11)
        nx1 = int(w * 0.50)
        ny0 = max(0, cb_y - 13)
        ny1 = min(h, cb_y + 13)
        crop = screen[ny0:ny1, nx0:nx1]
        if crop.size == 0: return ""
        # Upscale x5 + threshold
        big  = cv2.resize(crop, None, fx=5, fy=5,
                          interpolation=cv2.INTER_LANCZOS4)
        gray = cv2.cvtColor(big, cv2.COLOR_BGR2GRAY)
        _, thr = cv2.threshold(gray, 155, 255, cv2.THRESH_BINARY)
        pil  = PILImage.fromarray(thr)
        try:
            raw = pytesseract.image_to_string(
                pil, lang="vie+eng", config="--psm 7 --oem 3")
        except Exception:
            try:
                raw = pytesseract.image_to_string(
                    pil, lang="eng", config="--psm 7 --oem 3")
            except Exception:
                return ""
        # Lay word dau tien, bo leading garbage chars
        raw  = raw.strip()
        parts = raw.split()
        word  = parts[0] if parts else ""
        word  = re.sub(r"^[lI|!\[\](){}\-]+", "", word)
        word  = re.sub(r"[^\w\u00c0-\u1ef9]", "", word)
        return word

    @staticmethod
    def fuzzy_match_name(ocr_word: str,
                         targets: List[str]) -> Optional[str]:
        """
        Khop partial OCR voi danh sach ten muc tieu.
        OCR mat 1-2 ky tu dau => dung suffix/substring match.
        Vi du: 'uto5' khop 'auto5', 'aid1' khop 'raid1'.
        """
        import re
        def norm(s: str) -> str:
            s = s.lower().strip()
            s = re.sub(r"[^a-z0-9\u00e0-\u1ef9]", "", s)
            return s
        got = norm(ocr_word)
        if len(got) < 2: return None
        best_name = None; best_score = 0.0
        for t in targets:
            tn = norm(t)
            if not tn: continue
            if tn == got: return t                         # exact
            if tn.endswith(got) and len(got) >= 2:         # suffix
                score = len(got) / len(tn)
                if score > best_score:
                    best_score = score; best_name = t
            if got in tn and len(got) >= 3:                # substring
                score = len(got) / len(tn)
                if score > best_score:
                    best_score = score; best_name = t
        return best_name if best_score >= 0.5 else None



    def read_march_time(self, screen, near_y: int = -1) -> str:
        """
        OCR doc dong 'TG hanh quan H:MM:SS'.
        near_y: y pixel cua checkbox vua click, scan vung phia duoi no.
        Neu near_y=-1 thi scan toan man hinh.
        Tra ve chuoi thoi gian, vi du '0:12:09', hoac '' neu khong doc duoc.
        """
        if not HAS_CV2 or screen is None: return ""
        try:
            import pytesseract
            from PIL import Image as PILImage
        except ImportError:
            return ""
        import re
        h, w = screen.shape[:2]
        if near_y > 0:
            # Scan vung tu checkbox den checkbox+200px
            y0 = min(near_y, h - 10)
            y1 = min(near_y + 200, h)
        else:
            # Scan toan man hinh
            y0, y1 = 0, h
        roi = screen[y0:y1, :]
        if roi.size == 0: return ""
        big  = cv2.resize(roi, None, fx=3, fy=3,
                          interpolation=cv2.INTER_LANCZOS4)
        gray = cv2.cvtColor(big, cv2.COLOR_BGR2GRAY)
        _, thr = cv2.threshold(gray, 160, 255, cv2.THRESH_BINARY)
        pil = PILImage.fromarray(thr)
        try:
            raw = pytesseract.image_to_string(
                pil, lang="vie+eng", config="--psm 6 --oem 3")
        except Exception:
            try:
                raw = pytesseract.image_to_string(
                    pil, lang="eng", config="--psm 6 --oem 3")
            except Exception:
                return ""
        m = re.search(r"(\d{1,2}:\d{2}:\d{2}|\d{1,2}:\d{2})", raw)
        return m.group(0) if m else ""


    def _ocr_text_roi(self, screen, x0: int, y0: int, x1: int, y1: int,
                      psm: int = 6, scale: int = 2) -> str:
        """OCR nho gon cho 1 ROI, tra ve text lower-case hoac ''."""
        if not HAS_CV2 or screen is None:
            return ""
        try:
            import pytesseract
        except Exception:
            return ""
        h, w = screen.shape[:2]
        x0 = max(0, min(w - 1, int(x0)))
        x1 = max(x0 + 1, min(w, int(x1)))
        y0 = max(0, min(h - 1, int(y0)))
        y1 = max(y0 + 1, min(h, int(y1)))
        roi = screen[y0:y1, x0:x1]
        if roi.size == 0:
            return ""
        try:
            big = cv2.resize(roi, None, fx=scale, fy=scale,
                             interpolation=cv2.INTER_CUBIC)
            gray = cv2.cvtColor(big, cv2.COLOR_BGR2GRAY)
            _, th = cv2.threshold(gray, 0, 255,
                                  cv2.THRESH_BINARY + cv2.THRESH_OTSU)
            raw = pytesseract.image_to_string(th, config=f"--psm {psm}")
            return re.sub(r"\s+", " ", raw).strip().lower()
        except Exception:
            return ""

    def _has_generic_popup(self, screen) -> bool:
        """
        Phat hien tong quat: co mot panel/popup lon o giua man hinh.
        Dung de gom chat / lucky wheel / mall ... ve nhom 'other'.
        """
        if not HAS_CV2 or screen is None:
            return False
        h, w = screen.shape[:2]
        # ROI giua man hinh - popup thuong co nen kem/trang.
        y0, y1 = int(h * 0.18), int(h * 0.88)
        x0, x1 = int(w * 0.06), int(w * 0.94)
        roi = screen[y0:y1, x0:x1]
        if roi.size == 0:
            return False

        # Popup game nay thuong co nen kem nhat + vien nau.
        light_lo = np.array([170, 185, 190], np.uint8)
        light_hi = np.array([255, 255, 255], np.uint8)
        light_ratio = float(cv2.inRange(roi, light_lo, light_hi).sum() / 255) / max(1, roi.shape[0] * roi.shape[1])

        brown_lo = np.array([40, 60, 90], np.uint8)
        brown_hi = np.array([130, 170, 210], np.uint8)
        top_band = roi[:max(10, roi.shape[0] // 6), :]
        brown_ratio = float(cv2.inRange(top_band, brown_lo, brown_hi).sum() / 255) / max(1, top_band.shape[0] * top_band.shape[1])

        # Lucky wheel la truong hop dac biet: vong tron lon mau vang kem o giua.
        center = screen[int(h * 0.22):int(h * 0.82), int(w * 0.10):int(w * 0.90)]
        wheel_lo = np.array([120, 160, 190], np.uint8)
        wheel_hi = np.array([220, 235, 255], np.uint8)
        wheel_ratio = 0.0
        if center.size:
            wheel_ratio = float(cv2.inRange(center, wheel_lo, wheel_hi).sum() / 255) / max(1, center.shape[0] * center.shape[1])

        return (light_ratio >= 0.28 and brown_ratio >= 0.05) or wheel_ratio >= 0.22 or self.has_error_popup(screen)

    def _is_army_selection_popup(self, screen) -> bool:
        if not HAS_CV2 or screen is None:
            return False
        ys = self.detect_checkboxes(screen)
        if len(ys) < 2:
            return False
        h, w = screen.shape[:2]

        # Confirm button: vang/cam lon o giua day popup.
        roi = screen[int(h * 0.78):int(h * 0.97), int(w * 0.20):int(w * 0.82)]
        if roi.size == 0:
            return False
        lo = np.array([40, 140, 210], np.uint8)
        hi = np.array([160, 230, 255], np.uint8)
        mask = cv2.inRange(roi, lo, hi)
        cnts, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        has_big_confirm = False
        for c in cnts:
            a = cv2.contourArea(c)
            x, y, bw, bh = cv2.boundingRect(c)
            if a >= 2500 and bw >= int(w * 0.22) and bh >= 35:
                has_big_confirm = True
                break

        if has_big_confirm:
            return True

        # Fallback OCR title.
        txt = self._ocr_text_roi(screen,
                                 int(w * 0.18), int(h * 0.16),
                                 int(w * 0.82), int(h * 0.32),
                                 psm=6, scale=2)
        return ('army selection' in txt) or ('selected' in txt and 'confirm' in txt)

    def _is_all_armies_popup(self, screen) -> bool:
        if not HAS_CV2 or screen is None:
            return False
        h, w = screen.shape[:2]

        # All Armies co tieu de/tab o phia tren, khong co big confirm, va thuong co text Owned Armies.
        txt_top = self._ocr_text_roi(screen,
                                     int(w * 0.05), int(h * 0.12),
                                     int(w * 0.95), int(h * 0.38),
                                     psm=6, scale=2)
        if ('all armies' in txt_top) or ('owned armies' in txt_top):
            return True

        # Fallback: co popup lon + nhieu row army nhung it checkbox.
        if self.detect_checkboxes(screen):
            return False
        if not self._has_generic_popup(screen):
            return False

        # Scan 1 dong title khu vuc tren trai.
        txt_left = self._ocr_text_roi(screen,
                                      int(w * 0.05), int(h * 0.20),
                                      int(w * 0.60), int(h * 0.34),
                                      psm=6, scale=2)
        return ('owned' in txt_left and 'arm' in txt_left) or ('armies' in txt_left)

    def _is_lucky_wheel_popup(self, screen) -> bool:
        """Phát hiện popup Lucky Wheel bằng OCR title + text 'chance'."""
        if not HAS_CV2 or screen is None:
            return False
        h, w = screen.shape[:2]

        # OCR vùng title (15-25% H) — tìm "Lucky Wheel"
        txt_title = self._ocr_text_roi(screen,
                                        int(w * 0.15), int(h * 0.14),
                                        int(w * 0.85), int(h * 0.26),
                                        psm=6, scale=2)
        if 'lucky' in txt_title and 'wheel' in txt_title:
            return True

        # Fallback: OCR vùng dưới (78-92% H) — tìm "chance(s) left"
        txt_bot = self._ocr_text_roi(screen,
                                      int(w * 0.05), int(h * 0.78),
                                      int(w * 0.95), int(h * 0.92),
                                      psm=6, scale=2)
        if 'chance' in txt_bot and 'left' in txt_bot:
            return True

        return False

    def detect_popup_type(self, screen) -> str:
        """
        Phan loai 5 popup quan trong:
        - captcha
        - territory_map
        - army_selection
        - all_armies
        - lucky_wheel
        Con lai neu co popup => 'other', khong co popup => ''.
        """
        if screen is None:
            return ""
        try:
            if self.has_captcha_popup(screen):
                return 'captcha'
        except Exception:
            pass
        try:
            if self._is_territory_map_popup(screen):
                return 'territory_map'
        except Exception:
            pass
        try:
            if self._is_army_selection_popup(screen):
                return 'army_selection'
        except Exception:
            pass
        try:
            if self._is_all_armies_popup(screen):
                return 'all_armies'
        except Exception:
            pass
        try:
            if self._is_lucky_wheel_popup(screen):
                return 'lucky_wheel'
        except Exception:
            pass
        try:
            if self._has_generic_popup(screen):
                return 'other'
        except Exception:
            pass
        return ''


# ──────────────────────────────────────────────
