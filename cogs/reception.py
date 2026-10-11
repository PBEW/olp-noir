"""ส่วนงานรีเซปชั่น: แผงควบคุมแอดมิน, เปิดบิล, ต่อเวลา, พนักงานรับงาน"""
from __future__ import annotations

import datetime as dt
import json
import logging
import re

import discord
from discord import app_commands
from discord.ext import commands

from core.embeds import COLOR_DANGER, COLOR_INFO, COLOR_MAIN, COLOR_OK, COLOR_WARN, job_embed, panel_embed
from core.pricing import job_staff_ids, job_staff_mentions, quote_services, reserve_quota_for_job, split_quote_multi
from core.utils import (
    TimeParseError,
    discord_ts,
    fmt_time,
    from_iso,
    NOT_RECEPTION,
    is_reception,
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
    """แผงเลือกข้อมูลเปิดบิลแบบขั้นตอนเดียว (ephemeral)
    เลือกลูกค้าได้หลายคน (แยกบิลให้คนละใบ จ่ายเงินแยกกัน) และพนักงานได้หลายคน (ใช้ร่วมกันทุกบิล)"""

    MAX_CUSTOMERS = 10
    MAX_STAFF = 10

    def __init__(self, cog: "ReceptionCog", opener: discord.Member, working: list[tuple[discord.Member, str]]) -> None:
        super().__init__(timeout=600)
        self.cog = cog
        self.cfg = cog.cfg
        self.opener = opener
        self.customer_ids: list[int] = []
        self.staff_ids: list[int] = []
        self.service_keys: list[str] = []
        self.room_key: str | None = None

        self.customer_select = discord.ui.UserSelect(
            placeholder=f"👤 เลือกลูกค้า (ได้สูงสุด {self.MAX_CUSTOMERS} คน · แยกบิลคนละใบ)",
            min_values=1,
            max_values=self.MAX_CUSTOMERS,
            row=0,
        )
        self.customer_select.callback = self._on_customer
        self.add_item(self.customer_select)

        # เลือกได้เฉพาะพนักงานที่กดเข้างานวันนี้แล้ว · เลือกได้หลายคน (ทำงานร่วมกัน)
        self.staff_select = discord.ui.Select(
            placeholder=f"💃 เลือกพนักงาน (เข้างานวันนี้ {len(working)} คน · เลือกได้หลายคน)",
            min_values=1,
            max_values=max(1, min(len(working), self.MAX_STAFF, 25)),
            row=1,
            options=[
                discord.SelectOption(
                    label=m.display_name[:100], value=str(m.id), emoji="🟢", description=f"เข้างาน {since} น."
                )
                for m, since in working[:25]
            ],
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
        self.customer_ids = [u.id for u in self.customer_select.values if not u.bot]
        await self._refresh(interaction)

    async def _on_staff(self, interaction: discord.Interaction) -> None:
        self.staff_ids = [int(v) if isinstance(v, str) else v.id for v in self.staff_select.values]
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
        if not self.customer_ids:
            return "ยังไม่ได้เลือก **ลูกค้า** ค่ะ"
        if not self.staff_ids:
            return "ยังไม่ได้เลือก **พนักงาน** ค่ะ"
        if not self.service_keys:
            return "ยังไม่ได้เลือก **บริการ** ค่ะ"
        if self._requires_room() and not self.room_key:
            return "บริการที่เลือกต้องระบุ **ห้อง** ด้วยค่ะ"
        return None

    async def summary_embed(self) -> discord.Embed:
        embed = discord.Embed(
            title="🧾 เปิดบิลใหม่",
            description=(
                "เลือกข้อมูลให้ครบ แล้วกดปุ่ม **กรอกเวลา & ยืนยันเปิดบิล**\n"
                "-# ลูกค้าหลายคน = แยกบิลคนละใบ (ราคา/สิทธิ์ VIP ของใครของมัน) · พนักงานหลายคน = ทำงานร่วมกันทุกบิล"
            ),
            color=COLOR_MAIN,
        )
        embed.add_field(
            name=f"ลูกค้า ({len(self.customer_ids)})" if self.customer_ids else "ลูกค้า",
            value=", ".join(f"<@{c}>" for c in self.customer_ids) or "*ยังไม่เลือก*",
            inline=True,
        )
        embed.add_field(
            name=f"พนักงาน ({len(self.staff_ids)})" if self.staff_ids else "พนักงาน",
            value=", ".join(f"<@{s}>" for s in self.staff_ids) or "*ยังไม่เลือก*",
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

        if self.service_keys and self.customer_ids and self.staff_ids:
            now_local = dt.datetime.now(self.cfg.tz)
            total_in = total_shop = 0.0
            payout: dict[int, float] = {sid: 0.0 for sid in self.staff_ids}
            lines = []
            for cid in self.customer_ids:
                tier = await active_tier(self.cog.db, cid, now_local)
                quote = await quote_services(
                    self.cfg, self.cog.db, self.service_keys, customer_id=cid, tier=tier, now_local=now_local
                )
                shares, shop = split_quote_multi(self.cfg, self.staff_ids, quote)
                total_in += quote.total_price
                total_shop += shop
                for sid, amount in shares.items():
                    payout[sid] += amount
                tier_note = f" · {self.cfg.vip_tier_name(tier)}" if tier else ""
                lines.append(f"<@{cid}> — **{money(quote.total_price)}**{tier_note}")
            embed.add_field(name="💵 ยอดแต่ละบิล", value="\n".join(lines)[:1024], inline=False)
            embed.add_field(name="รายรับรวม (In)", value=money(total_in), inline=True)
            embed.add_field(name="จ่ายพนักงาน (Out)", value=money(sum(payout.values())), inline=True)
            embed.add_field(name="ร้านได้สุทธิ", value=f"**{money(total_shop)}**", inline=True)
            if len(self.staff_ids) > 1:
                embed.add_field(
                    name="💃 พนักงานได้คนละ",
                    value="\n".join(f"<@{sid}> — {money(v)}" for sid, v in payout.items())[:1024],
                    inline=False,
                )
            if total_shop < 0:
                embed.add_field(name="⚠️ ร้านขาดทุน", value="ส่วนพนักงานรวมมากกว่ายอดที่ลูกค้าจ่าย (เช่น ใช้สิทธิ์ฟรี VIP หรือโหมดได้เต็มทุกคน)", inline=False)
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
        job_ids: list[int] = []
        for cid in self.customer_ids:
            job_ids.append(
                await self.cog.create_job(
                    guild=interaction.guild,
                    opener=interaction.user,
                    customer_id=cid,
                    staff_ids=self.staff_ids,
                    service_keys=self.service_keys,
                    room_key=self.room_key if self._requires_room() else None,
                    note=note.strip(),
                    start=start,
                    group_id=job_ids[0] if job_ids else None,
                )
            )
        if len(job_ids) > 1:
            await self.cog.db.update_job(job_ids[0], group_id=job_ids[0])

        self.stop()
        if len(job_ids) == 1:
            job = await self.cog.db.get_job(job_ids[0])
            embed = job_embed(self.cfg, job, title="✅ เปิดบิลแล้ว", color=COLOR_OK)
        else:
            jobs = [await self.cog.db.get_job(j) for j in job_ids]
            embed = discord.Embed(title=f"✅ เปิดบิลกลุ่มแล้ว {len(jobs)} ใบ", color=COLOR_OK)
            embed.description = "\n".join(
                f"`#{j['id']}` <@{j['customer_id']}> · {money(j['total_price'])}" for j in jobs
            )
            embed.add_field(name="พนักงาน", value=job_staff_mentions(jobs[0]), inline=True)
            embed.add_field(name="รายรับรวม", value=money(sum(j["total_price"] for j in jobs)), inline=True)
            embed.add_field(name="ร้านได้สุทธิ", value=money(sum(j["shop_share"] for j in jobs)), inline=True)
        await interaction.edit_original_response(embed=embed, view=None)


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
                    f"`#{job['id']}` · ลูกค้า <@{job['customer_id']}> · พนักงาน {job_staff_mentions(job)}\n"
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

    @discord.ui.button(
        label="ยืนยันชำระเงิน",
        emoji="✅",
        style=discord.ButtonStyle.success,
        custom_id="olp:panel:bill_paid",
        row=1,
    )
    async def bill_paid(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not is_reception(interaction.user, interaction.client.cfg):
            await interaction.response.send_message(NOT_RECEPTION, ephemeral=True)
            return
        await interaction.response.send_modal(BillActionModal("paid"))

    @discord.ui.button(
        label="ยกเลิกบิล",
        emoji="❌",
        style=discord.ButtonStyle.danger,
        custom_id="olp:panel:bill_cancel",
        row=1,
    )
    async def bill_cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not is_reception(interaction.user, interaction.client.cfg):
            await interaction.response.send_message(NOT_RECEPTION, ephemeral=True)
            return
        await interaction.response.send_modal(BillActionModal("cancel"))

    @discord.ui.button(
        label="ดูบิล",
        emoji="🔍",
        style=discord.ButtonStyle.secondary,
        custom_id="olp:panel:bill_info",
        row=1,
    )
    async def bill_info(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not is_reception(interaction.user, interaction.client.cfg):
            await interaction.response.send_message(NOT_RECEPTION, ephemeral=True)
            return
        await interaction.response.send_modal(BillActionModal("info"))

    @discord.ui.button(
        label="คนมาทำงานวันนี้",
        emoji="👥",
        style=discord.ButtonStyle.secondary,
        custom_id="olp:panel:working_today",
        row=2,
    )
    async def working_today(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not is_reception(interaction.user, interaction.client.cfg):
            await interaction.response.send_message(NOT_RECEPTION, ephemeral=True)
            return
        embed = await interaction.client.get_cog("AttendanceCog").on_duty_embed()
        await interaction.response.send_message(embed=embed, ephemeral=True)


class BillActionModal(discord.ui.Modal):
    """ยืนยันชำระด้วยมือ / ยกเลิกบิล / ดูบิล จากแผงรีเซปชั่น (เหมือน /bill paid · /bill cancel · /bill info)"""

    TITLES = {"paid": "✅ ยืนยันชำระเงินด้วยมือ", "cancel": "❌ ยกเลิกบิล", "info": "🔍 ดูรายละเอียดบิล"}

    def __init__(self, action: str) -> None:
        super().__init__(title=self.TITLES[action])
        self.action = action
        self.job_id = discord.ui.TextInput(label="เลขที่บิล", placeholder="เช่น 12", max_length=10)
        self.add_item(self.job_id)
        self.reason: discord.ui.TextInput | None = None
        if action == "cancel":
            self.reason = discord.ui.TextInput(
                label="เหตุผล (ไม่ใส่ก็ได้ ส่งให้ลูกค้า/พนักงาน)", required=False, max_length=200
            )
            self.add_item(self.reason)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        raw = self.job_id.value.strip().lstrip("#")
        if not raw.isdigit():
            await interaction.response.send_message("⚠️ เลขที่บิลต้องเป็นตัวเลขค่ะ", ephemeral=True)
            return
        if not is_reception(interaction.user, interaction.client.cfg):
            await interaction.response.send_message(NOT_RECEPTION, ephemeral=True)
            return
        cfg = interaction.client.cfg
        if self.action == "info":
            job = await interaction.client.db.get_job(int(raw))
            if job is None:
                await interaction.response.send_message("ไม่พบบิลนี้ค่ะ", ephemeral=True)
                return
            await interaction.response.send_message(embed=job_embed(cfg, job, title=f"🧾 บิล #{raw}"), ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)  # ส่ง DM / ลงชีต อาจเกิน 3 วินาที
        payments = interaction.client.get_cog("PaymentsCog")
        if self.action == "paid":
            ok, msg = await payments.mark_job_paid(int(raw), interaction.user)
        else:
            reason = str(self.reason.value).strip() if self.reason else ""
            ok, msg = await payments.cancel_job(int(raw), interaction.user, reason or None)
        await interaction.followup.send(
            embed=discord.Embed(description=msg, color=COLOR_OK if ok else COLOR_DANGER), ephemeral=True
        )
        if ok:
            await payments.notify_admin_text(f"🧾 {interaction.user.mention} (แผงรีเซปชั่น): {msg}", topic="slip")


def reception_panel_embed(guild: discord.Guild | None) -> discord.Embed:
    return panel_embed(
        "🎛️ OLP-Noir · Reception",
        "แผงงานรีเซปชั่น ใช้ได้ทั้ง **แอดมิน** และ **Role รีเซปชั่น** ผลลัพธ์เห็นแค่คนกด",
        [
            ("🧾 งานบิล", [
                ("🧾 เปิดบิลใหม่", "เลือกลูกค้า · พนักงาน · บริการ · ห้อง แล้วคำนวณราคาให้อัตโนมัติ"),
                ("⏱️ ต่อเวลา", "เปิดบิลต่อเวลาและขยายเวลาจบของบิลเดิม"),
                ("📋 งานที่กำลังดำเนินอยู่", "ดูบิลที่ยังไม่จบเวลา"),
            ]),
            ("👥 ทีม", [
                ("👥 คนมาทำงานวันนี้", "ดูว่าพนักงานคนไหนกดเข้างานแล้ว ก่อนเลือกพนักงานเปิดบิล"),
            ]),
            ("💳 การเงิน", [
                ("✅ ยืนยันชำระเงิน", "ลูกค้าจ่ายแล้วแต่ไม่ได้ส่งสลิปในบอท (เหมือน /bill paid)"),
                ("❌ ยกเลิกบิล", "ยกเลิกพร้อมเหตุผล ไม่ลงบัญชี (เหมือน /bill cancel)"),
                ("🔍 ดูบิล", "ดูรายละเอียดบิลจากเลขที่"),
            ]),
            ("📨 งานที่เข้าห้องอัตโนมัติ", (
                "💬 ลูกค้าทัก DM ใหม่ → กด **รับเรื่อง**\n"
                "🔎 สลิปรอยืนยัน → กด **✅ / ❌**\n"
                "⏰ สลิปค้างตรวจนาน · ⚠️ ส่ง DM ยอดชำระไม่สำเร็จ"
            )),
        ],
        footer="ตั้งห้องแยก 💬 ตั๋ว / 🧾 สลิป ได้ที่ ⚙️ ตั้งค่าร้าน → 🧭 ระบบบอท",
        guild=guild,
    )


# --------------------------------------------------------------------- cog
class ReceptionCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.cfg = bot.cfg
        self.db = bot.db

    # ------------------------------------------------------------- panels
    async def open_bill_panel(self, interaction: discord.Interaction) -> None:
        if not self._admin_guard(interaction):
            await interaction.response.send_message(NOT_RECEPTION, ephemeral=True)
            return
        attendance = self.bot.get_cog("AttendanceCog")
        working = []
        for r in await attendance.working_today():
            member = interaction.guild.get_member(r["user_id"]) if interaction.guild else None
            if member is not None:
                working.append((member, fmt_time(from_iso(r["clock_in"]), self.cfg.tz)))
        if not working:
            await interaction.response.send_message(
                "ยังไม่มีพนักงานกดเข้างานวันนี้ค่ะ ให้พนักงานกด 🟢 เข้างานทำงานวันนี้ ใน `/panel_staff` ก่อน "
                "แล้วค่อยเปิดบิลนะคะ",
                ephemeral=True,
            )
            return
        wizard = OpenBillWizard(self, interaction.user, working)
        await interaction.response.send_message(
            embed=await wizard.summary_embed(), view=wizard, ephemeral=True
        )

    async def open_extend_panel(self, interaction: discord.Interaction) -> None:
        if not self._admin_guard(interaction):
            await interaction.response.send_message(NOT_RECEPTION, ephemeral=True)
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
            await interaction.response.send_message(NOT_RECEPTION, ephemeral=True)
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
                    f"<@{job['customer_id']}> ↔ {job_staff_mentions(job)}\n"
                    f"{self.cfg.service_names(job['services'])} · {money(job['total_price'])}\n"
                    f"จบ {discord_ts(from_iso(job['end_time']))}"
                ),
                inline=False,
            )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    def _admin_guard(self, interaction: discord.Interaction) -> bool:
        """แอดมิน หรือ Role รีเซปชั่น"""
        return is_reception(interaction.user, self.cfg)

    # ---------------------------------------------------------- สร้างบิล
    async def create_job(
        self,
        *,
        guild: discord.Guild,
        opener: discord.abc.User,
        customer_id: int,
        staff_ids: list[int],
        service_keys: list[str],
        room_key: str | None,
        note: str,
        start: dt.datetime,
        job_type: str = "NORMAL",
        parent_job_id: int | None = None,
        group_id: int | None = None,
    ) -> int:
        now_local = dt.datetime.now(self.cfg.tz)
        tier = await active_tier(self.db, customer_id, now_local)
        quote = await quote_services(
            self.cfg, self.db, service_keys, customer_id=customer_id, tier=tier, now_local=now_local
        )
        staff_id = staff_ids[0]  # คนแรก = พนักงานหลัก (กดรับงาน)
        shares, shop_share = split_quote_multi(self.cfg, staff_ids, quote)
        staff_share = round(sum(shares.values()), 2)
        end = start + dt.timedelta(minutes=quote.duration_minutes)
        cycle = cycle_month_key(now_local)

        job_id = await self.db.create_job(
            guild_id=guild.id,
            job_type=job_type,
            parent_job_id=parent_job_id,
            customer_id=customer_id,
            staff_id=staff_id,
            staff_shares=json.dumps({str(k): v for k, v in shares.items()}) if len(shares) > 1 else None,
            group_id=group_id,
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
        embed.add_field(name="ส่วนแบ่งของคุณ", value=money(shares[staff_id]), inline=True)
        sent = await send_dm(self.bot, staff_id, embed=embed, view=accept_view(job_id))
        failed = [staff_id] if sent is None else []
        # พนักงานร่วม: แจ้งให้ทราบอย่างเดียว พนักงานหลักเป็นคนกดรับงาน
        for sid in staff_ids[1:]:
            info = job_embed(self.cfg, job, title="🔔 คุณถูกจัดให้ทำงานร่วมในบิลนี้", color=COLOR_INFO)
            info.add_field(name="ส่วนแบ่งของคุณ", value=money(shares[sid]), inline=True)
            info.set_footer(text=f"พนักงานหลักที่กดรับงาน: {staff_id}")
            if await send_dm(self.bot, sid, embed=info) is None:
                failed.append(sid)
        if failed:
            payments = self.bot.get_cog("PaymentsCog")
            await payments.notify_admin_text(
                f"⚠️ ส่ง DM แจ้งงานบิล `#{job_id}` ถึงพนักงาน "
                + ", ".join(f"<@{sid}>" for sid in failed) + " ไม่สำเร็จ (ปิด DM อยู่)"
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
        shares, shop_share = split_quote_multi(self.cfg, job_staff_ids(parent), quote)
        staff_share = round(sum(shares.values()), 2)
        cycle = cycle_month_key(now_local)

        old_end = from_iso(parent["end_time"])
        new_end = old_end + dt.timedelta(minutes=quote.duration_minutes)

        extend_id = await self.db.create_job(
            guild_id=guild.id,
            job_type="EXTEND",
            parent_job_id=parent_id,
            customer_id=parent["customer_id"],
            staff_id=parent["staff_id"],
            staff_shares=json.dumps({str(k): v for k, v in shares.items()}) if len(shares) > 1 else None,
            group_id=parent.get("group_id"),
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
        for sid in shares:
            await send_dm(
                self.bot,
                sid,
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
            f"✅ <@{job['staff_id']}> รับงานบิล `#{job_id}` แล้ว ส่งยอดให้ลูกค้าแล้ว"
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
            await interaction.response.send_message("บิลนี้มีคนกดไปแล้วค่ะ", ephemeral=True)
            return
        await interaction.response.send_modal(JobRejectReasonModal(job_id))

    async def staff_reject(self, interaction: discord.Interaction, job_id: int, reason: str) -> None:
        job = await self.db.get_job(job_id)
        if job is None or interaction.user.id != job["staff_id"] or job["status"] != "PENDING_STAFF":
            await interaction.response.send_message("บิลนี้มีคนกดไปแล้วค่ะ", ephemeral=True)
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

    async def post_reception_panel(self, interaction: discord.Interaction) -> None:
        if not self._admin_guard(interaction):
            await interaction.response.send_message(NOT_RECEPTION, ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        removed = await purge_old_panels(interaction.channel, self.bot.user.id, "olp:panel:")
        await interaction.channel.send(embed=reception_panel_embed(interaction.guild), view=ReceptionPanel())
        note = f" (ลบแผงเก่าออก {removed} อัน)" if removed else ""
        await interaction.followup.send(f"โพสต์แผงรีเซปชั่นแล้วค่ะ{note}", ephemeral=True)

    @panel_group.command(name="reception", description="โพสต์แผงรีเซปชั่น (แอดมิน / Role รีเซปชั่น)")
    async def panel_reception(self, interaction: discord.Interaction) -> None:
        await self.post_reception_panel(interaction)

    @app_commands.command(name="panel_reception", description="โพสต์แผงรีเซปชั่น (แอดมิน / Role รีเซปชั่น)")
    async def panel_reception_flat(self, interaction: discord.Interaction) -> None:
        await self.post_reception_panel(interaction)

    bill_group = app_commands.Group(name="bill", description="จัดการบิล")

    @bill_group.command(name="info", description="ดูรายละเอียดบิลตามเลขที่")
    @app_commands.describe(job_id="เลขที่บิล")
    async def bill_info(self, interaction: discord.Interaction, job_id: int) -> None:
        if not self._admin_guard(interaction):
            await interaction.response.send_message(NOT_RECEPTION, ephemeral=True)
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
            await interaction.response.send_message(NOT_RECEPTION, ephemeral=True)
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
            await interaction.response.send_message(NOT_RECEPTION, ephemeral=True)
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
