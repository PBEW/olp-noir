"""คำนวณราคา, ระยะเวลา, โควต้าฟรีรายเดือน และส่วนแบ่งรายได้"""
from __future__ import annotations

import datetime as dt
import json
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
    extra_staff_pool: float = 0.0  # ส่วนของพนักงานจากค่าพนักงานเพิ่ม/ค่าลูกค้าเพิ่ม (หารเท่ากันทุกคน)

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
    staff_count: int = 1,
    customer_count: int = 1,
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
    extra_pool = 0.0

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

        # 👥 ค่าพนักงานเพิ่ม (เกินจำนวนที่รวมในราคาแล้ว) → ส่วนใหญ่เป็นของพนักงาน ทุกคนได้ใกล้เคียงมาคนเดียว
        more_staff = extra_staff_count(svc, staff_count)
        if more_staff and svc.get("extra_staff_price"):
            add = more_staff * float(svc["extra_staff_price"])
            lines.append((f"พนักงานเพิ่ม ({svc['name']}) {more_staff} คน", add))
            total += add
            extra_pool += add * float(svc.get("extra_staff_percent", 100)) / 100.0

        # 👤 ค่าลูกค้าเพิ่ม (คนที่ 2 ขึ้นไป)
        more_cust = max(0, customer_count - 1)
        if more_cust and svc.get("extra_customer_price"):
            add = more_cust * float(svc["extra_customer_price"])
            lines.append((f"ลูกค้าเพิ่ม ({svc['name']}) {more_cust} คน", add))
            total += add
            extra_pool += add * float(svc.get("extra_customer_percent", 100)) / 100.0

    return Quote(
        services=list(service_keys),
        total_price=total,
        duration_minutes=duration,
        tier=tier,
        quota_services=quota_services,
        lines=lines,
        fixed_staff=fixed_staff,
        percent_base=percent_base,
        extra_staff_pool=extra_pool,
    )


def extra_staff_count(svc: dict, staff_count: int) -> int:
    """จำนวนพนักงานที่เกินจากที่รวมในราคาแล้ว (included_staff ค่าเริ่มต้น 1)"""
    return max(0, staff_count - int(svc.get("included_staff", 1)))


def validate_selection(cfg: Config, service_keys: list[str], staff_count: int, customer_count: int) -> str | None:
    """ตรวจจำนวนพนักงาน/ลูกค้าตามที่ตั้งไว้ในแต่ละบริการ (max_staff / max_customers ค่าเริ่มต้น 10)"""
    for key in dict.fromkeys(service_keys):
        svc = cfg.service(key)
        if svc is None:
            continue
        max_staff = int(svc.get("max_staff", 10))
        if staff_count > max_staff:
            return f"**{svc['name']}** รับพนักงานได้สูงสุด {max_staff} คนค่ะ"
        max_cust = int(svc.get("max_customers", 10))
        if customer_count > max_cust:
            return f"**{svc['name']}** รับลูกค้าได้สูงสุด {max_cust} คนต่อบิลค่ะ"
    return None


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
    staff_share = round(quote.fixed_staff + quote.percent_base * percent / 100.0 + quote.extra_staff_pool, 2)
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


def split_quote_multi(cfg: Config, staff_ids: list[int], quote: Quote) -> tuple[dict[int, float], float]:
    """ส่วนแบ่งเมื่อบิลมีพนักงานหลายคน → ({staff_id: บาท}, ส่วนร้าน)

    ส่วนพนักงานของราคาหลัก แบ่งตาม revenue_share.multi_staff_mode:
      "split" (ค่าเริ่มต้น) = หารเท่ากัน · ร้านได้เท่าเดิมไม่ว่ามากี่คน
      "each"  = ทุกคนได้เต็มตามตาราง · ร้านจ่ายเพิ่มตามจำนวนคน
    ส่วนจากค่าพนักงานเพิ่ม/ค่าลูกค้าเพิ่ม (extra_staff_pool) หารเท่ากันทุกคนเสมอ
    """
    staff_ids = list(dict.fromkeys(staff_ids))
    n = len(staff_ids)
    pool = quote.extra_staff_pool

    def base(sid: int) -> float:
        return split_quote(cfg, sid, quote)[0] - pool

    if n > 1 and cfg.get("revenue_share.multi_staff_mode", "split") == "each":
        shares = {sid: round(base(sid) + pool / n, 2) for sid in staff_ids}
    else:
        total = round(base(staff_ids[0]) + pool, 2)
        each = round(total / n, 2)
        shares = {sid: each for sid in staff_ids}
        shares[staff_ids[0]] = round(total - each * (n - 1), 2)  # เศษสตางค์ให้คนหลัก
    return shares, round(quote.total_price - sum(shares.values()), 2)


def job_shares(job: dict) -> dict[int, float]:
    """ส่วนแบ่งรายคนของบิล (บิลเก่าที่มีพนักงานคนเดียวก็ใช้ได้)"""
    raw = job.get("staff_shares")
    if raw:
        data = json.loads(raw) if isinstance(raw, str) else raw
        if data:
            return {int(k): float(v) for k, v in data.items()}
    return {job["staff_id"]: float(job.get("staff_share") or 0)}


def job_staff_ids(job: dict) -> list[int]:
    return list(job_shares(job))


def job_staff_mentions(job: dict) -> str:
    return ", ".join(f"<@{sid}>" for sid in job_staff_ids(job))


def job_customer_ids(job: dict) -> list[int]:
    """ลูกค้าทุกคนในบิล (คนแรก = คนจ่าย)"""
    raw = job.get("co_customers")
    others = (json.loads(raw) if isinstance(raw, str) else raw) or []
    return list(dict.fromkeys([job["customer_id"], *[int(c) for c in others]]))


def job_customer_mentions(job: dict) -> str:
    ids = job_customer_ids(job)
    if len(ids) == 1:
        return f"<@{ids[0]}>"
    return f"<@{ids[0]}> 💳 + " + ", ".join(f"<@{c}>" for c in ids[1:])
