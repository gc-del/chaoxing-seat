# -*- coding: utf-8 -*-
"""结果通知：钉钉 / 企业微信 / Server酱（无人值守时把结果推到你手机）

config 配置:
  notify_webhook: 钉钉机器人 https://oapi.dingtalk.com/robot/send?access_token=XXX
                  企业微信   https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=XXX
                  Server酱   https://sctapi.ftqq.com/XXX.send
                  留空则不通知
"""
import requests

TIMEOUT = (10, 20)


def notify(cfg, title, content=""):
    """发送通知，失败只记录不抛出（通知挂了不影响抢座）"""
    url = (cfg.get("notify_webhook") or "").strip()
    if not url:
        return False
    text = f"{title}\n{content}".strip()
    try:
        if "oapi.dingtalk.com" in url:
            payload = {"msgtype": "text", "text": {"content": text}}
            r = requests.post(url, json=payload, timeout=TIMEOUT)
        elif "qyapi.weixin.qq.com" in url:
            payload = {"msgtype": "text", "text": {"content": text}}
            r = requests.post(url, json=payload, timeout=TIMEOUT)
        else:
            r = requests.post(url, data={"title": title, "desp": content},
                              timeout=TIMEOUT)
        if r.status_code != 200:
            return False
        try:
            body = r.json()
        except ValueError:
            return True
        if isinstance(body, dict) and body.get("errcode"):
            return False
        return True
    except Exception:
        return False
