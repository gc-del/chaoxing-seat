# -*- coding: utf-8 -*-
"""学习通登录 / 座位页解析 / 房间列表 / 预约提交"""
import re
import time

import requests

from utils import aes_encrypt, enc_sign

PASSPORT = "https://passport2.chaoxing.com"
OFFICE = "https://office.chaoxing.com"
SEAT_CODE_URL = f"{OFFICE}/front/third/apps/seat/code"
SEAT_SUBMIT_URL = f"{OFFICE}/data/apps/seat/submit"
ROOM_LIST_URL = f"{OFFICE}/data/apps/seat/room/list"

UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_6 like Mac OS X) AppleWebKit/605.1.15 "
      "(KHTML, like Gecko) Version/17.6 Mobile/15E148 Safari/604.1")

TOKEN_PATTERNS = [
    r'id="submit_enc"[^>]*value="([^"]+)"',
    r'value="([^"]+)"[^>]*id="submit_enc"',
    r"token:\s*'([^']+)'",
    r'"token"\s*:\s*"([^"]+)"',
]
SALT_PATTERNS = [
    r'id="algorithm"[^>]*value="([^"]*)"',
    r'value="([^"]*)"[^>]*id="algorithm"',
    r'algorithm["\']?\s*[:=]\s*["\']([^"\']*)["\']',
]
DEPT_ENC_PATTERNS = [
    r'deptIdEnc["\']?\s*[:=]\s*["\']([^"\']{6,})["\']',
]


class ChaoxingError(Exception):
    pass


def _first_match(html, patterns):
    for p in patterns:
        m = re.search(p, html)
        if m:
            return m.group(1)
    return None


class ChaoxingClient:
    def __init__(self, username, password, refer=None):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": UA,
            "Accept-Language": "zh-CN,zh;q=0.9",
            "X-Requested-With": "XMLHttpRequest",
        })
        self.username = username
        self.password = password
        self.refer = refer or f"{OFFICE}/front/third/apps/seat/select"

    def login(self):
        self.session.get(f"{PASSPORT}/mlogin?loginType=1&newversion=true&fid=", timeout=(10, 20))
        params = {
            "fid": "-1",
            "uname": aes_encrypt(self.username),
            "password": aes_encrypt(self.password),
            "refer": requests.utils.quote(self.refer, safe=""),
            "t": "True",
        }
        r = self.session.post(f"{PASSPORT}/fanyalogin", params=params, timeout=(10, 20))
        try:
            data = r.json()
        except ValueError:
            raise ChaoxingError(f"登录响应非 JSON（HTTP {r.status_code}）: {r.text[:200]}")
        if data.get("status") is True:
            return data
        msg = data.get("msg2") or data.get("msg") or data
        raise ChaoxingError(f"登录失败: {msg}")

    def fetch_seat_page(self, room_id, seat_num):
        """取座位页并提取 submit_enc 盐值。

        实测：偶发返回未渲染的模板页（无 submit_enc），预热房间列表后重试即可恢复，
        故此处做有限次自愈重试，保证无人值守时不被瞬时波动卡住。
        """
        url = f"{SEAT_CODE_URL}?id={room_id}&seatNum={seat_num}"
        html = ""
        enc_salt = None
        for attempt in range(1, 4):
            r = self.session.get(url, timeout=(10, 20))
            html = r.text
            if r.status_code != 200:
                raise ChaoxingError(f"座位页 HTTP {r.status_code}")
            if "passport2.chaoxing.com" in html and "seat" not in html:
                raise ChaoxingError("座位页被重定向到登录页（登录态失效，重新登录）")
            enc_salt = _first_match(html, TOKEN_PATTERNS)
            if enc_salt:
                break
            if attempt < 3:
                time.sleep(1.5)
                try:
                    self.session.get(ROOM_LIST_URL, timeout=(10, 15))
                except Exception:
                    pass
        dept_enc = _first_match(html, DEPT_ENC_PATTERNS)
        return {"url": url, "html": html, "enc_salt": enc_salt, "dept_enc": dept_enc}

    def build_submit(self, *, room_id, seat_num, day, start, end, captcha, enc_salt, wy_token=""):
        """新版 submit 协议（来自本馆前端 code_myself_use_third.js 逆向）：
        参数集 {roomId,startTime,endTime,day,captcha,seatNum,wyToken}，
        enc = MD5(字典序 [k=v] 拼接 + [submit_enc 盐值])"""
        info = {
            "roomId": str(room_id),
            "startTime": start,
            "endTime": end,
            "day": day,
            "captcha": captcha,
            "seatNum": str(seat_num).zfill(3),
            "wyToken": wy_token,
        }
        info["enc"] = enc_sign(info, enc_salt)
        return info

    def submit(self, info):
        r = self.session.post(SEAT_SUBMIT_URL, data=info, timeout=(10, 20))
        try:
            return r.json()
        except ValueError:
            return {"success": False, "raw": r.text[:300]}

    def room_list(self, dept_id_enc):
        params = {
            "cpage": "1", "pageSize": "100",
            "firstLevelName": "", "secondLevelName": "", "thirdLevelName": "",
            "deptIdEnc": dept_id_enc or "",
        }
        r = self.session.get(ROOM_LIST_URL, params=params, timeout=(10, 20))
        try:
            data = r.json()
        except ValueError:
            raise ChaoxingError(f"房间列表响应非 JSON: {r.text[:200]}")
        return (data.get("data") or {}).get("seatRoomList") or []

    def server_time_offset(self):
        """用 HTTP Date 头估算本机与服务器时间差（秒），提醒用户校时"""
        r = self.session.request("HEAD", OFFICE + "/front/third/apps/seat/select", timeout=(10, 20))
        import email.utils as eu
        date_hdr = r.headers.get("Date")
        if not date_hdr:
            return None
        return round(eu.parsedate_to_datetime(date_hdr).timestamp() - __import__("time").time(), 1)
