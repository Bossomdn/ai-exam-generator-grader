"""เมนูหลายหน้า: หน้าครูเรียงตามลำดับงานจริง (ห้องเรียน → ออกข้อสอบ → สอบทั้งห้อง → คะแนน) แยกจากหน้าผู้เรียน

แต่ละหน้าเป็นไฟล์ใน views/ (AppTest สลับหน้าได้เฉพาะหน้าที่เป็นไฟล์) ซึ่งเรียก render() ของโมดูลใน ui/
"""

import streamlit as st

ROOMS = "views/rooms.py"
CREATE = "views/create.py"
CLASS = "views/class_exam.py"
SCORES = "views/scores.py"
PRACTICE = "views/practice.py"
FEEDBACK = "views/feedback.py"
BACKUP = "views/backup.py"

TITLES = {
    ROOMS: "ห้องเรียนและรายชื่อ",
    CREATE: "ออกข้อสอบ",
    CLASS: "สอบและตรวจทั้งห้อง",
    SCORES: "คะแนนและรายงาน",
    PRACTICE: "ฝึกทำข้อสอบ",
    FEEDBACK: "ผลและคำแนะนำ",
    BACKUP: "สำรองและย้ายเครื่อง",
}
SECTIONS = {
    "งานครู": [(ROOMS, ":material/counter_1:"), (CREATE, ":material/counter_2:"),
               (CLASS, ":material/counter_3:"), (SCORES, ":material/counter_4:")],
    "ผู้เรียน": [(PRACTICE, ":material/edit_note:"), (FEEDBACK, ":material/insights:")],
    "ระบบ": [(BACKUP, ":material/backup:")],
}


def pages() -> dict[str, list]:
    return {
        section: [st.Page(path, title=TITLES[path], icon=icon, default=path == CREATE) for path, icon in items]
        for section, items in SECTIONS.items()
    }


def link(path: str, label: str | None = None) -> None:
    """ปุ่มไปหน้าอื่น เช่น "ต่อไป: สอบและตรวจทั้งห้อง" """
    st.page_link(path, label=label or TITLES[path], icon=":material/arrow_forward:")
