"""ส่วนงานรีเซปชั่น: แผงควบคุมแอดมิน, เปิดบิล, ต่อเวลา, พนักงานรับงาน"""
from __future__ import annotations

import datetime as dt
import logging
import re

import discord
from discord import app_commands
from discord.ext import commands

from core.embeds import COLOR_DANGER, COLOR_INFO, COLOR_MAIN, COLOR_OK, COLOR_WARN, job_embed
from core.pricing import quote_services, reserve_quota_for_job, split_revenue
from core.utils import (
    TimeParseError,
    discord_ts,
    from_iso,
    is_admin,
    money,
    now_utc,
    parse_start_time,
    purge_old_panels,
    send_dm,
    to_iso,
)
from core.vip_logic import active_tier, cycle_month_key

log = logging.getLogger("olp.reception")

ACTIVE_STATUSES = ("PENDING_STAFF", "ACCEPTED", "SLIP_PENDING", "PAID")


# ------------------------------------------------------------ ปุ่มรับงาน
class JobAcceptButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"olp:job_accept:(?P<job_id>\d+)",
):
    def __init__(self, job_id: int) -> None:
        self.job_id = job_id
        super().__init__(
            discord.ui.Button(
                label="รับงาน",
                emoji="✅",
                style=discord.ButtonStyle.success,
                custom_id=f"olp:job_accept:{job_id}",
            )
        )

    @classmethod
    async def from_custom_id(cls, interaction, item, match: re.Match[str]):
        return cls(int(match["job_id"]))

    async def callback(self, interaction: discord.Interaction) -> None:  # type: ignore[override]
        cog: ReceptionCog = interaction.client.get_cog("ReceptionCog")  # type: ignore[assignment]
        await cog.staff_accept(interaction, self.job_id)


class JobRejectButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"olp:job_reject:(?P<job_id>\d+)",
):
    """ปุ่มให้พนักงานปฏิเสธงาน เผื่อกรณีแอดมินคีย์บิลผิด (ใช้ได้เฉพาะก่อนกดรับงาน)"""

    def __init__(self, job_id: int) -> None:
        self.job_id = job_id
        super().__init__(
            discord.ui.Button(
                label="ปฏิเสธ (คีย์ผิด)",
                emoji="❌",
                style=discord.ButtonStyle.danger,
                custom_id=f"olp:job_reject:{job_id}",
            )
        )

    @classmethod
    async def from_custom_id(cls, interaction, item, match: re.Match[str]):
        return cls(int(match["job_id"]))

    async def callback(self, interaction: discord.Interaction) -> None:  # type: ignore[override]
        cog: ReceptionCog = interaction.client.get_cog("ReceptionCog")  # type: ignore[assignment]
        await cog.staff_reject_prompt(interaction, self.job_id)


class JobRejectReasonModal(discord.ui.Modal, title="ปฏิเสธงาน"):
    reason = discord.ui.TextInput(
        label="เหตุผล (ถ้ามี)",
        placeholder="เช่น คีย์ผิดคน / ผิดบริการ / ผิดเวลา",
        style=discord.TextStyle.paragraph,
        required=False,
        max_length=300,
    )

    def __init__(self, job_id: int) -> None:
        super().__init__()
        self.job_id = job_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        cog: ReceptionCog = interaction.client.get_cog("ReceptionCog")  # type: ignore[assignment]
        await cog.staff_reject(interaction, self.job_id, str(self.reason).strip())


def accept_view(job_id: int) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(JobAcceptButton(job_id))
    view.add_item(JobRejectButton(job_id))
    return view


# ------------------------------------------------------------ Modal เวลา
class BillDetailModal(discord.ui.Modal, title="รายละเอียดบิล"):
    start_time = discord.ui.TextInput(
        label="เวลาเริ่มงาน",
        placeholder="ตอนนี้ / +15 / 20:30 / 05/09 20:30",
        default="ตอนนี้",
        required=True,
        max_length=32,
    )
    note = discord.ui.TextInput(
        label="หมายเหตุ",
        style=discord.TextStyle.paragraph,
        required=False,
        max_length=400,
    )

    def __init__(self, wizard: "OpenBillWizard") -> None:
        super().__init__()
        self.wizard = wizard

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.wizard.submit(interaction, str(self.start_time), str(self.note))


# ---------------------------------------------------------- Wizard เปิดบิล
class OpenBillWizard(discord.ui.View):
    """แผงเลือกข้อมูลเปิดบิลแบบขั้นตอนเดียว (ephemeral)"""

    def __init__(self, cog: "ReceptionCog", opener: discord.Member) -> None:
        super().__init__(timeout=600)
        self.cog = cog
        self.cfg = cog.cfg
        self.opener = opener
        self.customer_id: int | None = None
        self.staff_id: int | None = None
        self.service_keys: list[str] = []
        self.room_key: str | None = None

        self.customer_select = discord.ui.UserSelect(
            placeholder="👤 เลือกลูกค้า", min_values=1, max_values=1, row=0
        )
        self.customer_select.callback = self._on_customer
        self.add_item(self.customer_select)

        self.staff_select = discord.ui.UserSelect(
            placeholder="💃 เลือกพนักงาน / โฮสต์", min_values=1, max_values=1, row=1
        )
        self.staff_select.callback = self._on_staff
        self.add_item(self.staff_select)

        services = self.cfg.bookable_services()
        self.service_select = discord.ui.Select(
            placeholder="🛎️ เลือกบริการ (เลือกได้มากกว่า 1)",
            min_values=1,
            max_values=max(1, min(len(services), 25)),
            row=2,
            options=[
                discord.SelectOption(
                    label=svc["name"],
                    value=svc["key"],
                    emoji=svc.get("emoji"),
                    description=(
                        f"ปกติ {svc['pricing'].get('normal', 0):,.0f} บาท / "
                        f"{svc.get('duration_minutes', 60)} นาที"
                    ),
                )
                for svc in services[:25]
            ],
        )
        self.service_select.callback = self._on_services
        self.add_item(self.service_select)

        rooms = self.cfg.rooms
        self.room_select = discord.ui.Select(
            placeholder="🚪 เลือกห้อง (เฉพาะบริการที่ต้องใช้ห้อง)",
            min_values=1,
            max_values=1,
            row=3,
            options=[
                discord.SelectOption(label=room["name"], value=room["key"]) for room in rooms[:25]
            ]
            or [discord.SelectOption(label="ไม่มีห้องใน config", value="none")],
        )
        self.room_select.callback = self._on_room
        self.add_item(self.room_select)

    # ------------------------------------------------------------ callbacks
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.opener.id:
            await interaction.response.send_message("แผงนี้ของแอดมินคนอื่นค่ะ", ephemeral=True)
            return False
        return True

    async def _on_customer(self, interaction: discord.Interaction) -> None:
        self.customer_id = self.customer_select.values[0].id
        await self._refresh(interaction)

    async def _on_staff(self, interaction: discord.Interaction) -> None:
        self.staff_id = self.staff_select.values[0].id
        await self._refresh(interaction)

    async def _on_services(self, interaction: discord.Interaction) -> None:
        self.service_keys = list(self.service_select.values)
        await self._refresh(interaction)

    async def _on_room(self, interaction: discord.Interaction) -> None:
        self.room_key = self.room_select.values[0]
        await self._refresh(interaction)

    @discord.ui.button(
        label="กรอกเวลา & ยืนยันเปิดบิล", emoji="🧾", style=discord.ButtonStyle.primary, row=4
    )
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        problem = self._validate()
        if problem:
            await interaction.response.send_message(problem, ephemeral=True)
            return
        await interaction.response.send_modal(BillDetailModal(self))

    # -------------------------------------------------------------- helpers
    def _requires_room(self) -> bool:
        return any(
            (self.cfg.service(key) or {}).get("require_room") for key in self.service_keys
        )

    def _validate(self) -> str | None:
        if not self.customer_id:
            return "ยังไม่ได้เลือก **ลูกค้า** ค่ะ"
        if not self.staff_id:
            return "ยังไม่ได้เลือก **พนักงาน** ค่ะ"
        if not self.service_keys:
            return "ยังไม่ได้เลือก **บริการ** ค่ะ"
        if self._requires_room() and not self.room_key:
            return "บริการที่เลือกต้องระบุ **ห้อง** ด้วยค่ะ"
        return None

    async def _customer_tier(self) -> str | None:
        if not self.customer_id:
            return None
        return await active_tier(self.cog.db, self.customer_id, dt.datetime.now(self.cfg.tz))

    async def summary_embed(self) -> discord.Embed:
        embed = discord.Embed(
            title="🧾 เปิดบิลใหม่",
            description="เลือกข้อมูลให้ครบ แล้วกดปุ่ม **กรอกเวลา & ยืนยันเปิดบิล**",
            color=COLOR_MAIN,
        )
        embed.add_field(
            name="ลูกค้า",
            value=f"<@{self.customer_id}>" if self.customer_id else "*ยังไม่เลือก*",
            inline=True,
        )
        embed.add_field(
            name="พนักงาน",
            value=f"<@{self.staff_id}>" if self.staff_id else "*ยังไม่เลือก*",
            inline=True,
        )
        embed.add_field(
            name="ห้อง",
            value=self.cfg.room_name(self.room_key) if self.room_key else "*ยังไม่เลือก*",
            inline=True,
        )
        embed.add_field(
            name="บริการ",
            value=self.cfg.service_names(self.service_keys) if self.service_keys else "*ยังไม่เลือก*",
            inline=False,
        )

        if self.service_keys and self.customer_id:
            tier = await self._customer_tier()
            quote = await quote_services(
                self.cfg, self.cog.db, self.service_keys, customer_id=self.customer_id, tier=tier
            )
            embed.add_field(
                name="ราคาโดยประมาณ",
                value=(
                    f"{quote.breakdown}\n"
                    f"**รวม {money(quote.total_price)}** · {quote.duration_minutes} นาที"
                    + (f"\n{self.cfg.vip_tier_name(tier)} — คิดราคา/สิทธิ์ตามระดับอัตโนมัติ" if tier else "")
                ),
                inline=False,
            )
        if self._requires_room() and not self.room_key:
            embed.set_footer(text="⚠️ บริการที่เลือกต้องระบุห้อง")
        return embed

    async def _refresh(self, interaction: discord.Interaction) -> None:
        await interaction.response.edit_message(embed=await self.summary_embed(), view=self)

    # --------------------------------------------------------------- submit
    async def submit(self, interaction: discord.Interaction, raw_time: str, note: str) -> None:
        problem = self._validate()
        if problem:
            await interaction.response.send_message(problem, ephemeral=True)
            return

        try:
            start = parse_start_time(raw_time, self.cfg.tz)
        except TimeParseError as exc:
            await interaction.response.send_message(f"⚠️ {exc}", ephemeral=True)
            return

        await interaction.response.defer()
        job_id = await self.cog.create_job(
            guild=interaction.guild,
            opener=interaction.user,
            customer_id=self.customer_id,
            staff_id=self.staff_id,
            service_keys=self.service_keys,
            room_key=self.room_key if self._requires_room() else None,
            note=note.strip(),
            start=start,
        )
        job = await self.cog.db.get_job(job_id)

        self.stop()
        await interaction.edit_original_response(
            embed=job_embed(self.cfg, job, title="✅ เปิดบิลเรียบร้อย", color=COLOR_OK),
            view=None,
        )


# ---------------------------------------------------------- Wizard ต่อเวลา
class ExtendWizard(discord.ui.View):
    def __init__(self, cog: "ReceptionCog", opener: discord.Member, jobs: list[dict]) -> None:
        super().__init__(timeout=600)
        self.cog = cog
        self.cfg = cog.cfg
        self.opener = opener
        self.jobs = {job["id"]: job for job in jobs}
        self.job_id: int | None = None
        self.service_keys: list[str] = []

        self.job_select = discord.ui.Select(
            placeholder="🧾 เลือกบิลที่ต้องการต่อเวลา",
            row=0,
            options=[
                discord.SelectOption(
                    label=f"บิล #{job['id']} · {self.cfg.service_names(job['services'])[:60]}",
                    value=str(job["id"]),
                    description=(
                        f"จบ {from_iso(job['end_time']).astimezone(self.cfg.tz):%d/%m %H:%M}"
                    ),
                )
                for job in jobs[:25]
            ],
        )
        self.job_select.callback = self._on_job
        self.add_item(self.job_select)

        extends = self.cfg.extend_services()
        self.service_select = discord.ui.Select(
            placeholder="⏱️ เลือกแพ็กเกจต่อเวลา",
            min_values=1,
            max_values=max(1, min(len(extends), 25)),
            row=1,
            options=[
                discord.SelectOption(
                    label=svc["name"],
                    value=svc["key"],
                    emoji=svc.get("emoji"),
                    description=(
                        f"ปกติ {svc['pricing'].get('normal', 0):,.0f} บาท / "
                        f"{svc.get('duration_minutes', 30)} นาที"
                    ),
                )
                for svc in extends[:25]
            ]
            or [discord.SelectOption(label="ยังไม่ได้ตั้งค่าแพ็กเกจต่อเวลา", value="none")],
        )
        self.service_select.callback = self._on_services
        self.add_item(self.service_select)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.opener.id:
            await interaction.response.send_message("แผงนี้ของแอดมินคนอื่นค่ะ", ephemeral=True)
            return False
        return True

    async def _on_job(self, interaction: discord.Interaction) -> None:
        self.job_id = int(self.job_select.values[0])
        await self._refresh(interaction)

    async def _on_services(self, interaction: discord.Interaction) -> None:
        self.service_keys = [v for v in self.service_select.values if v != "none"]
        await self._refresh(interaction)

    async def summary_embed(self) -> discord.Embed:
        embed = discord.Embed(title="⏱️ ต่อเวลา (EXTEND)", color=COLOR_MAIN)
        if self.job_id:
            job = self.jobs[self.job_id]
            end = from_iso(job["end_time"])
            embed.add_field(
                name="บิลเดิม",
                value=(
                    f"`#{job['id']}` · ลูกค้า <@{job['customer_id']}> · พนักงาน <@{job['staff_id']}>\n"
                    f"เวลาจบปัจจุบัน: {discord_ts(end)}"
                ),
                inline=False,
            )
        else:
            embed.add_field(name="บิลเดิม", value="*ยังไม่เลือก*", inline=False)

        if self.service_keys and self.job_id:
            job = self.jobs[self.job_id]
            tier = await active_tier(self.cog.db, job["customer_id"], dt.datetime.now(self.cfg.tz))
            quote = await quote_services(
                self.cfg, self.cog.db, self.service_keys, customer_id=job["customer_id"], tier=tier
            )
            embed.add_field(
                name="แพ็กเกจต่อเวลา",
                value=f"{quote.breakdown}\n**รวม {money(quote.total_price)}** · +{quote.duration_minutes} นาที",
                inline=False,
            )
        return embed

    async def _refresh(self, interaction: discord.Interaction) -> None:
        await interaction.response.edit_message(embed=await self.summary_embed(), view=self)

    @discord.ui.button(label="ยืนยันต่อเวลา", emoji="✅", style=discord.ButtonStyle.primary, row=2)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not self.job_id:
            await interaction.response.send_message("ยังไม่ได้เลือกบิลค่ะ", ephemeral=True)
            return
        if not self.service_keys:
            await interaction.response.send_message("ยังไม่ได้เลือกแพ็กเกจต่อเวลาค่ะ", ephemeral=True)
            return

        await interaction.response.defer()
        extend_id = await self.cog.create_extend(
            interaction.guild, interaction.user, self.job_id, self.service_keys
        )
        extend_job = await self.cog.db.get_job(extend_id)
        parent = await self.cog.db.get_job(self.job_id)

        self.stop()
        embed = job_embed(self.cfg, extend_job, title="✅ เปิดบิลต่อเวลาแล้ว", color=COLOR_OK)
        embed.add_field(
            name="เวลาจบใหม่ของบิลเดิม",
            value=f"บิล `#{parent['id']}` → {discord_ts(from_iso(parent['end_time']))}",
            inline=False,
        )
        await interaction.edit_original_response(embed=embed, view=None)


# -------------------------------------------------------------- แผงควบคุม
class ReceptionPanel(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(
        label="เปิดบิลใหม่",
        emoji="🧾",
        style=discord.ButtonStyle.primary,
        custom_id="olp:panel:open_bill",
    )
    async def open_bill(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        cog: ReceptionCog = interaction.client.get_cog("ReceptionCog")  # type: ignore[assignment]
        await cog.open_bill_panel(interaction)

    @discord.ui.button(
        label="ต่อเวลา",
        emoji="⏱️",
        style=discord.ButtonStyle.secondary,
        custom_id="olp:panel:extend",
    )
    async def extend(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        cog: ReceptionCog = interaction.client.get_cog("ReceptionCog")  # type: ignore[assignment]
        await cog.open_extend_panel(interaction)

    @discord.ui.button(
        label="งานที่กำลังดำเนินอยู่",
        emoji="📋",
        style=discord.ButtonStyle.secondary,
        custom_id="olp:panel:active",
    )
    async def active(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        cog: ReceptionCog = interaction.client.get_cog("ReceptionCog")  # type: ignore[assignment]
        await cog.show_active_jobs(interaction)


# --------------------------------------------------------------------- cog
class ReceptionCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.cfg = bot.cfg
        self.db = bot.db

    # ------------------------------------------------------------- panels
    async def open_bill_panel(self, interaction: discord.Interaction) -> None:
        if not self._admin_guard(interaction):
            await interaction.response.send_message("เฉพาะแอดมินเท่านั้นค่ะ", ephemeral=True)
            return
        wizard = OpenBillWizard(self, interaction.user)
        await interaction.response.send_message(
            embed=await wizard.summary_embed(), view=wizard, ephemeral=True
        )

    async def open_extend_panel(self, interaction: discord.Interaction) -> None:
        if not self._admin_guard(interaction):
            await interaction.response.send_message("เฉพาะแอดมินเท่านั้นค่ะ", ephemeral=True)
            return
        now = now_utc()
        jobs = [
            job
            for job in await self.db.jobs_by_status(["ACCEPTED", "SLIP_PENDING", "PAID"])
            if job["job_type"] == "NORMAL" and from_iso(job["end_time"]) > now
        ]
        if not jobs:
            await interaction.response.send_message(
                "ตอนนี้ยังไม่มีบิลที่กำลังดำเนินอยู่ค่ะ", ephemeral=True
            )
            return
        wizard = ExtendWizard(self, interaction.user, jobs)
        await interaction.response.send_message(
            embed=await wizard.summary_embed(), view=wizard, ephemeral=True
        )

    async def show_active_jobs(self, interaction: discord.Interaction) -> None:
        if not self._admin_guard(interaction):
            await interaction.response.send_message("เฉพาะแอดมินเท่านั้นค่ะ", ephemeral=True)
            return
        now = now_utc()
        jobs = [
            job
            for job in await self.db.jobs_by_status(list(ACTIVE_STATUSES))
            if from_iso(job["end_time"]) > now
        ]
        if not jobs:
            await interaction.response.send_message("ยังไม่มีงานค้างอยู่ค่ะ", ephemeral=True)
            return

        embed = discord.Embed(title="📋 งานที่กำลังดำเนินอยู่", color=COLOR_INFO)
        for job in jobs[:20]:
            embed.add_field(
                name=f"บิล #{job['id']} · {job['status']}",
                value=(
                    f"<@{job['customer_id']}> ↔ <@{job['staff_id']}>\n"
                    f"{self.cfg.service_names(job['services'])} · {money(job['total_price'])}\n"
                    f"จบ {discord_ts(from_iso(job['end_time']))}"
                ),
                inline=False,
            )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    def _admin_guard(self, interaction: discord.Interaction) -> bool:
        return is_admin(interaction.user, self.cfg.admin_role_id)

    # ---------------------------------------------------------- สร้างบิล
    async def create_job(
        self,
        *,
        guild: discord.Guild,
        opener: discord.abc.User,
        customer_id: int,
        staff_id: int,
        service_keys: list[str],
        room_key: str | None,
        note: str,
        start: dt.datetime,
        job_type: str = "NORMAL",
        parent_job_id: int | None = None,
    ) -> int:
        now_local = dt.datetime.now(self.cfg.tz)
        tier = await active_tier(self.db, customer_id, now_local)
        quote = await quote_services(
            self.cfg, self.db, service_keys, customer_id=customer_id, tier=tier, now_local=now_local
        )
        staff_share, shop_share = split_revenue(self.cfg, staff_id, quote.total_price)
        end = start + dt.timedelta(minutes=quote.duration_minutes)
        cycle = cycle_month_key(now_local)

        job_id = await self.db.create_job(
            guild_id=guild.id,
            job_type=job_type,
            parent_job_id=parent_job_id,
            customer_id=customer_id,
            staff_id=staff_id,
            services=service_keys,
            room=room_key,
            note=note or None,
            start_time=to_iso(start),
            end_time=to_iso(end),
            duration_minutes=quote.duration_minutes,
            vip_tier=tier,
            quota_services=quote.quota_services,
            quota_cycle=cycle if quote.quota_services else None,
            total_price=quote.total_price,
            staff_share=staff_share,
            shop_share=shop_share,
            status="PENDING_STAFF",
            opened_by=opener.id,
            created_at=to_iso(now_utc()),
        )
        await reserve_quota_for_job(self.db, customer_id, quote, cycle)

        job = await self.db.get_job(job_id)
        embed = job_embed(self.cfg, job, title="🔔 มีงานใหม่เข้ามา", color=COLOR_WARN)
        embed.add_field(name="ส่วนแบ่งของคุณ", value=money(staff_share), inline=True)
        sent = await send_dm(self.bot, staff_id, embed=embed, view=accept_view(job_id))
        if sent is None:
            payments = self.bot.get_cog("PaymentsCog")
            await payments.notify_admin_text(
                f"⚠️ ส่ง DM แจ้งงานบิล `#{job_id}` ถึงพนักงาน <@{staff_id}> ไม่สำเร็จ (ปิด DM อยู่)"
            )
        return job_id

    async def create_extend(
        self,
        guild: discord.Guild,
        opener: discord.abc.User,
        parent_id: int,
        service_keys: list[str],
    ) -> int:
        parent = await self.db.get_job(parent_id)
        now_local = dt.datetime.now(self.cfg.tz)
        tier = await active_tier(self.db, parent["customer_id"], now_local)
        quote = await quote_services(
            self.cfg,
            self.db,
            service_keys,
            customer_id=parent["customer_id"],
            tier=tier,
            now_local=now_local,
        )
        staff_share, shop_share = split_revenue(self.cfg, parent["staff_id"], quote.total_price)
        cycle = cycle_month_key(now_local)

        old_end = from_iso(parent["end_time"])
        new_end = old_end + dt.timedelta(minutes=quote.duration_minutes)

        extend_id = await self.db.create_job(
            guild_id=guild.id,
            job_type="EXTEND",
            parent_job_id=parent_id,
            customer_id=parent["customer_id"],
            staff_id=parent["staff_id"],
            services=service_keys,
            room=parent.get("room"),
            note=f"ต่อเวลาจากบิล #{parent_id}",
            start_time=parent["end_time"],
            end_time=to_iso(new_end),
            duration_minutes=quote.duration_minutes,
            vip_tier=tier,
            quota_services=quote.quota_services,
            quota_cycle=cycle if quote.quota_services else None,
            total_price=quote.total_price,
            staff_share=staff_share,
            shop_share=shop_share,
            status="ACCEPTED",
            opened_by=opener.id,
            created_at=to_iso(now_utc()),
            accepted_at=to_iso(now_utc()),
        )
        await reserve_quota_for_job(self.db, parent["customer_id"], quote, cycle)

        # ขยายเวลาจบของบิลเดิม และรีเซ็ตแจ้งเตือนก่อนจบงานให้คำนวณใหม่
        await self.db.update_job(
            parent_id,
            end_time=to_iso(new_end),
            duration_minutes=parent["duration_minutes"] + quote.duration_minutes,
            notified_end=0,
            review_sent=0,
        )

        payments = self.bot.get_cog("PaymentsCog")
        extend_job = await self.db.get_job(extend_id)
        await payments.start_job_payment(extend_job)
        await send_dm(
            self.bot,
            parent["staff_id"],
            embed=discord.Embed(
                title="⏱️ ลูกค้าต่อเวลา",
                description=(
                    f"บิล `#{parent_id}` ต่อเวลา +{quote.duration_minutes} นาที\n"
                    f"เวลาจบใหม่: {discord_ts(new_end)}"
                ),
                color=COLOR_INFO,
            ),
        )
        return extend_id

    # ------------------------------------------------------- พนักงานรับงาน
    async def staff_accept(self, interaction: discord.Interaction, job_id: int) -> None:
        job = await self.db.get_job(job_id)
        if job is None:
            await interaction.response.send_message("ไม่พบบิลนี้ค่ะ", ephemeral=True)
            return
        if interaction.user.id != job["staff_id"]:
            await interaction.response.send_message("ปุ่มนี้สำหรับพนักงานที่ถูกจ่ายงานค่ะ", ephemeral=True)
            return
        if job["status"] != "PENDING_STAFF":
            await interaction.response.send_message("บิลนี้ถูกรับงานไปแล้วค่ะ", ephemeral=True)
            return

        await interaction.response.defer()
        await self.db.update_job(job_id, status="ACCEPTED", accepted_at=to_iso(now_utc()))
        job = await self.db.get_job(job_id)

        await interaction.edit_original_response(
            embed=job_embed(self.cfg, job, title="✅ รับงานแล้ว", color=COLOR_OK), view=None
        )

        payments = self.bot.get_cog("PaymentsCog")
        await payments.start_job_payment(job)
        await payments.notify_admin_text(
            f"✅ <@{job['staff_id']}> รับงานบิล `#{job_id}` แล้ว — ส่งยอดชำระให้ลูกค้าเรียบร้อย"
        )

    async def staff_reject_prompt(self, interaction: discord.Interaction, job_id: int) -> None:
        job = await self.db.get_job(job_id)
        if job is None:
            await interaction.response.send_message("ไม่พบบิลนี้ค่ะ", ephemeral=True)
            return
        if interaction.user.id != job["staff_id"]:
            await interaction.response.send_message("ปุ่มนี้สำหรับพนักงานที่ถูกจ่ายงานค่ะ", ephemeral=True)
            return
        if job["status"] != "PENDING_STAFF":
            await interaction.response.send_message("บิลนี้ถูกดำเนินการไปแล้วค่ะ", ephemeral=True)
            return
        await interaction.response.send_modal(JobRejectReasonModal(job_id))

    async def staff_reject(self, interaction: discord.Interaction, job_id: int, reason: str) -> None:
        job = await self.db.get_job(job_id)
        if job is None or interaction.user.id != job["staff_id"] or job["status"] != "PENDING_STAFF":
            await interaction.response.send_message("บิลนี้ถูกดำเนินการไปแล้วค่ะ", ephemeral=True)
            return

        await interaction.response.defer()
        payments = self.bot.get_cog("PaymentsCog")
        await payments.reject_job_by_staff(job, interaction.user, reason)

        job = await self.db.get_job(job_id)
        embed = job_embed(self.cfg, job, title="❌ ปฏิเสธงานแล้ว", color=COLOR_DANGER)
        if reason:
            embed.add_field(name="เหตุผล", value=reason, inline=False)
        await interaction.edit_original_response(embed=embed, view=None)

    # ------------------------------------------------------ คำสั่ง slash
    panel_group = app_commands.Group(name="panel", description="โพสต์แผงควบคุมของบอท")

    @panel_group.command(name="reception", description="โพสต์แผงควบคุมรีเซปชั่น (สำหรับแอดมิน)")
    async def panel_reception(self, interaction: discord.Interaction) -> None:
        if not self._admin_guard(interaction):
            await interaction.response.send_message("เฉพาะแอดมินเท่านั้นค่ะ", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        removed = await purge_old_panels(interaction.channel, self.bot.user.id, "olp:panel:")

        embed = discord.Embed(
            title="🎛️ Reception Control Panel",
            description=(
                "แผงควบคุมสำหรับแอดมิน / พนักงานต้อนรับ\n\n"
                "🧾 **เปิดบิลใหม่** — เลือกลูกค้า พนักงาน บริการ ห้อง แล้วคำนวณราคาอัตโนมัติ\n"
                "⏱️ **ต่อเวลา** — เปิดบิลต่อเวลาและขยายเวลาจบงานของบิลเดิม\n"
                "📋 **งานที่กำลังดำเนินอยู่** — ดูงานที่ยังไม่จบเวลา"
            ),
            color=COLOR_MAIN,
        )
        await interaction.channel.send(embed=embed, view=ReceptionPanel())

        note = f" (ลบแผงเก่าออก {removed} อัน)" if removed else ""
        await interaction.followup.send(f"โพสต์แผงควบคุมแล้วค่ะ{note}", ephemeral=True)

    bill_group = app_commands.Group(name="bill", description="จัดการบิล")

    @bill_group.command(name="info", description="ดูรายละเอียดบิลตามเลขที่")
    @app_commands.describe(job_id="เลขที่บิล")
    async def bill_info(self, interaction: discord.Interaction, job_id: int) -> None:
        if not self._admin_guard(interaction):
            await interaction.response.send_message("เฉพาะแอดมินเท่านั้นค่ะ", ephemeral=True)
            return
        job = await self.db.get_job(job_id)
        if job is None:
            await interaction.response.send_message("ไม่พบบิลนี้ค่ะ", ephemeral=True)
            return
        await interaction.response.send_message(
            embed=job_embed(self.cfg, job, title=f"🧾 บิล #{job_id}"), ephemeral=True
        )

    @bill_group.command(name="cancel", description="ยกเลิกบิล (ไม่บันทึกลง Google Sheets)")
    @app_commands.describe(job_id="เลขที่บิล", reason="เหตุผล (ถ้ามี)")
    async def bill_cancel(
        self, interaction: discord.Interaction, job_id: int, reason: str | None = None
    ) -> None:
        if not self._admin_guard(interaction):
            await interaction.response.send_message("เฉพาะแอดมินเท่านั้นค่ะ", ephemeral=True)
            return
        payments = self.bot.get_cog("PaymentsCog")
        ok, msg = await payments.cancel_job(job_id, interaction.user, reason)
        await interaction.response.send_message(
            embed=discord.Embed(description=msg, color=COLOR_OK if ok else COLOR_DANGER),
            ephemeral=True,
        )

    @bill_group.command(name="paid", description="ทำเครื่องหมายว่าชำระเงินแล้วด้วยมือ")
    @app_commands.describe(job_id="เลขที่บิล")
    async def bill_paid(self, interaction: discord.Interaction, job_id: int) -> None:
        if not self._admin_guard(interaction):
            await interaction.response.send_message("เฉพาะแอดมินเท่านั้นค่ะ", ephemeral=True)
            return
        payments = self.bot.get_cog("PaymentsCog")
        ok, msg = await payments.mark_job_paid(job_id, interaction.user)
        await interaction.response.send_message(
            embed=discord.Embed(description=msg, color=COLOR_OK if ok else COLOR_DANGER),
            ephemeral=True,
        )


async def setup(bot: commands.Bot) -> None:
    bot.add_dynamic_items(JobAcceptButton, JobRejectButton)
    bot.add_view(ReceptionPanel())
    await bot.add_cog(ReceptionCog(bot))
