"""ระบบ VIP Self-Service: ซื้อ / ต่ออายุ / อัปเกรด / ตรวจสอบสิทธิ์ (multi-tier: Lace, Desire, Obsession)"""
from __future__ import annotations

import datetime as dt
import logging
import re

import discord
from discord import app_commands
from discord.ext import commands

from core.embeds import COLOR_DANGER, COLOR_GOLD, COLOR_INFO, COLOR_OK, COLOR_WARN
from core.pricing import apply_discount
from core.utils import fmt_datetime, from_iso, is_admin, money, now_utc, send_dm, to_iso
from core.vip_logic import (
    active_tier,
    compute_new_expiry,
    cycle_month_key,
    free_upgrade_expiry,
    next_streak_months,
)

log = logging.getLogger("olp.vip")


def _unit_label(pkg: dict) -> str:
    return "1 เดือน" if pkg["unit"] == "month" else "1 ปี"


class DiscountModal(discord.ui.Modal, title="ยืนยันการสั่งซื้อ"):
    code = discord.ui.TextInput(
        label="โค้ดส่วนลด (ถ้ามี)",
        placeholder="เว้นว่างได้ถ้าไม่มีโค้ด",
        required=False,
        max_length=32,
    )

    def __init__(self, shop: "VipShopView") -> None:
        super().__init__()
        self.shop = shop

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.shop.checkout(interaction, str(self.code).strip())


class VipShopView(discord.ui.View):
    def __init__(self, cog: "VipCog", buyer_id: int) -> None:
        super().__init__(timeout=600)
        self.cog = cog
        self.cfg = cog.cfg
        self.buyer_id = buyer_id
        self.package_key: str | None = None

        purchasable_tiers = {t["key"] for t in self.cfg.purchasable_vip_tiers()}
        packages = [p for p in self.cfg.vip_packages if p["tier"] in purchasable_tiers]
        self.package_select = discord.ui.Select(
            placeholder="💎 เลือกแพ็กเกจ VIP",
            row=0,
            options=[
                discord.SelectOption(
                    label=pkg["name"],
                    value=pkg["key"],
                    emoji=pkg.get("emoji"),
                    description=f"{pkg['price']:,.0f} บาท / {_unit_label(pkg)}",
                )
                for pkg in packages[:25]
            ]
            or [discord.SelectOption(label="ยังไม่ได้ตั้งค่าแพ็กเกจ", value="none")],
        )
        self.package_select.callback = self._on_package
        self.add_item(self.package_select)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.buyer_id

    async def _on_package(self, interaction: discord.Interaction) -> None:
        value = self.package_select.values[0]
        self.package_key = None if value == "none" else value
        await interaction.response.edit_message(embed=self.embed(), view=self)

    def embed(self) -> discord.Embed:
        embed = discord.Embed(
            title="💎 ซื้อ / ต่ออายุ VIP",
            description=(
                "เลือกแพ็กเกจ แล้วกดปุ่ม **ยืนยัน & ใส่โค้ดส่วนลด**\n"
                "*ต่ออายุก่อนหมดอายุจะสะสมต่อจากวันหมดอายุเดิม ถ้าเปลี่ยนระดับ (อัปเกรด) จะเริ่มนับใหม่*"
            ),
            color=COLOR_GOLD,
        )
        if self.package_key:
            pkg = self.cfg.vip_package(self.package_key)
            embed.add_field(
                name="แพ็กเกจที่เลือก",
                value=f"**{pkg['name']}**\nราคา {money(pkg['price'])} · อายุ {_unit_label(pkg)}",
                inline=False,
            )
        else:
            embed.add_field(name="แพ็กเกจที่เลือก", value="*ยังไม่เลือก*", inline=False)
        embed.set_footer(text="ระบบจะส่ง QR ชำระเงินไปที่ DM ของคุณ")
        return embed

    @discord.ui.button(
        label="ยืนยัน & ใส่โค้ดส่วนลด", emoji="🏷️", style=discord.ButtonStyle.primary, row=1
    )
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not self.package_key:
            await interaction.response.send_message("ยังไม่ได้เลือกแพ็กเกจค่ะ", ephemeral=True)
            return
        await interaction.response.send_modal(DiscountModal(self))

    async def checkout(self, interaction: discord.Interaction, code: str) -> None:
        pkg = self.cfg.vip_package(self.package_key)
        if pkg is None:
            await interaction.response.send_message("ไม่พบแพ็กเกจนี้ค่ะ", ephemeral=True)
            return

        await interaction.response.defer()
        base = float(pkg["price"])
        total, discount, used_code = apply_discount(self.cfg, code, base)

        order_id = await self.cog.db.create_vip_order(
            guild_id=interaction.guild_id or self.cfg.guild_id,
            customer_id=interaction.user.id,
            package_key=pkg["key"],
            base_price=base,
            discount_code=used_code,
            discount_amount=discount,
            total_price=total,
            status="AWAITING_PAYMENT",
            created_at=to_iso(now_utc()),
        )
        order = await self.cog.db.get_vip_order(order_id)

        payments = self.cog.bot.get_cog("PaymentsCog")
        await payments.start_vip_payment(order)

        note = ""
        if code and used_code is None:
            note = "\n⚠️ โค้ดส่วนลดที่กรอกไม่ถูกต้อง ระบบจึงคิดราคาปกติค่ะ"

        self.stop()
        await interaction.edit_original_response(
            embed=discord.Embed(
                title="📨 ส่งรายละเอียดการชำระเงินให้แล้ว",
                description=(
                    f"คำสั่งซื้อ `V#{order_id}` · {pkg['name']}\n"
                    f"ยอดชำระ **{money(total)}**\n"
                    "กรุณาตรวจสอบ DM ของบอทและส่งภาพสลิปกลับมาได้เลยค่ะ" + note
                ),
                color=COLOR_OK,
            ),
            view=None,
        )


# ------------------------------------------------------- ปุ่มอนุมัติอัปเกรดฟรี
class VipUpgradeDecisionButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"olp:vipup_(?P<action>ok|no):(?P<request_id>\d+)",
):
    def __init__(self, action: str, request_id: int) -> None:
        self.action = action
        self.request_id = request_id
        approve = action == "ok"
        super().__init__(
            discord.ui.Button(
                label="อนุมัติอัปเกรดฟรี" if approve else "ปฏิเสธ",
                emoji="✅" if approve else "❌",
                style=discord.ButtonStyle.success if approve else discord.ButtonStyle.danger,
                custom_id=f"olp:vipup_{action}:{request_id}",
            )
        )

    @classmethod
    async def from_custom_id(cls, interaction, item, match: re.Match[str]):
        return cls(match["action"], int(match["request_id"]))

    async def callback(self, interaction: discord.Interaction) -> None:  # type: ignore[override]
        cog: VipCog = interaction.client.get_cog("VipCog")  # type: ignore[assignment]
        if not is_admin(interaction.user, cog.cfg.admin_role_id):
            await interaction.response.send_message("เฉพาะแอดมินเท่านั้นค่ะ", ephemeral=True)
            return
        await interaction.response.defer()
        if self.action == "ok":
            ok, msg = await cog.approve_upgrade_request(self.request_id, interaction.user)
        else:
            ok, msg = await cog.reject_upgrade_request(self.request_id, interaction.user)

        message = interaction.message
        if message is not None:
            embed = message.embeds[0] if message.embeds else discord.Embed()
            embed.color = COLOR_OK if ok else COLOR_DANGER
            embed.add_field(name="ผลการตรวจสอบ", value=msg, inline=False)
            await message.edit(embed=embed, view=None)


def upgrade_request_view(request_id: int) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(VipUpgradeDecisionButton("ok", request_id))
    view.add_item(VipUpgradeDecisionButton("no", request_id))
    return view


class VipCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.cfg = bot.cfg
        self.db = bot.db

    # --------------------------------------------------------------- ร้าน
    async def open_vip_shop(self, interaction: discord.Interaction) -> None:
        view = VipShopView(self, interaction.user.id)
        await interaction.response.send_message(embed=view.embed(), view=view, ephemeral=True)

    async def check_vip(self, interaction: discord.Interaction) -> None:
        now_local = dt.datetime.now(self.cfg.tz)
        tier = await active_tier(self.db, interaction.user.id, now_local)

        if tier is None:
            embed = discord.Embed(
                title="🔍 สถานะสิทธิ์ VIP",
                description="ตอนนี้คุณยังไม่มีสิทธิ์ VIP ค่ะ กดปุ่ม 💎 เพื่อเลือกแพ็กเกจได้เลย",
                color=COLOR_INFO,
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        record = await self.db.get_vip_member(interaction.user.id)
        expires = from_iso(record["expires_at"])
        tier_cfg = self.cfg.vip_tier(tier)

        embed = discord.Embed(title="🔍 สถานะสิทธิ์ VIP", color=COLOR_GOLD)
        embed.add_field(name="ระดับ", value=f"{tier_cfg.get('emoji', '')} {tier_cfg['name']}", inline=True)
        embed.add_field(name="หมดอายุ", value=fmt_datetime(expires, self.cfg.tz), inline=True)
        embed.add_field(name="เดือนสะสม (ระดับนี้)", value=f"{record['streak_months']} เดือน", inline=True)

        if tier_cfg.get("upgrade_to"):
            need = int(tier_cfg["upgrade_streak_months"])
            remain = max(need - int(record["streak_months"]), 0)
            embed.add_field(
                name="เงื่อนไขอัปเกรดฟรี",
                value=(
                    f"สะสมครบ {need} เดือน จะได้อัปเกรดเป็น {self.cfg.vip_tier_name(tier_cfg['upgrade_to'])} ฟรี 1 เดือน\n"
                    + (f"อีก {remain} เดือน" if remain else "ครบแล้ว รอแอดมินอนุมัติ")
                ),
                inline=False,
            )

        quota_lines = await self._quota_status_lines(interaction.user.id, tier, now_local)
        if quota_lines:
            embed.add_field(name="สิทธิ์ฟรีเดือนนี้", value="\n".join(quota_lines), inline=False)

        await interaction.response.send_message(embed=embed, ephemeral=True)

    async def _quota_status_lines(self, user_id: int, tier: str, now_local: dt.datetime) -> list[str]:
        cycle = cycle_month_key(now_local)
        lines: list[str] = []
        for svc in self.cfg.services:
            entry = self.cfg.service_pricing_entry(svc, tier)
            if not isinstance(entry, dict):
                continue
            if entry.get("unlimited"):
                lines.append(f"• {svc['name']} — ไม่จำกัด")
                continue
            limit = int(entry.get("free_per_month", 0))
            used = await self.db.get_quota_used(user_id, svc["key"], cycle)
            lines.append(f"• {svc['name']} — ใช้ไป {min(used, limit)}/{limit} ครั้ง")
        return lines

    # ------------------------------------------------- คำนวณ+บันทึกสิทธิ์ (ใช้ร่วมกัน)
    async def _apply_grant(
        self,
        *,
        guild_id: int,
        customer_id: int,
        tier_key: str,
        unit: str,
        months: int,
        package_key: str | None = None,
    ) -> tuple[dt.datetime, int, str, dict]:
        """คำนวณวันหมดอายุ/เดือนสะสมตาม stacking logic แล้วบันทึก + สลับ Role ให้

        คืนค่า (วันหมดอายุใหม่ (tz ไทย), เดือนสะสมใหม่, หมายเหตุเรื่อง role, tier_cfg)
        """
        tier_cfg = self.cfg.vip_tier(tier_key)
        if tier_cfg is None:
            raise ValueError(f"ไม่พบระดับ VIP `{tier_key}` ใน config")

        now_local = dt.datetime.now(self.cfg.tz)
        existing = await self.db.get_vip_member(customer_id)
        previous_expiry_utc = from_iso(existing["expires_at"]) if existing and existing["expires_at"] else None
        previous_expiry_local = previous_expiry_utc.astimezone(self.cfg.tz) if previous_expiry_utc else None
        still_active = bool(previous_expiry_utc and previous_expiry_utc > now_utc())
        same_tier = bool(existing and existing["tier"] == tier_key)

        new_expiry_local = compute_new_expiry(
            now_local=now_local,
            unit=unit,
            package_months=months,
            previous_expiry=previous_expiry_local,
            same_tier_renewal=same_tier,
        )
        new_streak = next_streak_months(
            current_streak=int(existing["streak_months"]) if existing else 0,
            same_tier_renewal=same_tier,
            still_active=still_active,
            months_added=months,
        )

        role_note = await self._swap_role(guild_id, customer_id, existing, tier_cfg)

        now_iso = to_iso(now_utc())
        expires_iso = to_iso(new_expiry_local)
        await self.db.upsert_vip_member(
            customer_id,
            tier_key,
            package_key,
            int(tier_cfg.get("role_id") or 0),
            new_streak,
            expires_iso,
            now_iso,
        )
        return new_expiry_local, new_streak, role_note, tier_cfg

    # ---------------------------------------------------- ยืนยันสลิป -> role
    async def activate_order(self, order_id: int, admin: discord.abc.User) -> tuple[bool, str]:
        order = await self.db.get_vip_order(order_id)
        if order is None:
            return False, "ไม่พบคำสั่งซื้อนี้"
        if order["status"] == "ACTIVE":
            return False, "คำสั่งซื้อนี้ถูกยืนยันไปแล้ว"

        pkg = self.cfg.vip_package(order["package_key"])
        if pkg is None:
            return False, "ไม่พบแพ็กเกจนี้ใน config"
        if self.cfg.vip_tier(pkg["tier"]) is None:
            return False, f"ไม่พบระดับ VIP `{pkg['tier']}` ใน config"

        try:
            new_expiry_local, new_streak, role_note, tier_cfg = await self._apply_grant(
                guild_id=order["guild_id"],
                customer_id=order["customer_id"],
                tier_key=pkg["tier"],
                unit=pkg["unit"],
                months=int(pkg["months"]),
                package_key=order["package_key"],
            )
        except ValueError as exc:
            return False, str(exc)

        now_iso = to_iso(now_utc())
        expires_iso = to_iso(new_expiry_local)
        await self.db.update_vip_order(order_id, status="ACTIVE", paid_at=now_iso, expires_at=expires_iso)
        await self.db.clear_pending_slip(order["customer_id"])

        await send_dm(
            self.bot,
            order["customer_id"],
            embed=discord.Embed(
                title="🎉 ยืนยันสิทธิ์ VIP เรียบร้อย",
                description=(
                    f"ระดับ **{tier_cfg.get('emoji', '')} {tier_cfg['name']}**\n"
                    f"หมดอายุ: **{fmt_datetime(new_expiry_local, self.cfg.tz)}**\n\n"
                    "ครั้งต่อไปที่ใช้บริการ ระบบจะคิดราคา/สิทธิ์ฟรีตามระดับให้อัตโนมัติค่ะ 💎"
                ),
                color=COLOR_OK,
            ),
        )

        await self._check_streak_upgrade(order["guild_id"], order["customer_id"], tier_cfg, new_streak)

        return True, (
            f"ยืนยันสลิป VIP `V#{order_id}` แล้ว โดย {admin.mention}\n"
            f"ตั้งระดับ {tier_cfg['name']} หมดอายุ {fmt_datetime(new_expiry_local, self.cfg.tz)}{role_note}"
        )

    async def _swap_role(
        self, guild_id: int, customer_id: int, existing: dict | None, new_tier_cfg: dict
    ) -> str:
        guild = self.bot.get_guild(guild_id) or self.bot.get_guild(self.cfg.guild_id)
        if guild is None:
            return "\n⚠️ ไม่พบเซิร์ฟเวอร์ (ตรวจสอบ guild_id ใน config)"
        member = guild.get_member(customer_id)
        if member is None:
            return "\n⚠️ ไม่พบสมาชิกในเซิร์ฟเวอร์"

        note = ""
        if existing and existing.get("role_id") and existing["role_id"] != new_tier_cfg.get("role_id"):
            old_role = guild.get_role(int(existing["role_id"]))
            if old_role and old_role in member.roles:
                try:
                    await member.remove_roles(old_role, reason="เปลี่ยนระดับ VIP")
                except discord.Forbidden:
                    pass

        new_role = guild.get_role(int(new_tier_cfg.get("role_id") or 0))
        if new_role is None:
            return note + "\n⚠️ ไม่พบ Role ของระดับนี้ (ตรวจสอบ role_id ใน config)"
        try:
            await member.add_roles(new_role, reason="ยืนยัน VIP")
        except discord.Forbidden:
            note += "\n⚠️ บอทไม่มีสิทธิ์ให้ Role (ตรวจสอบลำดับ Role ของบอท)"
        return note

    # ------------------------------------------------- สะสมครบ 12 เดือน -> แจ้งแอดมิน
    async def _check_streak_upgrade(
        self, guild_id: int, customer_id: int, tier_cfg: dict, streak_months: int
    ) -> None:
        upgrade_to = tier_cfg.get("upgrade_to")
        threshold = tier_cfg.get("upgrade_streak_months")
        if not upgrade_to or not threshold or streak_months < int(threshold):
            return
        if await self.db.pending_upgrade_request(customer_id, upgrade_to):
            return

        request_id = await self.db.create_upgrade_request(
            guild_id=guild_id,
            user_id=customer_id,
            from_tier=tier_cfg["key"],
            to_tier=upgrade_to,
            streak_months=streak_months,
            status="PENDING",
            created_at=to_iso(now_utc()),
        )
        to_name = self.cfg.vip_tier_name(upgrade_to)
        embed = discord.Embed(
            title="🎁 ลูกค้าสะสมครบ 12 เดือน — รออนุมัติอัปเกรดฟรี",
            description=(
                f"ลูกค้า <@{customer_id}>\n"
                f"สะสมระดับ **{tier_cfg['name']}** ต่อเนื่อง **{streak_months} เดือน**\n"
                f"เข้าเงื่อนไขอัปเกรดฟรีเป็น **{to_name}** (ฟรี 1 เดือน)"
            ),
            color=COLOR_WARN,
        )
        payments = self.bot.get_cog("PaymentsCog")
        msg = await payments.notify_admin(embed=embed, view=upgrade_request_view(request_id))
        if msg is not None:
            await self.db.update_upgrade_request(request_id, admin_msg_id=msg.id)

    async def approve_upgrade_request(
        self, request_id: int, admin: discord.abc.User
    ) -> tuple[bool, str]:
        req = await self.db.get_upgrade_request(request_id)
        if req is None:
            return False, "ไม่พบคำขอนี้"
        if req["status"] != "PENDING":
            return False, "คำขอนี้ถูกดำเนินการไปแล้วค่ะ"

        to_tier_cfg = self.cfg.vip_tier(req["to_tier"])
        if to_tier_cfg is None:
            return False, f"ไม่พบระดับ `{req['to_tier']}` ใน config"

        now_local = dt.datetime.now(self.cfg.tz)
        existing = await self.db.get_vip_member(req["user_id"])
        previous_expiry_local = (
            from_iso(existing["expires_at"]).astimezone(self.cfg.tz)
            if existing and existing["expires_at"]
            else now_local
        )
        new_expiry_local = free_upgrade_expiry(previous_expiry_local, now_local)

        role_note = await self._swap_role(req["guild_id"], req["user_id"], existing, to_tier_cfg)

        now_iso = to_iso(now_utc())
        expires_iso = to_iso(new_expiry_local)
        await self.db.upsert_vip_member(
            req["user_id"],
            req["to_tier"],
            None,
            int(to_tier_cfg.get("role_id") or 0),
            0,  # เริ่มนับเดือนสะสมของระดับใหม่ใหม่ตั้งแต่ 0
            expires_iso,
            now_iso,
        )
        await self.db.update_upgrade_request(
            request_id, status="APPROVED", handled_at=now_iso, handled_by=admin.id
        )

        await send_dm(
            self.bot,
            req["user_id"],
            embed=discord.Embed(
                title="🎁 อัปเกรด VIP ฟรีเรียบร้อย!",
                description=(
                    f"ยินดีด้วยค่ะ คุณสะสมครบ {req['streak_months']} เดือน "
                    f"ได้รับการอัปเกรดเป็น **{to_tier_cfg['name']}** ฟรี 1 เดือน\n"
                    f"หมดอายุ: **{fmt_datetime(new_expiry_local, self.cfg.tz)}**"
                ),
                color=COLOR_OK,
            ),
        )
        return True, f"อนุมัติอัปเกรดเป็น {to_tier_cfg['name']} แล้ว โดย {admin.mention}{role_note}"

    async def reject_upgrade_request(
        self, request_id: int, admin: discord.abc.User
    ) -> tuple[bool, str]:
        req = await self.db.get_upgrade_request(request_id)
        if req is None:
            return False, "ไม่พบคำขอนี้"
        if req["status"] != "PENDING":
            return False, "คำขอนี้ถูกดำเนินการไปแล้วค่ะ"
        await self.db.update_upgrade_request(
            request_id, status="REJECTED", handled_at=to_iso(now_utc()), handled_by=admin.id
        )
        return True, f"ปฏิเสธคำขออัปเกรดแล้ว โดย {admin.mention}"

    # ------------------------------------------------------ ตรวจสิทธิ์หมดอายุ
    async def expire_pass(self, record: dict) -> None:
        guild = self.bot.get_guild(self.cfg.guild_id)
        if guild is not None:
            member = guild.get_member(record["user_id"])
            role = guild.get_role(int(record["role_id"] or 0))
            if member and role and role in member.roles:
                try:
                    await member.remove_roles(role, reason="VIP หมดอายุ")
                except discord.Forbidden:
                    log.warning("ถอด Role VIP ของ %s ไม่สำเร็จ", record["user_id"])

        # เคลียร์ expires_at เพื่อไม่ให้ loop ตรวจซ้ำ แต่เก็บ tier ไว้อ้างอิงประวัติ และรีเซ็ตเดือนสะสม
        await self.db.execute(
            "UPDATE vip_members SET expires_at = NULL, streak_months = 0, updated_at = ? "
            "WHERE user_id = ?",
            (to_iso(now_utc()), record["user_id"]),
        )
        await send_dm(
            self.bot,
            record["user_id"],
            embed=discord.Embed(
                title="⌛ สิทธิ์ VIP หมดอายุแล้ว",
                description="ต่ออายุได้ที่ปุ่ม 💎 ซื้อ VIP/ต่ออายุ ที่หน้าแผงบริการค่ะ",
                color=COLOR_DANGER,
            ),
        )

    # ----------------------------------------------------------- คำสั่ง
    @app_commands.command(name="vip_grant", description="ให้สิทธิ์ VIP ด้วยมือ (แอดมิน) — ให้ได้ทุกระดับ รวม Obsession")
    @app_commands.describe(member="สมาชิก", tier="ระดับ VIP", months="จำนวนเดือน (ค่าเริ่มต้น 1 เดือน)")
    @app_commands.choices(
        tier=[
            app_commands.Choice(name=t["name"], value=t["key"])
            for t in [
                {"key": "lace", "name": "VIP Lace"},
                {"key": "desire", "name": "VIP Desire"},
                {"key": "obsession", "name": "VIP Obsession"},
            ]
        ]
    )
    async def vip_grant(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        tier: app_commands.Choice[str],
        months: app_commands.Range[int, 1, 60] = 1,
    ) -> None:
        if not is_admin(interaction.user, self.cfg.admin_role_id):
            await interaction.response.send_message("เฉพาะแอดมินเท่านั้นค่ะ", ephemeral=True)
            return
        await self.grant_vip(interaction, member, tier.value, months)

    async def grant_vip(
        self, interaction: discord.Interaction, member: discord.Member, tier_key: str, months: int
    ) -> None:
        """ให้สิทธิ์ VIP ด้วยมือ (ใช้ทั้งจาก /vip_grant และเมนูแอดมิน) — ผู้เรียกต้องตรวจสิทธิ์แอดมินก่อน"""
        if self.cfg.vip_tier(tier_key) is None:
            await interaction.response.send_message(
                f"ยังไม่ได้ตั้งค่าระดับ `{tier_key}` ใน config (vip_tiers)", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)
        try:
            new_expiry_local, new_streak, role_note, tier_cfg = await self._apply_grant(
                guild_id=interaction.guild_id,
                customer_id=member.id,
                tier_key=tier_key,
                unit="month",
                months=months,
            )
        except ValueError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return

        await send_dm(
            self.bot,
            member.id,
            embed=discord.Embed(
                title="🎉 แอดมินให้สิทธิ์ VIP",
                description=(
                    f"ระดับ **{tier_cfg.get('emoji', '')} {tier_cfg['name']}**\n"
                    f"หมดอายุ: **{fmt_datetime(new_expiry_local, self.cfg.tz)}**"
                ),
                color=COLOR_OK,
            ),
        )
        await self._check_streak_upgrade(interaction.guild_id, member.id, tier_cfg, new_streak)

        await interaction.followup.send(
            embed=discord.Embed(
                description=(
                    f"ให้สิทธิ์ {tier_cfg['name']} แก่ {member.mention} แล้ว "
                    f"หมดอายุ {fmt_datetime(new_expiry_local, self.cfg.tz)}{role_note}"
                ),
                color=COLOR_OK,
            ),
            ephemeral=True,
        )


async def setup(bot: commands.Bot) -> None:
    bot.add_dynamic_items(VipUpgradeDecisionButton)
    await bot.add_cog(VipCog(bot))
