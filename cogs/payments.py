"""ระบบชำระเงิน: ส่ง QR ให้ลูกค้า, รับสลิป, ให้แอดมินตรวจสอบ, บันทึกบัญชี"""
from __future__ import annotations

import datetime as dt
import logging
import re

import discord
from discord.ext import commands

from core.cycle import cycle_title
from core.pricing import release_quota_for_job
from core.embeds import (
    COLOR_DANGER,
    COLOR_GOLD,
    COLOR_INFO,
    COLOR_OK,
    job_embed,
    payment_embed,
)
from core.utils import (
    display_name,
    fmt_date,
    fmt_time,
    from_iso,
    is_admin,
    money,
    now_utc,
    send_dm,
    to_iso,
)

log = logging.getLogger("olp.payments")


# --------------------------------------------------------------------- ปุ่ม
class SlipConfirmButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"olp:slip_confirm:(?P<kind>JOB|VIP):(?P<ref>\d+)",
):
    """ปุ่มให้ลูกค้ายืนยันว่าจะส่งสลิปภาพนี้ให้แอดมินตรวจ"""

    def __init__(self, kind: str, ref_id: int) -> None:
        self.kind = kind
        self.ref_id = ref_id
        super().__init__(
            discord.ui.Button(
                label="ยืนยันส่งสลิป",
                emoji="📤",
                style=discord.ButtonStyle.success,
                custom_id=f"olp:slip_confirm:{kind}:{ref_id}",
            )
        )

    @classmethod
    async def from_custom_id(cls, interaction, item, match: re.Match[str]):
        return cls(match["kind"], int(match["ref"]))

    async def callback(self, interaction: discord.Interaction) -> None:  # type: ignore[override]
        cog: PaymentsCog = interaction.client.get_cog("PaymentsCog")  # type: ignore[assignment]
        await cog.submit_slip_to_admin(interaction, self.kind, self.ref_id)


class SlipDecisionButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"olp:slip_(?P<action>ok|no):(?P<kind>JOB|VIP):(?P<ref>\d+)",
):
    """ปุ่มฝั่งแอดมิน: ยืนยันสลิปถูกต้อง / ยกเลิกบิล"""

    def __init__(self, action: str, kind: str, ref_id: int) -> None:
        self.action = action
        self.kind = kind
        self.ref_id = ref_id
        approve = action == "ok"
        reject_label = "ยกเลิกบิล" if kind == "JOB" else "ยกเลิกคำสั่งซื้อ"
        super().__init__(
            discord.ui.Button(
                label="ยืนยันสลิปถูกต้อง" if approve else reject_label,
                emoji="✅" if approve else "❌",
                style=discord.ButtonStyle.success if approve else discord.ButtonStyle.danger,
                custom_id=f"olp:slip_{action}:{kind}:{ref_id}",
            )
        )

    @classmethod
    async def from_custom_id(cls, interaction, item, match: re.Match[str]):
        return cls(match["action"], match["kind"], int(match["ref"]))

    async def callback(self, interaction: discord.Interaction) -> None:  # type: ignore[override]
        cog: PaymentsCog = interaction.client.get_cog("PaymentsCog")  # type: ignore[assignment]
        if not is_admin(interaction.user, cog.cfg.admin_role_id):
            await interaction.response.send_message("เฉพาะแอดมินเท่านั้นค่ะ", ephemeral=True)
            return
        if self.action == "ok":
            await cog.approve_slip(interaction, self.kind, self.ref_id)
        else:
            await cog.reject_slip(interaction, self.kind, self.ref_id)


def slip_view(kind: str, ref_id: int) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(SlipConfirmButton(kind, ref_id))
    return view


def admin_slip_view(kind: str, ref_id: int) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(SlipDecisionButton("ok", kind, ref_id))
    view.add_item(SlipDecisionButton("no", kind, ref_id))
    return view


# --------------------------------------------------------------------- cog
class PaymentsCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.cfg = bot.cfg
        self.db = bot.db

    # ------------------------------------------------------- เริ่มเก็บเงิน
    async def start_job_payment(self, job: dict) -> None:
        """ส่งสรุปยอด + QR ให้ลูกค้าใน DM และเปิดสถานะรอสลิป"""
        embed = payment_embed(
            self.cfg,
            title="💳 สรุปยอดชำระเงิน",
            description=(
                f"บิล `#{job['id']}` — {self.cfg.service_names(job['services'])}\n"
                f"พนักงาน: <@{job['staff_id']}>"
            ),
            amount=job["total_price"],
        )
        await self.db.set_pending_slip(job["customer_id"], "JOB", job["id"], to_iso(now_utc()))
        sent = await send_dm(self.bot, job["customer_id"], embed=embed)
        if sent is None:
            await self.notify_admin_text(
                f"⚠️ ส่ง DM แจ้งยอดชำระบิล `#{job['id']}` ถึง <@{job['customer_id']}> ไม่สำเร็จ "
                f"(ลูกค้าปิด DM)"
            )

    async def start_vip_payment(self, order: dict) -> None:
        package = self.cfg.vip_package(order["package_key"])
        name = package["name"] if package else order["package_key"]
        detail = f"แพ็กเกจ: **{name}**\nราคาปกติ: {money(order['base_price'])}"
        if order["discount_amount"]:
            detail += f"\nส่วนลด ({order['discount_code']}): -{money(order['discount_amount'])}"

        embed = payment_embed(
            self.cfg,
            title="💎 สรุปยอดชำระ VIP",
            description=f"คำสั่งซื้อ `V#{order['id']}`\n{detail}",
            amount=order["total_price"],
        )
        await self.db.set_pending_slip(order["customer_id"], "VIP", order["id"], to_iso(now_utc()))
        await send_dm(self.bot, order["customer_id"], embed=embed)

    # ------------------------------------------------------------ รับสลิป
    async def receive_slip_image(self, message: discord.Message, pending: dict) -> None:
        """ลูกค้าส่งภาพสลิปเข้ามาใน DM"""
        attachment = next(
            (a for a in message.attachments if (a.content_type or "").startswith("image")), None
        )
        if attachment is None:
            return

        kind, ref_id = pending["kind"], pending["ref_id"]
        if kind == "JOB":
            await self.db.update_job(ref_id, slip_url=attachment.url)
        else:
            await self.db.update_vip_order(ref_id, slip_url=attachment.url)

        embed = discord.Embed(
            title="📎 ได้รับภาพสลิปแล้ว",
            description="ตรวจสอบภาพให้ถูกต้อง แล้วกดปุ่ม **ยืนยันส่งสลิป** เพื่อส่งให้แอดมินตรวจสอบค่ะ",
            color=COLOR_GOLD,
        )
        embed.set_image(url=attachment.url)
        await message.reply(embed=embed, view=slip_view(kind, ref_id))

    async def submit_slip_to_admin(
        self, interaction: discord.Interaction, kind: str, ref_id: int
    ) -> None:
        record = (
            await self.db.get_job(ref_id) if kind == "JOB" else await self.db.get_vip_order(ref_id)
        )
        if record is None:
            await interaction.response.send_message("ไม่พบรายการนี้ในระบบค่ะ", ephemeral=True)
            return
        if not record.get("slip_url"):
            await interaction.response.send_message(
                "ยังไม่พบภาพสลิป กรุณาส่งภาพสลิปเข้ามาใน DM ก่อนค่ะ", ephemeral=True
            )
            return
        if record["status"] not in ("ACCEPTED", "AWAITING_PAYMENT", "SLIP_PENDING"):
            await interaction.response.send_message(
                "รายการนี้ไม่อยู่ในสถานะรอชำระเงินแล้วค่ะ", ephemeral=True
            )
            return

        await interaction.response.defer()

        if kind == "JOB":
            await self.db.update_job(ref_id, status="SLIP_PENDING")
            title = f"🔎 สลิปรอตรวจสอบ — บิล #{ref_id}"
            desc = (
                f"ลูกค้า: <@{record['customer_id']}>\n"
                f"พนักงาน: <@{record['staff_id']}>\n"
                f"บริการ: {self.cfg.service_names(record['services'])}\n"
                f"ยอด: **{money(record['total_price'])}**"
            )
        else:
            await self.db.update_vip_order(ref_id, status="SLIP_PENDING")
            package = self.cfg.vip_package(record["package_key"])
            title = f"🔎 สลิปรอตรวจสอบ — VIP #{ref_id}"
            desc = (
                f"ลูกค้า: <@{record['customer_id']}>\n"
                f"แพ็กเกจ: {package['name'] if package else record['package_key']}\n"
                f"ยอด: **{money(record['total_price'])}**"
            )

        embed = discord.Embed(title=title, description=desc, color=COLOR_GOLD)
        embed.set_image(url=record["slip_url"])
        await self.notify_admin(embed=embed, view=admin_slip_view(kind, ref_id))

        await interaction.edit_original_response(
            embed=discord.Embed(
                title="📤 ส่งสลิปให้แอดมินแล้ว",
                description="รอแอดมินตรวจสอบสักครู่นะคะ ระบบจะแจ้งผลกลับมาทาง DM นี้ค่ะ",
                color=COLOR_INFO,
            ),
            view=None,
        )

    # -------------------------------------------------------- ตัดสินใจสลิป
    async def approve_slip(self, interaction: discord.Interaction, kind: str, ref_id: int) -> None:
        await interaction.response.defer()
        if kind == "JOB":
            ok, msg = await self.mark_job_paid(ref_id, interaction.user)
        else:
            vip = self.bot.get_cog("VipCog")
            ok, msg = await vip.activate_order(ref_id, interaction.user)

        await self._finish_admin_message(interaction, msg, COLOR_OK if ok else COLOR_DANGER)

    async def reject_slip(self, interaction: discord.Interaction, kind: str, ref_id: int) -> None:
        await interaction.response.defer()
        if kind == "JOB":
            ok, msg = await self.cancel_job(ref_id, interaction.user)
        else:
            order = await self.db.get_vip_order(ref_id)
            if order is None:
                ok, msg = False, "ไม่พบคำสั่งซื้อนี้"
            else:
                await self.db.update_vip_order(ref_id, status="CANCELLED")
                await self.db.clear_pending_slip(order["customer_id"])
                await send_dm(
                    self.bot,
                    order["customer_id"],
                    embed=discord.Embed(
                        title="❌ คำสั่งซื้อ VIP ถูกยกเลิก",
                        description="แอดมินยกเลิกรายการนี้ หากมีข้อสงสัยติดต่อแอดมินได้เลยค่ะ",
                        color=COLOR_DANGER,
                    ),
                )
                ok, msg = True, f"ยกเลิกคำสั่งซื้อ VIP `#{ref_id}` แล้ว โดย {interaction.user.mention}"

        await self._finish_admin_message(interaction, msg, COLOR_OK if ok else COLOR_DANGER)

    async def _finish_admin_message(
        self, interaction: discord.Interaction, text: str, color: int
    ) -> None:
        message = interaction.message
        if message is None:
            await interaction.followup.send(text, ephemeral=True)
            return
        embed = message.embeds[0] if message.embeds else discord.Embed()
        embed.color = color
        embed.add_field(name="ผลการตรวจสอบ", value=text, inline=False)
        await message.edit(embed=embed, view=None)

    # ------------------------------------------------------------ สถานะบิล
    async def mark_job_paid(self, job_id: int, admin: discord.abc.User) -> tuple[bool, str]:
        job = await self.db.get_job(job_id)
        if job is None:
            return False, "ไม่พบบิลนี้ในระบบ"
        if job["status"] in ("PAID", "COMPLETED"):
            return False, "บิลนี้ชำระเงินเรียบร้อยแล้ว"
        if job["status"] == "CANCELLED":
            return False, "บิลนี้ถูกยกเลิกไปแล้ว"

        await self.db.update_job(job_id, status="PAID", paid_at=to_iso(now_utc()))
        await self.db.clear_pending_slip(job["customer_id"])
        job = await self.db.get_job(job_id)

        await send_dm(
            self.bot,
            job["customer_id"],
            embed=job_embed(self.cfg, job, title="✅ ชำระเงินสำเร็จ", color=COLOR_OK),
        )
        await send_dm(
            self.bot,
            job["staff_id"],
            embed=discord.Embed(
                title="💰 ลูกค้าชำระเงินแล้ว",
                description=(
                    f"บิล `#{job_id}` ยอด {money(job['total_price'])}\n"
                    f"ส่วนแบ่งของคุณ: **{money(job['staff_share'])}**"
                ),
                color=COLOR_OK,
            ),
        )

        await self.log_job_to_sheet(job)
        return True, f"ยืนยันสลิปแล้ว โดย {admin.mention} — บิล `#{job_id}` สถานะ **PAID**"

    async def cancel_job(self, job_id: int, admin: discord.abc.User) -> tuple[bool, str]:
        job = await self.db.get_job(job_id)
        if job is None:
            return False, "ไม่พบบิลนี้ในระบบ"
        if job["status"] == "CANCELLED":
            return False, "บิลนี้ถูกยกเลิกไปแล้ว"

        await self.db.update_job(job_id, status="CANCELLED", cancelled_at=to_iso(now_utc()))
        await self.db.clear_pending_slip(job["customer_id"])

        # คืนสิทธิ์ฟรีที่เคยล็อกไว้ตอนเปิดบิล (ถ้ามี)
        if job.get("quota_services") and job.get("quota_cycle"):
            await release_quota_for_job(self.db, job["customer_id"], job["quota_services"], job["quota_cycle"])

        # ถ้าเป็นบิลต่อเวลา ให้ถอนเวลาที่ต่อออกจากบิลแม่
        if job["job_type"] == "EXTEND" and job.get("parent_job_id"):
            parent = await self.db.get_job(job["parent_job_id"])
            if parent is not None:
                new_end = from_iso(parent["end_time"]) - dt.timedelta(minutes=job["duration_minutes"])
                await self.db.update_job(
                    parent["id"],
                    end_time=to_iso(new_end),
                    duration_minutes=max(parent["duration_minutes"] - job["duration_minutes"], 0),
                )

        note = discord.Embed(
            title="❌ บิลถูกยกเลิก",
            description=f"บิล `#{job_id}` ถูกยกเลิกโดยแอดมิน ยอดเงินจะไม่ถูกบันทึกลงบัญชีค่ะ",
            color=COLOR_DANGER,
        )
        await send_dm(self.bot, job["customer_id"], embed=note)
        await send_dm(self.bot, job["staff_id"], embed=note)
        return True, f"ยกเลิกบิล `#{job_id}` แล้ว โดย {admin.mention} (ไม่บันทึกลง Google Sheets)"

    # ----------------------------------------------------- Google Sheets
    async def log_job_to_sheet(self, job: dict) -> None:
        if not self.bot.sheets.ready:
            return
        if job.get("sheet_logged"):
            return

        tz = self.cfg.tz
        guild = self.bot.get_guild(job["guild_id"])
        start, end = from_iso(job["start_time"]), from_iso(job["end_time"])
        row = [
            job["id"],
            fmt_date(start, tz),
            fmt_time(start, tz),
            fmt_time(end, tz),
            await display_name(self.bot, guild, job["customer_id"]),
            str(job["customer_id"]),
            await display_name(self.bot, guild, job["staff_id"]),
            str(job["staff_id"]),
            self.cfg.service_names(job["services"]),
            self.cfg.room_name(job.get("room")),
            job["total_price"],
            job["staff_share"],
            job["shop_share"],
            "ต่อเวลา" if job["job_type"] == "EXTEND" else "ปกติ",
            self.cfg.vip_tier_name(job.get("vip_tier")) if job.get("vip_tier") else "ลูกค้าทั่วไป",
            job.get("note") or "",
        ]
        title = await self.db.get_meta("current_cycle") or cycle_title(self.cfg)
        if await self.bot.sheets.append_job_row(title, row):
            await self.db.update_job(job["id"], sheet_logged=1)

    # -------------------------------------------------------------- utils
    async def notify_admin(
        self,
        *,
        content: str | None = None,
        embed: discord.Embed | None = None,
        view: discord.ui.View | None = None,
    ) -> discord.Message | None:
        channel = self.bot.get_channel(self.cfg.channel_id("admin"))
        if channel is None:
            log.warning("ไม่พบห้องแอดมิน (channels.admin) ใน config")
            return None
        kwargs: dict = {}
        if content is not None:
            kwargs["content"] = content
        if embed is not None:
            kwargs["embed"] = embed
        if view is not None:
            kwargs["view"] = view
        return await channel.send(**kwargs)

    async def notify_admin_text(self, text: str) -> None:
        await self.notify_admin(content=text)


async def setup(bot: commands.Bot) -> None:
    bot.add_dynamic_items(SlipConfirmButton, SlipDecisionButton)
    await bot.add_cog(PaymentsCog(bot))
