# -*- coding: utf-8 -*-
"""百度云 OCR 混合验证码求解器：自动识别优先，同题失败立即弹人工点选兜底

本馆验证码为易盾文字点选（变形艺术字+照片背景），通用 OCR 识别率很低，
因此设计为"先自动后人工"：同一道题先交给百度 OCR，识别不全或校验失败
立即转人工弹窗，不浪费取题请求，也不占用放座瞬间。
"""
import base64
import json
import time
from pathlib import Path

import requests

from captcha_click import CaptchaError
from manual_captcha import ManualCaptchaSolver, _popup_collect

TOKEN_URL = "https://aip.baidubce.com/oauth/2.0/token"
OCR_URL = "https://aip.baidubce.com/rest/2.0/ocr/v1/accurate"
CACHE_PATH = Path(__file__).resolve().parent / "debug_median" / "baidu_token.json"
_TOKEN_CACHE = None


def _fetch_token(api_key, secret_key):
    """获取/缓存百度 access_token（有效期约 30 天，落盘复用）"""
    global _TOKEN_CACHE
    if _TOKEN_CACHE and _TOKEN_CACHE.get("expire_at", 0) > time.time() + 60:
        return _TOKEN_CACHE["access_token"]
    if CACHE_PATH.exists():
        try:
            data = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
            if data.get("expire_at", 0) > time.time() + 60:
                _TOKEN_CACHE = data
                return data["access_token"]
        except Exception:
            pass
    r = requests.post(TOKEN_URL, params={
        "grant_type": "client_credentials",
        "client_id": api_key,
        "client_secret": secret_key,
    }, timeout=(10, 20))
    r.raise_for_status()
    data = r.json()
    if "access_token" not in data:
        raise CaptchaError(f"百度 token 获取失败: {str(data)[:200]}")
    _TOKEN_CACHE = {
        "access_token": data["access_token"],
        "expire_at": time.time() + float(data.get("expires_in", 2592000)) - 3600,
    }
    CACHE_PATH.write_text(json.dumps(_TOKEN_CACHE), encoding="utf-8")
    return _TOKEN_CACHE["access_token"]


def ocr_locate(image_bytes, api_key, secret_key):
    """百度通用文字识别（高精度含位置），返回 [{text,left,top,width,height}, ...]"""
    token = _fetch_token(api_key, secret_key)
    r = requests.post(
        OCR_URL, params={"access_token": token},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data={"image": base64.b64encode(image_bytes).decode()},
        timeout=(10, 30))
    r.raise_for_status()
    data = r.json()
    if data.get("error_code") is not None:
        raise CaptchaError(f"百度 OCR 错误 {data.get('error_code')}: {data.get('error_msg')}")
    out = []
    for item in data.get("words_result", []):
        loc = item.get("location") or {}
        out.append({
            "text": item.get("words", ""),
            "left": int(loc.get("left", 0)),
            "top": int(loc.get("top", 0)),
            "width": int(loc.get("width", 0)),
            "height": int(loc.get("height", 0)),
        })
    return out


def match_clicks(ocr_items, words):
    """按提示词顺序从 OCR 结果挑出点击坐标（原图坐标系）；找不全返回 None"""
    used = set()
    clicks = []
    for w in words:
        hit = None
        for idx, it in enumerate(ocr_items):
            if idx in used:
                continue
            pos = it["text"].find(w)
            if pos < 0:
                continue
            if len(it["text"]) == 1:
                cx = it["left"] + it["width"] // 2
            else:
                cx = it["left"] + int((pos + 0.5) * it["width"] / len(it["text"]))
            cy = it["top"] + it["height"] // 2
            hit = {"x": cx, "y": cy}
            used.add(idx)
            break
        if hit is None:
            return None
        clicks.append(hit)
    return clicks


class BaiduHybridSolver:
    """与 ManualCaptchaSolver 同接口：solve(session, referer, max_rounds, save_dir) → validate"""

    def __init__(self, api_key, secret_key, log=None):
        self._api_key = api_key or ""
        self._secret_key = secret_key or ""
        self._manual = ManualCaptchaSolver(log=log)
        self._base = self._manual._base
        self._log = log if log else (lambda m: None)

    def preload_models(self):
        if not (self._api_key and self._secret_key):
            self._log("[百度OCR] 未配置 baidu_api_key/baidu_secret_key，将全程人工点选")
            return
        try:
            _fetch_token(self._api_key, self._secret_key)
            self._log("[百度OCR] access_token 就绪")
        except Exception as e:
            self._log(f"[百度OCR] token 获取失败（将全程人工点选）: {e}")

    def _try_baidu(self, session, q, img, words):
        """对当前题尝试百度 OCR；命中且校验通过返回 validate，否则返回 None"""
        if not (self._api_key and self._secret_key):
            return None
        try:
            items = ocr_locate(img, self._api_key, self._secret_key)
            coords = match_clicks(items, words)
            if not coords:
                self._log(f"[验证码] 百度OCR未在图中找全 {words}，同题转人工")
                return None
            self._log(f"[验证码] 百度OCR命中 {words}，坐标 {coords}")
            validate, resp = self._base._check(session, q, coords)
            if validate:
                self._log("[验证码] 百度OCR校验通过（全自动）")
                return validate
            self._log(f"[验证码] OCR坐标校验未通过({str(resp)[:100]})，同题转人工")
        except CaptchaError as e:
            self._log(f"[验证码] 百度OCR调用失败（{e}），同题转人工")
        except Exception as e:
            self._log(f"[验证码] 百度OCR异常（{e}），同题转人工")
        return None

    def solve(self, session, referer, max_rounds=3, save_dir=None, allow_manual=True):
        for round_no in range(1, max_rounds + 1):
            q = self._base._get_question(session, referer)
            if q["type"] != "textclick":
                raise CaptchaError(f"验证码类型为 {q['type']}，仅支持文字点选")
            img = self._base._download(session, q["image_url"])
            words = self._base._parse_words(q["context"])
            if not words:
                raise CaptchaError(f"未解析出点击顺序提示词（context={q['context']!r}）")

            validate = self._try_baidu(session, q, img, words)
            if validate:
                return validate

            stem = None
            if save_dir:
                stem = f"manual_round{round_no}_{int(time.time())}"
                (save_dir / f"{stem}.jpg").write_bytes(img)
            self._log(f"[验证码] 第{round_no}题，请在弹窗中按顺序点击: {words}")
            try:
                coords = _popup_collect(img, words)
            except CaptchaError:
                raise
            except Exception as e:
                raise CaptchaError(f"弹窗点选失败: {e}")
            validate, resp = self._base._check(session, q, coords)
            if save_dir and stem:
                suffix = "ok" if validate else "fail"
                (save_dir / f"{stem}_{suffix}.json").write_text(
                    json.dumps(
                        {"round": round_no, "question": q, "coords": coords, "check_resp": resp},
                        ensure_ascii=False, indent=2),
                    encoding="utf-8")
            if validate:
                self._log("[验证码] 人工点选校验通过")
                return validate
            self._log(f"[验证码] 校验未通过({str(resp)[:120]})，弹新题重来")
        raise CaptchaError(f"连续 {max_rounds} 轮未通过（含百度OCR尝试）")
