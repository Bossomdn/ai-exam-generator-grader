"""แท็บ 5 (ครู): ห้องเรียนและรายชื่อนักเรียน · คะแนนรายห้อง / รายคน / รายชั้น · สำรอง / กู้คืนฐานข้อมูล"""

from datetime import date
from typing import Optional

import streamlit as st

from src import config, csv_import, school_db
from src.class_session import table_csv
from src.school_db import Classroom, SchoolDbError

from .common import state

BY_ROOM, BY_STUDENT, BY_GRADE = "รายห้อง", "รายคน", "รายชั้น"


def render() -> None:
    st.caption("เก็บรายชื่อนักเรียนและผลสอบทุกครั้งไว้ในฐานข้อมูลในเครื่อง ดูคะแนนรายห้อง รายคน รายชั้น "
               "และสำรองข้อมูลไว้ย้ายเครื่อง")
    show_notice()
    try:
        with school_db.connect() as conn:
            rooms = school_db.classrooms(conn)
    except SchoolDbError as e:
        st.error(str(e))
        restore_section()  # ฐานข้อมูลเสีย / มาจากโค้ดใหม่กว่า ยังกู้คืนจากไฟล์สำรองได้
        return
    st.markdown("#### ห้องเรียนและรายชื่อ")
    room = classroom_section(rooms)
    st.markdown("#### คะแนน")
    scores_section(room)
    st.markdown("#### สำรอง / กู้คืน / ย้ายเครื่อง")
    backup_section()


def show_notice() -> None:
    if state.school_notice:
        message, warnings = state.school_notice
        st.success(message)
        if warnings:
            with st.expander(f"⚠️ คำเตือน ({len(warnings)})"):
                st.markdown("\n".join(f"- {w}" for w in warnings))
        state.school_notice = None


# ---------------- ห้องเรียนและรายชื่อ ----------------

def classroom_section(rooms: list[Classroom]) -> Optional[Classroom]:
    with st.expander("➕ เพิ่มห้องเรียน", expanded=not rooms):
        with st.form("add_room", clear_on_submit=True):
            col_year, col_grade, col_room = st.columns(3)
            year = col_year.number_input("ปีการศึกษา (พ.ศ.)", min_value=2500, max_value=2700,
                                         value=school_db.current_term()[0], step=1)
            grade = col_grade.text_input("ชั้น", placeholder="ม.4")
            room = col_room.text_input("ห้อง", placeholder="1")
            if st.form_submit_button("เพิ่มห้อง"):
                try:
                    with school_db.connect() as conn:
                        added = school_db.add_classroom(conn, int(year), grade, room)
                except SchoolDbError as e:
                    st.error(str(e))
                else:
                    state.school_room = added.id
                    st.rerun()
    if not rooms:
        st.info("ยังไม่มีห้องเรียน — เพิ่มห้องด้านบน แล้วนำเข้ารายชื่อนักเรียน")
        return None
    by_id = {r.id: r for r in rooms}
    if state.get("school_room") not in by_id:
        state.school_room = rooms[0].id
    room = by_id[st.selectbox("ห้องเรียน", list(by_id), format_func=lambda i: by_id[i].label, key="school_room")]
    roster_section(room)
    return room


def roster_section(room: Classroom) -> None:
    with school_db.connect() as conn:
        entries = school_db.roster(conn, room.id)
    if entries:
        with st.expander(f"รายชื่อนักเรียน {room.name} ({len(entries)} คน)"):
            st.dataframe([{"เลขที่": e.number, "รหัสนักเรียน": e.code, "ชื่อ-นามสกุล": e.name} for e in entries],
                         hide_index=True)
    else:
        st.info("ห้องนี้ยังไม่มีรายชื่อ — นำเข้ารายชื่อแล้ว ระบบจะใช้ชื่อจากรายชื่อตามเลขที่ที่ AI อ่านจากกระดาษคำตอบ "
                "(AI อ่านเลขที่แม่นกว่าชื่อลายมือ)")
    uploaded = st.file_uploader("นำเข้ารายชื่อ (.csv) — เพิ่มคนใหม่ / แก้ชื่อคนเดิมตามเลขที่", type=["csv"],
                                key=f"roster_{state.school_upload_n}")
    st.download_button("⬇️ เทมเพลตรายชื่อ", csv_import.roster_template(), file_name="roster_template.csv",
                       mime="text/csv")
    st.caption("คอลัมน์: เลขที่, รหัสนักเรียน (ไม่มีก็ได้ แต่ถ้ามีจะเชื่อมผลสอบของคนเดิมข้ามปีได้), ชื่อ-นามสกุล "
               "· ไฟล์ที่แยกคำนำหน้า / ชื่อ / นามสกุล เป็นคนละคอลัมน์ก็ใช้ได้")
    if st.button("นำเข้ารายชื่อ", disabled=uploaded is None):
        try:
            parsed, warnings = csv_import.parse_roster(uploaded.getvalue())
        except csv_import.CsvImportError as e:
            st.error(str(e))
            return
        with school_db.connect() as conn:
            added, existing = school_db.import_roster(conn, room.id, parsed)
        state.school_notice = (f"นำเข้ารายชื่อ {room.name}: เพิ่ม {added} คน · อัปเดต {existing} คน", warnings)
        state.school_upload_n += 1
        st.rerun()


# ---------------- คะแนน ----------------

def scores_section(room: Optional[Classroom]) -> None:
    view = st.radio("ดูคะแนน", [BY_ROOM, BY_STUDENT, BY_GRADE], horizontal=True, key="school_view")
    st.caption("ตัวเลขเป็นร้อยละของคะแนนเต็ม นับเฉพาะผู้สอบที่ตรวจแล้ว")
    if view == BY_GRADE:
        grade_view()
        return
    if room is None:
        st.info("เพิ่มห้องเรียนก่อน")
        return
    with school_db.connect() as conn:
        rows = school_db.classroom_table(conn, room.id)
    if view == BY_ROOM:
        room_view(room, rows)
    else:
        student_view(room, rows)


def room_view(room: Classroom, rows: list[dict]) -> None:
    if not rows or len(rows[0]) <= 3:  # มีแค่ เลขที่ ชื่อ เฉลี่ย = ยังไม่มีการสอบ
        st.info(f"{room.name} ยังไม่มีผลสอบ — ตรวจที่แท็บ 4 แล้วผลจะมาอยู่ที่นี่")
        return
    st.dataframe(rows, hide_index=True)
    st.download_button("⬇️ ตารางคะแนนห้องนี้ (CSV)", table_csv(rows), mime="text/csv",
                       file_name=f"scores_{room.grade}-{room.room}_{room.year}.csv")


def student_view(room: Classroom, rows: list[dict]) -> None:
    if not rows:
        st.info(f"{room.name} ยังไม่มีรายชื่อหรือผลสอบ")
        return
    pick = st.selectbox(
        "นักเรียน", range(len(rows)), index=None, placeholder="เลือกนักเรียน", key=f"school_student_{room.id}",
        format_func=lambda i: f"{rows[i]['เลขที่']} {rows[i]['ชื่อ']}".strip(),
    )
    if pick is None:
        return
    key = rows[pick]["เลขที่"] or rows[pick]["ชื่อ"]
    with school_db.connect() as conn:
        history = school_db.student_history(conn, room.id, key)
        concepts = school_db.student_concepts(conn, room.id, key)
    if not history:
        st.info("ยังไม่มีผลสอบที่ตรวจแล้ว")
        return
    st.markdown("**ผลสอบทุกครั้ง**")
    st.dataframe(history, hide_index=True)
    st.markdown(f"**คะแนนรายหัวข้อ รวมทุกครั้ง** (ต่ำกว่า {config.WEAK_THRESHOLD:g}% = ต้องเรียนเพิ่ม)")
    st.dataframe(
        [{**c, "": "⚠️ ต้องเรียนเพิ่ม" if c["ร้อยละ"] is not None and c["ร้อยละ"] < config.WEAK_THRESHOLD else ""}
         for c in concepts],
        hide_index=True,
    )


def grade_view() -> None:
    with school_db.connect() as conn:
        levels = school_db.grades(conn)
    if not levels:
        st.info("เพิ่มห้องเรียนก่อน")
        return
    year, grade = st.selectbox("ชั้น", levels, format_func=lambda g: f"{g[1]} ปีการศึกษา {g[0]}", key="school_grade")
    with school_db.connect() as conn:
        exams = school_db.grade_exam_table(conn, year, grade)
        concepts = school_db.grade_concept_table(conn, year, grade)
    if not exams:
        st.info(f"{grade} ปีการศึกษา {year} ยังไม่มีผลสอบ")
        return
    st.markdown("**ค่าเฉลี่ยของแต่ละห้อง แยกตามข้อสอบ**")
    st.dataframe(exams, hide_index=True)
    st.markdown("**ร้อยละรายหัวข้อ แยกห้อง** (เรียงจากหัวข้อที่ทั้งชั้นอ่อนที่สุด — ช่วยดูว่าควรสอนซ้ำเรื่องไหนที่ห้องใด)")
    st.dataframe(concepts, hide_index=True)


# ---------------- สำรอง / กู้คืน ----------------

def backup_section() -> None:
    with school_db.connect() as conn:
        n = school_db.counts(conn)
    st.caption(
        f"ไฟล์ฐานข้อมูล: `{config.DB_PATH}` (ย้ายตำแหน่งได้ที่ `DATA_DIR` ในไฟล์ `.env`) · "
        f"ห้อง {n['classrooms']} · นักเรียน {n['students']} · การสอบ {n['sittings']} · คำตอบ {n['submissions']}"
    )
    col_backup, col_restore = st.columns(2)
    with col_backup:
        st.markdown("**สำรอง / ย้ายไปเครื่องอื่น**")
        if st.button("สร้างไฟล์สำรอง"):
            state.school_backup = school_db.backup_bytes()
        if state.school_backup:
            st.download_button("⬇️ ดาวน์โหลดไฟล์สำรอง (.db)", state.school_backup,
                               file_name=f"school-{date.today():%Y%m%d}.db", mime="application/octet-stream")
        st.caption("ไฟล์มีข้อมูลส่วนบุคคลของนักเรียน — เก็บในที่ปลอดภัย ห้ามขึ้น GitHub หรือแชร์สาธารณะ")
    with col_restore:
        restore_section()


def restore_section() -> None:
    st.markdown("**กู้คืนจากไฟล์สำรอง**")
    uploaded = st.file_uploader("ไฟล์สำรอง (.db)", type=["db"], key=f"restore_{state.school_upload_n}")
    confirm = st.checkbox("แทนที่ข้อมูลทั้งหมดในเครื่องนี้ด้วยไฟล์นี้ (ข้อมูลเดิมจะถูกสำรองไว้ในโฟลเดอร์ backups ก่อน)",
                          key=f"restore_confirm_{state.school_upload_n}")
    if st.button("กู้คืน", disabled=not (uploaded and confirm)):
        try:
            previous = school_db.restore(uploaded.getvalue())
        except SchoolDbError as e:
            st.error(str(e))
            return
        # การสอบที่เปิดค้างในแท็บ 4 อาจไม่มีในฐานข้อมูลใหม่
        state.class_session = state.class_sitting = state.class_import = None
        state.class_images = {}
        state.class_review_n += 1
        state.school_backup = None
        state.school_upload_n += 1
        state.school_notice = (
            "กู้คืนฐานข้อมูลแล้ว" + (f" · ข้อมูลเดิมสำรองไว้ที่ `{previous}`" if previous else ""), [])
        st.rerun()
