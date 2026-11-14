# -*- coding: utf-8 -*-
"""超星文字点选验证码：取题 → 识别 → check 校验 → validate

该流程不依赖登录态（超星验证码服务为匿名接口），因此可离线单独测试。
一个取题 token 只能校验一次，失败必须换新题重试。
"""
import io
import json
import re
import time
import uuid

import requests

from utils import md5_hex

try:
    import ddddocr
    from PIL import Image
    _OCR_READY = True
except ImportError:
    ddddocr = None
    Image = None
    _OCR_READY = False

BASE = "https://captcha.chaoxing.com"
CAPTCHA_ID = "42sxgHoTPTKbt0uZxPJ7ssOvtXr3ZgZ1"
CALLBACK = "cx_captcha_function"
VERSION = "1.1.20"


class CaptchaError(Exception):
    pass


def _now_ms() -> int:
    return int(time.time() * 1000)


def _strip_jsonp(text: str) -> dict:
    m = re.search(r"\((.+)\)\s*;?\s*$", text, re.S)
    if not m:
        raise CaptchaError(f"非法 JSONP 响应: {text[:150]}")
    return json.loads(m.group(1))


def new_session():
    """独立验证码会话：与主账号会话保持相同 UA，但不含 Cookie"""
    s = requests.Session()
    s.headers.update({
        "User-Agent": ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_6 like Mac OS X) "
                       "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.6 Mobile/15E148 Safari/604.1"),
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Referer": "https://office.chaoxing.com/front/third/apps/seat/select",
    })
    return s


class CaptchaSolver:
    def __init__(self, log=None):
        self._det = None
        self._cls = None
        self._log = log if log else (lambda m: None)

    def preload_models(self):
        """提前加载 ddddocr 两个模型，抢座流程启动时应先调用，避免关键时刻等待"""
        if not _OCR_READY:
            raise CaptchaError("ddddocr / pillow 未安装，请运行: pip install -r requirements.txt")
        if self._det is None:
            self._det = ddddocr.DdddOcr(det=True, ocr=False, show_ad=False)
            self._cls = ddddocr.DdddOcr(show_ad=False)

    def _conf(self, session):
        ts = _now_ms()
        r = session.get(
            f"{BASE}/captcha/get/conf",
            params={"callback": CALLBACK, "captchaId": CAPTCHA_ID, "_": ts},
            timeout=(10, 15),
        )
        return int(_strip_jsonp(r.text).get("t", ts))

    def _get_question(self, session, referer):
        server_t = self._conf(session)
        local_t = _now_ms()
        captcha_key = md5_hex(f"{server_t}{uuid.uuid4()}")
        token = f"{md5_hex(f'{server_t}{CAPTCHA_ID}textclick{captcha_key}')}:{server_t + 300000}"
        iv = md5_hex(f"{CAPTCHA_ID}textclick{local_t}{uuid.uuid4()}")
        params = {
            "callback": CALLBACK,
            "captchaId": CAPTCHA_ID,
            "type": "textclick",
            "version": VERSION,
            "captchaKey": captcha_key,
            "token": token,
            "referer": referer,
            "iv": iv,
            "_": local_t,
        }
        r = session.get(f"{BASE}/captcha/get/verification/image", params=params, timeout=(10, 20))
        data = _strip_jsonp(r.text)
        vo = data.get("imageVerificationVo") or {}
        if not vo.get("originImage"):
            raise CaptchaError(f"取题被拒（签名参数可能不匹配）: {str(data)[:200]}")
        return {
            "token": data.get("token"),
            "type": vo.get("type", "textclick"),
            "image_url": vo.get("originImage"),
            "context": vo.get("context", ""),
            "iv": iv,
        }

    def _download(self, session, url):
        if url.startswith("//"):
            url = "https:" + url
        r = session.get(
            url,
            headers={"Referer": "https://office.chaoxing.com/front/third/apps/seat/select"},
            timeout=(10, 20),
        )
        r.raise_for_status()
        return r.content

    def _recognize(self, image_bytes):
        self.preload_models()
        boxes = self._det.detection(image_bytes)
        pil = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        chars = []
        for box in boxes:
            x1, y1, x2, y2 = [int(v) for v in box]
            crop = pil.crop((x1, y1, x2, y2))
            buf = io.BytesIO()
            crop.save(buf, format="PNG")
            text = str(self._cls.classification(buf.getvalue())).strip()
            chars.append({"text": text, "x": (x1 + x2) // 2, "y": (y1 + y2) // 2})
        return chars

    @staticmethod
    def _parse_words(context):
        return [w.strip() for w in re.findall(r'"([^"]+)"', context) if w.strip()]

    @staticmethod
    def _match(words, chars):
        coords, missing = [], []
        used = set()
        for w in words:
            hit = None
            for i, c in enumerate(chars):
                if i in used:
                    continue
                if c["text"].replace(" ", "") == w:
                    hit = i
                    break
            if hit is None:
                for i, c in enumerate(chars):
                    if i in used:
                        continue
                    t = c["text"].replace(" ", "")
                    if t and (w in t or t in w):
                        hit = i
                        break
            if hit is None:
                missing.append(w)
            else:
                used.add(hit)
                coords.append({"x": chars[hit]["x"], "y": chars[hit]["y"]})
        return coords, missing

    def _check(self, session, q, coords):
        params = {
            "callback": CALLBACK,
            "captchaId": CAPTCHA_ID,
            "type": q["type"],
            "token": q["token"],
            "textClickArr": json.dumps(coords, ensure_ascii=False, separators=(",", ":")),
            "coordinate": "[]",
            "runEnv": "10",
            "version": VERSION,
            "t": "a",
            "iv": q["iv"],
            "_": _now_ms(),
        }
        r = session.get(f"{BASE}/captcha/check/verification/result", params=params, timeout=(10, 15))
        data = _strip_jsonp(r.text)
        if data.get("error") == 0 and data.get("result") is True and data.get("extraData"):
            extra = json.loads(data["extraData"]) if isinstance(data["extraData"], str) else data["extraData"]
            return extra.get("validate"), data
        return None, data

    def solve(self, session, referer, max_rounds=3, save_dir=None, allow_manual=True):
        """完整流程：取题→识别→check。失败自动换题（token 一次性）。成功返回 validate 字符串"""
        for round_no in range(1, max_rounds + 1):
            q = self._get_question(session, referer)
            if q["type"] != "textclick":
                raise CaptchaError(f"验证码类型为 {q['type']}，本脚本仅支持文字点选")
            img = self._download(session, q["image_url"])
            stem = None
            if save_dir:
                stem = f"cap_round{round_no}_{int(time.time())}"
                (save_dir / f"{stem}.jpg").write_bytes(img)
                stem = f"{stem}"
            words = self._parse_words(q["context"])
            if not words:
                raise CaptchaError(f"未解析出点击顺序提示词（原始 context={q['context']!r}）")
            try:
                chars = self._recognize(img)
            except Exception as e:
                self._log(f"[验证码] 第{round_no}题识别异常: {e}，换题重试")
                continue
            coords, missing = self._match(words, chars)
            debug_meta = {"round": round_no, "question": {k: v for k, v in q.items()},
                          "words": words, "chars": chars, "coords": coords}
            self._log(f"[验证码] 第{round_no}题 目标={words} 识别={[(c['text'], c['x'], c['y']) for c in chars]}")
            if missing:
                self._log(f"[验证码] 目标字 {missing} 未识别到，换题重试")
                if save_dir and stem:
                    (save_dir / f"{stem}_miss.json").write_text(
                        json.dumps(debug_meta, ensure_ascii=False, indent=2), encoding="utf-8")
                continue
            validate, resp = self._check(session, q, coords)
            debug_meta["check_resp"] = resp
            if save_dir and stem:
                suffix = "ok" if validate else "fail"
                (save_dir / f"{stem}_{suffix}.json").write_text(
                    json.dumps(debug_meta, ensure_ascii=False, indent=2), encoding="utf-8")
            if validate:
                self._log(f"[验证码] 第{round_no}题校验通过")
                return validate
            self._log(f"[验证码] 第{round_no}题校验未通过({str(resp)[:120]})，换题重试")
        raise CaptchaError(f"连续 {max_rounds} 轮未通过点选验证码，详见 debug_median 目录")
