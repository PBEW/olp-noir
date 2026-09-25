"""OLP-Noir — บอทบริหารร้าน Hosting / Cafe บน Discord

รันด้วย:  python bot.py
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

import discord
from discord.ext import commands
from dotenv import load_dotenv

from core.config import Config, ConfigError
from core.database import Database
from core.lockfile import SingleInstanceLock
from core.sheets import SheetsClient

BASE_DIR = Path(__file__).resolve().parent

EXTENSIONS = [
    "cogs.payments",
    "cogs.reception",
    "cogs.tickets",
    "cogs.vip",
    "cogs.reviews",
    "cogs.requestpanel",
    "cogs.scheduler",
    "cogs.attendance",
    "cogs.dmrouter",
    "cogs.admin",
]


def setup_logging() -> None:
    log_dir = BASE_DIR / "logs"
    log_dir.mkdir(exist_ok=True)

    fmt = logging.Formatter("[{asctime}] [{levelname:<7}] {name}: {message}", "%Y-%m-%d %H:%M:%S", "{")

    stream = logging.StreamHandler()
    stream.setFormatter(fmt)

    file_handler = RotatingFileHandler(
        log_dir / "olp.log", maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(stream)
    root.addHandler(file_handler)
    logging.getLogger("discord.http").setLevel(logging.WARNING)


log = logging.getLogger("olp")


class OLPBot(commands.Bot):
    def __init__(self, cfg: Config, db_path: str) -> None:
        intents = discord.Intents.default()
        intents.members = True           # ต้องเปิด Server Members Intent ใน Developer Portal
        intents.message_content = True   # ต้องเปิด Message Content Intent (ใช้กับสะพานแชท DM)

        super().__init__(
            command_prefix=commands.when_mentioned,
            intents=intents,
            help_command=None,
            activity=discord.Activity(type=discord.ActivityType.watching, name="OLP-Noir 🖤"),
        )
        self.cfg = cfg
        self.db = Database(db_path)
        self.sheets = SheetsClient(cfg)

    async def setup_hook(self) -> None:
        await self.db.connect()
        await self.sheets.start()

        for ext in EXTENSIONS:
            await self.load_extension(ext)
            log.info("โหลด extension: %s", ext)

        if self.cfg.guild_id:
            guild = discord.Object(id=self.cfg.guild_id)
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            log.info("ซิงก์ slash command %d รายการเข้ากับกิลด์ %s", len(synced), self.cfg.guild_id)
        else:
            synced = await self.tree.sync()
            log.info("ซิงก์ slash command %d รายการแบบ global (อาจใช้เวลาถึง 1 ชม.)", len(synced))

    async def on_ready(self) -> None:
        log.info("เข้าสู่ระบบในชื่อ %s (id=%s)", self.user, self.user.id)
        if not self.cfg.guild_id:
            log.warning("ยังไม่ได้ตั้งค่า guild_id ใน config.json")
        if not self.cfg.channel_id("admin"):
            log.warning("ยังไม่ได้ตั้งค่า channels.admin ใน config.json")

    async def close(self) -> None:
        await self.db.close()
        await super().close()


async def main() -> None:
    setup_logging()
    load_dotenv(BASE_DIR / ".env")

    token = os.getenv("DISCORD_TOKEN", "").strip()
    if not token:
        log.error("ไม่พบ DISCORD_TOKEN — คัดลอก .env.example เป็น .env แล้วใส่โทเคนก่อนค่ะ")
        sys.exit(1)

    try:
        cfg = Config(BASE_DIR / os.getenv("OLP_CONFIG", "config.json"))
    except ConfigError as exc:
        log.error("%s", exc)
        sys.exit(1)

    db_path = str(BASE_DIR / os.getenv("OLP_DB", "data/olp.sqlite3"))

    # กันรันซ้อน: ถ้ามีบอทตัวอื่นอยู่แล้ว ทุกอย่างจะทำงานซ้ำ (DM ซ้ำ, ลงบัญชีซ้ำ, ให้ Role ซ้ำ)
    lock = SingleInstanceLock(BASE_DIR / "data" / "bot.lock")
    if not lock.acquire():
        log.error(
            "มีบอทตัวอื่นรันอยู่แล้ว จึงไม่เปิดตัวใหม่ซ้ำ\n"
            "  หากรันซ้อนกัน ลูกค้าจะได้ DM ซ้ำ และบัญชีจะถูกบันทึกซ้ำแถว\n"
            "  ให้ปิดหน้าต่าง terminal เดิมที่รันบอทอยู่ก่อน (กด Ctrl+C ในหน้าต่างนั้น)\n"
            "  ตรวจสอบว่ามีตัวไหนรันอยู่บ้างด้วยคำสั่ง PowerShell:\n"
            "    Get-CimInstance Win32_Process -Filter \"Name like '%python%'\" |"
            " Select-Object ProcessId, CommandLine"
        )
        sys.exit(1)

    bot = OLPBot(cfg, db_path)

    try:
        async with bot:
            await bot.start(token)
    finally:
        lock.release()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("ปิดบอทแล้ว")
