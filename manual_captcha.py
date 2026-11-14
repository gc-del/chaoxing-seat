# -*- coding: utf-8 -*-
"""人工点选验证码：弹窗显示验证码图，用户按顺序点击文字，脚本提交 check 换取 validate"""
import io

from captcha_click import CaptchaSolver, CaptchaError


def _popup_collect(image_bytes, words, scale=2):
    """弹窗显示图片并收集用户点击，返回原图坐标数组 [{"x":..,"y":..},..]"""
    import tkinter as tk
    from PIL import Image, ImageTk

    img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    disp = img.resize((img.width * scale, img.height * scale), Image.LANCZOS)

    root = tk.Tk()
    root.title("图书馆抢座 · 验证码")
    root.attributes("-topmost", True)
    root.resizable(False, False)
    try:
        import winsound
        for freq in (1200, 1600, 2000):
            winsound.Beep(freq, 150)
    except Exception:
        pass

    tk_img = ImageTk.PhotoImage(disp)
    canvas = tk.Canvas(root, width=disp.width, height=disp.height, cursor="crosshair")
    canvas.pack()
    canvas.create_image(0, 0, anchor="nw", image=tk_img)

    need = len(words)
    state = {"clicks": [], "done": False}
    tip_var = tk.StringVar()

    def refresh_tip():
        if state["done"]:
            return
        got = len(state["clicks"])
        tip_var.set(f"请按顺序点击: {'  '.join(words)}   （已点 {got}/{need}）")

    tip = tk.Label(root, textvariable=tip_var, font=("Microsoft YaHei", 13, "bold"), fg="#c0392b")
    tip.pack(pady=4)
    refresh_tip()

    def on_click(e):
        if state["done"] or len(state["clicks"]) >= need:
            return
        x, y = e.x / scale, e.y / scale
        state["clicks"].append({"x": int(round(x)), "y": int(round(y))})
        r = 6
        canvas.create_oval(e.x - r, e.y - r, e.x + r, e.y + r, outline="red", width=3)
        canvas.create_text(e.x, e.y - 14, text=str(len(state["clicks"])), fill="red",
                           font=("Arial", 12, "bold"))
        refresh_tip()
        if len(state["clicks"]) >= need:
            state["done"] = True
            tip_var.set("提交中...")
            root.after(250, root.destroy)

    canvas.bind("<Button-1>", on_click)

    def on_close():
        state["clicks"] = []
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_close)
    root.focus_force()
    root.mainloop()

    if len(state["clicks"]) < need:
        raise CaptchaError("用户取消了点选")
    return state["clicks"]


class ManualCaptchaSolver:
    """与 CaptchaSolver 同接口：solve(session, referer, max_rounds, save_dir) → validate"""

    def __init__(self, log=None):
        self._base = CaptchaSolver(log=log)
        self._log = log if log else (lambda m: None)

    def preload_models(self):
        pass  # 无模型需要加载

    def solve(self, session, referer, max_rounds=3, save_dir=None, allow_manual=True):
        if not allow_manual:
            raise CaptchaError("当前阶段不允许人工点选")
        import json
        import time
        for round_no in range(1, max_rounds + 1):
            q = self._base._get_question(session, referer)
            if q["type"] != "textclick":
                raise CaptchaError(f"验证码类型为 {q['type']}，仅支持文字点选")
            img = self._base._download(session, q["image_url"])
            stem = None
            if save_dir:
                stem = f"manual_round{round_no}_{int(time.time())}"
                (save_dir / f"{stem}.jpg").write_bytes(img)
            words = self._base._parse_words(q["context"])
            if not words:
                raise CaptchaError(f"未解析出点击顺序提示词（context={q['context']!r}）")
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
        raise CaptchaError(f"连续 {max_rounds} 轮人工点选未通过")
