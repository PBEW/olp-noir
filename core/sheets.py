"""บันทึกบัญชีลง Google Sheets (ทำงานแบบ optional — ปิดได้ใน config)"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
from pathlib import Path

from .config import Config

log = logging.getLogger("olp.sheets")

HEADERS = [
    "Job ID",
    "วันที่",
    "เวลาเริ่ม",
    "เวลาจบ",
    "ลูกค้า",
    "ID ลูกค้า",
    "พนักงาน",
    "ID พนักงาน",
    "บริการ",
    "ห้อง",
    "รายรับ (In)",
    "ส่วนแบ่งพนักงาน (Out)",
    "รายได้ร้าน",
    "ประเภท",
    "ระดับ VIP",
    "หมายเหตุ",
]

# บล็อกสรุปที่วางไว้ด้านขวาของตาราง (คอลัมน์ Q เป็นต้นไป)
SUMMARY_BLOCK = [
    ["สรุปรอบบิล", ""],
    ["รายรับรวม (In)", "=SUM(K2:K)"],
    ["ส่วนแบ่งพนักงาน (Out)", "=SUM(L2:L)"],
    ["รายได้เข้าร้าน", "=SUM(M2:M)"],
    ["จำนวนบิล", "=COUNTA(A2:A)"],
    ["", ""],
    ["พนักงาน", "ยอด In", "ส่วนแบ่ง"],
    [
        "=IFERROR(UNIQUE(FILTER(G2:G,G2:G<>\"\")),\"\")",
        "=ARRAYFORMULA(IF(Q8:Q=\"\",,SUMIF(G:G,Q8:Q,K:K)))",
        "=ARRAYFORMULA(IF(Q8:Q=\"\",,SUMIF(G:G,Q8:Q,L:L)))",
    ],
]


class SheetsClient:
    """ห่อ gspread ให้เรียกใช้แบบ async ได้ (gspread เป็น sync ล้วน)"""

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self._client = None
        self._spreadsheet = None
        self._lock = asyncio.Lock()

    @property
    def enabled(self) -> bool:
        return bool(self.cfg.get("google_sheets.enabled", False))

    @property
    def ready(self) -> bool:
        return self._spreadsheet is not None

    # ------------------------------------------------------------- startup
    async def start(self) -> None:
        if not self.enabled:
            log.info("ปิดการใช้งาน Google Sheets (google_sheets.enabled = false)")
            return
        try:
            await asyncio.to_thread(self._connect)
            log.info("เชื่อมต่อ Google Sheets สำเร็จ")
        except Exception:  # noqa: BLE001 - ไม่ให้บอทล่มเพราะ Sheets
            log.exception("เชื่อมต่อ Google Sheets ไม่สำเร็จ — บอทจะทำงานต่อโดยไม่บันทึกชีต")

    def _connect(self) -> None:
        import gspread
        from google.oauth2.service_account import Credentials

        creds_file = Path(self.cfg.get("google_sheets.credentials_file", "credentials.json"))
        if not creds_file.exists():
            raise FileNotFoundError(f"ไม่พบไฟล์ credential: {creds_file}")

        scopes = [
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive",
        ]
        creds = Credentials.from_service_account_file(str(creds_file), scopes=scopes)
        self._client = gspread.authorize(creds)
        self._spreadsheet = self._client.open_by_key(self.cfg.get("google_sheets.spreadsheet_id"))

    # ------------------------------------------------------------- helpers
    @staticmethod
    def cycle_name(day: dt.date) -> str:
        return f"Cycle_{day.isoformat()}"

    def _get_or_create_ws(self, title: str):
        import gspread

        assert self._spreadsheet is not None
        try:
            return self._spreadsheet.worksheet(title)
        except gspread.WorksheetNotFound:
            ws = self._spreadsheet.add_worksheet(title=title, rows=500, cols=26)
            self._init_ws(ws)
            return ws

    def _init_ws(self, ws) -> None:
        ws.update(values=[HEADERS], range_name="A1")
        ws.update(values=SUMMARY_BLOCK, range_name="Q1", value_input_option="USER_ENTERED")
        ws.format("A1:P1", {"textFormat": {"bold": True}})
        ws.format("Q1:S1", {"textFormat": {"bold": True}})
        ws.freeze(rows=1)

    # -------------------------------------------------------------- public
    async def append_job_row(self, sheet_title: str, row: list) -> bool:
        if not self.ready:
            return False
        async with self._lock:
            try:
                await asyncio.to_thread(self._append_sync, sheet_title, row)
                return True
            except Exception:  # noqa: BLE001
                log.exception("บันทึกแถวลง Google Sheets ไม่สำเร็จ")
                return False

    def _append_sync(self, sheet_title: str, row: list) -> None:
        ws = self._get_or_create_ws(sheet_title)
        ws.append_row(row, value_input_option="USER_ENTERED", table_range="A1")

    async def create_cycle_sheet(self, title: str) -> bool:
        if not self.ready:
            return False
        async with self._lock:
            try:
                await asyncio.to_thread(self._get_or_create_ws, title)
                return True
            except Exception:  # noqa: BLE001
                log.exception("สร้างชีตรอบใหม่ (%s) ไม่สำเร็จ", title)
                return False

    async def spreadsheet_url(self) -> str | None:
        if not self.ready:
            return None
        return f"https://docs.google.com/spreadsheets/d/{self.cfg.get('google_sheets.spreadsheet_id')}"
