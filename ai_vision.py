# -*- coding: utf-8 -*-
"""多模态大模型识别后端：DeepSeek V4.1 Flash / GLM-4V-Flash / Gemini

把验证码图 + 提示词交给视觉大模型，让它返回目标文字的中心像素坐标。
对"变形艺术字+照片背景"的识别能力远超传统 OCR。
"""
import base64
import json
import re

import requests

DEEPSEEK_URL = "https://api.deepseek.com/chat/completions"
GLM_URL = "https://open.bigmodel.cn/api/paas/v4/chat/completions"
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
TIMEOUT = (10, 60)


def build_textclick_prompt(words, width, height):
    """构造文字点选识别提示词。坐标基于给定 (width, height) 的图片"""
    listed = "、".join(f'第{i + 1}个字"{w}"' for i, w in enumerate(words))
    return (
        f"这是一张验证码图片，尺寸 {width}x{height} 像素。图中散布着若干个彩色艺术字，"
        f"字体经过变形、旋转、描边处理，背景是真实照片。\n"
        f"请找出图中所有能辨认的艺术字（不要遗漏，也不要把照片里的纹理当成字），逐个列出。\n"
        f"我需要从中挑出这几个字（顺序：{listed}），所以务必把每个字的所有实例都列全。\n"
        "要求：\n"
        "1. 同一个字若在图中出现多次，请全部列出。\n"
        "2. 每项给出 char（这个字是什么）、bbox（字形包围盒）、center（字形几何中心）。\n"
        "3. 坐标基于上面给出的图片尺寸，原点在左上角，x 向右、y 向下。\n"
        '4. 严格只输出一个 JSON 对象，格式示例：'
        '{"targets":[{"char":"字","bbox":[120,40,150,70],"center":{"x":135,"y":55}}]}，不要任何解释或代码块标记。'
    )


_TARGET_CHAR_RE = re.compile(r'"char"\s*:\s*"([^"]*)"')
_TARGET_BBOX_RE = re.compile(
    r'"bbox"\s*:\s*\[\s*(-?[\d.]+)\s*,\s*(-?[\d.]+)\s*,\s*(-?[\d.]+)\s*,\s*(-?[\d.]+)\s*\]')
_TARGET_CENTER_RE = re.compile(
    r'"center"\s*:\s*\{\s*"x"\s*:\s*(-?[\d.]+)\s*,\s*"y"\s*:\s*(-?[\d.]+)\s*\}')


def parse_targets(text):
    """解析模型输出的字清单 → [{char, center:{x,y}}]，非法 JSON 时正则兜底"""
    if not text:
        return []
    obj = _extract_json(text)
    if isinstance(obj, dict):
        for key in ("targets", "items", "results", "chars", "data"):
            if isinstance(obj.get(key), list):
                obj = obj[key]
                break
        else:
            obj = None
    if isinstance(obj, list):
        out = []
        for it in obj:
            if not isinstance(it, dict):
                continue
            xy = _item_center(it)
            if xy is None:
                continue
            out.append({"char": str(it.get("char") or "").strip(),
                        "center": {"x": xy[0], "y": xy[1]}})
        if out:
            return out

    out = []
    for seg in re.split(r'"char"\s*:\s*"', text)[1:]:
        char = seg.split('"')[0].strip()
        bbox = _TARGET_BBOX_RE.search(seg[:400])
        center = _TARGET_CENTER_RE.search(seg[:400])
        if bbox:
            x = (float(bbox.group(1)) + float(bbox.group(3))) / 2
            y = (float(bbox.group(2)) + float(bbox.group(4))) / 2
        elif center:
            x, y = float(center.group(1)), float(center.group(2))
        else:
            continue
        out.append({"char": char, "center": {"x": int(round(x)), "y": int(round(y))}})
    return out


def call_openai_vision(base_url, api_key, model, image_bytes, prompt,
                       timeout=None, extra=None):
    """通用 OpenAI 兼容视觉调用（百炼/豆包/硅基流动等均可用）"""
    b64 = base64.b64encode(image_bytes).decode()
    payload = {
        "model": model,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url",
                 "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
            ],
        }],
    }
    if extra:
        payload.update(extra)
    r = requests.post(
        base_url,
        headers={"Authorization": f"Bearer {api_key}",
                 "Content-Type": "application/json"},
        json=payload,
        timeout=timeout or TIMEOUT,
    )
    r.raise_for_status()
    data = r.json()
    if "error" in data:
        raise RuntimeError(f"{model} 错误: {str(data['error'])[:200]}")
    return data["choices"][0]["message"]["content"]


def call_deepseek(api_key, image_bytes, prompt, model="deepseek-flash",
                  thinking="disabled", effort=None):
    """DeepSeek V4.1 Flash（原生多模态，国内直连），OpenAI 兼容格式

    thinking: off=关闭思考(最快) / low=低强度思考(更准) / high=完整思考
    """
    b64 = base64.b64encode(image_bytes).decode()
    payload = {
        "model": model,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url",
                 "image_url": {"url": f"data:image/jpeg;base64,{b64}",
                               "detail": "original"}},
            ],
        }],
        "response_format": {"type": "json_object"},
    }
    if thinking in ("on", "enabled", "low", "high", "max"):
        payload["thinking"] = {"type": "enabled"}
        payload["reasoning_effort"] = effort or thinking
    else:
        payload["thinking"] = {"type": "disabled"}
    r = requests.post(
        DEEPSEEK_URL,
        headers={"Authorization": f"Bearer {api_key}",
                 "Content-Type": "application/json"},
        json=payload,
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    data = r.json()
    if "error" in data:
        raise RuntimeError(f"DeepSeek 错误: {str(data['error'])[:200]}")
    return data["choices"][0]["message"]["content"]


def call_glm(api_key, image_bytes, prompt, model="glm-4v-flash"):
    """智谱 GLM 视觉系列（open.bigmodel.cn），key 形如 id.secret"""
    b64 = base64.b64encode(image_bytes).decode()
    r = requests.post(
        GLM_URL,
        headers={"Authorization": f"Bearer {api_key}",
                 "Content-Type": "application/json"},
        json={
            "model": model,
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "image_url",
                     "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                    {"type": "text", "text": prompt},
                ],
            }],
        },
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    data = r.json()
    if "error" in data:
        raise RuntimeError(f"GLM 错误: {str(data['error'])[:200]}")
    return data["choices"][0]["message"]["content"]


def call_gemini(api_key, image_bytes, prompt, model="gemini-2.5-flash", proxy=None):
    """Google Gemini（备用），需能访问 generativelanguage.googleapis.com"""
    b64 = base64.b64encode(image_bytes).decode()
    proxies = {"http": proxy, "https": proxy} if proxy else None
    r = requests.post(
        GEMINI_URL.format(model=model),
        params={"key": api_key},
        headers={"Content-Type": "application/json"},
        json={
            "contents": [{"parts": [
                {"text": prompt},
                {"inline_data": {"mime_type": "image/jpeg", "data": b64}},
            ]}],
            "generationConfig": {"temperature": 0.1},
        },
        timeout=TIMEOUT, proxies=proxies,
    )
    r.raise_for_status()
    data = r.json()
    if "error" in data:
        raise RuntimeError(f"Gemini 错误: {str(data['error'])[:200]}")
    parts = (data.get("candidates") or [{}])[0].get("content", {}).get("parts", [])
    return "".join(p.get("text", "") for p in parts)


def ask_vision(cfg, image_bytes, prompt, provider=None):
    """按 config.ai_vision_provider 选择后端，返回模型文本输出

    provider 为空时读 ai_vision_provider；指定时强制走该后端
    （多后端轮转采样时用）。
    """
    provider = (provider or cfg.get("ai_vision_provider") or "deepseek").lower()
    if provider == "deepseek":
        return call_deepseek(cfg.get("deepseek_api_key"), image_bytes, prompt,
                             cfg.get("deepseek_model") or "deepseek-flash",
                             cfg.get("ai_thinking") or "disabled",
                             cfg.get("ai_reasoning_effort") or None)
    if provider == "qwen":
        return call_openai_vision(
            "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
            cfg.get("qwen_api_key"), cfg.get("qwen_model") or "qwen3-vl-plus",
            image_bytes, prompt)
    if provider == "doubao":
        return call_openai_vision(
            "https://ark.cn-beijing.volces.com/api/v3/chat/completions",
            cfg.get("doubao_api_key"), cfg.get("doubao_model") or "doubao-seed-1-6-vision",
            image_bytes, prompt)
    if provider == "gemini":
        return call_gemini(cfg.get("gemini_api_key"), image_bytes, prompt,
                           cfg.get("gemini_model") or "gemini-2.5-flash",
                           cfg.get("gemini_proxy") or None)
    return call_glm(cfg.get("glm_api_key"), image_bytes, prompt)


def _extract_json(text):
    """从模型输出里抠出 JSON（容忍代码块包裹、夹杂前后说明）"""
    if not text:
        return None
    t = text.strip()
    if "```" in t:
        for seg in t.split("```"):
            seg = seg.replace("json", "").strip()
            if seg.startswith("{") or seg.startswith("["):
                t = seg
                break
    for op, cl in (("{", "}"), ("[", "]")):
        s, e = t.find(op), t.rfind(cl)
        if s >= 0 and e > s:
            try:
                return json.loads(t[s:e + 1])
            except ValueError:
                continue
    return None


def _item_center(item):
    """从一项识别结果里取中心点：优先 center，其次 bbox 几何中心，最后 x/y"""
    if not isinstance(item, dict):
        return None
    c = item.get("center")
    if isinstance(c, dict):
        x, y = c.get("x"), c.get("y")
    elif isinstance(c, (list, tuple)) and len(c) >= 2:
        x, y = c[0], c[1]
    else:
        x = y = None
    if not isinstance(x, (int, float)) or not isinstance(y, (int, float)):
        x, y = None, None
    if x is None:
        bbox = item.get("bbox") or item.get("box")
        if isinstance(bbox, dict):
            bbox = [bbox.get("xmin"), bbox.get("ymin"), bbox.get("xmax"), bbox.get("ymax")]
        if isinstance(bbox, (list, tuple)) and len(bbox) >= 4:
            try:
                x = (float(bbox[0]) + float(bbox[2])) / 2
                y = (float(bbox[1]) + float(bbox[3])) / 2
            except (TypeError, ValueError):
                return None
        else:
            x, y = item.get("x"), item.get("y")
            if not isinstance(x, (int, float)) or not isinstance(y, (int, float)):
                return None
    return int(round(x)), int(round(y))


def parse_coordinates(text, expect, width, height):
    """从模型输出解析坐标数组，全对返回 [{x,y},...]，否则 None"""
    obj = _extract_json(text)
    if obj is None:
        return None
    if isinstance(obj, dict):
        for key in ("targets", "items", "results", "chars", "data"):
            if isinstance(obj.get(key), list):
                obj = obj[key]
                break
        else:
            return None
    if not isinstance(obj, list) or len(obj) != expect:
        return None
    out = []
    for item in obj:
        if isinstance(item, str):
            return None
        xy = _item_center(item)
        if xy is None:
            return None
        x, y = xy
        if not (0 <= x < width and 0 <= y < height):
            return None
        out.append({"x": x, "y": y})
    return out
