# -*- coding: utf-8 -*-
"""环境诊断：逐项检查登录、房间列表接口、座位页，用于定位 GitHub Actions 等异地环境的差异

用法: python diagnose.py
输出不含密码，仅含状态与特征串是否存在。
"""
import json
import os
from pathlib import Path

from chaoxing import (OFFICE, ROOM_LIST_URL, SEAT_CODE_URL, ChaoxingClient,
                      ChaoxingError)

BASE = Path(__file__).resolve().parent


def main():
    cfg = json.loads((BASE / "config.json").read_text(encoding="utf-8"))
    print("=== 基础信息 ===")
    try:
        import socket
        print("本机出口 IP:", socket.gethostbyname(socket.gethostname()))
    except Exception as e:
        print("IP 探测失败:", e)
    print("目标:", cfg["room_id"], cfg["seat_num"], cfg["start_time"], cfg["end_time"])

    client = ChaoxingClient(cfg["username"], cfg["password"])
    try:
        r = client.login()
        print("=== 登录 ===")
        print("status:", r.get("status"), "| cookies:", sorted(client.session.cookies.keys()))
    except ChaoxingError as e:
        print("登录失败:", e)
        return

    def probe(name, url):
        try:
            resp = client.session.get(url, timeout=(10, 25))
            text = resp.text
            flags = {k: (k in text) for k in ("fidEnc", "submit_enc", "seatConfig", "deptId")}
            print(f"--- {name}")
            print(f"    http={resp.status_code} 最终url={resp.url} 长度={len(text)}")
            print(f"    特征: {flags}")
            return text
        except Exception as e:
            print(f"--- {name}\n    请求异常: {e}")
            return ""

    probe("房间列表接口", ROOM_LIST_URL)
    probe("座位 code 页", f"{SEAT_CODE_URL}?id={cfg['room_id']}&seatNum={cfg['seat_num']}")
    probe("座位 select 页",
          f"{OFFICE}/front/third/apps/seat/select?roomId={cfg['room_id']}&seatNum={cfg['seat_num']}")

    print("=== 座位页提交尝试 ===")
    page = client.fetch_seat_page(cfg["room_id"], cfg["seat_num"])
    print("enc_salt:", page["enc_salt"] or "(未取到)")
    print("dept_enc:", page["dept_enc"] or "(未取到)")


if __name__ == "__main__":
    main()
