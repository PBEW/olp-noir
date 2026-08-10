"""คำนวณรอบตัดยอด (ค่าเริ่มต้น: ทุกวันเสาร์ 08:00 น. เวลาไทย)"""
from __future__ import annotations

import datetime as dt

from .config import Config


def cycle_start_local(now_local: dt.datetime, cfg: Config) -> dt.datetime:
    """เวลาเริ่มต้นของรอบปัจจุบัน (จุดตัดยอดล่าสุดที่ผ่านมาแล้ว)"""
    candidate = now_local.replace(
        hour=cfg.cutoff_hour, minute=cfg.cutoff_minute, second=0, microsecond=0
    )
    candidate -= dt.timedelta(days=(candidate.weekday() - cfg.cutoff_weekday) % 7)
    if candidate > now_local:
        candidate -= dt.timedelta(days=7)
    return candidate


def next_cutoff_local(now_local: dt.datetime, cfg: Config) -> dt.datetime:
    return cycle_start_local(now_local, cfg) + dt.timedelta(days=7)


def cycle_title(cfg: Config, now_local: dt.datetime | None = None) -> str:
    now_local = now_local or dt.datetime.now(cfg.tz)
    return f"Cycle_{cycle_start_local(now_local, cfg).date().isoformat()}"
