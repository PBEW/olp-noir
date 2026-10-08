"""ระบบโดเนท: ลูกค้ากดโดเนทเองจาก Request Panel → โอน → ส่งสลิป → แอดมินยืนยัน → ประกาศขอบคุณ"""
from __future__ import annotations

import datetime as dt
import logging
from collections import defaultdict

import discord
from discord import app_commands
from discord.ext import commands

from core.cycle import cycle_start_local
from core.embeds import COLOR_DANGER, COLOR_GOLD, COLOR_OK, payment_embed
from core.utils import display_name, fmt_datetime, money, now_utc, send_dm, to_iso

log = logging.getLogger("olp.donate")

SHOP = "shop"


def _parse_amount(raw: str) -> float | None:
    text = raw.strip().replace(",", "").replace("บาท", "").replace("฿", "").strip()
    try:
        value = float(text)
    except ValueError:
        return None
    return value if value > 0 else None


class DonateDetailModal(discord.ui.Modal, title="🎁 โดเนท"):
    amount = discord.ui.TextInput(label="จำนวนเงิน (บาท)", placeholder="เช่น 100", max_length=10)
    message = discord.ui.TextInput(
        label="ข้อความถึงผู้รับ (ไม่บังคับ)",
        style=discord.TextStyle.paragraph,
        required=False,
        max_length=200,
    )

    def __init__(self, view: "DonateView") -> None:
        super().__init__()
        self.view = view

    async def on_submit(self, interaction: discord.Interaction) -> None:
        cog: DonateCog = interaction.client.get_cog("DonateCog")  # type: ignore[assignment]
        amount = _parse_amount(str(self.amount))
        minimum = cog.min_amount
        if amount is None or amount < minimum or amount > 100_000:
            await interaction.response.send_message(
                f"❌ จำนวนเงินไม่ถูกต้อง (ขั้นต่ำ {money(minimum)})", ephemeral=True
            )
            return
        await cog.create_and_bill(
            interaction,
            recipient_id=self.view.recipient_id,
            amount=amount,
            message=str(self.message).strip() or None,
            anonymous=self.view.anonymous,
        )


class DonateView(discord.ui.View):
    """แผงเลือกผู้รับ + การแสดงชื่อ (เห็นเฉพาะลูกค้าคนนั้น)"""

    def __init__(self, staff: list[discord.Member]) -> None:
        super().__init__(timeout=600)
        self.recipient_id: int | None = None
        self.anonymous = False

        options = [discord.SelectOption(label="ร้าน OLP-Noir", value=SHOP, emoji="🏠", default=True)]
        options += [
            discord.SelectOption(label=m.display_name[:100], value=str(m.id), emoji="💃") for m in staff[:24]
        ]
        self.recipient.options = options

    @discord.ui.select(placeholder="1) โดเนทให้ใคร", row=0)
    async def recipient(self, interaction: discord.Interaction, select: discord.ui.Select) -> None:
        value = select.values[0]
        self.recipient_id = None if value == SHOP else int(value)
        for opt in select.options:
            opt.default = opt.value == value
        await interaction.response.defer()

    @discord.ui.select(
        placeholder="2) แสดงชื่อในประกาศขอบคุณ",
        row=1,
        options=[
            discord.SelectOption(label="แสดงชื่อของฉัน", value="show", emoji="🙋", default=True),
            discord.SelectOption(label="ไม่ประสงค์ออกนาม", value="hide", emoji="🕶️"),
        ],
    )
    async def visibility(self, interaction: discord.Interaction, select: discord.ui.Select) -> None:
        self.anonymous = select.values[0] == "hide"
        for opt in select.options:
            opt.default = opt.value == select.values[0]
        await interaction.response.defer()

    @discord.ui.button(label="ใส่จำนวนเงิน & ข้อความ", emoji="💸", style=discord.ButtonStyle.success, row=2)
    async def next_step(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_modal(DonateDetailModal(self))


class DonateCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.cfg = bot.cfg
        self.db = bot.db

    # ------------------------------------------------------------ config
    @property
    def min_amount(self) -> float:
        return float(self.cfg.get("donate.min_amount", 20))

    @property
    def staff_percent(self) -> float:
        """ส่วนที่โฮสต์ได้รับเมื่อโดเนทให้โฮสต์โดยตรง (ที่เหลือเข้าร้าน)"""
        return float(self.cfg.get("donate.staff_percent", 100))

    def _staff_members(self, guild: discord.Guild | None) -> list[discord.Member]:
        if guild is None:
            return []
        members: dict[int, discord.Member] = {}
        for role_id in self.cfg.staff_role_ids:
            role = guild.get_role(role_id)
            if role is not None:
                members.update({m.id: m for m in role.members if not m.bot})
        return sorted(members.values(), key=lambda m: m.display_name.lower())

    # ----------------------------------------------------- ฝั่งลูกค้า
    async def open_donate(self, interaction: discord.Interaction) -> None:
        embed = discord.Embed(
            title="🎁 โดเนท",
            description=(
                "1) เลือกว่าจะโดเนทให้ **ร้าน** หรือ **โฮสต์** คนไหน\n"
                "2) เลือกว่าจะแสดงชื่อในประกาศขอบคุณหรือไม่\n"
                "3) กด **ใส่จำนวนเงิน & ข้อความ** แล้วบอทจะส่ง QR ไปทาง DM ค่ะ\n\n"
                f"*ขั้นต่ำ {money(self.min_amount)}*"
            ),
            color=COLOR_GOLD,
        )
        await interaction.response.send_message(
            embed=embed, view=DonateView(self._staff_members(interaction.guild)), ephemeral=True
        )

    async def create_and_bill(
        self,
        interaction: discord.Interaction,
        *,
        recipient_id: int | None,
        amount: float,
        message: str | None,
        anonymous: bool,
    ) -> None:
        donor = interaction.user
        pending = await self.db.get_pending_slip(donor.id)
        if pending is not None and pending["kind"] != "DON":
            await interaction.response.send_message(
                "ยังมีบิลหรือ VIP ที่รอจ่ายอยู่ ส่งสลิปอันนั้นก่อนแล้วค่อยมาโดเนทนะคะ",
                ephemeral=True,
            )
            return
        if pending is not None:
            # โดเนทใหม่แทนรายการโดเนทเดิมที่ยังไม่ได้ส่งสลิป
            old = await self.db.get_donation(pending["ref_id"])
            if old is not None and old["status"] == "AWAITING_PAYMENT":
                await self.db.update_donation(old["id"], status="CANCELLED")

        staff_share = round(amount * self.staff_percent / 100, 2) if recipient_id else 0.0
        donation_id = await self.db.create_donation(
            guild_id=interaction.guild_id or self.cfg.guild_id,
            donor_id=donor.id,
            recipient_id=recipient_id,
            amount=amount,
            message=message,
            anonymous=int(anonymous),
            staff_share=staff_share,
            shop_share=round(amount - staff_share, 2),
            created_at=to_iso(now_utc()),
        )

        target = f"<@{recipient_id}>" if recipient_id else "ร้าน OLP-Noir"
        embed = payment_embed(
            self.cfg,
            title="🎁 ยอดโดเนท",
            description=f"โดเนทให้ {target} (`D#{donation_id}`)" + (f"\nข้อความ: {message}" if message else ""),
            amount=amount,
        )
        await self.db.set_pending_slip(donor.id, "DON", donation_id, to_iso(now_utc()))
        sent = await send_dm(self.bot, donor.id, embed=embed)
        if sent is None:
            await self.db.update_donation(donation_id, status="CANCELLED")
            await self.db.clear_pending_slip(donor.id)
            await interaction.response.send_message(
                "❌ บอทส่ง DM หาคุณไม่ได้ ลองเปิดรับ DM จากสมาชิกในเซิร์ฟเวอร์ แล้วกดใหม่อีกทีนะคะ",
                ephemeral=True,
            )
            return
        await interaction.response.send_message(
            f"✅ ส่งรายละเอียดการโอน {money(amount)} ไปทาง DM แล้วค่ะ โอนแล้วส่งสลิปใน DM ได้เลย",
            ephemeral=True,
        )

    # ------------------------------------------------- ฝั่งแอดมิน (จาก PaymentsCog)
    async def approve(self, donation_id: int, admin: discord.abc.User) -> tuple[bool, str]:
        donation = await self.db.get_donation(donation_id)
        if donation is None:
            return False, "ไม่พบรายการโดเนทนี้"
        if donation["status"] == "PAID":
            return False, "รายการนี้ยืนยันไปแล้ว"
        if donation["status"] == "CANCELLED":
            return False, "รายการนี้ถูกยกเลิกไปแล้ว"

        await self.db.update_donation(donation_id, status="PAID", paid_at=to_iso(now_utc()))
        await self.db.clear_pending_slip(donation["donor_id"])
        donation = await self.db.get_donation(donation_id)

        await send_dm(
            self.bot,
            donation["donor_id"],
            embed=discord.Embed(
                title="💖 ขอบคุณสำหรับการโดเนท",
                description=f"แอดมินยืนยันยอด **{money(donation['amount'])}** แล้วค่ะ ขอบคุณมากนะคะ",
                color=COLOR_OK,
            ),
        )
        if donation["recipient_id"]:
            donor_text = "ผู้ไม่ประสงค์ออกนาม" if donation["anonymous"] else f"<@{donation['donor_id']}>"
            body = f"{donor_text} โดเนทให้คุณ **{money(donation['amount'])}**"
            if donation["staff_share"] != donation["amount"]:
                body += f"\nส่วนของคุณ: **{money(donation['staff_share'])}**"
            if donation.get("message"):
                body += f"\nข้อความ: {donation['message']}"
            await send_dm(
                self.bot,
                donation["recipient_id"],
                embed=discord.Embed(title="🎁 มีคนโดเนทให้คุณ", description=body, color=COLOR_GOLD),
            )

        await self._announce(donation)
        await self._log_to_sheet(donation)
        return True, f"ยืนยันโดเนทแล้ว โดย {admin.mention} — `D#{donation_id}` {money(donation['amount'])}"

    async def reject(self, donation_id: int, admin: discord.abc.User, reason: str = "") -> tuple[bool, str]:
        donation = await self.db.get_donation(donation_id)
        if donation is None:
            return False, "ไม่พบรายการโดเนทนี้"
        if donation["status"] in ("PAID", "CANCELLED"):
            return False, "รายการนี้ปิดไปแล้ว"
        await self.db.update_donation(donation_id, status="CANCELLED")
        await self.db.clear_pending_slip(donation["donor_id"])
        note = discord.Embed(
            title="❌ รายการโดเนทถูกยกเลิก",
            description="แอดมินตรวจสลิปไม่ผ่าน หากมีข้อสงสัยติดต่อแอดมินได้เลยค่ะ",
            color=COLOR_DANGER,
        )
        if reason:
            note.add_field(name="เหตุผล", value=reason, inline=False)
        await send_dm(self.bot, donation["donor_id"], embed=note)
        suffix = f"\nเหตุผล: {reason}" if reason else ""
        return True, f"ยกเลิกโดเนท `D#{donation_id}` แล้ว โดย {admin.mention}{suffix}"

    async def _announce(self, donation: dict) -> None:
        channel = self.bot.get_channel(self.cfg.channel_id("donate"))
        if channel is None:
            return
        donor_text = "🕶️ ผู้ไม่ประสงค์ออกนาม" if donation["anonymous"] else f"<@{donation['donor_id']}>"
        target = f"<@{donation['recipient_id']}>" if donation["recipient_id"] else "ร้าน OLP-Noir"
        embed = discord.Embed(
            title="💖 ขอบคุณสำหรับการโดเนท",
            description=f"{donor_text} โดเนท **{money(donation['amount'])}** ให้ {target}",
            color=COLOR_GOLD,
        )
        if donation.get("message"):
            embed.add_field(name="ข้อความ", value=f"```{donation['message']}```", inline=False)
        try:
            await channel.send(embed=embed)
        except discord.HTTPException as exc:
            log.warning("ประกาศโดเนทไม่สำเร็จ: %s", exc)

    async def _log_to_sheet(self, donation: dict) -> None:
        if donation.get("sheet_logged") or not self.bot.sheets.ready:
            return
        guild = self.bot.get_guild(donation["guild_id"])
        row = [
            donation["id"],
            fmt_datetime(dt.datetime.fromisoformat(donation["paid_at"]), self.cfg.tz),
            await display_name(self.bot, guild, donation["donor_id"]),
            str(donation["donor_id"]),
            await display_name(self.bot, guild, donation["recipient_id"]) if donation["recipient_id"] else "ร้าน",
            str(donation["recipient_id"] or ""),
            donation["amount"],
            donation["staff_share"],
            donation["shop_share"],
            "ไม่เปิดเผยชื่อ" if donation["anonymous"] else "",
            donation.get("message") or "",
        ]
        if await self.bot.sheets.append_donation_row(row):
            await self.db.update_donation(donation["id"], sheet_logged=1)

    # ------------------------------------------------------- สรุป/อันดับ
    async def totals_between(self, start: dt.datetime, end: dt.datetime) -> dict:
        rows = await self.db.donations_paid_between(to_iso(start), to_iso(end))
        per_staff: dict[int, float] = defaultdict(float)
        for r in rows:
            if r["recipient_id"]:
                per_staff[r["recipient_id"]] += r["staff_share"]
        return {
            "rows": rows,
            "total": sum(r["amount"] for r in rows),
            "staff": sum(r["staff_share"] for r in rows),
            "shop": sum(r["shop_share"] for r in rows),
            "per_staff": dict(per_staff),
        }

    async def leaderboard_embed(self, period: str, guild: discord.Guild | None) -> discord.Embed:
        now_local = dt.datetime.now(self.cfg.tz)
        if period == "cycle":
            start, label = cycle_start_local(now_local, self.cfg), "รอบนี้"
        elif period == "month":
            start, label = now_local.replace(day=1, hour=0, minute=0, second=0, microsecond=0), "เดือนนี้"
        else:
            start, label = dt.datetime(2000, 1, 1, tzinfo=self.cfg.tz), "ทั้งหมด"
        rows = await self.db.donations_paid_between(to_iso(start), to_iso(now_local + dt.timedelta(minutes=1)))

        donors: dict[int, float] = defaultdict(float)
        hidden = 0.0
        for r in rows:
            if r["anonymous"]:
                hidden += r["amount"]
            else:
                donors[r["donor_id"]] += r["amount"]

        embed = discord.Embed(title=f"🏆 อันดับผู้โดเนท · {label}", color=COLOR_GOLD)
        if not donors and not hidden:
            embed.description = "ยังไม่มีการโดเนทในช่วงนี้ค่ะ"
            return embed
        medals = ["🥇", "🥈", "🥉"]
        lines = []
        for i, (uid, amt) in enumerate(sorted(donors.items(), key=lambda kv: kv[1], reverse=True)[:10]):
            mark = medals[i] if i < 3 else f"`#{i + 1}`"
            lines.append(f"{mark} <@{uid}> — **{money(amt)}**")
        if hidden:
            lines.append(f"🕶️ ผู้ไม่ประสงค์ออกนาม — **{money(hidden)}**")
        embed.description = "\n".join(lines)
        embed.set_footer(text=f"ยอดรวม {money(sum(r['amount'] for r in rows))}")
        return embed

    @app_commands.command(name="donate_top", description="ดูอันดับผู้โดเนท")
    @app_commands.describe(period="ช่วงเวลา")
    @app_commands.choices(
        period=[
            app_commands.Choice(name="รอบนี้ (ตัดรอบวันเสาร์)", value="cycle"),
            app_commands.Choice(name="เดือนนี้", value="month"),
            app_commands.Choice(name="ทั้งหมด", value="all"),
        ]
    )
    async def donate_top(
        self, interaction: discord.Interaction, period: app_commands.Choice[str] | None = None
    ) -> None:
        embed = await self.leaderboard_embed(period.value if period else "month", interaction.guild)
        await interaction.response.send_message(embed=embed)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(DonateCog(bot))
