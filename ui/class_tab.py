"""แท็บ 4 (ครู): เลือกห้อง → พิมพ์ข้อสอบ → สอบบนกระดาษ → อัปโหลดกระดาษคำตอบทีละหลายใบ (หรือ CSV)
→ AI อ่านคำตอบ → ครูตรวจทาน → ตรวจทั้งห้อง → ตารางคะแนน → วิเคราะห์ข้อสอบจากผลจริง (ui/analysis_section.py)

ทุกการเปลี่ยนแปลงบันทึกลงฐานข้อมูล (src/school_db.py) ทันที ปิดแล้วเปิดกลับมาทำต่อได้"""

import streamlit as st

from src import answer_reader, class_session, config, csv_import, export, school_db, scoring
from src.class_session import normalize_no
from src.llm import LLMError
from src.schemas import OBJECTIVE_TYPES, TYPE_LABELS, ClassSession, Exam, StudentRecord

from . import analysis_section
from .answer_form import answer_fields, collect, prefill
from .common import panel_text, show_panel, state, use_exam

SHEETS = "สแกนกระดาษคำตอบ (PDF / รูปถ่าย)"
CSV = "ไฟล์ CSV (กรอกคำตอบเอง)"


def render(ready: bool) -> None:
    st.caption("สอบบนกระดาษ แล้วสแกนหรือถ่ายรูปกระดาษคำตอบอัปโหลดทีละหลายใบ ให้ AI อ่านคำตอบ ครูตรวจทาน แล้วตรวจทั้งห้อง")
    try:
        with school_db.connect() as conn:
            rooms, saved = school_db.classrooms(conn), school_db.sittings(conn)
    except school_db.SchoolDbError as e:
        st.error(str(e))
        return
    if state.class_session is None:
        start_session(rooms, saved)
        return
    show_header(state.class_session)
    st.markdown("#### 1. พิมพ์ข้อสอบไปสอบ")
    print_section(state.class_session.exam)
    st.markdown("#### 2. นำเข้าคำตอบ")
    import_section(ready)
    if class_session.awaiting_review(state.class_session):
        st.markdown("#### 3. ตรวจทานคำตอบที่ AI อ่าน")
        review_section()
    st.markdown("#### 4. ตรวจและดูคะแนน")
    grade_section(ready)
    if any(s.results is not None for s in state.class_session.students):
        st.markdown("#### 5. วิเคราะห์ข้อสอบจากผลสอบจริง")
        analysis_section.render(ready)
    close_section()


def reset_class(session: ClassSession | None, sitting: school_db.Sitting | None = None) -> None:
    state.class_session = session
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
        st.info("ยังไม่มีห้องเรียน — เพิ่มห้องและนำเข้ารายชื่อนักเรียนที่แท็บ 5 ก่อน")
    elif exam is None:
        st.info("สร้างข้อสอบที่แท็บ 1 ก่อน")
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
    state.class_session = session
    with school_db.connect() as conn:
        school_db.save_session(conn, state.class_sitting.id, session)


def roster_names() -> dict[str, str]:
    """รายชื่อห้องของการสอบนี้ {เลขที่: ชื่อ} (ว่าง = ห้องนี้ยังไม่ได้นำเข้ารายชื่อ)"""
    with school_db.connect() as conn:
        return {e.number: e.name for e in school_db.roster(conn, state.class_sitting.classroom.id)}


def show_header(session: ClassSession) -> None:
    exam, sitting = session.exam, state.class_sitting
    graded = sum(1 for s in session.students if s.results is not None)
    waiting = len(class_session.awaiting_review(session))
    st.markdown(
        f"**{sitting.classroom.name}** ภาคเรียน {sitting.term}/{sitting.classroom.year} · **ข้อสอบ:** {exam.title} · "
        f"{len(exam.questions)} ข้อ · ผู้สอบ {len(session.students)} คน (รอตรวจทาน {waiting} · ตรวจแล้ว {graded})"
    )
    roster = roster_names()
    if not roster:
        st.caption("ห้องนี้ยังไม่มีรายชื่อนักเรียน — นำเข้าที่แท็บ 5 แล้วระบบจะใช้ชื่อจากรายชื่อตามเลขที่ "
                   "แทนชื่อที่ AI อ่านจากลายมือ")
    elif missing := class_session.absent(session, roster):
        st.caption(f"ยังไม่มีคำตอบ {len(missing)} คน: เลขที่ {', '.join(missing)}")
    if state.exam and state.exam != exam:
        st.info("การสอบนี้ใช้ข้อสอบชุดก่อน ไม่ใช่ชุดล่าสุดในแท็บ 1 — ถ้าจะใช้ชุดล่าสุด ให้ปิดการสอบนี้ (ด้านล่าง) แล้วเริ่มใหม่")


def print_section(exam: Exam) -> None:
    st.caption("ให้ผู้สอบเขียนชื่อ เลขที่ (ให้ชัด) และคำตอบลงในข้อสอบ (ปรนัยให้วงกลมตัวเลือก) "
               "ระบบใช้เลขที่ที่ AI อ่านได้จับคู่กับรายชื่อห้อง")
    if st.button("📄 สร้าง PDF ข้อสอบ (ไม่มีเฉลย)"):
        with st.spinner("กำลังสร้าง PDF..."):
            try:
                state.class_exam_pdf = export.exam_pdf(exam)
            except export.ExportError as e:
                st.error(str(e))
    if state.class_exam_pdf:
        st.download_button("⬇️ ข้อสอบสำหรับพิมพ์", state.class_exam_pdf, file_name="exam.pdf", mime="application/pdf")


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
    st.caption("AI อ่านประมาณ 5–15 วินาทีต่อคน (Ollama · คนแรกช้ากว่าเพราะต้องโหลดโมเดล) "
               "ทุกคนต้องผ่านการตรวจทานในขั้นที่ 3 ก่อนตรวจคะแนน")
    if st.button("อ่านคำตอบด้วย AI", type="primary", disabled=not files or not ready):
        read_sheets(files, pages_per)


def read_sheets(files, pages_per: int | None) -> None:
    exam = state.class_session.exam
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
            students.append(class_session.read_sheet(exam, sheet))
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
    exam = state.class_session.exam
    uploaded = st.file_uploader("ไฟล์คำตอบ (.csv)", type=["csv"], key=f"class_csv_{state.class_upload_n}")
    st.download_button(
        "⬇️ เทมเพลต CSV", csv_import.template_csv(exam), file_name="answers_template.csv", mime="text/csv",
    )
    st.caption("กรอกเทมเพลต: ปรนัยใส่ ก ข ค ง หรือข้อความตัวเลือก · ถูก/ผิด ใส่ “ถูก” หรือ “ผิด” · อัตนัยใส่ข้อความคำตอบ")
    if st.button("นำเข้าคำตอบ", type="primary", disabled=uploaded is None):
        try:
            result = csv_import.parse_responses(exam, uploaded.getvalue())
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


def review_student(index: int) -> None:
    session = state.class_session
    exam, student = session.exam, session.students[index]
    roster = roster_names()
    prefix = f"rev_{state.class_review_n}_{index}"
    if roster:
        state.setdefault(f"{prefix}_no", student.student_no if student.student_no in roster else None)
    else:
        state.setdefault(f"{prefix}_name", student.name)
        state.setdefault(f"{prefix}_no", student.student_no)
    prefill(exam, prefix, student.answers)

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
        "name": name, "student_no": number, "answers": collect(session.exam, prefix), "reviewed": True,
        "results": None,
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
    percents = [r["ร้อยละ"] for r in rows if r["ร้อยละ"] is not None]
    if percents:
        st.caption(
            f"ตรวจแล้ว {len(percents)} คน · เฉลี่ย {sum(percents) / len(percents):.1f}% · "
            f"สูงสุด {max(percents):g}% · ต่ำสุด {min(percents):g}%"
        )
        st.download_button(
            "⬇️ คะแนนรายคน (CSV สำหรับลงสมุดคะแนน)", class_session.scores_csv(session),
            file_name="scores.csv", mime="text/csv",
        )
    choice = st.selectbox(
        "ดูรายละเอียดรายคน", range(len(session.students)), index=None, placeholder="เลือกผู้สอบ",
        format_func=lambda i: f"{session.students[i].student_no} {session.students[i].name}".strip(),
    )
    if choice is not None:
        show_student(session.exam, session.students[choice])


def grade_all(indices: list[int]) -> None:
    """ตรวจทีละคน เก็บผลทันทีหลังตรวจแต่ละคน ถ้าล้มกลางทาง คนที่ตรวจแล้วไม่หาย"""
    bar = st.progress(0.0)
    for n, i in enumerate(indices):
        student = state.class_session.students[i]
        bar.progress(n / len(indices), text=f"กำลังตรวจ {student.name} ({n + 1}/{len(indices)})")
        try:
            graded = class_session.grade_student(state.class_session.exam, student)
        except LLMError as e:
            st.error(f"ตรวจของ {student.name} ไม่สำเร็จ: {e} — คนที่ตรวจแล้วเก็บไว้แล้ว กดตรวจต่อได้")
            return
        save(class_session.update_student(state.class_session, i, graded))
    bar.progress(1.0, text=f"ตรวจครบ {len(indices)} คนแล้ว")


def grading_note(exam: Exam) -> str:
    note = "ข้ออัตนัยใช้โมเดลตรวจทีละข้อ (Ollama ประมาณ 2–10 วินาทีต่อข้อต่อคน)"
    if config.GRADER_MODE == "panel" and any(q.type in config.PANEL_TYPES for q in exam.questions):
        note += " · เรียงความใช้คณะผู้ตรวจ 3 คน (+ Moderator เมื่อเห็นต่าง) จึงใช้เวลาประมาณ 3 เท่า"
    return note


def show_student(exam: Exam, student: StudentRecord) -> None:
    st.caption(f"สถานะ: {class_session.status(student)} · ที่มา: "
               + ("กระดาษคำตอบ " + student.sheet_name if student.source == "answer_sheet" else "CSV"))
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
    for r in student.results or []:
        if r.panel:
            st.markdown(f"**ข้อ {r.question_id} (เรียงความ)**")
            show_panel(r)


# ---------------- ปิดการสอบ ----------------

def close_section() -> None:
    st.caption("บันทึกลงฐานข้อมูลในเครื่องอัตโนมัติทุกครั้งที่นำเข้า ตรวจทาน หรือตรวจคะแนน (ไม่รวมภาพกระดาษ) "
               "· ดูคะแนนข้ามการสอบ รายคน รายห้อง รายชั้น ได้ที่แท็บ 5")
    if st.button("ปิดการสอบนี้ (เปิดกลับมาได้จากรายการการสอบที่บันทึกไว้)"):
        reset_class(None)
        st.rerun()
