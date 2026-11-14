# -*- coding: utf-8 -*-
"""从环境变量（GitHub Actions Secrets）渲染 config.json

用法:
  python render_config.py                 # 环境变量缺失即报错
  python render_config.py --check         # 只校验必填项是否存在，不写文件
"""
import json
import os
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent


def env(name, default=None, required=False):
    v = os.environ.get(name)
    if v is None or v == "":
        if required:
            raise SystemExit(f"缺少必需的环境变量: {name}")
        return default
    return v


def env_int(name, default):
    v = env(name)
    return int(v) if v else default


def env_bool(name, default=False):
    v = (env(name) or "").strip().lower()
    if not v:
        return default
    return v in ("1", "true", "yes", "on")


def build_config():
    return {
        "dry_run": env_bool("CX_DRY_RUN", False),
        "username": env("CX_USER", required=True),
        "password": env("CX_PASS", required=True),
        "room_id": env("CX_ROOM_ID", "6657"),
        "seat_num": env("CX_SEAT_NUM", "192"),
        "start_time": env("CX_START", "12:00"),
        "end_time": env("CX_END", "22:00"),
        "grab_time": env("CX_GRAB_TIME", "07:00:00"),
        "reserve_next_day": env_bool("CX_RESERVE_NEXT_DAY", False),
        "days_of_week": [d.strip() for d in (env("CX_DAYS", "") or "").split(",") if d.strip()],
        "max_submit_attempts": env_int("CX_MAX_ATTEMPTS", 3),
        "captcha_mode": env("CX_CAPTCHA_MODE", "ai"),
        "ai_vision_provider": env("AI_PROVIDER", "deepseek"),
        "ai_providers": env("AI_PROVIDERS", "deepseek"),
        "ai_samples": env_int("AI_SAMPLES", 3),
        "ai_image_scale": env_int("AI_IMAGE_SCALE", 3),
        "ai_thinking": env("AI_THINKING", "disabled"),
        "ai_max_rounds": env_int("AI_MAX_ROUNDS", 2),
        "ai_pre_rounds": env_int("AI_PRE_ROUNDS", 8),
        "captcha_max_rounds": env_int("CAPTCHA_MAX_ROUNDS", 4),
        "pre_brush_fails": env_int("PRE_BRUSH_FAILS", 3),
        "login_lead_minutes": env_int("LOGIN_LEAD_MINUTES", 10),
        "pre_brush_lead_minutes": env_int("PRE_BRUSH_LEAD_MINUTES", 9),
        "catchup_hours": float(env("CATCHUP_HOURS", 4)),
        "max_wait_minutes": env_int("MAX_WAIT_MINUTES", 25),
        "loop_start_hour": 6,
        "loop_backfill": False,
        "deepseek_api_key": env("DEEPSEEK_KEY", ""),
        "deepseek_model": env("DEEPSEEK_MODEL", "deepseek-flash"),
        "glm_api_key": env("GLM_KEY", ""),
        "glm_model": env("GLM_MODEL", "glm-4v-flash"),
        "qwen_api_key": env("QWEN_KEY", ""),
        "doubao_api_key": env("DOUBAO_KEY", ""),
        "notify_webhook": env("NOTIFY_WEBHOOK", ""),
        "salt": env("CX_SALT", ""),
    }


def main():
    cfg = build_config()
    if "--check" in sys.argv:
        print(f"必填项齐全: user={cfg['username']} room={cfg['room_id']} "
              f"seat={cfg['seat_num']} mode={cfg['captcha_mode']} "
              f"provider={cfg['ai_providers']} "
              f"key={'有' if cfg['deepseek_api_key'] else '无'}")
        return
    out = BASE / "config.json"
    if "--out" in sys.argv:
        idx = sys.argv.index("--out")
        if idx + 1 < len(sys.argv):
            out = Path(sys.argv[idx + 1])
    out.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"config.json 已生成（密码与 key 不回显）: {out}")


if __name__ == "__main__":
    main()
