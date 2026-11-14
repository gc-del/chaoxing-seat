# -*- coding: utf-8 -*-
"""AI 视觉求解器：大模型识别验证码坐标，失败同题转人工弹窗

流程：取题 → 下载图 → 放大 N 倍喂给多模态模型 → 解析坐标缩回原图 → check
校验不过或识别失败 → 同一张题弹窗人工点选（不浪费取题请求）。
"""
import io
import itertools
import json
import time

from PIL import Image

import ai_vision
from captcha_click import CaptchaError
from manual_captcha import ManualCaptchaSolver, _popup_collect


class AICaptchaSolver:
    """与 ManualCaptchaSolver 同接口：solve(session, referer, max_rounds, save_dir) → validate"""

    def __init__(self, cfg, log=None):
        self._cfg = cfg
        self._manual = ManualCaptchaSolver(log=log)
        self._base = self._manual._base
        self._log = log if log else (lambda m: None)
        self._scale = int(cfg.get("ai_image_scale") or 3)

    def preload_models(self):
        provider = (self._cfg.get("ai_vision_provider") or "deepseek").lower()
        providers = [p.strip() for p in
                     (self._cfg.get("ai_providers") or provider).split(",") if p.strip()]
        ready = [p for p in providers if ai_vision.provider_ready(self._cfg, p)]
        if ready:
            self._log(f"[AI] 视觉后端 {','.join(ready)} 已就绪")
        else:
            self._log(f"[AI] {','.join(providers)} 未配置 api_key，将全程人工点选")

    def _to_original(self, point, w, h):
        """把模型坐标（基于放大图尺寸）换算回原图，越界返回 None"""
        x = int(round(point["x"] / self._scale))
        y = int(round(point["y"] / self._scale))
        if not (0 <= x < w and 0 <= y < h):
            return None
        return x, y

    def _ask(self, img_bytes, words):
        """调模型识别，返回 (主候选坐标 or None, 模型原始文本, 候选组合列表)

        模型按 prompt 里声明的尺寸输出绝对像素坐标（已实测验证：声明 ×2 坐标 ×2），
        所以声明必须等于喂给模型的图片尺寸，再按放大倍数缩回原图。
        同一张图采样多次，合并各次结果里的同字实例（按出现次数加权），
        组合出多组候选坐标供在线逐一验证。
        """
        img = Image.open(io.BytesIO(img_bytes)).convert("RGB")
        w, h = img.size
        big = img.resize((w * self._scale, h * self._scale), Image.LANCZOS)
        buf = io.BytesIO()
        big.save(buf, format="JPEG", quality=95)
        prompt = ai_vision.build_textclick_prompt(words, big.width, big.height)

        samples = max(1, int(self._cfg.get("ai_samples") or 2))
        providers = [p.strip() for p in
                     (self._cfg.get("ai_providers")
                      or self._cfg.get("ai_vision_provider") or "deepseek").split(",")
                     if p.strip()]
        ready = [p for p in providers if ai_vision.provider_ready(self._cfg, p)]
        if ready:
            providers = ready
        else:
            self._log(f"[AI] 无可用后端（{providers} 均未配置 key）")
        pool = {word: [] for word in words}
        raw = ""
        primary = providers[0]
        backup = providers[1:]

        def collect(prov):
            """调用一次某后端，返回 (原始文本, 是否成功)"""
            try:
                return ai_vision.ask_vision(self._cfg, buf.getvalue(), prompt, prov), True
            except Exception as e:
                self._log(f"[AI] {prov} 调用失败: {str(e)[:80]}")
                return "", False

        def absorb(text):
            """把一次返回里的候选并入投票池"""
            for t in ai_vision.parse_targets(text):
                for word in words:
                    if t["char"] != word:
                        continue
                    xy = self._to_original(t["center"], w, h)
                    if xy is None:
                        continue
                    for item in pool[word]:
                        if abs(item[0][0] - xy[0]) <= 8 and abs(item[0][1] - xy[1]) <= 8:
                            item[1] += 1
                            break
                    else:
                        pool[word].append([xy, 1])

        primary_ok = 0
        for _ in range(samples):
            text, ok = collect(primary)
            if ok:
                primary_ok += 1
                raw = text
                absorb(text)
        if primary_ok == 0 and backup:
            # 主后端完全不可用时才启用备用（免费后端有速率限制，不能平摊调用量）
            self._log(f"[AI] 主后端 {primary} 全部失败，切换备用后端 {backup[0]}")
            for _ in range(samples):
                text, ok = collect(backup[0])
                if ok:
                    raw = text
                    absorb(text)

        if any(not pool[word] for word in words):
            return None, raw, []

        ranked = []
        for word in words:
            ranked.append(sorted(pool[word], key=lambda it: -it[1])[:3])
        combos = []
        for combo in itertools.product(*ranked):
            score = sum(it[1] for it in combo)
            coords = [{"x": it[0][0], "y": it[0][1]} for it in combo]
            combos.append((score, coords))
        combos.sort(key=lambda t: -t[0])
        return combos[0][1], raw, [c for _, c in combos]

    def solve(self, session, referer, max_rounds=4, save_dir=None, allow_manual=True):
        """AI 优先：每题只 check 一次（同一题第二次 check 必失败并作废题目），
        AI 失败就换新题重试；连续失败 ai_rounds 次后转人工点选（人工同样在新题上）。
        """
        ai_rounds = max(1, int(self._cfg.get("ai_max_rounds") or 2))
        ai_fail = 0
        use_ai = True
        checked = False
        for round_no in range(1, max_rounds + 1):
            q = self._base._get_question(session, referer)
            if q["type"] != "textclick":
                raise CaptchaError(f"验证码类型为 {q['type']}，仅支持文字点选")
            img = self._base._download(session, q["image_url"])
            words = self._base._parse_words(q["context"])
            if not words:
                raise CaptchaError(f"未解析出点击顺序提示词（context={q['context']!r}）")

            stem = None
            if save_dir:
                stem = f"ai_round{round_no}_{int(time.time())}"
                (save_dir / f"{stem}.jpg").write_bytes(img)

            if use_ai:
                try:
                    coords, raw, combos = self._ask(img, words)
                except Exception as e:
                    coords, raw, combos = None, f"ERR {e}", []
                if combos:
                    cand = combos[0]
                    validate, resp = self._base._check(session, q, cand)
                    checked = True
                    if save_dir and stem:
                        self._dump(save_dir, stem, round_no, q, cand, resp, True, validate, raw)
                    if validate:
                        self._log(f"[验证码] AI 命中 {words} → {cand} 校验通过")
                        return validate
                    self._log(f"[验证码] AI 坐标未通过({str(resp)[:80]})，换新题重试")
                else:
                    self._log(f"[验证码] AI 未能定位 {words}，换新题重试")
                    if save_dir and stem:
                        self._dump(save_dir, stem, round_no, q, None, None, True, None, raw)
                ai_fail += 1
                if ai_fail >= ai_rounds and allow_manual:
                    use_ai = False
                    self._log("[验证码] AI 连续失败，转人工点选")
                elif ai_fail >= ai_rounds:
                    self._log(f"[验证码] AI 连续失败{ai_fail}题，继续换题重试")
                continue

            self._log(f"[验证码] 第{round_no}题，请在弹窗中按顺序点击: {words}")
            try:
                coords = _popup_collect(img, words)
            except CaptchaError:
                raise
            except Exception as e:
                raise CaptchaError(f"弹窗点选失败: {e}")
            validate, resp = self._base._check(session, q, coords)
            if save_dir and stem:
                self._dump(save_dir, stem, round_no, q, coords, resp, False, validate, None)
            if validate:
                self._log("[验证码] 人工点选校验通过")
                return validate
            self._log(f"[验证码] 校验未通过({str(resp)[:120]})，弹新题重来")
        if not checked:
            raise CaptchaError("AI 后端不可用（全部请求均异常）")
        raise CaptchaError(f"连续 {max_rounds} 轮未通过（AI {ai_rounds} 题 + 人工）")

    def _dump(self, save_dir, stem, round_no, q, coords, resp, ai_used, validate, raw):
        (save_dir / f"{stem}.json").write_text(
            json.dumps(
                {"round": round_no, "question": q, "coords": coords,
                 "check_resp": resp, "ai_used": ai_used, "validate_ok": bool(validate),
                 "ai_raw": raw},
                ensure_ascii=False, indent=2),
            encoding="utf-8")
