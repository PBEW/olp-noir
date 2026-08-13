"""กันไม่ให้บอทรันซ้อนกันหลายตัว

ถ้ารันซ้อนกัน ทุกตัวจะรับ event เดียวกันหมด ทำให้ลูกค้าได้ DM ซ้ำ,
บันทึกลง Google Sheets ซ้ำแถว, ให้ Role VIP ซ้ำ และแจ้งเตือนเวลาซ้ำ

ใช้ล็อกระดับ OS บนไฟล์ — ระบบปฏิบัติการจะปลดล็อกให้เองเมื่อโปรเซสตาย
จึงไม่มีปัญหาไฟล์ล็อกค้างตอนบอทดับกะทันหัน (ไม่ต้องลบไฟล์เอง)
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import IO


class SingleInstanceLock:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._fh: IO | None = None

    def acquire(self) -> bool:
        """คืน True ถ้าจองสิทธิ์รันได้ / False ถ้ามีบอทตัวอื่นถือล็อกอยู่แล้ว"""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self.path, "a+")
        try:
            fh.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.close()
            return False

        self._fh = fh
        return True

    def release(self) -> None:
        if self._fh is None:
            return
        try:
            self._fh.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            self._fh.close()
            self._fh = None
