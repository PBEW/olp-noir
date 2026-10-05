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


ATTENDANCE_SHEET = "Attendance"
ATTENDANCE_HEADERS = [
    "ลำดับ",
    "วันที่",
    "เข้างาน",
    "ออกงาน (ตัดอัตโนมัติ)",
    "พนักงาน",
    "ID พนักงาน",
    "ชั่วโมง",
    "หมายเหตุ",
]

DONATION_SHEET = "Donations"
DONATION_HEADERS = [
    "โดเนท ID",
    "วันเวลา",
    "ผู้โดเนท",
    "ID ผู้โดเนท",
    "ผู้รับ",
    "ID ผู้รับ",
    "ยอด",
    "ส่วนของโฮสต์",
    "เข้าร้าน",
    "หมายเหตุ",
    "ข้อความ",
]


# ------------------------------------------------------------------ สไตล์
def _rgb(hex_color: str) -> dict:
    h = hex_color.lstrip("#")
    return {"red": int(h[0:2], 16) / 255, "green": int(h[2:4], 16) / 255, "blue": int(h[4:6], 16) / 255}


WHITE = _rgb("#FFFFFF")
MONEY = {"numberFormat": {"type": "NUMBER", "pattern": '#,##0" ฿"'}}


def _header(bg: str) -> dict:
    return {
        "backgroundColor": _rgb(bg),
        "textFormat": {"bold": True, "foregroundColor": WHITE, "fontSize": 10},
        "horizontalAlignment": "CENTER",
        "verticalAlignment": "MIDDLE",
        "wrapStrategy": "WRAP",
    }


# สีประจำชีต (หัวตาราง) และสีแถวสลับ
THEMES = {
    "cycle": ("#2B2D42", "#F4F5FA"),
    "attendance": ("#0F766E", "#ECFDF5"),
    "donation": ("#BE185D", "#FDF2F8"),
}


class SheetsClient:
    """ห่อ gspread ให้เรียกใช้แบบ async ได้ (gspread เป็น sync ล้วน)"""

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self._client = None
        self._spreadsheet = None
        self._lock = asyncio.Lock()
        self._styled: set[str] = set()  # ชีตที่จัดรูปแบบแล้วในการรันครั้งนี้

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
        self._style_cycle(ws)
        self._styled.add(ws.title)

    # ------------------------------------------------------ จัดรูปแบบชีต
    def _ensure_styled(self, ws, kind: str) -> None:
        """จัดรูปแบบชีตครั้งแรกที่ใช้ในการรันนี้ (ชีตเก่าที่สร้างก่อนอัปเดตก็ได้สีด้วย)"""
        if ws.title in self._styled:
            return
        try:
            if kind == "cycle":
                self._style_cycle(ws)
            elif kind == "attendance":
                self._style_simple(ws, ATTENDANCE_HEADERS, "attendance", widths=[60, 95, 75, 140, 160, 170, 70, 220], hours_col="G")
            else:
                self._style_simple(ws, DONATION_HEADERS, "donation", widths=[80, 130, 150, 170, 150, 170, 90, 110, 90, 120, 260], money_cols="G:I")
        except Exception:  # noqa: BLE001 - สีไม่ติดก็ไม่เป็นไร ข้อมูลสำคัญกว่า
            log.exception("จัดรูปแบบชีต %s ไม่สำเร็จ", ws.title)
        self._styled.add(ws.title)

    def _set_widths(self, ws, widths: list[int], start: int = 0) -> list[dict]:
        return [
            {
                "updateDimensionProperties": {
                    "range": {"sheetId": ws.id, "dimension": "COLUMNS", "startIndex": start + i, "endIndex": start + i + 1},
                    "properties": {"pixelSize": w},
                    "fields": "pixelSize",
                }
            }
            for i, w in enumerate(widths)
        ]

    def _banding(self, ws, columns: int, color: str) -> dict:
        return {
            "addBanding": {
                "bandedRange": {
                    "range": {"sheetId": ws.id, "startRowIndex": 1, "startColumnIndex": 0, "endColumnIndex": columns},
                    "rowProperties": {"firstBandColor": WHITE, "secondBandColor": _rgb(color)},
                }
            }
        }

    def _batch(self, requests: list[dict]) -> None:
        assert self._spreadsheet is not None
        for req in requests:  # ทีละคำขอ — ถ้ามีแถบสีสลับอยู่แล้ว addBanding จะ error แต่ตัวอื่นยังทำงาน
            try:
                self._spreadsheet.batch_update({"requests": [req]})
            except Exception:  # noqa: BLE001
                pass

    def _style_cycle(self, ws) -> None:
        head, band = THEMES["cycle"]
        ws.batch_format(
            [
                {"range": "A1:P1", "format": _header(head)},
                {"range": "K1", "format": _header("#15803D")},  # รายรับ = เขียว
                {"range": "L1", "format": _header("#C2410C")},  # ส่วนแบ่งพนักงาน = ส้ม
                {"range": "M1", "format": _header("#1D4ED8")},  # รายได้ร้าน = น้ำเงิน
                {"range": "K2:M", "format": MONEY},
                {"range": "K2:K", "format": {"textFormat": {"foregroundColor": _rgb("#15803D"), "bold": True}}},
                {"range": "L2:L", "format": {"textFormat": {"foregroundColor": _rgb("#C2410C")}}},
                {"range": "M2:M", "format": {"textFormat": {"foregroundColor": _rgb("#1D4ED8"), "bold": True}}},
                {"range": "A2:A", "format": {"horizontalAlignment": "CENTER"}},
                # กล่องสรุปด้านขวา
                {"range": "Q1:S1", "format": {**_header("#B45309"), "textFormat": {"bold": True, "foregroundColor": WHITE, "fontSize": 12}}},
                {"range": "Q2:Q5", "format": {"backgroundColor": _rgb("#FEF3C7"), "textFormat": {"bold": True}}},
                {"range": "R2:R4", "format": {**MONEY, "backgroundColor": _rgb("#FFFBEB"), "textFormat": {"bold": True, "fontSize": 12}}},
                {"range": "R2", "format": {"textFormat": {"bold": True, "fontSize": 12, "foregroundColor": _rgb("#15803D")}}},
                {"range": "R3", "format": {"textFormat": {"bold": True, "fontSize": 12, "foregroundColor": _rgb("#C2410C")}}},
                {"range": "R4", "format": {"textFormat": {"bold": True, "fontSize": 12, "foregroundColor": _rgb("#1D4ED8")}}},
                {"range": "R5", "format": {"backgroundColor": _rgb("#FFFBEB"), "textFormat": {"bold": True, "fontSize": 12}}},
                {"range": "Q7:S7", "format": _header(head)},
                {"range": "R8:S", "format": MONEY},
            ]
        )
        ws.freeze(rows=1)
        self._batch(
            self._set_widths(ws, [60, 90, 70, 70, 150, 170, 150, 170, 220, 140, 100, 120, 100, 80, 110, 200])
            + self._set_widths(ws, [170, 110, 110], start=16)
            + [self._banding(ws, 16, band)]
        )

    def _style_simple(self, ws, headers: list[str], theme: str, *, widths: list[int], money_cols: str | None = None, hours_col: str | None = None) -> None:
        head, band = THEMES[theme]
        last = chr(ord("A") + len(headers) - 1)
        ws.update(values=[headers], range_name="A1")  # อัปเดตหัวตารางให้เป็นชื่อล่าสุด
        formats = [{"range": f"A1:{last}1", "format": _header(head)}, {"range": "A2:A", "format": {"horizontalAlignment": "CENTER"}}]
        if money_cols:
            formats.append({"range": f"{money_cols.split(':')[0]}2:{money_cols.split(':')[1]}", "format": {**MONEY, "textFormat": {"bold": True}}})
        if hours_col:
            formats.append({"range": f"{hours_col}2:{hours_col}", "format": {"numberFormat": {"type": "NUMBER", "pattern": "0.00"}, "horizontalAlignment": "CENTER"}})
        ws.batch_format(formats)
        ws.freeze(rows=1)
        self._batch(self._set_widths(ws, widths) + [self._banding(ws, len(headers), band)])

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
        self._ensure_styled(ws, "cycle")
        ws.append_row(row, value_input_option="USER_ENTERED", table_range="A1")

    async def append_attendance_row(self, row: list) -> bool:
        if not self.ready:
            return False
        async with self._lock:
            try:
                await asyncio.to_thread(self._append_attendance_sync, row)
                return True
            except Exception:  # noqa: BLE001
                log.exception("บันทึกเวลาเข้างานลง Google Sheets ไม่สำเร็จ")
                return False

    def _append_attendance_sync(self, row: list) -> None:
        import gspread

        assert self._spreadsheet is not None
        try:
            ws = self._spreadsheet.worksheet(ATTENDANCE_SHEET)
        except gspread.WorksheetNotFound:
            ws = self._spreadsheet.add_worksheet(title=ATTENDANCE_SHEET, rows=1000, cols=8)
        self._ensure_styled(ws, "attendance")
        ws.append_row(row, value_input_option="USER_ENTERED", table_range="A1")

    async def append_donation_row(self, row: list) -> bool:
        if not self.ready:
            return False
        async with self._lock:
            try:
                await asyncio.to_thread(self._append_simple_sync, DONATION_SHEET, DONATION_HEADERS, row)
                return True
            except Exception:  # noqa: BLE001
                log.exception("บันทึกโดเนทลง Google Sheets ไม่สำเร็จ")
                return False

    def _append_simple_sync(self, title: str, headers: list[str], row: list) -> None:
        import gspread

        assert self._spreadsheet is not None
        try:
            ws = self._spreadsheet.worksheet(title)
        except gspread.WorksheetNotFound:
            ws = self._spreadsheet.add_worksheet(title=title, rows=1000, cols=len(headers))
        self._ensure_styled(ws, "donation")
        ws.append_row(row, value_input_option="USER_ENTERED", table_range="A1")

    async def create_cycle_sheet(self, title: str) -> bool:
        if not self.ready:
            return False
        async with self._lock:
            try:
                ws = await asyncio.to_thread(self._get_or_create_ws, title)
                self._styled.add(ws.title)
                return True
            except Exception:  # noqa: BLE001
                log.exception("สร้างชีตรอบใหม่ (%s) ไม่สำเร็จ", title)
                return False

    async def spreadsheet_url(self) -> str | None:
        if not self.ready:
            return None
        return f"https://docs.google.com/spreadsheets/d/{self.cfg.get('google_sheets.spreadsheet_id')}"
