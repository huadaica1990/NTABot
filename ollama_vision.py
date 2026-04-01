"""
Ollama Vision - AI-based screen analysis via local Ollama.
"""
import json

from config import HAS_CV2


class OllamaVision:
    def __init__(self, url="http://localhost:11434", model="qwen2-vl:7b"):
        self.url   = url.rstrip("/")
        self.model = model

    def is_occupying(self, image_bytes: bytes):
        """True=dang chiem, False=het, None=loi"""
        import base64, urllib.request
        # Crop vung trung tam man hinh truoc khi gui AI
        try:
            import cv2, numpy as np
            arr = np.frombuffer(image_bytes, np.uint8)
            img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
            h, w = img.shape[:2]
            # Crop 30%-70% chieu cao, 25%-75% chieu ngang
            y1, y2 = int(h * 0.30), int(h * 0.70)
            x1, x2 = int(w * 0.25), int(w * 0.75)
            crop = img[y1:y2, x1:x2]
            # Scale up 3x de AI nhin ro hon
            crop = cv2.resize(crop, (crop.shape[1]*3, crop.shape[0]*3),
                              interpolation=cv2.INTER_NEAREST)
            _, buf = cv2.imencode(".png", crop)
            image_bytes = buf.tobytes()
        except Exception:
            pass  # fallback: dung anh goc
        b64 = base64.b64encode(image_bytes).decode()
        prompt = (
            "Look at this image. "
            "Do you see a small pixel art icon of a sword or a shield (or both) anywhere in the image? "
            "It may appear as just a sword or just a shield since it is animated. "
            "Answer only YES or NO."
        )
        payload = json.dumps({
            "model": self.model, "prompt": prompt,
            "images": [b64], "stream": False,
        }).encode()
        req = urllib.request.Request(
            f"{self.url}/api/generate", data=payload,
            headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                ans = json.loads(r.read()).get("response","").strip()
                self._last_response = ans
                u = ans.upper()
                if "YES" in u: return True
                if "NO"  in u: return False
                return None
        except Exception as e:
            self._last_response = str(e)
            return None

