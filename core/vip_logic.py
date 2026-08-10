"""Life-cycle ของสิทธิ์ VIP: คำนวณวันหมดอายุ, สะสม/อัปเกรดยศ, รอบโควต้ารายเดือน

อ้างอิงจาก "VIP logic.txt":
- รายเดือน = บวก 1 เดือนปฏิทิน (ถ้าวันที่ 29-31 ม.ค. ชนกับ ก.พ. ที่มีวันน้อยกว่า จะเลื่อนไปวันสุดท้ายของ ก.พ. อัตโนมัติ)
- รายปี = บวก 365 วันตรงๆ
- ต่ออายุก่อนหมดอายุ (ระดับเดิม) -> สะสมต่อจากวันหมดอายุเดิม
- หมดอายุไปแล้วค่อยซื้อใหม่ หรือเปลี่ยนระดับ (อัปเกรด) -> เริ่มนับใหม่จาก "วันนี้"
- ตัวนับเดือนสะสม (สำหรับแจ้งอัปเกรดฟรีเมื่อครบ 12 เดือน) รีเซ็ตเป็น 0 ทันทีที่ขาดอายุ
"""
from __future__ import annotations

import calendar
import datetime as dt

from .database import Database
from .utils import to_iso

UTC = dt.timezone.utc


async def active_tier(db: Database, user_id: int, now_local: dt.datetime) -> str | None:
    """ระดับ VIP ปัจจุบันของลูกค้า (None ถ้าไม่มี/หมดอายุแล้ว) — DB คือแหล่งความจริง"""
    row = await db.active_vip_member(user_id, to_iso(now_local))
    return row["tier"] if row else None


def add_calendar_months(base: dt.date, months: int) -> dt.date:
    """บวกจำนวนเดือนปฏิทิน โดย clamp วันที่เกินเดือนปลายทางให้เป็นวันสุดท้ายของเดือนนั้น
    (เช่น 31 ม.ค. + 1 เดือน -> 28/29 ก.พ. ตามที่ระบุใน VIP logic.txt)
    """
    month_index = base.month - 1 + months
    year = base.year + month_index // 12
    month = month_index % 12 + 1
    last_day = calendar.monthrange(year, month)[1]
    return base.replace(year=year, month=month, day=min(base.day, last_day))


def cycle_month_key(now_local: dt.datetime) -> str:
    """คีย์รอบโควต้ารายเดือน เช่น '2026-08' — รีเซ็ตพร้อมกันทุกวันที่ 1 ของเดือน"""
    return f"{now_local.year:04d}-{now_local.month:02d}"


def compute_new_expiry(
    *,
    now_local: dt.datetime,
    unit: str,
    package_months: int,
    previous_expiry: dt.datetime | None,
    same_tier_renewal: bool,
) -> dt.datetime:
    """คำนวณวันหมดอายุใหม่ตาม stacking logic

    - same_tier_renewal=True และยังไม่หมดอายุ -> สะสมต่อจาก previous_expiry
    - นอกนั้น (หมดอายุไปแล้ว หรือเป็นการอัปเกรด/เปลี่ยนระดับ) -> เริ่มนับจาก now_local
    - unit="month" -> บวกเดือนปฏิทิน (รองรับ edge case ม.ค. 29-31 -> ก.พ.)
    - unit="year" -> บวก 365 วันตรงๆ ตามเอกสาร
    """
    still_active = previous_expiry is not None and previous_expiry > now_local
    base = previous_expiry if (same_tier_renewal and still_active) else now_local

    if unit == "month":
        new_date = add_calendar_months(base.date(), package_months)
        return dt.datetime.combine(new_date, base.time(), tzinfo=base.tzinfo)

    return base + dt.timedelta(days=365)


def free_upgrade_expiry(previous_expiry: dt.datetime, now_local: dt.datetime) -> dt.datetime:
    """วันหมดอายุใหม่เมื่อแอดมินอนุมัติอัปเกรดฟรี 1 เดือน (ต่อจากวันหมดอายุเดิม)"""
    base = previous_expiry if previous_expiry > now_local else now_local
    new_date = add_calendar_months(base.date(), 1)
    return dt.datetime.combine(new_date, base.time(), tzinfo=base.tzinfo)


def next_streak_months(*, current_streak: int, same_tier_renewal: bool, still_active: bool, months_added: int) -> int:
    """ตัวนับเดือนสะสมของระดับปัจจุบัน (ใช้แจ้งอัปเกรดฟรีเมื่อครบ 12 เดือน)

    - ต่ออายุระดับเดิมก่อนหมดอายุ -> สะสมบวกเพิ่ม
    - หมดอายุไปแล้ว หรือเปลี่ยนระดับ -> เริ่มนับใหม่จากจำนวนเดือนที่เพิ่งซื้อ
    """
    if same_tier_renewal and still_active:
        return current_streak + months_added
    return months_added
