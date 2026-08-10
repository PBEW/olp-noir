"""ระบบรีวิวหลังจบบริการ: ส่งคำเชิญ, ฟอร์มรีวิว, แอดมินอนุมัติ, โพสต์ลงห้องรีวิว"""
from __future__ import annotations

import datetime as dt
import logging
import re

import discord
from discord.ext import commands

from core.embeds import COLOR_DANGER, COLOR_INFO, COLOR_OK, COLOR_WARN
from core.utils import from_iso, is_admin, now_utc, send_dm, to_iso

log = logging.getLogger("olp.reviews")


# ------------------------------------------------------------------- ปุ่ม
class ReviewStartButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"olp:review_start:(?P<job_id>\d+)",
):
    def __init__(self, job_id: int) -> None:
        self.job_id = job_id
        super().__init__(
            discord.ui.Button(
                label="เขียนรีวิวเลย",
                emoji="✍️",
                style=discord.ButtonStyle.primary,
                custom_id=f"olp:review_start:{job_id}",
            )
        )

    @classmethod
    async def from_custom_id(cls, interaction, item, match: re.Match[str]):
        return cls(int(match["job_id"]))

    async def callback(self, interaction: discord.Interaction) -> None:  # type: ignore[override]
        cog: ReviewsCog = interaction.client.get_cog("ReviewsCog")  # type: ignore[assignment]
        await cog.start_review(interaction, self.job_id)


class ReviewDecisionButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"olp:review_(?P<action>ok|no):(?P<review_id>\d+)",
):
    def __init__(self, action: str, review_id: int) -> None:
        self.action = action
        self.review_id = review_id
        approve = action == "ok"
        super().__init__(
            discord.ui.Button(
                label="อนุมัติลงรีวิว" if approve else "ปฏิเสธ",
                emoji="✅" if approve else "❌",
                style=discord.ButtonStyle.success if approve else discord.ButtonStyle.danger,
                custom_id=f"olp:review_{action}:{review_id}",
            )
        )

    @classmethod
    async def from_custom_id(cls, interaction, item, match: re.Match[str]):
        return cls(match["action"], int(match["review_id"]))

    async def callback(self, interaction: discord.Interaction) -> None:  # type: ignore[override]
        cog: ReviewsCog = interaction.client.get_cog("ReviewsCog")  # type: ignore[assignment]
        if not is_admin(interaction.user, cog.cfg.admin_role_id):
            await interaction.response.send_message("เฉพาะแอดมินเท่านั้นค่ะ", ephemeral=True)
            return
        if self.action == "ok":
            await cog.approve_review(interaction, self.review_id)
        else:
            await cog.reject_review(interaction, self.review_id)


def review_invite_view(job_id: int) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(ReviewStartButton(job_id))
    return view


def review_admin_view(review_id: int) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(ReviewDecisionButton("ok", review_id))
    view.add_item(ReviewDecisionButton("no", review_id))
    return view


# ------------------------------------------------------------------ ฟอร์ม
class ReviewTextModal(discord.ui.Modal, title="เขียนรีวิว"):
    content = discord.ui.TextInput(
        label="ความประทับใจของคุณ",
        style=discord.TextStyle.paragraph,
        placeholder="เล่าประสบการณ์ของคุณให้ฟังหน่อยค่ะ",
        required=True,
        max_length=800,
    )

    def __init__(self, form: "ReviewForm") -> None:
        super().__init__()
        self.form = form

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.form.submit(interaction, str(self.content).strip())


class ReviewForm(discord.ui.View):
    def __init__(self, cog: "ReviewsCog", job: dict) -> None:
        super().__init__(timeout=900)
        self.cog = cog
        self.job = job
        self.stars: int | None = None
        self.spice: int | None = None

        self.star_select = discord.ui.Select(
            placeholder="⭐ คะแนนความประทับใจ (1-5)",
            row=0,
            options=[
                discord.SelectOption(label="⭐" * n, value=str(n), description=f"{n} ดาว")
                for n in range(1, 6)
            ],
        )
        self.star_select.callback = self._on_star
        self.add_item(self.star_select)

        self.spice_select = discord.ui.Select(
            placeholder="🔥 คะแนนความแซ่บ (1-5)",
            row=1,
            options=[
                discord.SelectOption(label="🔥" * n, value=str(n), description=f"{n} ไฟ")
                for n in range(1, 6)
            ],
        )
        self.spice_select.callback = self._on_spice
        self.add_item(self.spice_select)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.job["customer_id"]

    async def _on_star(self, interaction: discord.Interaction) -> None:
        self.stars = int(self.star_select.values[0])
        await interaction.response.edit_message(embed=self.embed(), view=self)

    async def _on_spice(self, interaction: discord.Interaction) -> None:
        self.spice = int(self.spice_select.values[0])
        await interaction.response.edit_message(embed=self.embed(), view=self)

    def embed(self) -> discord.Embed:
        embed = discord.Embed(
            title="✍️ แบบประเมินความพึงพอใจ",
            description=(
                f"บิล `#{self.job['id']}` · {self.cog.cfg.service_names(self.job['services'])}\n"
                "เลือกคะแนนทั้ง 2 ช่อง แล้วกดปุ่มเขียนข้อความรีวิวค่ะ"
            ),
            color=COLOR_INFO,
        )
        embed.add_field(
            name="ความประทับใจ", value="⭐" * self.stars if self.stars else "*ยังไม่เลือก*"
        )
        embed.add_field(name="ความแซ่บ", value="🔥" * self.spice if self.spice else "*ยังไม่เลือก*")
        return embed

    @discord.ui.button(
        label="เขียนข้อความรีวิว", emoji="📝", style=discord.ButtonStyle.primary, row=2
    )
    async def write(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not self.stars or not self.spice:
            await interaction.response.send_message(
                "กรุณาเลือกคะแนนให้ครบทั้ง 2 ช่องก่อนค่ะ", ephemeral=True
            )
            return
        await interaction.response.send_modal(ReviewTextModal(self))

    async def submit(self, interaction: discord.Interaction, content: str) -> None:
        await interaction.response.defer()
        ok, message = await self.cog.record_review(self.job, self.stars, self.spice, content)
        self.stop()
        await interaction.edit_original_response(
            embed=discord.Embed(
                title="💌 ส่งรีวิวเรียบร้อย" if ok else "⚠️ ส่งรีวิวไม่สำเร็จ",
                description=message,
                color=COLOR_OK if ok else COLOR_DANGER,
            ),
            view=None,
        )


# ------------------------------------------------------------------- cog
class ReviewsCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.cfg = bot.cfg
        self.db = bot.db

    # ------------------------------------------------------- ส่งคำเชิญรีวิว
    async def send_review_invite(self, job: dict) -> None:
        embed = discord.Embed(
            title="💖 ขอบคุณที่ใช้บริการค่ะ",
            description=(
                f"บิล `#{job['id']}` · {self.cfg.service_names(job['services'])}\n"
                f"พนักงาน: <@{job['staff_id']}>\n\n"
                "ช่วยให้คะแนนและเขียนรีวิวสั้นๆ ให้หน่อยนะคะ 🥰\n"
                f"*เขียนรีวิวได้ภายใน {self.cfg.review_window_hours} ชั่วโมงหลังจบงาน และรีวิวได้ 1 ครั้งต่อ 1 บิล*"
            ),
            color=self.cfg.review_color,
        )
        await send_dm(self.bot, job["customer_id"], embed=embed, view=review_invite_view(job["id"]))

    # ------------------------------------------------------------ เปิดฟอร์ม
    async def start_review(self, interaction: discord.Interaction, job_id: int) -> None:
        job = await self.db.get_job(job_id)
        if job is None:
            await interaction.response.send_message("ไม่พบบิลนี้ค่ะ", ephemeral=True)
            return
        if interaction.user.id != job["customer_id"]:
            await interaction.response.send_message("ปุ่มนี้สำหรับลูกค้าเจ้าของบิลค่ะ", ephemeral=True)
            return
        if job["reviewed"] or await self.db.review_for_job(job_id):
            await interaction.response.send_message(
                "บิลนี้ส่งรีวิวไปแล้วค่ะ (1 บิล รีวิวได้ 1 ครั้ง)", ephemeral=True
            )
            return

        end = from_iso(job["end_time"])
        deadline = end + dt.timedelta(hours=self.cfg.review_window_hours)
        if now_utc() > deadline:
            await interaction.response.send_message(
                f"หมดเวลาเขียนรีวิวแล้วค่ะ (ภายใน {self.cfg.review_window_hours} ชั่วโมงหลังจบงาน)",
                ephemeral=True,
            )
            return

        form = ReviewForm(self, job)
        await interaction.response.send_message(embed=form.embed(), view=form)

    # ------------------------------------------------------------ บันทึกรีวิว
    async def record_review(
        self, job: dict, stars: int, spice: int, content: str
    ) -> tuple[bool, str]:
        if await self.db.review_for_job(job["id"]):
            return False, "บิลนี้มีรีวิวอยู่แล้วค่ะ"

        guild = self.bot.get_guild(job["guild_id"])
        staff_name = None
        if guild is not None:
            member = guild.get_member(job["staff_id"])
            staff_name = member.display_name if member else None
        if staff_name is None:
            try:
                staff_name = (await self.bot.fetch_user(job["staff_id"])).display_name
            except discord.HTTPException:
                staff_name = f"พนักงาน {job['staff_id']}"

        review_id = await self.db.create_review(
            job_id=job["id"],
            guild_id=job["guild_id"],
            customer_id=job["customer_id"],
            staff_id=job["staff_id"],
            staff_name=staff_name,
            services=self.cfg.service_names(job["services"]),
            stars=stars,
            spice=spice,
            content=content,
            status="PENDING",
            created_at=to_iso(now_utc()),
        )
        await self.db.update_job(job["id"], reviewed=1)

        embed = discord.Embed(
            title="📝 รีวิวใหม่รอตรวจสอบ",
            description=(
                f"รีวิว `R#{review_id}` · บิล `#{job['id']}`\n"
                f"ลูกค้า: <@{job['customer_id']}>\n"
                f"พนักงาน: <@{job['staff_id']}> ({staff_name})\n"
                f"บริการ: {self.cfg.service_names(job['services'])}"
            ),
            color=COLOR_WARN,
        )
        embed.add_field(name="คะแนน", value="⭐" * stars, inline=True)
        embed.add_field(name="ความแซ่บ", value="🔥" * spice, inline=True)
        embed.add_field(name="ข้อความ", value=f"```\n{content[:900]}\n```", inline=False)

        payments = self.bot.get_cog("PaymentsCog")
        msg = await payments.notify_admin(embed=embed, view=review_admin_view(review_id))
        if msg is not None:
            await self.db.update_review(review_id, admin_msg_id=msg.id)

        return True, "ส่งรีวิวให้แอดมินตรวจสอบแล้ว ขอบคุณมากค่ะ 💕"

    # --------------------------------------------------------------- อนุมัติ
    async def approve_review(self, interaction: discord.Interaction, review_id: int) -> None:
        review = await self.db.get_review(review_id)
        if review is None:
            await interaction.response.send_message("ไม่พบรีวิวนี้ค่ะ", ephemeral=True)
            return
        if review["status"] != "PENDING":
            await interaction.response.send_message("รีวิวนี้ถูกดำเนินการไปแล้วค่ะ", ephemeral=True)
            return

        channel = self.bot.get_channel(self.cfg.channel_id("review"))
        if channel is None:
            await interaction.response.send_message(
                "ยังไม่ได้ตั้งค่าห้องรีวิว (channels.review) ใน config ค่ะ", ephemeral=True
            )
            return

        await interaction.response.defer()
        embed = await self.build_public_embed(review)
        posted = await channel.send(embed=embed)

        await self.db.update_review(
            review_id,
            status="APPROVED",
            handled_at=to_iso(now_utc()),
            handled_by=interaction.user.id,
            public_msg_id=posted.id,
        )
        await self._finish(interaction, f"✅ อนุมัติแล้ว โดย {interaction.user.mention}", COLOR_OK)
        await send_dm(
            self.bot,
            review["customer_id"],
            embed=discord.Embed(
                title="💖 รีวิวของคุณถูกเผยแพร่แล้ว",
                description=f"ขอบคุณสำหรับรีวิวนะคะ ดูได้ที่ {channel.mention}",
                color=self.cfg.review_color,
            ),
        )

    async def reject_review(self, interaction: discord.Interaction, review_id: int) -> None:
        review = await self.db.get_review(review_id)
        if review is None:
            await interaction.response.send_message("ไม่พบรีวิวนี้ค่ะ", ephemeral=True)
            return
        if review["status"] != "PENDING":
            await interaction.response.send_message("รีวิวนี้ถูกดำเนินการไปแล้วค่ะ", ephemeral=True)
            return

        await interaction.response.defer()
        await self.db.update_review(
            review_id,
            status="REJECTED",
            handled_at=to_iso(now_utc()),
            handled_by=interaction.user.id,
        )
        await self._finish(interaction, f"❌ ปฏิเสธแล้ว โดย {interaction.user.mention}", COLOR_DANGER)

    async def _finish(self, interaction: discord.Interaction, text: str, color: int) -> None:
        message = interaction.message
        if message is None:
            return
        embed = message.embeds[0] if message.embeds else discord.Embed()
        embed.color = color
        embed.add_field(name="ผลการตรวจสอบ", value=text, inline=False)
        await message.edit(embed=embed, view=None)

    # ------------------------------------------------------ Embed สาธารณะ
    async def build_public_embed(self, review: dict) -> discord.Embed:
        guild = self.bot.get_guild(review["guild_id"])
        member = guild.get_member(review["staff_id"]) if guild else None

        # พนักงานลาออก/ไม่อยู่ในเซิร์ฟเวอร์แล้ว -> ใช้ชื่อธรรมดาแทนการ mention
        staff_value = member.mention if member else (review["staff_name"] or "ไม่ระบุ")
        avatar_url = member.display_avatar.url if member else None
        if avatar_url is None:
            try:
                user = await self.bot.fetch_user(review["staff_id"])
                avatar_url = user.display_avatar.url
            except discord.HTTPException:
                avatar_url = None

        embed = discord.Embed(title="💖 รีวิวจากลูกค้า", color=self.cfg.review_color)
        if avatar_url:
            embed.set_thumbnail(url=avatar_url)
        embed.add_field(name="คะแนน", value="⭐" * int(review["stars"]), inline=True)
        embed.add_field(name="ความแซ่บ", value="🔥" * int(review["spice"]), inline=True)
        embed.add_field(
            name="ความประทับใจ", value=f"```\n{(review['content'] or '-')[:900]}\n```", inline=False
        )
        embed.add_field(name="พนักงาน", value=staff_value, inline=True)
        embed.add_field(name="บริการที่ใช้", value=review["services"] or "-", inline=True)

        customer_name = "ลูกค้า"
        if guild is not None:
            customer = guild.get_member(review["customer_id"])
            if customer is not None:
                customer_name = customer.display_name
        if customer_name == "ลูกค้า":
            try:
                customer_name = (await self.bot.fetch_user(review["customer_id"])).display_name
            except discord.HTTPException:
                pass
        embed.set_footer(text=f"โดยคุณ: {customer_name}")
        embed.timestamp = from_iso(review["created_at"])
        return embed


async def setup(bot: commands.Bot) -> None:
    bot.add_dynamic_items(ReviewStartButton, ReviewDecisionButton)
    await bot.add_cog(ReviewsCog(bot))
