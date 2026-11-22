"""หน้าสอบและตรวจทั้งห้อง (ครู): เลือกห้อง → พิมพ์ข้อสอบ → สอบบนกระดาษ → อัปโหลดกระดาษคำตอบทีละหลายใบ (หรือ CSV)
→ AI อ่านคำตอบ → ครูตรวจทาน → ตรวจทั้งห้อง → ตารางคะแนน → วิเคราะห์ข้อสอบจากผลจริง (ui/analysis_section.py)

แถบขั้นตอนด้านบนบอกว่าทำถึงไหน ขั้นที่เสร็จแล้วพับเก็บ ขั้นที่ต้องทำต่อเปิดไว้
ทุกการเปลี่ยนแปลงบันทึกลงฐานข้อมูล (src/school_db.py) ทันที ปิดแล้วเปิดกลับมาทำต่อได้"""

import sqlite3
from dataclasses import dataclass

import streamlit as st

from src import (
    answer_reader, class_session, config, csv_import, exam_forms, export, item_analysis, retest, school_db, scoring,
    sources, teacher_review,
)
from src.class_session import normalize_no
from src.llm import LLMError
from src.schemas import OBJECTIVE_TYPES, TYPE_LABELS, ClassSession, Exam, ExamForm, StudentRecord

from . import analysis_section, nav
from .answer_form import answer_fields, collect, prefill
from .common import panel_text, show_panel, show_progress, state, use_exam
from .create_tab import render_answer_key
from .result_tab import show_question_details

SHEETS = "สแกนกระดาษคำตอบ (PDF / รูปถ่าย)"
CSV = "ไฟล์ CSV (กรอกคำตอบเอง)"
STEP_STYLE = {  # สถานะ: (ไอคอน, สี)
    "done": (":material/check_circle:", "green"),
    "now": (":material/arrow_circle_right:", "blue"),
    "todo": (":material/radio_button_unchecked:", "gray"),
}


@dataclass
class Step:
    title: str
    short: str  # ชื่อสั้นในแถบขั้นตอน
    status: str  # done / now / todo
    detail: str
    shown: bool = True  # แสดงส่วนของขั้นนี้ไหม (ตรวจทานแสดงเฉพาะตอนมีคนรอ)
    expanded: bool = False


def render(ready: bool) -> None:
    st.caption("สอบบนกระดาษ แล้วสแกนหรือถ่ายรูปกระดาษคำตอบอัปโหลดทีละหลายใบ ให้ AI อ่านคำตอบ ครูตรวจทาน แล้วตรวจทั้งห้อง")
    try:
        with school_db.connect() as conn:
            rooms, saved = school_db.classrooms(conn), school_db.sittings(conn)
    except school_db.SchoolDbError as e:
        st.error(str(e))
        nav.link(nav.BACKUP, "กู้คืนจากไฟล์สำรอง")
        return
    if state.class_session is None:
        start_session(rooms, saved)
        return
    show_header(state.class_session)
    steps = class_steps(state.class_session, state.class_sitting)
    show_stepper(steps)
    sections = [lambda: print_section(state.class_session.exam), lambda: import_section(ready), review_section,
                lambda: grade_section(ready)]
    if state.class_sitting.retest_of:
        sections.append(after_retest_section)
    else:
        sections += [lambda: analysis_section.render(ready), lambda: retest_section(ready)]
    for n, (step, section) in enumerate(zip(steps, sections), start=1):
        if step.shown:
            # key เปลี่ยนตามสถานะ: ขั้นที่เพิ่งเสร็จพับลง ขั้นถัดไปเปิดขึ้นเอง (ระหว่างนั้นครูเปิด/ปิดเองได้)
            with st.expander(f"{n}. {step.title}", expanded=step.expanded, key=f"class_step_{n}_{step.expanded}",
                             icon=STEP_STYLE[step.status][0]):
                section()


def class_steps(session: ClassSession, sitting: school_db.Sitting) -> list[Step]:
    """สถานะของแต่ละขั้นจากข้อมูลห้อง: ขั้นที่ต้องทำต่อ = ขั้นแรกที่ยังไม่เสร็จ
    การสอบซ่อมไม่วิเคราะห์ข้อสอบ (ผู้สอบเป็นกลุ่มที่ไม่ผ่านเท่านั้น) แต่สรุปคะแนนหลังซ่อมแทน"""
    n = len(session.students)
    waiting = len(class_session.awaiting_review(session))
    pending = len(class_session.ready_to_grade(session))
    graded = sum(1 for s in session.students if s.results is not None)
    teacher = len(teacher_review.pending(session))  # เรียงความที่คณะผู้ตรวจเห็นต่างมาก รอครูตัดสิน
    finished = graded > 0 and not waiting and not pending and not teacher
    imported = "done" if n else "now"
    reviewed = "todo" if not n else "now" if waiting else "done"
    scored = "now" if pending or teacher else "done" if n and graded == n else "todo"
    score_detail = f"ตรวจแล้ว {graded}/{n}" + (f" · รอครูตัดสิน {teacher} ข้อ" if teacher else "")
    steps = [
        Step("พิมพ์ข้อสอบไปสอบ", "พิมพ์ข้อสอบ", "done" if n or state.class_exam_pdf else "todo",
             f"{len(sitting.forms)} ชุดสลับ" if sitting.forms else "ไม่บังคับ", expanded=not n),
        Step("นำเข้าคำตอบ", "นำเข้าคำตอบ", imported, f"{n} คน" if n else "สแกนกระดาษ / CSV", expanded=not n),
        Step("ตรวจทานคำตอบที่ AI อ่าน", "ตรวจทาน", reviewed, f"รอ {waiting} คน" if waiting else "ครบแล้ว" if n else "",
             shown=bool(waiting), expanded=True),
        Step("ตรวจและดูคะแนน", "ตรวจคะแนน", scored, score_detail if n else "", shown=bool(n),
             expanded=bool(pending or graded)),
    ]
    if sitting.retest_of:
        steps.append(Step("คะแนนหลังสอบซ่อม", "หลังซ่อม", "now" if finished else "todo",
                          f"ซ่อมได้ไม่เกิน {sitting.pass_percent:g}%", shown=graded > 0, expanded=True))
        return steps
    can_analyze = graded >= item_analysis.MIN_STUDENTS
    below = len(retest.below_pass(session, sitting.pass_percent))
    with school_db.connect() as conn:
        has_retest = bool(school_db.retests(conn, sitting.id))
    steps += [
        Step("วิเคราะห์ข้อสอบจากผลสอบจริง", "วิเคราะห์ข้อสอบ", "now" if can_analyze and finished else "todo",
             "พร้อม" if can_analyze else f"ต้องตรวจอย่างน้อย {item_analysis.MIN_STUDENTS} คน",
             shown=graded > 0, expanded=True),
        Step("สอบซ่อม", "สอบซ่อม", "done" if has_retest else "todo",
             f"ต่ำกว่าเกณฑ์ {below} คน" if below else "ทุกคนผ่านเกณฑ์" if graded else "", shown=graded > 0,
             expanded=bool(finished and (below or has_retest))),
    ]
    return steps


def show_stepper(steps: list[Step]) -> None:
    for col, (n, step) in zip(st.columns(len(steps)), enumerate(steps, start=1)):
        icon, color = STEP_STYLE[step.status]
        col.badge(f"{n}. {step.short}", icon=icon, color=color)
        col.caption(step.detail)


def reset_class(session: ClassSession | None, sitting: school_db.Sitting | None = None) -> None:
    state.class_session = state.class_saved_session = session
    state.class_sitting = sitting
    state.class_import = None
    state.class_images = {}
    state.class_exam_pdf = None
    state.item_report = state.item_revision = None
    state.class_review_n += 1


def start_session(rooms: list[school_db.Classroom], saved: list[school_db.Sitting]) -> None:
    exam = state.exam
    st.markdown("#### เริ่มการสอบ")
    if not rooms:
        st.info("ยังไม่มีห้องเรียน — เพิ่มห้องและนำเข้ารายชื่อนักเรียนก่อน")
        nav.link(nav.ROOMS, "เพิ่มห้องเรียนและรายชื่อ")
    elif exam is None:
        st.info("ยังไม่มีข้อสอบ — สร้างข้อสอบก่อน แล้วกลับมาเริ่มสอบที่หน้านี้")
        nav.link(nav.CREATE, "ไปออกข้อสอบ")
    else:
        col_room, col_term = st.columns([3, 1])
        room = col_room.selectbox("ห้องที่สอบ", rooms, format_func=lambda c: c.label, key="class_room")
        term = col_term.selectbox("ภาคเรียน", [1, 2, 3], index=school_db.current_term()[1] - 1, key="class_term")
        if st.button(f"เริ่มเก็บคำตอบทั้งห้องด้วยข้อสอบ “{exam.title}”", type="primary"):
            with school_db.connect() as conn:
                sitting_id = school_db.open_sitting(conn, exam, room.id, term)
            open_sitting(sitting_id)
        st.caption("ถ้าห้องนี้เคยสอบข้อสอบชุดนี้แล้ว จะเปิดข้อมูลเดิมมาทำต่อ")
    if saved:
        st.markdown("#### เปิดการสอบที่บันทึกไว้")
        by_id = {s.id: s for s in saved}
        pick = st.selectbox(
            "การสอบ", list(by_id), key="class_saved",
            format_func=lambda i: f"{by_id[i].label} · ผู้สอบ {by_id[i].students} คน (ตรวจแล้ว {by_id[i].graded})",
        )
        if st.button("เปิดการสอบนี้"):
            open_sitting(pick)


def open_sitting(sitting_id: int) -> None:
    with school_db.connect() as conn:
        sitting, session = school_db.load_session(conn, sitting_id)
    reset_class(session, sitting)
    if state.exam is None:  # ให้แท็บอื่นเห็นข้อสอบชุดนี้ด้วย (ดูเฉลย)
        use_exam(session.exam)
    st.rerun()


def save(session: ClassSession) -> None:
    """เก็บข้อมูลห้องในแอป และบันทึกลงฐานข้อมูลทันที"""
    try:
        with school_db.connect() as conn:
            school_db.save_session(conn, state.class_sitting.id, session, expected=state.class_saved_session)
    except school_db.SchoolDbError as e:
        st.error(str(e))
        try:
            with school_db.connect() as conn:
                state.class_sitting, state.class_session = school_db.load_session(conn, state.class_sitting.id)
            state.class_saved_session = state.class_session
            state.class_review_n += 1
        except school_db.SchoolDbError:
            reset_class(None)
        st.stop()
    except sqlite3.Error as e:
        st.error(f"บันทึกการสอบไม่สำเร็จ ({e}) กรุณาลองอีกครั้ง")
        st.stop()
    state.class_session = state.class_saved_session = session


def roster_names() -> dict[str, str]:
    """รายชื่อห้องของการสอบนี้ {เลขที่: ชื่อ} (ว่าง = ห้องนี้ยังไม่ได้นำเข้ารายชื่อ)"""
    with school_db.connect() as conn:
        return {e.number: e.name for e in school_db.roster(conn, state.class_sitting.classroom.id)}


def show_header(session: ClassSession) -> None:
    exam, sitting = session.exam, state.class_sitting
    col_info, col_close = st.columns([5, 1], vertical_alignment="center")
    kind = "สอบซ่อม · " if sitting.retest_of else ""
    col_info.markdown(f"### {kind}{sitting.classroom.name} · ภาคเรียน {sitting.term}/{sitting.classroom.year}")
    forms = f" · {len(sitting.forms)} ชุดสลับ ({' '.join(f.code for f in sitting.forms)})" if sitting.forms else ""
    col_info.caption(f"ข้อสอบ: **{exam.title}** · {len(exam.questions)} ข้อ{forms} · บันทึกลงฐานข้อมูลในเครื่องอัตโนมัติ "
                     "(ไม่รวมภาพกระดาษ)")
    if col_close.button("ปิดการสอบนี้", help="เปิดกลับมาทำต่อได้จากรายการการสอบที่บันทึกไว้"):
        reset_class(None)
        st.rerun()
    roster = roster_names()
    if sitting.takers is not None:  # สอบซ่อม: เฉพาะคนที่มีสิทธิ์
        st.caption(f"ผู้มีสิทธิ์สอบซ่อม {len(sitting.takers)} คน: เลขที่ {', '.join(sitting.takers)}")
        roster = {no: roster.get(no, no) for no in sitting.takers}
    if not roster:
        st.caption("ห้องนี้ยังไม่มีรายชื่อนักเรียน — ถ้านำเข้ารายชื่อ ระบบจะใช้ชื่อจากรายชื่อตามเลขที่ "
                   "แทนชื่อที่ AI อ่านจากลายมือ")
        nav.link(nav.ROOMS, "นำเข้ารายชื่อ")
    elif missing := class_session.absent(session, roster):
        st.caption(f"ยังไม่มีคำตอบ {len(missing)} คน: เลขที่ {', '.join(missing)}")
    if state.exam and state.exam != exam and not sitting.retest_of:
        st.info("การสอบนี้ใช้ข้อสอบชุดก่อน ไม่ใช่ชุดล่าสุดในหน้าออกข้อสอบ — ถ้าจะใช้ชุดล่าสุด ให้ปิดการสอบนี้แล้วเริ่มใหม่")


def print_section(exam: Exam) -> None:
    st.caption("ให้ผู้สอบเขียนชื่อ เลขที่ (ให้ชัด) และคำตอบลงในข้อสอบ (ปรนัยให้วงกลมตัวเลือก) "
               "ระบบใช้เลขที่ที่ AI อ่านได้จับคู่กับรายชื่อห้อง")
    forms_picker(exam)
    forms = list(state.class_sitting.forms)
    if st.button("📄 สร้าง PDF ข้อสอบ + เฉลย" + (f" ({len(forms)} ชุด)" if forms else "")):
        with st.spinner("กำลังสร้าง PDF..."):
            try:
                state.class_exam_pdf = {
                    f.code: (export.exam_pdf(exam_forms.form_exam(exam, f), f.code),
                             export.answer_key_pdf(exam_forms.form_exam(exam, f), f.code))
                    for f in forms
                } or {"": (export.exam_pdf(exam), export.answer_key_pdf(exam))}
            except export.ExportError as e:
                st.error(str(e))
    for code, (exam_file, key_file) in (state.class_exam_pdf or {}).items():
        name = f"ชุด {code}" if code else ""
        col_exam, col_key = st.columns(2)
        col_exam.download_button(f"⬇️ ข้อสอบ{name} (แจกผู้สอบ)", exam_file, file_name=f"exam{code}.pdf",
                                 mime="application/pdf")
        col_key.download_button(f"⬇️ เฉลย{name} (สำหรับครู)", key_file, file_name=f"answer_key{code}.pdf",
                                mime="application/pdf")
    with st.expander("ดูข้อสอบพร้อมเฉลย (ต้นฉบับ)"):
        for q in exam.questions:
            render_answer_key(q)


def forms_picker(exam: Exam) -> None:
    """จำนวนชุดสลับกันลอก (เปลี่ยนได้จนกว่าจะนำเข้าคำตอบ เพราะคำตอบที่นำเข้าแล้วอ้างลำดับของชุดเดิม)"""
    sitting = state.class_sitting
    current = max(len(sitting.forms), 1)
    locked = bool(state.class_session.students)
    count = st.radio(
        "ชุดข้อสอบ (ข้อเดิม สลับลำดับข้อและตัวเลือกกันลอก)", range(1, exam_forms.MAX_FORMS + 1), index=current - 1,
        horizontal=True, disabled=locked, key=f"class_forms_{sitting.id}_{current}",
        format_func=lambda n: "ชุดเดียว" if n == 1 else f"{n} ชุด ({' '.join(exam_forms.FORM_CODES[:n])})",
    )
    if locked and sitting.forms:
        st.caption("นำเข้าคำตอบแล้ว จึงเปลี่ยนจำนวนชุดไม่ได้")
    elif sitting.forms:
        st.caption("แจกสลับชุดตามที่นั่ง ตอนตรวจ AI อ่านรหัสชุดจากหัวกระดาษเอง คะแนนทุกชุดรวมเป็นข้อสอบชุดเดียว")
    if count != current:
        with school_db.connect() as conn:
            school_db.set_forms(conn, sitting.id, exam_forms.make_forms(exam, count, seed=sitting.id))
            state.class_sitting = school_db.sittings(conn, sitting.id)[0]
        state.class_exam_pdf = None
        st.rerun()


# ---------------- นำเข้าคำตอบ ----------------

def import_section(ready: bool) -> None:
    method = st.radio("นำเข้าจาก", [SHEETS, CSV], horizontal=True, key="class_import_method")
    if method == SHEETS:
        sheet_import(ready)
    else:
        csv_upload()
    report = state.class_import
    if report:
        st.success(f"นำเข้าจาก{report['source']}: เพิ่ม {report['added']} คน · อัปเดต {report['updated']} คน" + report["extra"])
        if report["warnings"]:
            with st.expander(f"⚠️ คำเตือนจากการนำเข้า ({len(report['warnings'])})"):
                st.markdown("\n".join(f"- {w}" for w in report["warnings"]))


def sheet_import(ready: bool) -> None:
    files = st.file_uploader(
        "กระดาษคำตอบ เลือกได้หลายไฟล์", type=["pdf", "png", "jpg", "jpeg"], accept_multiple_files=True,
        key=f"class_sheets_{state.class_upload_n}",
    )
    combined = st.radio(
        "รูปแบบไฟล์", ["1 ไฟล์ = ผู้สอบ 1 คน", "PDF ไฟล์เดียวรวมทุกคน (สแกนทั้งปึก)"], horizontal=True,
        key="class_sheet_layout",
    ) != "1 ไฟล์ = ผู้สอบ 1 คน"
    pages_per = None
    if combined:
        pages_per = int(st.number_input(
            "กระดาษคำตอบคนละกี่หน้า", min_value=1, max_value=answer_reader.MAX_PAGES, value=1, key="class_pages_per"
        ))
    form_code = ""
    if forms := state.class_sitting.forms:
        form_code = st.radio(
            "ชุดของกระดาษที่อัปโหลด", [""] + [f.code for f in forms], horizontal=True, key="class_sheet_form",
            format_func=lambda c: f"ชุด {c} ทั้งหมด" if c else "ปนกัน (ให้ AI อ่านรหัสชุดจากหัวกระดาษ)",
        )
        st.caption("ถ้ารหัสชุดบนหัวกระดาษไม่ตรงกับที่เลือก ระบบอ่านใหม่ตามชุดที่พิมพ์จริงให้เอง (ใช้เวลาเพิ่มเฉพาะใบนั้น)")
    st.caption("AI อ่านประมาณ 5–15 วินาทีต่อคน (Ollama · คนแรกช้ากว่าเพราะต้องโหลดโมเดล) "
               "ทุกคนต้องผ่านการตรวจทานในขั้นที่ 3 ก่อนตรวจคะแนน")
    if st.button("อ่านคำตอบด้วย AI", type="primary", disabled=not files or not ready):
        read_sheets(files, pages_per, form_code)


def read_sheets(files, pages_per: int | None, form_code: str = "") -> None:
    exam, forms = state.class_session.exam, list(state.class_sitting.forms)
    sheets, problems = [], []
    for f in files:
        try:
            if pages_per:
                if not f.name.lower().endswith(".pdf"):
                    raise answer_reader.SheetError("แบบรวมทั้งปึกใช้ได้กับไฟล์ PDF เท่านั้น")
                sheets += answer_reader.split_pdf(f.name, f.getvalue(), pages_per)
            else:
                sheets.append(answer_reader.load_answer_sheet(f.name, f.getvalue()))
        except answer_reader.SheetError as e:
            problems.append(f"{f.name}: {e}")

    students = []
    bar = st.progress(0.0)
    for i, sheet in enumerate(sheets):
        bar.progress(i / len(sheets), text=f"AI กำลังอ่าน {sheet.name} ({i + 1}/{len(sheets)})")
        try:
            students.append(class_session.read_sheet(exam, sheet, forms, form_code))
        except LLMError as e:
            problems.append(f"{sheet.name}: {e} — หยุดอ่านใบที่เหลือ อ่านใหม่ได้ภายหลัง")
            break
        state.class_images[sheet.name] = sheet.images
    students = class_session.dedupe(class_session.apply_roster(students, roster_names()))
    merged, added, updated = class_session.merge_students(state.class_session, students)
    flagged = sum(1 for s in students if s.import_notes)
    save(merged)
    state.class_import = {
        "source": "กระดาษคำตอบ", "added": added, "updated": updated,
        "extra": f" · มีคำเตือน {flagged} คน (ดูในขั้นที่ 3)" if flagged else "",
        "warnings": problems,
    }
    state.class_upload_n += 1  # ล้างช่องอัปโหลด กันกดอ่านซ้ำ
    state.class_review_n += 1
    st.rerun()


def csv_upload() -> None:
    exam, forms = state.class_session.exam, list(state.class_sitting.forms)
    uploaded = st.file_uploader("ไฟล์คำตอบ (.csv)", type=["csv"], key=f"class_csv_{state.class_upload_n}")
    st.download_button(
        "⬇️ เทมเพลต CSV", csv_import.template_csv(exam, forms), file_name="answers_template.csv", mime="text/csv",
    )
    st.caption("กรอกเทมเพลต: ปรนัยใส่ ก ข ค ง หรือข้อความตัวเลือก · ถูก/ผิด ใส่ “ถูก” หรือ “ผิด” · อัตนัยใส่ข้อความคำตอบ"
               + (" · คอลัมน์ “ชุด” ใส่ชุดที่คนนั้นสอบ แล้วกรอกเลขข้อและตัวเลือกตามที่พิมพ์ในชุดนั้น" if forms else ""))
    if st.button("นำเข้าคำตอบ", type="primary", disabled=uploaded is None):
        try:
            result = csv_import.parse_responses(exam, uploaded.getvalue(), forms)
        except csv_import.CsvImportError as e:
            st.error(str(e))
            return
        students = class_session.apply_roster(result.students, roster_names())
        merged, added, updated = class_session.merge_students(state.class_session, students)
        save(merged)
        state.class_import = {
            "source": " CSV", "added": added, "updated": updated,
            "extra": f" · จับคู่คอลัมน์ได้ {len(result.matched_questions)}/{len(exam.questions)} ข้อ",
            "warnings": result.warnings + [f"{s.name} — {n}" for s in students for n in s.import_notes],
        }
        state.class_upload_n += 1  # ล้างช่องอัปโหลด กันกดนำเข้าซ้ำแล้วผลตรวจหาย
        state.class_review_n += 1
        st.rerun()


# ---------------- ตรวจทาน ----------------

def review_section() -> None:
    session = state.class_session
    pending = class_session.awaiting_review(session)
    clean = [i for i in pending if not session.students[i].import_notes]
    st.caption(
        f"รอตรวจทาน {len(pending)} คน (มีคำเตือน {len(pending) - len(clean)} คน) — "
        "เทียบคำตอบกับภาพกระดาษ แก้ส่วนที่ AI อ่านผิด แล้วกดยืนยัน"
    )
    if clean:
        if st.button(f"ยืนยันทุกคนที่ไม่มีคำเตือน ({len(clean)} คน)"):
            for i in clean:
                session = class_session.update_student(
                    session, i, session.students[i].model_copy(update={"reviewed": True}))
            save(session)
            state.class_review_n += 1
            st.rerun()
        st.caption("AI อาจอ่านผิดโดยไม่มีคำเตือนได้ (เช่น วงตัวเลือกไม่ชัด) ควรสุ่มเปิดดูบางคนก่อนยืนยันรวด")

    def label(i: int) -> str:
        s = session.students[i]
        mark = f"⚠️ {len(s.import_notes)} คำเตือน" if s.import_notes else "✓ ไม่มีคำเตือน"
        return f"{s.student_no} {s.name}".strip() + f" — {mark}"

    index = st.selectbox("เลือกผู้สอบที่จะตรวจทาน", pending, format_func=label, key=f"review_pick_{state.class_review_n}")
    if index is not None:
        review_student(index)


def printed_form(student: StudentRecord) -> ExamForm | None:
    """ชุดที่ผู้สอบคนนี้ทำ (None = ชุดเดียว) ใช้แสดงเลขข้อและตัวเลือกตามที่พิมพ์ในกระดาษของเขา"""
    return exam_forms.find(list(state.class_sitting.forms), student.form) if student.form else None


def review_student(index: int) -> None:
    session = state.class_session
    student = session.students[index]
    form = printed_form(student)
    exam = exam_forms.form_exam(session.exam, form) if form else session.exam
    roster = roster_names()
    prefix = f"rev_{state.class_review_n}_{index}"
    if roster:
        state.setdefault(f"{prefix}_no", student.student_no if student.student_no in roster else None)
    else:
        state.setdefault(f"{prefix}_name", student.name)
        state.setdefault(f"{prefix}_no", student.student_no)
    prefill(exam, prefix, exam_forms.to_form(form, student.answers) if form else student.answers)

    col_image, col_form = st.columns(2)
    with col_image:
        images = state.class_images.get(student.sheet_name)
        if images:
            for page, image in enumerate(images, start=1):
                st.image(image, caption=f"{student.sheet_name} · หน้า {page}")
        else:
            st.info("ไม่มีภาพกระดาษ (ภาพไม่ได้บันทึกในฐานข้อมูล) — อัปโหลดกระดาษใบนี้ใหม่ถ้าต้องการเทียบ")
    with col_form:
        for note in student.import_notes:
            if not note.startswith("ข้อ "):  # คำเตือนรายข้อแสดงในฟอร์มอยู่แล้ว
                st.warning(f"⚠️ {note}")
        if roster and student.extracted and student.extracted.student_name.strip():
            st.caption(f"ชื่อที่ AI อ่านจากกระดาษ: {student.extracted.student_name}")
        if form:
            st.caption(f"ชุด {form.code} — เลขข้อและตัวเลือกด้านล่างเรียงตามที่พิมพ์ในชุดนี้ "
                       "(ถ้าชุดผิด ให้อัปโหลดใบนี้ใหม่โดยเลือกชุดให้ถูก)")
        extracted = {a.question_id: a for a in student.extracted.answers} if student.extracted else None
        with st.form(f"{prefix}_form"):
            if roster:
                st.selectbox("ผู้สอบ (จากรายชื่อห้อง)", list(roster), format_func=lambda no: f"เลขที่ {no} {roster[no]}",
                             placeholder="เลือกนักเรียน", key=f"{prefix}_no")
            else:
                st.text_input("ชื่อ-นามสกุล", key=f"{prefix}_name")
                st.text_input("เลขที่", key=f"{prefix}_no")
            answer_fields(exam, prefix, extracted)
            if st.form_submit_button("ยืนยันคำตอบของคนนี้", type="primary"):
                confirm_review(index, prefix, roster)


def confirm_review(index: int, prefix: str, roster: dict[str, str]) -> None:
    session = state.class_session
    student = session.students[index]
    form = printed_form(student)
    answers = collect(exam_forms.form_exam(session.exam, form) if form else session.exam, prefix)
    if roster:
        number = state[f"{prefix}_no"]
        if number is None:
            st.error("เลือกผู้สอบจากรายชื่อห้องก่อนยืนยัน")
            return
        name = roster[number]
    else:
        number = normalize_no(state[f"{prefix}_no"])
        name = state[f"{prefix}_name"].strip() or student.name
    taken = [s.name for i, s in enumerate(session.students) if i != index and number and s.student_no == number]
    if taken:
        st.error(f"เลขที่ {number} มีคำตอบของ “{taken[0]}” อยู่แล้ว — ตรวจดูว่าใบไหนเลขที่ผิด")
        return
    confirmed = student.model_copy(update={
        "name": name, "student_no": number, "answers": exam_forms.to_base(form, answers) if form else answers,
        "reviewed": True, "results": None,
    })
    save(class_session.update_student(session, index, confirmed))
    state.class_review_n += 1
    st.rerun()


# ---------------- ตรวจและดูคะแนน ----------------

def grade_section(ready: bool) -> None:
    session = state.class_session
    if not session.students:
        st.info("ยังไม่มีคำตอบ นำเข้าในขั้นที่ 2 ก่อน")
        return
    if state.class_graded:
        st.success(state.class_graded)
        state.class_graded = None
    pending = class_session.ready_to_grade(session)
    waiting = class_session.awaiting_review(session)
    needs_llm = class_session.needs_llm(session.exam)
    if pending:
        if st.button(f"ตรวจคำตอบที่ยังไม่ได้ตรวจ ({len(pending)} คน)", type="primary", disabled=needs_llm and not ready):
            grade_all(pending)
        st.caption(grading_note(session.exam) if needs_llm else "ข้อสอบปรนัยล้วน ตรวจด้วยโค้ดทันที ไม่ต้องใช้โมเดล")
    if waiting:
        st.caption(f"ยังมี {len(waiting)} คนรอตรวจทาน — ต้องตรวจทานในขั้นที่ 3 ก่อนจึงจะตรวจคะแนนได้")

    session = state.class_session
    rows = class_session.score_rows(session)
    st.dataframe(rows, hide_index=True)
    if teacher_review.pending(session):
        st.caption("คะแนนของผู้สอบที่รอครูตัดสินเป็นคะแนนชั่วคราว")
    percents = [r["ร้อยละ"] for r in rows if r["ร้อยละ"] is not None]
    if percents:
        st.caption(
            f"ตรวจแล้ว {len(percents)} คน · เฉลี่ย {sum(percents) / len(percents):.1f}% · "
            f"สูงสุด {max(percents):g}% · ต่ำสุด {min(percents):g}%"
        )
        col_csv, col_scores = st.columns(2)
        col_csv.download_button(
            "⬇️ คะแนนรายคน (CSV สำหรับลงสมุดคะแนน)", class_session.scores_csv(session),
            file_name="scores.csv", mime="text/csv",
        )
        with col_scores:
            nav.link(nav.SCORES, "ดูคะแนนข้ามการสอบ รายคน รายห้อง รายชั้น")
    choice = st.selectbox(
        "ดูรายละเอียดรายคน", range(len(session.students)), index=None, placeholder="เลือกผู้สอบ",
        format_func=lambda i: f"{session.students[i].student_no} {session.students[i].name}".strip(),
    )
    if choice is not None:
        show_student(session.exam, session.students[choice])
    teacher_review_section()


def grade_all(indices: list[int]) -> None:
    """ตรวจทีละคน เก็บผลทันทีหลังตรวจแต่ละคน ถ้าล้มกลางทาง คนที่ตรวจแล้วไม่หาย"""
    bar = st.progress(0.0)
    guidance = teacher_review.grading_guidance(state.class_session)
    for n, i in enumerate(indices):
        student = state.class_session.students[i]
        bar.progress(n / len(indices), text=f"กำลังตรวจ {student.name} ({n + 1}/{len(indices)})")
        try:
            graded = class_session.grade_student(state.class_session.exam, student, guidance=guidance)
        except LLMError as e:
            st.error(f"ตรวจของ {student.name} ไม่สำเร็จ: {e} — คนที่ตรวจแล้วเก็บไว้แล้ว กดตรวจต่อได้")
            return
        save(class_session.update_student(state.class_session, i, graded))
    state.class_graded = f"ตรวจครบ {len(indices)} คนแล้ว"
    st.rerun()  # ให้แถบขั้นตอนขยับไปขั้นถัดไป


def grading_note(exam: Exam) -> str:
    note = "ข้ออัตนัยใช้โมเดลตรวจทีละข้อ (Ollama ประมาณ 2–10 วินาทีต่อข้อต่อคน)"
    if config.GRADER_MODE == "panel" and any(q.type in config.PANEL_TYPES for q in exam.questions):
        note += " · เรียงความใช้คณะผู้ตรวจ 3 คน (+ Moderator เมื่อเห็นต่าง) จึงใช้เวลาประมาณ 3 เท่า"
    return note


def show_student(exam: Exam, student: StudentRecord) -> None:
    st.caption(f"สถานะ: {class_session.status(student)} · ที่มา: "
               + ("กระดาษคำตอบ " + student.sheet_name if student.source == "answer_sheet" else "CSV")
               + (f" · สอบชุด {student.form} (ตารางด้านล่างเรียงตามข้อสอบต้นฉบับ)" if student.form else ""))
    for note in student.import_notes:
        st.warning(note)
    results = {r.question_id: r for r in student.results or []}
    rows = []
    for q in exam.questions:
        answer = student.answers.get(q.id)
        if q.type in OBJECTIVE_TYPES:
            shown = scoring.choice_label(q, answer if isinstance(answer, int) else None)
        else:
            shown = str(answer or "").strip() or "(ไม่ได้ตอบ)"
        r = results.get(q.id)
        rows.append({
            "ข้อ": q.id,
            "ชนิด": TYPE_LABELS[q.type],
            "คำตอบ": shown,
            "คะแนน": f"{r.points_awarded:g}/{r.max_points:g}" if r else f"–/{q.points:g}",
            "คณะผู้ตรวจ": panel_text(r) if r else "",
        })
    st.dataframe(rows, hide_index=True)
    if student.results:
        show_question_details(exam, student.results, teacher=True)  # รวมผลคณะผู้ตรวจของข้อเรียงความ


def teacher_review_section() -> None:
    session = state.class_session
    pending = teacher_review.pending(session)
    if not pending:
        return
    with st.expander(f"คิวตัดสินคะแนนเรียงความ ({len(pending)} ข้อ)", expanded=True):
        selected = st.selectbox(
            "คำตอบที่รอครูตัดสิน", range(len(pending)),
            format_func=lambda i: f"{session.students[pending[i][0]].name} · ข้อ {pending[i][1].question_id}",
            key=f"teacher_queue_{state.class_review_n}",
        )
        student_index, result = pending[selected]
        question = next(q for q in session.exam.questions if q.id == result.question_id)
        st.markdown(question.question)
        st.text(result.student_answer)
        show_panel(result)
        criteria = {c.criterion: c.points_awarded for c in result.criteria}
        with st.form(f"teacher_{state.class_review_n}_{student_index}_{question.id}"):
            scores = [
                st.number_input(
                    f"{name} ({maximum:g} คะแนน) — {description}",
                    min_value=0.0, max_value=float(maximum),
                    value=min(maximum, criteria.get(name, 0.0)), step=0.5,
                    key=f"teacher_score_{state.class_review_n}_{student_index}_{question.id}_{i}",
                )
                for i, (name, description, maximum) in enumerate(scoring.subjective_criteria(question))
            ]
            note = st.text_area("เหตุผลที่ยืนยันหรือแก้คะแนน", max_chars=600)
            guidance = st.text_area(
                "แนวทางให้ AI ตรวจข้อนี้ในห้องนี้ (ถ้ามี)", max_chars=600,
                help="ระบุวิธีใช้เกณฑ์กับคำตอบอื่น ๆ โดยละชื่อและรายละเอียดของนักเรียน",
            )
            anchor = st.text_area(
                "ตัวอย่างคำตอบที่ลบข้อมูลระบุตัวตนแล้ว (ถ้ามี)", max_chars=600,
                help="ระบบจะใช้ตัวอย่างนี้พร้อมคะแนนรายเกณฑ์ของครูช่วยตรวจคำตอบอื่นในห้องนี้",
            )
            if st.form_submit_button("ยืนยันคะแนนของครู", type="primary"):
                try:
                    updated = teacher_review.resolve(question, result, scores, note, guidance, anchor)
                except ValueError as e:
                    st.error(str(e))
                    return
                student = session.students[student_index]
                results = [updated if r.question_id == question.id else r for r in student.results]
                save(class_session.update_student(session, student_index, student.model_copy(update={"results": results})))
                state.class_review_n += 1
                st.rerun()


# ---------------- สอบซ่อม ----------------

def retest_section(ready: bool) -> None:
    """การสอบครั้งแรก: ตั้งเกณฑ์ผ่าน → เลือกคนสอบซ่อม → ออกข้อสอบชุดซ่อม → เปิดการสอบซ่อม"""
    sitting, session = state.class_sitting, state.class_session
    pass_percent = st.number_input("เกณฑ์ผ่าน (%)", min_value=0.0, max_value=100.0, value=float(sitting.pass_percent),
                                   step=5.0, key=f"class_pass_{sitting.id}")
    if pass_percent != sitting.pass_percent:
        with school_db.connect() as conn:
            school_db.set_pass_percent(conn, sitting.id, pass_percent)
            state.class_sitting = school_db.sittings(conn, sitting.id)[0]
        st.rerun()
    st.caption(f"คะแนนหลังสอบซ่อม = คะแนนสอบซ่อมแต่ไม่เกิน {pass_percent:g}% และไม่ต่ำกว่าครั้งแรก")
    if waiting := len(teacher_review.pending(session)):
        st.warning(f"ยังมีเรียงความรอครูตัดสิน {waiting} ข้อ (ขั้นที่ 4) รายชื่อผู้สอบซ่อมด้านล่างคิดจากคะแนนชั่วคราว")
    with school_db.connect() as conn:
        done = school_db.retests(conn, sitting.id)
        retest_sessions = [school_db.load_session(conn, r.id)[1] for r in done]
    for r in done:
        col_info, col_open = st.columns([4, 1], vertical_alignment="center")
        col_info.markdown(f"**{r.label}** · ผู้มีสิทธิ์ {len(r.takers or ())} คน · "
                          f"ส่งคำตอบแล้ว {r.students} คน · ตรวจแล้ว {r.graded} คน")
        if col_open.button("เปิดการสอบซ่อม", key=f"open_retest_{r.id}"):
            open_sitting(r.id)
    if rows := retest.summary(session, retest_sessions, pass_percent):
        st.dataframe(rows, hide_index=True)

    below = retest.below_pass(session, pass_percent)
    if not below:
        st.success(f"ทุกคนที่ตรวจแล้วได้ตั้งแต่ {pass_percent:g}% ขึ้นไป ไม่ต้องสอบซ่อม")
        return
    graded = {s.key: s for s in session.students if s.results is not None}
    takers = st.multiselect(
        f"ผู้สอบซ่อม (ต่ำกว่าเกณฑ์ {len(below)} คน เลือกไว้ให้แล้ว เพิ่ม/ลดได้)", list(graded),
        default=[s.key for s in below], key=f"retest_takers_{sitting.id}",
        format_func=lambda k: f"{graded[k].student_no} {graded[k].name} ({retest.percent(graded[k]):g}%)".strip(),
    )
    label = f"🛠️ ออกข้อสอบชุดซ่อม ({len(takers)} คน) — Generator ออกข้อใหม่ → Reviewer ตรวจ → เช็กไม่ซ้ำข้อเดิม"
    if st.button(label, type="primary", disabled=not takers or not ready):
        exam = session.exam
        # ใช้เนื้อหาเดิมถ้าข้อสอบนี้สร้างในรอบนี้ ไม่งั้นใช้ชื่อชุดและหัวข้อแทน (ข้อสอบที่เปิดจากฐานข้อมูล)
        source = state.source if state.exam == exam and state.source else sources.from_topic(
            f"{exam.title}: {', '.join(exam.concepts)}")
        result = show_progress(
            "กำลังออกข้อสอบชุดซ่อม: Generator ออกข้อใหม่ → Reviewer ตรวจ → โค้ดเช็กไม่ซ้ำข้อเดิม", "ออกข้อสอบชุดซ่อม",
            lambda on_step: retest.make_retest(source, exam, on_step=on_step),
        )
        if result:
            with school_db.connect() as conn:
                retest_id = school_db.open_retest(conn, result.exam, sitting.id, takers)
            state.notice = f"สร้างการสอบซ่อม “{result.exam.title}” แล้ว ({len(takers)} คน) พิมพ์ข้อสอบได้ที่ขั้นที่ 1"
            open_sitting(retest_id)
    st.caption("ข้อสอบชุดซ่อมวัดหัวข้อ ชนิด จำนวนข้อ คะแนน และความยากเท่าครั้งแรก แต่ใช้คำถามใหม่")
    st.markdown("**สอบซ่อมเฉพาะบุคคลตามหัวข้อที่อ่อน**")
    person_key = st.selectbox(f"เลือกผู้สอบซ่อมรายคน (ต่ำกว่าเกณฑ์ {len(below)} คน)", [s.key for s in below],
                              format_func=lambda k: f"{graded[k].student_no} {graded[k].name}",
                              key=f"personal_retest_student_{sitting.id}")
    person = graded[person_key]
    concepts = retest.weak_concepts(person, pass_percent)
    pending = any(r.needs_teacher_review for r in person.results or [])
    st.caption("หัวข้อที่คะแนนต่ำกว่าเกณฑ์: " + (", ".join(concepts) if concepts else "ไม่มี"))
    st.caption("ชุดนี้วัดเฉพาะหัวข้อที่อ่อน คะแนนหลังซ่อมยังใช้กติกาเดิม ครูควรตรวจความเหมาะสมก่อนใช้")
    if pending:
        st.warning("ต้องให้ครูตัดสินคะแนนที่รอตรวจของนักเรียนคนนี้ก่อนสร้างชุดเฉพาะบุคคล")
    if st.button("ออกข้อสอบซ่อมเฉพาะบุคคล", disabled=not ready or not concepts or pending,
                 key=f"personal_retest_{sitting.id}"):
        exam = session.exam
        source = state.source if state.exam == exam and state.source else sources.from_topic(
            f"{exam.title}: {', '.join(exam.concepts)}")
        result = show_progress(
            "กำลังออกข้อสอบเฉพาะหัวข้อที่อ่อน", "ออกข้อสอบซ่อมเฉพาะบุคคล",
            lambda on_step: retest.make_retest(source, exam, concepts=concepts, on_step=on_step),
        )
        if result:
            with school_db.connect() as conn:
                retest_id = school_db.open_retest(conn, result.exam, sitting.id, [person_key])
            state.notice = f"สร้างข้อสอบซ่อมเฉพาะบุคคลสำหรับ {person.name} แล้ว"
            open_sitting(retest_id)


def after_retest_section() -> None:
    """การสอบซ่อม: คะแนนครั้งแรกเทียบกับสอบซ่อม และคะแนนที่ใช้จริงหลังซ่อม"""
    sitting = state.class_sitting
    with school_db.connect() as conn:
        original_sitting, original = school_db.load_session(conn, sitting.retest_of)
        sessions = [school_db.load_session(conn, r.id)[1] for r in school_db.retests(conn, sitting.retest_of)]
    rows = retest.summary(original, sessions, original_sitting.pass_percent)
    st.caption(f"สอบซ่อมของ “{original_sitting.label}” · ซ่อมได้ไม่เกิน {original_sitting.pass_percent:g}% "
               "และไม่ต่ำกว่าครั้งแรก (แก้เกณฑ์ได้ที่การสอบครั้งแรก)")
    if rows:
        st.dataframe(rows, hide_index=True)
        passed = sum(1 for r in rows if r["ผ่านเกณฑ์"] == "✓")
        st.caption(f"ผ่านเกณฑ์หลังซ่อม {passed}/{len(rows)} คน · คะแนนหลังซ่อมขึ้นในหน้าคะแนนรายคน รายห้องแล้ว")
    if st.button("กลับไปการสอบครั้งแรก"):
        open_sitting(sitting.retest_of)
