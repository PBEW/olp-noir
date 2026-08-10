"""ระบบ Ticket สอบถามข้อมูล + สะพานแชท DM ระหว่างลูกค้ากับแอดมิน"""
from __future__ import annotations

import logging
import re

import discord
from discord.ext import commands

from core.embeds import COLOR_DANGER, COLOR_INFO, COLOR_OK, COLOR_WARN
from core.utils import display_name, is_admin, now_utc, send_dm, to_iso

log = logging.getLogger("olp.tickets")


class TicketAcceptButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"olp:ticket_accept:(?P<ticket_id>\d+)",
):
    def __init__(self, ticket_id: int) -> None:
        self.ticket_id = ticket_id
        super().__init__(
            discord.ui.Button(
                label="รับเรื่อง (Chat)",
                emoji="✅",
                style=discord.ButtonStyle.success,
                custom_id=f"olp:ticket_accept:{ticket_id}",
            )
        )

    @classmethod
    async def from_custom_id(cls, interaction, item, match: re.Match[str]):
        return cls(int(match["ticket_id"]))

    async def callback(self, interaction: discord.Interaction) -> None:  # type: ignore[override]
        cog: TicketsCog = interaction.client.get_cog("TicketsCog")  # type: ignore[assignment]
        await cog.accept_ticket(interaction, self.ticket_id)


class TicketCloseButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"olp:ticket_close:(?P<ticket_id>\d+)",
):
    def __init__(self, ticket_id: int) -> None:
        self.ticket_id = ticket_id
        super().__init__(
            discord.ui.Button(
                label="ปิดการสนทนา",
                emoji="🔒",
                style=discord.ButtonStyle.danger,
                custom_id=f"olp:ticket_close:{ticket_id}",
            )
        )

    @classmethod
    async def from_custom_id(cls, interaction, item, match: re.Match[str]):
        return cls(int(match["ticket_id"]))

    async def callback(self, interaction: discord.Interaction) -> None:  # type: ignore[override]
        cog: TicketsCog = interaction.client.get_cog("TicketsCog")  # type: ignore[assignment]
        await cog.close_ticket(self.ticket_id, reason=f"ปิดโดย {interaction.user.display_name}")
        await interaction.response.send_message("ปิดการสนทนาเรียบร้อยค่ะ", ephemeral=True)


def ticket_admin_view(ticket_id: int) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(TicketAcceptButton(ticket_id))
    view.add_item(TicketCloseButton(ticket_id))
    return view


def ticket_close_view(ticket_id: int) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(TicketCloseButton(ticket_id))
    return view


class TicketsCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.cfg = bot.cfg
        self.db = bot.db

    # ------------------------------------------------------------- เปิดตั๋ว
    async def open_ticket(self, interaction: discord.Interaction) -> None:
        existing = await self.db.open_ticket_for_customer(interaction.user.id)
        if existing is not None:
            await interaction.response.send_message(
                "คุณมีรายการสอบถามที่ยังเปิดอยู่ค่ะ กรุณาคุยต่อใน DM ของบอทได้เลย",
                ephemeral=True,
            )
            return

        now = to_iso(now_utc())
        ticket_id = await self.db.create_ticket(
            guild_id=interaction.guild_id or self.cfg.guild_id,
            customer_id=interaction.user.id,
            status="OPEN",
            created_at=now,
            last_activity=now,
        )

        dm = await send_dm(
            self.bot,
            interaction.user.id,
            embed=discord.Embed(
                title="💬 เปิดรายการสอบถามแล้ว",
                description=(
                    f"หมายเลข `T#{ticket_id}`\n"
                    "กำลังแจ้งเจ้าหน้าที่ให้รับเรื่องค่ะ เมื่อเจ้าหน้าที่กดรับเรื่องแล้ว "
                    "คุณสามารถพิมพ์ข้อความใน DM นี้เพื่อคุยกับเจ้าหน้าที่ได้ทันที"
                ),
                color=COLOR_INFO,
            ),
        )
        if dm is None:
            await self.db.update_ticket(ticket_id, status="CLOSED", closed_at=now)
            await interaction.response.send_message(
                "ส่ง DM ไม่ได้ค่ะ กรุณาเปิดรับข้อความ DM จากสมาชิกในเซิร์ฟเวอร์ก่อนนะคะ",
                ephemeral=True,
            )
            return

        embed = discord.Embed(
            title="💬 มีรายการสอบถามใหม่",
            description=(
                f"หมายเลข `T#{ticket_id}`\n"
                f"ลูกค้า: {interaction.user.mention} (`{interaction.user}`)\n\n"
                "กด **รับเรื่อง (Chat)** เพื่อเปิดสะพานแชท DM กับลูกค้า"
            ),
            color=COLOR_WARN,
        )
        payments = self.bot.get_cog("PaymentsCog")
        msg = await payments.notify_admin(embed=embed, view=ticket_admin_view(ticket_id))
        if msg is not None:
            await self.db.update_ticket(ticket_id, admin_msg_id=msg.id)

        await interaction.response.send_message(
            "เปิดรายการสอบถามแล้วค่ะ กรุณาตรวจสอบ DM ของบอท", ephemeral=True
        )

    # ------------------------------------------------------------ รับเรื่อง
    async def accept_ticket(self, interaction: discord.Interaction, ticket_id: int) -> None:
        if not is_admin(interaction.user, self.cfg.admin_role_id):
            await interaction.response.send_message("เฉพาะแอดมินเท่านั้นค่ะ", ephemeral=True)
            return

        ticket = await self.db.get_ticket(ticket_id)
        if ticket is None:
            await interaction.response.send_message("ไม่พบรายการนี้ค่ะ", ephemeral=True)
            return
        if ticket["status"] != "OPEN":
            await interaction.response.send_message(
                "รายการนี้ถูกรับเรื่องหรือปิดไปแล้วค่ะ", ephemeral=True
            )
            return

        busy = await self.db.active_ticket_for_admin(interaction.user.id)
        if busy is not None:
            await interaction.response.send_message(
                f"คุณกำลังคุยกับลูกค้าในรายการ `T#{busy['id']}` อยู่ กรุณาปิดรายการนั้นก่อนค่ะ",
                ephemeral=True,
            )
            return

        await interaction.response.defer()
        now = to_iso(now_utc())
        await self.db.update_ticket(
            ticket_id,
            status="ACTIVE",
            admin_id=interaction.user.id,
            accepted_at=now,
            last_activity=now,
        )

        timeout = self.cfg.ticket_timeout_minutes
        await send_dm(
            self.bot,
            ticket["customer_id"],
            embed=discord.Embed(
                title="✅ เจ้าหน้าที่รับเรื่องแล้ว",
                description=(
                    "พิมพ์ข้อความใน DM นี้ได้เลยค่ะ ระบบจะส่งต่อให้เจ้าหน้าที่ทันที\n"
                    f"*หากไม่มีการสนทนาเกิน {timeout} นาที ระบบจะปิดรายการอัตโนมัติ*"
                ),
                color=COLOR_OK,
            ),
            view=ticket_close_view(ticket_id),
        )
        await send_dm(
            self.bot,
            interaction.user.id,
            embed=discord.Embed(
                title=f"💬 เชื่อมต่อกับลูกค้าแล้ว · T#{ticket_id}",
                description=(
                    f"ลูกค้า: <@{ticket['customer_id']}>\n"
                    "พิมพ์ข้อความใน DM นี้เพื่อตอบลูกค้าได้เลยค่ะ"
                ),
                color=COLOR_OK,
            ),
            view=ticket_close_view(ticket_id),
        )

        if interaction.message is not None:
            embed = interaction.message.embeds[0]
            embed.color = COLOR_OK
            embed.add_field(name="ผู้รับเรื่อง", value=interaction.user.mention, inline=False)
            await interaction.message.edit(embed=embed, view=ticket_close_view(ticket_id))

    # ------------------------------------------------------------ สะพานแชท
    async def relay(self, message: discord.Message, ticket: dict) -> bool:
        """ส่งต่อข้อความระหว่างลูกค้ากับแอดมิน คืน True ถ้าส่งต่อแล้ว"""
        if ticket["status"] != "ACTIVE":
            return False

        is_customer = message.author.id == ticket["customer_id"]
        target_id = ticket["admin_id"] if is_customer else ticket["customer_id"]
        if not target_id:
            return False

        prefix = "🙋 ลูกค้า" if is_customer else "🎧 เจ้าหน้าที่"
        content = f"**{prefix} · {message.author.display_name}:**\n{message.content or '*(ไม่มีข้อความ)*'}"

        files = []
        for attachment in message.attachments[:5]:
            try:
                files.append(await attachment.to_file())
            except discord.HTTPException:
                content += f"\n{attachment.url}"

        try:
            user = self.bot.get_user(target_id) or await self.bot.fetch_user(target_id)
            await user.send(content=content[:1900], files=files)
        except (discord.Forbidden, discord.HTTPException) as exc:
            log.warning("ส่งต่อข้อความ ticket %s ไม่สำเร็จ: %s", ticket["id"], exc)
            await message.reply("⚠️ ส่งข้อความไม่สำเร็จ อีกฝ่ายอาจปิด DM อยู่ค่ะ")
            return True

        await self.db.update_ticket(ticket["id"], last_activity=to_iso(now_utc()))
        try:
            await message.add_reaction("📨")
        except discord.HTTPException:
            pass
        return True

    # -------------------------------------------------------------- ปิดตั๋ว
    async def close_ticket(self, ticket_id: int, *, reason: str) -> None:
        ticket = await self.db.get_ticket(ticket_id)
        if ticket is None or ticket["status"] == "CLOSED":
            return

        await self.db.update_ticket(ticket_id, status="CLOSED", closed_at=to_iso(now_utc()))
        embed = discord.Embed(
            title="🔒 ปิดรายการสอบถามแล้ว",
            description=(
                f"หมายเลข `T#{ticket_id}`\n{reason}\n\n"
                "หากต้องการสอบถามเพิ่มเติม กดปุ่ม 💬 สอบถามเจ้าหน้าที่ ที่หน้าแผงบริการได้ใหม่ค่ะ"
            ),
            color=COLOR_DANGER,
        )
        await send_dm(self.bot, ticket["customer_id"], embed=embed)
        if ticket["admin_id"]:
            await send_dm(self.bot, ticket["admin_id"], embed=embed)

        guild = self.bot.get_guild(ticket["guild_id"])
        name = await display_name(self.bot, guild, ticket["customer_id"])
        payments = self.bot.get_cog("PaymentsCog")
        await payments.notify_admin_text(f"🔒 ปิดรายการสอบถาม `T#{ticket_id}` ({name}) — {reason}")


async def setup(bot: commands.Bot) -> None:
    bot.add_dynamic_items(TicketAcceptButton, TicketCloseButton)
    await bot.add_cog(TicketsCog(bot))
