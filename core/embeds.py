"""ตัวสร้าง Embed ที่ใช้ร่วมกันหลายส่วน"""
from __future__ import annotations

import discord

from .config import Config
from .utils import discord_ts, from_iso, fmt_datetime, money

COLOR_MAIN = 0x2B2D31
COLOR_INFO = 0x5865F2
COLOR_OK = 0x57F287
COLOR_WARN = 0xFEE75C
COLOR_DANGER = 0xED4245
COLOR_GOLD = 0xF1C40F

STATUS_LABEL = {
    "PENDING_STAFF": "⏳ รอพนักงานรับงาน",
    "ACCEPTED": "💳 รอลูกค้าชำระเงิน",
    "SLIP_PENDING": "🔎 รอแอดมินตรวจสลิป",
    "PAID": "✅ ชำระเงินแล้ว",
    "COMPLETED": "🏁 จบงานแล้ว",
    "CANCELLED": "❌ ยกเลิก",
}


def menu_item(title: str, desc: str) -> str:
    return f"{title}\n-# {desc}" if desc else title


def rows_text(rows) -> str:
    """แถว ไอคอน + หัวข้อตัวหนา + ค่า"""
    return "\n".join(f"{icon} **{label}**　{value}" for icon, label, value in rows)


def panel_embed(
    title: str,
    intro: str,
    sections: list[tuple[str, list[tuple[str, str]] | str]],
    *,
    footer: str | None = None,
    guild: discord.Guild | None = None,
    color: int = COLOR_MAIN,
) -> discord.Embed:
    """หน้าตาแผงปุ่มมาตรฐาน — แบ่งหมวดตามแถวปุ่ม หมวดละ 1 field

    sections = [(ชื่อหมวด, [(ชื่อปุ่ม, คำอธิบาย), ...] หรือข้อความ), ...]
    """
    embed = discord.Embed(title=title, description=intro, color=color)
    if guild is not None and guild.icon:
        embed.set_thumbnail(url=guild.icon.url)
    for name, body in sections:
        value = body if isinstance(body, str) else "\n".join(menu_item(f"**{t}**", d) for t, d in body)
        embed.add_field(name=name[:256], value=value[:1024] or "-", inline=False)
    if footer:
        embed.set_footer(text=footer)
    return embed


def job_embed(cfg: Config, job: dict, *, title: str, color: int = COLOR_MAIN) -> discord.Embed:
    start = from_iso(job["start_time"])
    end = from_iso(job["end_time"])
    tz = cfg.tz

    embed = discord.Embed(title=title, color=color)
    embed.add_field(name="เลขที่บิล", value=f"`#{job['id']}`", inline=True)
    embed.add_field(name="สถานะ", value=STATUS_LABEL.get(job["status"], job["status"]), inline=True)
    embed.add_field(
        name="ประเภท",
        value="⏱️ ต่อเวลา" if job["job_type"] == "EXTEND" else "🧾 บิลปกติ",
        inline=True,
    )
    embed.add_field(name="ลูกค้า", value=f"<@{job['customer_id']}>", inline=True)
    embed.add_field(name="พนักงาน", value=f"<@{job['staff_id']}>", inline=True)
    embed.add_field(name="ห้อง", value=cfg.room_name(job.get("room")), inline=True)
    embed.add_field(name="บริการ", value=cfg.service_names(job["services"]), inline=False)
    embed.add_field(
        name="เวลา",
        value=(
            f"เริ่ม {discord_ts(start)} → จบ {discord_ts(end)}\n"
            f"({fmt_datetime(start, tz)} - {job['duration_minutes']} นาที)"
        ),
        inline=False,
    )
    tier = job.get("vip_tier")
    tier_note = f" · {cfg.vip_tier_name(tier)} {(cfg.vip_tier(tier) or {}).get('emoji', '')}" if tier else ""
    embed.add_field(
        name="ยอดชำระ",
        value=f"**{money(job['total_price'])}**" + tier_note,
        inline=True,
    )
    if job.get("quota_services"):
        embed.add_field(
            name="ใช้สิทธิ์ฟรีเดือนนี้",
            value=cfg.service_names(job["quota_services"]),
            inline=True,
        )
    if job.get("note"):
        embed.add_field(name="หมายเหตุ", value=job["note"], inline=False)
    return embed


def payment_embed(cfg: Config, *, title: str, description: str, amount: float) -> discord.Embed:
    embed = discord.Embed(title=title, description=description, color=COLOR_GOLD)
    embed.add_field(name="ยอดที่ต้องชำระ", value=f"**{money(amount)}**", inline=False)

    account = cfg.get("payment.account_name")
    promptpay = cfg.get("payment.promptpay")
    if account or promptpay:
        embed.add_field(
            name="ช่องทางชำระเงิน",
            value=f"พร้อมเพย์: `{promptpay}`\nชื่อบัญชี: {account}",
            inline=False,
        )

    qr = cfg.get("payment.qr_image_url")
    if qr and str(qr).startswith(("http://", "https://")):
        embed.set_image(url=qr)

    embed.set_footer(text=cfg.get("payment.note", "ส่งภาพสลิปกลับมาที่ DM นี้ได้เลยค่ะ"))
    return embed
