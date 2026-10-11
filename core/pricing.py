"""คำนวณราคา, ระยะเวลา, โควต้าฟรีรายเดือน และส่วนแบ่งรายได้"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from .config import Config
from .database import Database
from .vip_logic import cycle_month_key


@dataclass
class Quote:
    services: list[str]
    total_price: float
    duration_minutes: int
    tier: str | None
    quota_services: list[str] = field(default_factory=list)  # บริการที่ใช้สิทธิ์ฟรีในรอบนี้
    lines: list[tuple[str, float]] = field(default_factory=list)
    fixed_staff: float = 0.0  # ส่วนพนักงานแบบบาทตายตัว (บริการที่ตั้ง staff_share)
    percent_base: float = 0.0  # ยอดของบริการที่ไม่ได้ตั้ง staff_share → คิดตาม % ส่วนแบ่ง

    @property
    def breakdown(self) -> str:
        return "\n".join(f"• {name} — {price:,.0f} บาท" for name, price in self.lines) or "-"


async def quote_services(
    cfg: Config,
    db: Database,
    service_keys: list[str],
    *,
    customer_id: int,
    tier: str | None,
    now_local: dt.datetime | None = None,
) -> Quote:
    """รวมราคาและระยะเวลาของบริการที่เลือก ตามระดับ VIP ของลูกค้า

    ไม่ล็อกโควต้าให้ทันที — เป็นแค่การ "ดูตัวอย่าง" (นับสิทธิ์ที่ใช้ไปแล้วในเดือนนี้จริง
    แต่ยังไม่บันทึกเพิ่ม) ต้องเรียก reserve_quota_for_job แยกตอนสร้างบิลจริงเท่านั้น
    """
    now_local = now_local or dt.datetime.now(cfg.tz)
    cycle = cycle_month_key(now_local)

    total = 0.0
    duration = 0
    lines: list[tuple[str, float]] = []
    quota_services: list[str] = []
    fixed_staff = 0.0
    percent_base = 0.0

    for key in service_keys:
        svc = cfg.service(key)
        if svc is None:
            continue
        duration += int(svc.get("duration_minutes", 60))
        entry = cfg.service_pricing_entry(svc, tier)

        if isinstance(entry, dict):
            if entry.get("unlimited"):
                price = 0.0
                lines.append((f"{svc['name']} (สิทธิ์ไม่จำกัด)", 0.0))
            else:
                free_limit = int(entry.get("free_per_month", 0))
                used = await db.get_quota_used(customer_id, key, cycle)
                if used < free_limit:
                    price = 0.0
                    quota_services.append(key)
                    lines.append((f"{svc['name']} (สิทธิ์ฟรี {used + 1}/{free_limit} เดือนนี้)", 0.0))
                else:
                    price = float(entry.get("after_price", 0))
                    lines.append((f"{svc['name']} (ใช้สิทธิ์ฟรีครบแล้ว)", price))
        else:
            price = float(entry)
            lines.append((svc["name"], price))

        total += price
        if svc.get("staff_share") is not None:
            # ส่วนพนักงานตายตัวต่อครั้ง (ใช้สิทธิ์ฟรี VIP ร้านออกให้ พนักงานยังได้เต็ม)
            fixed_staff += float(svc["staff_share"])
        else:
            percent_base += price

    return Quote(
        services=list(service_keys),
        total_price=total,
        duration_minutes=duration,
        tier=tier,
        quota_services=quota_services,
        lines=lines,
        fixed_staff=fixed_staff,
        percent_base=percent_base,
    )


async def reserve_quota_for_job(db: Database, customer_id: int, quote: Quote, cycle: str) -> None:
    """ล็อกสิทธิ์ฟรีที่ตัดสินใจใช้จริงตอนสร้างบิล (เรียกครั้งเดียวตอน create_job)"""
    for key in quote.quota_services:
        await db.reserve_quota(customer_id, key, cycle)


async def release_quota_for_job(db: Database, customer_id: int, quota_services: list[str], cycle: str) -> None:
    """คืนสิทธิ์ฟรีที่เคยล็อกไว้ (เรียกตอนยกเลิกบิล)"""
    for key in quota_services:
        await db.release_quota(customer_id, key, cycle)


def split_revenue(cfg: Config, staff_id: int, total_price: float) -> tuple[float, float]:
    """คืนค่า (ส่วนแบ่งพนักงาน, รายได้เข้าร้าน) แบบคิดเป็น % ทั้งยอด"""
    percent = cfg.staff_percent(staff_id)
    staff_share = round(total_price * percent / 100.0, 2)
    shop_share = round(total_price - staff_share, 2)
    return staff_share, shop_share


def split_quote(cfg: Config, staff_id: int, quote: Quote) -> tuple[float, float]:
    """คืนค่า (ส่วนแบ่งพนักงาน, รายได้เข้าร้าน) ของบิล

    บริการที่ตั้ง staff_share = พนักงานได้เป็นบาทตามตารางร้าน · ที่เหลือคิดตาม % ส่วนแบ่ง (revenue_share)
    """
    percent = cfg.staff_percent(staff_id)
    staff_share = round(quote.fixed_staff + quote.percent_base * percent / 100.0, 2)
    return staff_share, round(quote.total_price - staff_share, 2)


def apply_discount(cfg: Config, code: str | None, price: float) -> tuple[float, float, str | None]:
    """คืนค่า (ยอดสุทธิ, ส่วนลด, โค้ดที่ใช้ได้จริง)"""
    if not code:
        return price, 0.0, None

    rule = cfg.discount(code)
    if rule is None:
        return price, 0.0, None

    if rule.get("type") == "percent":
        discount = round(price * float(rule.get("value", 0)) / 100.0, 2)
    else:
        discount = float(rule.get("value", 0))

    discount = min(discount, price)
    return round(price - discount, 2), discount, code.strip().upper()
