"""ฟังก์ชันช่วยเหลือทั่วไป: เวลา, เงิน, สิทธิ์, การส่ง DM"""
from __future__ import annotations

import datetime as dt
import logging
from zoneinfo import ZoneInfo

import discord

log = logging.getLogger("olp.utils")

UTC = dt.timezone.utc


# --------------------------------------------------------------------- time
def now_utc() -> dt.datetime:
    return dt.datetime.now(UTC)


def to_iso(value: dt.datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat()


def from_iso(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    parsed = dt.datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def local(value: dt.datetime, tz: ZoneInfo) -> dt.datetime:
    return value.astimezone(tz)


def fmt_time(value: dt.datetime, tz: ZoneInfo) -> str:
    return local(value, tz).strftime("%H:%M")


def fmt_datetime(value: dt.datetime, tz: ZoneInfo) -> str:
    return local(value, tz).strftime("%d/%m/%Y %H:%M")


def fmt_date(value: dt.datetime, tz: ZoneInfo) -> str:
    return local(value, tz).strftime("%d/%m/%Y")


def discord_ts(value: dt.datetime, style: str = "t") -> str:
    return f"<t:{int(value.timestamp())}:{style}>"


class TimeParseError(ValueError):
    pass


def parse_start_time(raw: str, tz: ZoneInfo) -> dt.datetime:
    """รับเวลาเริ่มงานจากผู้ใช้ แล้วคืนค่าเป็น datetime (UTC)

    รองรับ: ว่าง / now / ตอนนี้ , +15 (อีก 15 นาที), 20:30, 20.30,
    05/09 20:30, 05/09/2026 20:30, 2026-09-05 20:30
    """
    text = (raw or "").strip().lower()
    now_local = dt.datetime.now(tz)

    if text in ("", "-", "now", "ตอนนี้", "เดี๋ยวนี้", "ทันที"):
        return now_local.astimezone(UTC)

    if text.startswith("+"):
        try:
            minutes = int(text[1:].strip())
        except ValueError as exc:
            raise TimeParseError("รูปแบบ +นาที ไม่ถูกต้อง (ตัวอย่าง: +15)") from exc
        return (now_local + dt.timedelta(minutes=minutes)).astimezone(UTC)

    text = text.replace(".", ":")

    for fmt, has_date, has_year in (
        ("%Y-%m-%d %H:%M", True, True),
        ("%d/%m/%Y %H:%M", True, True),
        ("%d/%m %H:%M", True, False),
        ("%H:%M", False, False),
    ):
        try:
            parsed = dt.datetime.strptime(text, fmt)
        except ValueError:
            continue

        if not has_date:
            parsed = parsed.replace(year=now_local.year, month=now_local.month, day=now_local.day)
        elif not has_year:
            parsed = parsed.replace(year=now_local.year)

        result = parsed.replace(tzinfo=tz)
        # เวลาแบบไม่ระบุวัน ถ้าย้อนหลังเกิน 6 ชม. ให้ถือว่าเป็นของวันพรุ่งนี้
        if not has_date and result < now_local - dt.timedelta(hours=6):
            result += dt.timedelta(days=1)
        return result.astimezone(UTC)

    raise TimeParseError(
        "อ่านเวลาไม่ออก ลองใช้รูปแบบ: `ตอนนี้`, `+15`, `20:30`, `05/09 20:30`"
    )


# -------------------------------------------------------------------- money
def money(value: float | int | None) -> str:
    return f"{float(value or 0):,.0f} บาท"


# ------------------------------------------------------------------- access
def is_admin(member: discord.abc.User | discord.Member, admin_role_id: int) -> bool:
    if not isinstance(member, discord.Member):
        return False
    if member.guild_permissions.administrator or member.guild_permissions.manage_guild:
        return True
    return any(r.id == admin_role_id for r in member.roles)


# ---------------------------------------------------------------------- DM
async def send_dm(
    bot: discord.Client,
    user_id: int,
    *,
    content: str | None = None,
    embed: discord.Embed | None = None,
    view: discord.ui.View | None = None,
    file: discord.File | None = None,
) -> discord.Message | None:
    """ส่ง DM แบบไม่โยน exception ออกไป (คืน None ถ้าส่งไม่ได้)"""
    try:
        user = bot.get_user(user_id) or await bot.fetch_user(user_id)
        kwargs: dict = {}
        if content is not None:
            kwargs["content"] = content
        if embed is not None:
            kwargs["embed"] = embed
        if view is not None:
            kwargs["view"] = view
        if file is not None:
            kwargs["file"] = file
        return await user.send(**kwargs)
    except (discord.Forbidden, discord.HTTPException, discord.NotFound) as exc:
        log.warning("ส่ง DM ถึง %s ไม่สำเร็จ: %s", user_id, exc)
        return None


def _iter_components(message: discord.Message):
    for row in message.components:
        children = getattr(row, "children", None)
        if children is None:
            yield row
        else:
            yield from children


async def purge_old_panels(
    channel: discord.abc.Messageable,
    bot_user_id: int,
    custom_id_prefix: str,
    limit: int = 100,
) -> int:
    """ลบแผงเก่าของบอทในห้องนี้ก่อนโพสต์แผงใหม่ (กันแผงซ้อนกันหลายอัน)

    บอทลบข้อความของตัวเองได้เสมอ ไม่ต้องมีสิทธิ์ Manage Messages
    """
    removed = 0
    try:
        async for message in channel.history(limit=limit):
            if message.author.id != bot_user_id:
                continue
            if not any(
                (getattr(item, "custom_id", "") or "").startswith(custom_id_prefix)
                for item in _iter_components(message)
            ):
                continue
            try:
                await message.delete()
                removed += 1
            except discord.HTTPException as exc:
                log.warning("ลบแผงเก่า (%s) ไม่สำเร็จ: %s", message.id, exc)
    except discord.HTTPException as exc:
        log.warning("อ่านประวัติข้อความเพื่อลบแผงเก่าไม่สำเร็จ: %s", exc)
    return removed


async def display_name(bot: discord.Client, guild: discord.Guild | None, user_id: int) -> str:
    if guild is not None:
        member = guild.get_member(user_id)
        if member is not None:
            return member.display_name
    user = bot.get_user(user_id)
    if user is not None:
        return user.display_name
    try:
        fetched = await bot.fetch_user(user_id)
        return fetched.display_name
    except discord.HTTPException:
        return f"ผู้ใช้ {user_id}"
