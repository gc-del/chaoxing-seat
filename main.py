# -*- coding: utf-8 -*-
"""超星图书馆定时抢座（全自动稳妥版）

模式:
  python main.py template       生成/重置 config.json
  python main.py manualcaptcha  人工弹窗点选测试（不涉及账号，推荐先跑这个验证闭环）
  python main.py captcha        自动识别离线测试（不涉及账号；对彩色艺术字识别率低）
  python main.py test           登录测试 + 座位页解析（登录一次，不触发预约）
  python main.py room           查询图书馆房间列表（需 dept_id_enc）
  python main.py dryrun         组装完整预约请求包（含验证码 validate，不发送）
  python main.py grab           定时抢座（凭 dry_run 开关决定是否真实提交）
  python main.py loop           常驻模式：每天自动抢座，启动一次即可（无人值守）
"""
import json
import random
import sys
import time
import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DEBUG_DIR = BASE_DIR / "debug_median"

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

sys.path.insert(0, str(BASE_DIR))

import requests

from captcha_click import CaptchaError
from chaoxing import ChaoxingClient, ChaoxingError, OFFICE
from notify import notify as push_note

try:
    from zoneinfo import ZoneInfo
    TZ = ZoneInfo("Asia/Shanghai")
except Exception:
    TZ = datetime.timezone(datetime.timedelta(hours=8))


def now_cn():
    """当前北京时间（服务器/云主机时区可能是 UTC，必须显式指定）"""
    return datetime.datetime.now(TZ)


def today_cn():
    return now_cn().date()

LOG_FILE = None


def log(msg):
    line = f"[{now_cn().strftime('%H:%M:%S')}] {msg}"
    print(line)
    if LOG_FILE:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")


def beep(ok=True):
    try:
        import winsound
        seq = [(1000, 150), (1400, 150), (1800, 400)] if ok else [(500, 300)]
        for freq, dur in seq:
            winsound.Beep(freq, dur)
    except Exception:
        pass


WEEK_IDX = {"monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
            "friday": 4, "saturday": 5, "sunday": 6}

CONFIG_TEMPLATE = {
    "dry_run": True,
    "username": "学习通账号（手机号/学号）",
    "password": "学习通密码",
    "room_id": "",
    "seat_num": "",
    "start_time": "08:00",
    "end_time": "22:00",
    "grab_time": "07:00:00",
    "reserve_next_day": True,
    "days_of_week": [],
    "dept_id_enc": "",
    "use_dept_id_enc": True,
    "max_submit_attempts": 3,
    "captcha_mode": "ai",
    "ai_vision_provider": "deepseek",
    "ai_providers": "deepseek",
    "ai_samples": 3,
    "ai_max_rounds": 2,
    "ai_pre_rounds": 8,
    "ai_thinking": "disabled",
    "deepseek_api_key": "",
    "deepseek_model": "deepseek-flash",
    "glm_api_key": "",
    "glm_model": "glm-4v-flash",
    "qwen_api_key": "",
    "qwen_model": "qwen3-vl-plus",
    "doubao_api_key": "",
    "doubao_model": "doubao-seed-1-6-vision",
    "baidu_api_key": "",
    "baidu_secret_key": "",
    "gemini_api_key": "",
    "gemini_model": "gemini-2.5-flash",
    "gemini_proxy": "",
    "ai_image_scale": 3,
    "captcha_test_rounds": 3,
    "captcha_max_rounds": 6,
    "pre_brush_fails": 3,
    "loop_start_hour": 6,
    "loop_backfill": True,
    "login_lead_minutes": 10,
    "pre_brush_lead_minutes": 9,
    "notify_webhook": "",
    "salt": "",
}


def load_config():
    path = BASE_DIR / "config.json"
    if not path.exists():
        write_config_template()
        print("尚未创建 config.json，已为你生成模板并退出。请填写后重试。")
        print(f"路径: {path}")
        sys.exit(1)
    return json.loads(path.read_text(encoding="utf-8"))


def write_config_template():
    path = BASE_DIR / "config.json"
    path.write_text(json.dumps(CONFIG_TEMPLATE, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def init_debug_dir():
    DEBUG_DIR.mkdir(exist_ok=True)
    global LOG_FILE
    LOG_FILE = DEBUG_DIR / f"log_{now_cn().strftime('%Y%m%d_%H%M%S')}.txt"


def wait_to(target_ts):
    """分段睡眠到指定时刻，避免系统时改/长睡阻塞 Ctrl+C"""
    while True:
        remain = target_ts - time.time()
        if remain <= 0:
            return
        time.sleep(min(remain, 30))


def write_done_flag(status, note=""):
    """写入今日完成标记（Actions 后续 run 读到即作废，避免重复消耗与重复提交）"""
    day = today_cn().isoformat()
    path = BASE_DIR / f"done_{day}.txt"
    try:
        path.write_text(f"{status} {note}".strip(), encoding="utf-8")
    except Exception:
        return None
    return path


def next_target_datetime(cfg):
    grab_time = cfg["grab_time"]
    days = [d.lower().strip() for d in (cfg.get("days_of_week") or [])]
    for d in days:
        if d not in WEEK_IDX:
            raise ValueError(f"days_of_week 配置无效: {d}（应使用 Monday~Sunday 英文全称）")
    now = now_cn()
    for i in range(0, 8):
        date = now.date() + datetime.timedelta(days=i)
        if days and WEEK_IDX[date.strftime("%A").lower()] not in [WEEK_IDX[d] for d in days]:
            continue
        target = datetime.datetime.combine(date, datetime.time.fromisoformat(grab_time),
                                          tzinfo=TZ)
        if target > now:
            return target
    raise ValueError(f"未来 8 天内找不到包含 {days} 的放座时刻")


def make_solver(cfg, _log):
    """按 config.captcha_mode 选择求解器：
    ai=多模态大模型(Gglm/Gemini)识别+人工兜底 / manual=人工弹窗点选 / auto=ddddocr 自动识别"""
    mode = cfg.get("captcha_mode", "manual")
    if mode == "ai":
        from ai_captcha import AICaptchaSolver
        return AICaptchaSolver(cfg, log=_log)
    if mode == "baidu":
        from baidu_ocr import BaiduHybridSolver
        return BaiduHybridSolver(cfg.get("baidu_api_key"), cfg.get("baidu_secret_key"), log=_log)
    if mode == "manual":
        from manual_captcha import ManualCaptchaSolver
        return ManualCaptchaSolver(log=_log)
    from captcha_click import CaptchaSolver
    return CaptchaSolver(log=_log)


def cmd_captcha(cfg, _log):
    from captcha_click import CaptchaSolver, new_session
    rounds = int(cfg.get("captcha_test_rounds") or 3)
    solver = CaptchaSolver(log=_log)
    session = new_session()
    referer = f"{OFFICE}/front/third/apps/seat/select"
    ok = 0
    for i in range(1, rounds + 1):
        _log(f"----- 验证码测试第 {i}/{rounds} 轮 -----")
        try:
            validate = solver.solve(session, referer, max_rounds=3, save_dir=DEBUG_DIR)
            ok += 1
            _log(f"第 {i} 轮 validate = {validate}")
        except Exception as e:
            _log(f"第 {i} 轮失败: {e}")
    _log(f"成功率 {ok}/{rounds}。验证码图片与识别过程已保存到 {DEBUG_DIR}")
    return 0 if ok else 1


def cmd_manualcaptcha(cfg, _log):
    from captcha_click import new_session
    rounds = int(cfg.get("captcha_test_rounds") or 1)
    solver = make_solver(cfg, _log)
    solver.preload_models()
    session = new_session()
    referer = f"{OFFICE}/front/third/apps/seat/select"
    ok = 0
    for i in range(1, rounds + 1):
        _log(f"----- 验证码测试第 {i}/{rounds} 轮 -----")
        try:
            validate = solver.solve(session, referer, max_rounds=3, save_dir=DEBUG_DIR)
            ok += 1
            _log(f"第 {i} 轮 validate = {validate}")
        except Exception as e:
            _log(f"第 {i} 轮失败: {e}")
    _log(f"成功率 {ok}/{rounds}（captcha_mode={cfg.get('captcha_mode', 'manual')}，弹窗中点错会自动弹新题重来）")
    return 0 if ok else 1


def cmd_test(cfg, _log):
    if not cfg.get("username") or not cfg.get("password"):
        _log("config 中未填写账号密码，无法测试登录")
        return 1
    client = ChaoxingClient(cfg["username"], cfg["password"])
    _log("发起登录测试（仅验证账号，不触发任何预约）...")
    client.login()
    _log("[OK] 登录成功")
    offset = client.server_time_offset()
    if offset is not None:
        _log(f"本机与服务器时间相差约 {offset} 秒（超过 ±3 秒建议校时，会影响放座瞬间抢抓）")
    room_id = str(cfg.get("room_id") or "")
    seat_num = str(cfg.get("seat_num") or "")
    if room_id and seat_num:
        page = client.fetch_seat_page(room_id, seat_num)
        (DEBUG_DIR / "seat_page_last.html").write_text(page["html"], encoding="utf-8")
        _log(f"座位页获取[OK]: {page['url']}")
        _log(f"enc 盐值(submit_enc): {page['enc_salt']}（None 即提取失败，页面已保存到 debug_median/seat_page_last.html）")
        if page["dept_enc"]:
            _log(f"页面中发现 deptIdEnc: {page['dept_enc']}")
    else:
        _log("config 未填 room_id/seat_num，跳过座位页测试")
    return 0


def cmd_room(cfg, _log):
    if not cfg.get("username") or not cfg.get("password"):
        _log("config 中未填写账号密码")
        return 1
    client = ChaoxingClient(cfg["username"], cfg["password"])
    client.login()
    _log("[OK] 登录成功")
    rooms = client.room_list(cfg.get("dept_id_enc") or "")
    if not rooms:
        _log("房间列表为空：很可能缺少/填错 dept_id_enc。房间号备用获取法：")
        _log("在图书馆列表页断网 → 点某图书馆的'选座' → 复制出的网址里 id= 即房间号")
        return 1
    for r in rooms:
        path = " > ".join([r.get("firstLevelName", ""), r.get("secondLevelName", ""), r.get("thirdLevelName", "")])
        _log(f"roomId={r.get('id')}  {path}")
    return 0


def prepare_submit_package(client, cfg, solver, _log, day=None):
    """组装完整提交包：取座位页(拿 enc 盐值) → 刷验证码 → enc 签名。返回 (info, page)"""
    room_id = str(cfg["room_id"])
    seat_num = str(cfg["seat_num"])
    if day is None:
        day = today_cn()
        if cfg.get("reserve_next_day", True):
            day += datetime.timedelta(days=1)
        day = day.isoformat()
    page = client.fetch_seat_page(room_id, seat_num)
    if not page["enc_salt"]:
        (DEBUG_DIR / "seat_page_fail.html").write_text(page["html"], encoding="utf-8")
        raise ChaoxingError("座位页未能提取 submit_enc 盐值（HTML 已保存到 debug_median/seat_page_fail.html）")
    # 验证码 referer 固定用 select 页（与人工测试成功路径一致；seat/code referer 曾致校验失败）
    # 验证码必须用独立匿名会话：登录会话的 cookie 泄漏到验证码域会被风控判定拒绝（实测）
    from captcha_click import new_session
    validate = solver.solve(new_session(),
                            referer=f"{OFFICE}/front/third/apps/seat/select",
                            max_rounds=int(cfg.get("captcha_max_rounds") or 6),
                            save_dir=DEBUG_DIR, allow_manual=False)
    info = client.build_submit(
        room_id=room_id, seat_num=seat_num, day=day,
        start=cfg["start_time"], end=cfg["end_time"],
        captcha=validate, enc_salt=page["enc_salt"],
    )
    return info, page


def cmd_dryrun(cfg, _log):
    if not cfg.get("room_id") or not cfg.get("seat_num"):
        _log("config 需先填 room_id 与 seat_num")
        return 1
    if not cfg.get("username") or not cfg.get("password"):
        _log("config 中未填写账号密码")
        return 1
    solver = make_solver(cfg, _log)
    solver.preload_models()
    client = ChaoxingClient(cfg["username"], cfg["password"])
    client.login()
    _log("[OK] 登录成功，开始组装预约请求包（不会发送）...")
    info, page = prepare_submit_package(client, cfg, solver, _log)
    qs = "&".join(f"{k}={v}" for k, v in info.items())
    _log("----- 请求包（dry_run 模式，不会发出）-----")
    _log(f"URL: {OFFICE}/data/apps/seat/submit")
    _log(f"参数: {qs}")
    _log(f"day={info['day']} 时段={info['startTime']}-{info['endTime']} 座位={info['seatNum']} 房间={info['roomId']}")
    _log("[OK] dryrun 完成：请求参数可信后，把 config.json 的 dry_run 改为 false 即可实抢")
    return 0


def cmd_once(cfg, _log):
    """立即执行一次完整真实提交（协议验证 / 即时抢座）。显式调用，不受 dry_run 限制"""
    import json as _json
    for key in ("username", "password", "room_id", "seat_num", "start_time", "end_time"):
        if not cfg.get(key):
            _log(f"config 缺少必要字段: {key}")
            return 1
    solver = make_solver(cfg, _log)
    solver.preload_models()
    client = ChaoxingClient(cfg["username"], cfg["password"])
    client.login()
    _log("[OK] 登录成功")
    day = today_cn()
    if cfg.get("reserve_next_day", True):
        day += datetime.timedelta(days=1)
    info, page = prepare_submit_package(client, cfg, solver, _log, day=day.isoformat())
    _log(f"发出真实提交: day={info['day']} 座位={info['seatNum']} 房间={info['roomId']} "
         f"时段={info['startTime']}-{info['endTime']}")
    resp = client.submit(info)
    _log(f"服务端响应: {_json.dumps(resp, ensure_ascii=False)[:400]}")
    if resp.get("success"):
        beep(True)
        _log("[OK][OK][OK] 预约成功！请到图书馆确认。")
        push_note(cfg, "预约成功",
                  f"房间 {cfg['room_id']} 座位 {cfg['seat_num']} "
                  f"{cfg['start_time']}-{cfg['end_time']} 日期 {info['day']}")
        write_done_flag("ok", f"seat={cfg['seat_num']} {info['day']}")
        return 0
    beep(False)
    push_note(cfg, "预约未成功", f"服务端响应: {str(resp)[:120]}")
    write_done_flag("fail", "手动补抢未成功")
    return 1


def cmd_grab(cfg, _log):
    for key in ("username", "password", "room_id", "seat_num", "start_time", "end_time", "grab_time"):
        if not cfg.get(key):
            _log(f"config 缺少必要字段: {key}")
            return 1
    dry_run = bool(cfg.get("dry_run", True))
    if dry_run:
        _log("!!DRY_RUN 演练模式：不会发出任何预约请求，仅演练流程!!")
    max_attempts = int(cfg.get("max_submit_attempts") or 3)

    # 补抢模式：定时触发延迟到放座时刻之后，但当天 12:00 之前仍可预约当天时段，
    # 此时立即执行一次抢座（不等待 07:00）
    now = now_cn()
    gh, gm = (int(x) for x in cfg["grab_time"].split(":")[:2])
    open_at = datetime.datetime.combine(now.date(), datetime.time(gh, gm), tzinfo=TZ)
    catchup_until = open_at + datetime.timedelta(hours=float(cfg.get("catchup_hours") or 4))
    if open_at <= now < catchup_until:
        target = now
        _log(f"补抢模式：现在 {now.strftime('%H:%M')}，"
             f"{catchup_until.strftime('%H:%M')} 前仍可约当天时段 → 立即执行")
    else:
        target = next_target_datetime(cfg)
        _log(f"目标放座时刻: {target.isoformat()}（本机时间）")
        # 距放座时刻过久则不空等（schedule 冗余触发时快速退出，省 Actions 额度）
        max_wait = int(cfg.get("max_wait_minutes") or 90) * 60
        if target.timestamp() - now_cn().timestamp() > max_wait:
            _log(f"距放座时刻还有 {(target.timestamp() - now_cn().timestamp()) / 60:.0f} 分钟，"
                 f"超过等待上限 {max_wait // 60} 分钟，本轮跳过")
            return 1

    solver = make_solver(cfg, _log)
    solver.preload_models()

    client = ChaoxingClient(cfg["username"], cfg["password"])
    login_lead = int(cfg.get("login_lead_minutes") or 10) * 60
    wait_to(target.timestamp() - login_lead)
    _log(f"开始提前登录（放座前 {login_lead // 60} 分钟）...")
    while True:
        if time.time() > target.timestamp() - 20:
            _log("登录窗口已贻误，放弃本轮")
            return 1
        try:
            client.login()
            _log("[OK] 提前登录成功")
            break
        except Exception as e:
            _log(f"登录失败: {e}，60 秒后重试")
            time.sleep(60)

    # 到点才解题：validate 只保 5 分钟，提前解出来到提交时已过期作废，
    # 还会白耗失败次数（风控累积），所以不做提前预刷。
    wait_to(target.timestamp() - 3)
    _log("到点，开始解题（最多 10 题）...")
    validate = None
    for attempt in range(1, 4):
        try:
            from captcha_click import new_session
            validate = solver.solve(new_session(),
                                    referer=f"{OFFICE}/front/third/apps/seat/select",
                                    max_rounds=int(cfg.get("captcha_max_rounds") or 6),
                                    save_dir=DEBUG_DIR, allow_manual=False)
            _log(f"[OK] 验证码通过: {str(validate)[:40]}...")
            break
        except Exception as e:
            if "后端不可用" in str(e) and attempt < 3:
                _log(f"AI 后端暂时不可用，20 秒后重试({attempt}/3): {str(e)[:60]}")
                time.sleep(20)
                continue
            _log(f"验证码失败({attempt}/3): {str(e)[:80]}")
            break
    day = today_cn()
    if cfg.get("reserve_next_day", True):
        day += datetime.timedelta(days=1)
    day = day.isoformat()
    page = None
    try:
        page = client.fetch_seat_page(cfg["room_id"], cfg["seat_num"])
        if not page["enc_salt"]:
            (DEBUG_DIR / "seat_page_fail.html").write_text(page["html"], encoding="utf-8")
            _log("!!座位页 submit_enc 盐值提取失败（页面已保存），本轮中止")
            beep(False)
            return 1
    except Exception as e:
        _log(f"!!座位页获取失败: {e}，本轮中止")
        beep(False)
        return 1

    info = client.build_submit(
        room_id=cfg["room_id"], seat_num=cfg["seat_num"], day=day,
        start=cfg["start_time"], end=cfg["end_time"],
        captcha=validate or "", enc_salt=page["enc_salt"],
    )
    wait_to(target.timestamp())
    if dry_run:
        _log("[演练] 到点命中。完整请求参数如下（未发送）:")
        _log("&".join(f"{k}={v}" for k, v in info.items()))
        beep(True)
        return 0

    for attempt in range(1, max_attempts + 1):
        resp = client.submit(info)
        if resp.get("success"):
            _log("[OK][OK][OK] 抢座成功! 座位信息见图书馆。")
            beep(True)
            push_note(cfg, "抢座成功",
                      f"房间 {cfg['room_id']} 座位 {cfg['seat_num']} "
                      f"{cfg['start_time']}-{cfg['end_time']} 日期 {info['day']}")
            write_done_flag("ok", f"seat={cfg['seat_num']} {info['day']}")
            return 0
        _log(f"第 {attempt} 次提交失败: {str(resp)[:200]}")
        if attempt >= max_attempts:
            break
        msg = str(resp)
        if "验证" in msg or "token" in msg.lower() or validate is None:
            _log("疑似验证码/凭证问题 → 重新实时取验证码与座位页盐值...")
            try:
                from captcha_click import new_session
                new_validate = solver.solve(new_session(),
                                            referer=f"{OFFICE}/front/third/apps/seat/select",
                                            max_rounds=3,
                                            save_dir=DEBUG_DIR, allow_manual=False)
                page = client.fetch_seat_page(cfg["room_id"], cfg["seat_num"])
                info = client.build_submit(
                    room_id=cfg["room_id"], seat_num=cfg["seat_num"], day=day,
                    start=cfg["start_time"], end=cfg["end_time"],
                    captcha=new_validate, enc_salt=page["enc_salt"],
                )
            except Exception as e:
                _log(f"实时重取失败: {e}")
                break
        if "时间" in msg:
            time.sleep(random.uniform(0.5, 1.0))
        else:
            time.sleep(random.uniform(2.0, 4.0))
    _log("本轮抢座结束(未成功)。日志已存 debug_median。")
    beep(False)
    push_note(cfg, "抢座未成功", f"房间 {cfg['room_id']} 座位 {cfg['seat_num']}，详见日志")
    write_done_flag("fail", "今日已放弃（达到重试上限）")
    return 1


def cmd_loop(cfg, _log):
    """常驻模式：每天到点自动抢座，用户只需启动一次，无需早起"""
    _log("[loop] 常驻模式启动：每天自动执行抢座（Ctrl+C 退出）")
    now = now_cn()
    grab_at = datetime.time.fromisoformat(cfg.get("grab_time", "07:00:00"))
    if now > datetime.datetime.combine(now.date(), grab_at, tzinfo=TZ) and cfg.get("loop_backfill", True):
        _log("[loop] 今日放座时刻已过，先立即补抢一次今天的座位")
        try:
            cmd_once(cfg, _log)
        except Exception as e:
            _log(f"[loop] 补抢异常: {e}")
    while True:
        try:
            cmd_grab(cfg, _log)
        except (CaptchaError, ChaoxingError) as e:
            _log(f"[loop] 本轮异常: {e}")
        except Exception as e:
            _log(f"[loop] 本轮未处理异常: {e}")
        now = now_cn()
        nxt = (now + datetime.timedelta(days=1)).replace(
            hour=int(cfg.get("loop_start_hour", 6)), minute=30, second=0, microsecond=0)
        if nxt <= now:
            nxt += datetime.timedelta(days=1)
        _log(f"[loop] 下一轮将于 {nxt} 开始")
        wait_to(nxt.timestamp())


def main():
    init_debug_dir()
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        sys.exit(0)
    mode = args[0].lower()
    if mode == "template":
        write_config_template()
        print(f"config.json 已生成/重置: {BASE_DIR / 'config.json'}")
        return
    cfg = load_config()
    dispatch = {
        "manualcaptcha": cmd_manualcaptcha,
        "captcha": cmd_captcha,
        "test": cmd_test,
        "room": cmd_room,
        "dryrun": cmd_dryrun,
        "once": cmd_once,
        "grab": cmd_grab,
        "loop": cmd_loop,
    }
    if mode not in dispatch:
        print(f"未知模式: {mode}，可用模式: template/manualcaptcha/captcha/test/room/dryrun/once/grab/loop")
        sys.exit(1)
    try:
        code = dispatch[mode](cfg, log)
    except (CaptchaError, ChaoxingError) as e:
        log(f"[错误] {e}")
        code = 1
    except KeyboardInterrupt:
        log("用户中断")
        code = 130
    sys.exit(code or 0)


if __name__ == "__main__":
    main()
