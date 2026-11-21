"""หน้าครูที่ใช้ฐานข้อมูลโรงเรียน: ห้องเรียนและรายชื่อ (render_rooms) · คะแนนรายห้อง / รายคน / รายชั้น (render_scores)
· สำรอง / กู้คืนฐานข้อมูล (render_backup)"""

from datetime import date
from typing import Optional

import streamlit as st

from src import config, csv_import, school_db
from src.class_session import normalize_no, table_csv
from src.school_db import Classroom, SchoolDbError

from . import nav
from .common import state

BY_ROOM, BY_STUDENT, BY_GRADE = "รายห้อง", "รายคน", "รายชั้น"
MAX_ROOMS_AT_ONCE = 30


def render_rooms() -> None:
    st.caption("เพิ่มห้องและนำเข้ารายชื่อครั้งเดียวต่อปีการศึกษา ระบบใช้รายชื่อจับคู่กับเลขที่ที่ AI อ่านจากกระดาษคำตอบ")
    show_notice()
    rooms = load_rooms()
    if rooms is None:
        return
    classroom_section(rooms)
    if rooms:
        nav.link(nav.CREATE, "ต่อไป: ออกข้อสอบ")


def render_scores() -> None:
    st.caption("ผลสอบทุกครั้งที่ตรวจในหน้าสอบและตรวจทั้งห้อง · ตัวเลขเป็นร้อยละของคะแนนเต็ม นับเฉพาะผู้สอบที่ตรวจแล้ว")
    rooms = load_rooms()
    if rooms is None:
        return
    if not rooms:
        st.info("ยังไม่มีห้องเรียน")
        nav.link(nav.ROOMS, "เพิ่มห้องเรียนและรายชื่อ")
        return
    scores_section(rooms)


def render_backup() -> None:
    st.caption("รายชื่อนักเรียนและผลสอบทั้งหมดเก็บในไฟล์ฐานข้อมูลในเครื่องนี้ สำรองไว้กันข้อมูลหาย หรือใช้ย้ายไปเครื่องอื่น")
    show_notice()
    backup_section()


def load_rooms() -> Optional[list[Classroom]]:
    """ห้องทั้งหมด (None = เปิดฐานข้อมูลไม่ได้ แสดงข้อผิดพลาดและทางไปกู้คืนแล้ว)"""
    try:
        with school_db.connect() as conn:
            return school_db.classrooms(conn)
    except SchoolDbError as e:
        st.error(str(e))
        nav.link(nav.BACKUP, "กู้คืนจากไฟล์สำรอง")
        return None


def show_notice() -> None:
    if state.school_notice:
        message, warnings = state.school_notice
        st.success(message)
        if warnings:
            with st.expander(f"⚠️ คำเตือน ({len(warnings)})"):
                st.markdown("\n".join(f"- {w}" for w in warnings))
        state.school_notice = None


# ---------------- ห้องเรียนและรายชื่อ ----------------

def classroom_section(rooms: list[Classroom]) -> None:
    # แสดงฟอร์มตลอด (ไม่พับ) เพราะมักเพิ่มหลายห้องต่อกัน
    with st.container(border=True):
        st.markdown("**➕ เพิ่มห้องเรียน**")
        state.setdefault("new_room_year", school_db.current_term()[0])
        with st.form("add_room"):
            col_year, col_grade, col_room = st.columns(3)
            col_year.number_input("ปีการศึกษา (พ.ศ.)", min_value=2500, max_value=2700, step=1, key="new_room_year")
            col_grade.text_input("ชั้น", placeholder="เช่น ม.4", key="new_room_grade")
            col_room.text_input("ห้อง", placeholder="เช่น 1 หรือ 1-5 หรือ 1,3", key="new_room_numbers")
            st.form_submit_button("เพิ่มห้อง", on_click=add_rooms)
        st.caption("เพิ่มหลายห้องพร้อมกันได้ เช่น ห้อง 1-5 · เพิ่มแล้วปีและชั้นยังอยู่ แก้แค่เลขห้องแล้วกดเพิ่มต่อได้")
        if state.school_error:
            st.error(state.school_error)
            state.school_error = None
    if not rooms:
        st.info("ยังไม่มีห้องเรียน — เพิ่มห้องด้านบน แล้วนำเข้ารายชื่อนักเรียน")
        return
    with school_db.connect() as conn:
        sizes = {r.id: len(school_db.roster(conn, r.id)) for r in rooms}
    for year in sorted({r.year for r in rooms}, reverse=True):  # ห้องทั้งหมด แยกปีการศึกษา
        in_year = [r for r in rooms if r.year == year]
        st.markdown(f"**ปีการศึกษา {year}** ({len(in_year)} ห้อง): " + " · ".join(
            f"{r.name} ({sizes[r.id]} คน)" if sizes[r.id] else f"{r.name} (ยังไม่มีรายชื่อ)" for r in in_year))
    by_id = {r.id: r for r in rooms}
    if state.get("school_room") not in by_id:
        state.school_room = rooms[0].id
    room = by_id[st.selectbox("ห้องเรียน", list(by_id), format_func=lambda i: by_id[i].label, key="school_room")]
    roster_section(room)


def room_numbers(text: str) -> list[str]:
    """เลขห้องจากช่องกรอก: "1" / "1-5" / "1,3,5" (ผสมกันได้ เลขไทยได้) เรียงตามที่กรอก ไม่ซ้ำ"""
    numbers: list[str] = []
    for part in text.replace("，", ",").split(","):
        part = "".join(part.split())
        if not part:
            continue
        first, dash, last = part.partition("-")
        if dash:
            start, end = normalize_no(first), normalize_no(last)
            if not (start.isdigit() and end.isdigit()) or int(start) > int(end):
                raise SchoolDbError(f"ช่วงห้อง “{part}” ไม่ถูกต้อง — ใช้รูปแบบ 1-5")
            if int(end) - int(start) >= MAX_ROOMS_AT_ONCE:
                raise SchoolDbError(f"เพิ่มได้ครั้งละไม่เกิน {MAX_ROOMS_AT_ONCE} ห้อง")
            numbers += [str(n) for n in range(int(start), int(end) + 1)]
        else:
            numbers.append(normalize_no(part))
    if not numbers:
        raise SchoolDbError("กรอกเลขห้อง เช่น 1 หรือ 1-5")
    return list(dict.fromkeys(numbers))


def add_rooms() -> None:
    """กดเพิ่มห้อง (callback จึงล้างช่องเลขห้องได้ก่อนวาดหน้าใหม่) — ปีและชั้นคงไว้ให้เพิ่มห้องถัดไปได้เลย"""
    year, grade = int(state.new_room_year), state.new_room_grade
    try:
        numbers = room_numbers(state.new_room_numbers)
        with school_db.connect() as conn:
            existing = {r.id for r in school_db.classrooms(conn)}
            rooms = [school_db.add_classroom(conn, year, grade, n) for n in numbers]
    except SchoolDbError as e:
        state.school_error = str(e)
        return
    added = [r.name for r in rooms if r.id not in existing]
    already = [r.name for r in rooms if r.id in existing]
    message = f"เพิ่มห้อง {', '.join(added)} (ปีการศึกษา {year}) แล้ว" if added else "ไม่มีห้องใหม่"
    state.school_notice = (message, [f"{name} มีอยู่แล้ว — ข้าม" for name in already])
    state.school_room = rooms[-1].id
    state.new_room_numbers = ""


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

def scores_section(rooms: list[Classroom]) -> None:
    view = st.radio("ดูคะแนน", [BY_ROOM, BY_STUDENT, BY_GRADE], horizontal=True, key="school_view")
    if view == BY_GRADE:
        grade_view()
        return
    by_id = {r.id: r for r in rooms}
    if state.get("scores_room") not in by_id:  # เริ่มที่ห้องของการสอบที่เปิดอยู่
        sitting = state.class_sitting
        state.scores_room = sitting.classroom.id if sitting and sitting.classroom.id in by_id else rooms[0].id
    room = by_id[st.selectbox("ห้องเรียน", list(by_id), format_func=lambda i: by_id[i].label, key="scores_room")]
    with school_db.connect() as conn:
        rows = school_db.classroom_table(conn, room.id)
    if view == BY_ROOM:
        room_view(room, rows)
    else:
        student_view(room, rows)


def room_view(room: Classroom, rows: list[dict]) -> None:
    if not rows or len(rows[0]) <= 3:  # มีแค่ เลขที่ ชื่อ เฉลี่ย = ยังไม่มีการสอบ
        st.info(f"{room.name} ยังไม่มีผลสอบ — ตรวจแล้วผลจะมาอยู่ที่นี่")
        nav.link(nav.CLASS)
        return
    st.dataframe(rows, hide_index=True)
    if any(column.endswith("หลังซ่อม") for column in rows[0]):
        st.caption("การสอบที่มีสอบซ่อม: คอลัมน์ “หลังซ่อม” = คะแนนสอบซ่อมแต่ไม่เกินเกณฑ์ผ่าน และไม่ต่ำกว่าครั้งแรก "
                   "· ค่าเฉลี่ยใช้คะแนนหลังซ่อม")
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
        st.info("ยังไม่มีห้องเรียน")
        return
    year, grade = st.selectbox("ชั้น", levels, format_func=lambda g: f"{g[1]} ปีการศึกษา {g[0]}", key="school_grade")
    with school_db.connect() as conn:
        exams = school_db.grade_exam_table(conn, year, grade)
        concepts = school_db.grade_concept_table(conn, year, grade)
    if not exams:
        st.info(f"{grade} ปีการศึกษา {year} ยังไม่มีผลสอบ")
        return
    st.markdown("**ค่าเฉลี่ยของแต่ละห้อง แยกตามข้อสอบ** (สอบครั้งแรก ไม่รวมสอบซ่อมซึ่งสอบเฉพาะคนไม่ผ่าน)")
    st.dataframe(exams, hide_index=True)
    st.markdown("**ร้อยละรายหัวข้อ แยกห้อง** (เรียงจากหัวข้อที่ทั้งชั้นอ่อนที่สุด — ช่วยดูว่าควรสอนซ้ำเรื่องไหนที่ห้องใด)")
    st.dataframe(concepts, hide_index=True)


# ---------------- สำรอง / กู้คืน ----------------

def backup_section() -> None:
    try:
        with school_db.connect() as conn:
            n = school_db.counts(conn)
    except SchoolDbError as e:
        st.error(str(e))
        restore_section()  # ฐานข้อมูลเสีย / มาจากโค้ดใหม่กว่า ยังกู้คืนจากไฟล์สำรองได้
        return
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
        # การสอบที่เปิดค้างในหน้าสอบทั้งห้องอาจไม่มีในฐานข้อมูลใหม่
        state.class_session = state.class_sitting = state.class_import = None
        state.class_images = {}
        state.class_review_n += 1
        state.school_backup = None
        state.school_upload_n += 1
        state.school_notice = (
            "กู้คืนฐานข้อมูลแล้ว" + (f" · ข้อมูลเดิมสำรองไว้ที่ `{previous}`" if previous else ""), [])
        st.rerun()
