# -*- coding: utf-8 -*-
"""环境诊断：逐项检查登录、房间列表接口、座位页，用于定位异地环境（GitHub Actions 等）的差异

用法: python diagnose.py
结果同时打印到 stdout 并写入 debug_median/diagnose.json（Actions 会作为 artifact 上传）
输出不含密码，仅含状态与特征串是否存在。
"""
import datetime
import json
import os
import sys
import traceback
from pathlib import Path

from chaoxing import (OFFICE, ROOM_LIST_URL, SEAT_CODE_URL, ChaoxingClient,
                      ChaoxingError)

BASE = Path(__file__).resolve().parent
OUT_DIR = BASE / "debug_median"


def say(msg):
    print(f"[{datetime.datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    result = {"started": datetime.datetime.now().isoformat()}
    cfg = json.loads((BASE / "config.json").read_text(encoding="utf-8"))
    say(f"目标: room={cfg['room_id']} seat={cfg['seat_num']} "
        f"{cfg['start_time']}-{cfg['end_time']}")

    client = ChaoxingClient(cfg["username"], cfg["password"])
    try:
        r = client.login()
        result["login"] = {"status": r.get("status"),
                           "cookies": sorted(client.session.cookies.keys())}
        say(f"登录 status={r.get('status')} cookies={result['login']['cookies']}")
    except Exception as e:
        result["login"] = {"error": str(e)[:300]}
        say(f"登录失败: {str(e)[:200]}")
        dump(result)
        return

    def probe(name, url):
        entry = {"url": url}
        try:
            resp = client.session.get(url, timeout=(10, 15))
            text = resp.text
            entry.update({"http": resp.status_code, "final_url": resp.url,
                          "length": len(text),
                          "flags": {k: (k in text) for k in
                                    ("fidEnc", "submit_enc", "seatConfig", "deptId", "seatNum")}})
            say(f"--- {name}: http={resp.status_code} len={len(text)} {entry['flags']}")
        except Exception as e:
            entry["error"] = str(e)[:200]
            say(f"--- {name}: 请求异常 {str(e)[:160]}")
        result[name] = entry

    probe("room_list", ROOM_LIST_URL)
    probe("seat_code", f"{SEAT_CODE_URL}?id={cfg['room_id']}&seatNum={cfg['seat_num']}")
    probe("seat_select",
          f"{OFFICE}/front/third/apps/seat/select?roomId={cfg['room_id']}&seatNum={cfg['seat_num']}")

    try:
        page = client.fetch_seat_page(cfg["room_id"], cfg["seat_num"])
        result["enc_salt"] = page["enc_salt"] or ""
        result["dept_enc"] = page["dept_enc"] or ""
        say(f"enc_salt={result['enc_salt'] or '(未取到)'}")
    except Exception as e:
        result["enc_error"] = str(e)[:200]
        say(f"取座位页失败: {str(e)[:160]}")
    dump(result)


def dump(result):
    result["finished"] = datetime.datetime.now().isoformat()
    path = OUT_DIR / "diagnose.json"
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    say(f"诊断结果已写入 {path}")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        sys.exit(1)
