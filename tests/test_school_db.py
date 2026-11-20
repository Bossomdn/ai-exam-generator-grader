"""ฐานข้อมูลคะแนน: ห้อง/รายชื่อ, บันทึก/เปิดการสอบ, ตารางคะแนน 3 ระดับ, อัปเกรดโครงสร้าง, สำรอง/กู้คืน"""

import sqlite3
from contextlib import closing
from datetime import date

import pytest

from src import class_session, school_db
from src.schemas import ClassSession, Exam, Question, RosterEntry, StudentRecord
from src.school_db import SchoolDbError

COMMON = dict(source_ref="สไลด์ 1", bloom_level="understand", difficulty="medium", explanation="", model_answer="",
              key_points=[], rubric=[], points=1)
MCQ = Question(**COMMON, id=1, type="mcq", concept="โมเดล", question="q1", choices=["a", "b", "c", "d"],
               correct_choice=0)
TF = Question(**COMMON, id=2, type="true_false", concept="ข้อมูล", question="q2", choices=["ถูก", "ผิด"],
              correct_choice=1)
EXAM = Exam(title="ชุด A", spec_summary="", concepts=["โมเดล", "ข้อมูล"], questions=[MCQ, TF])


def graded(name: str, no: str, answers: dict) -> StudentRecord:
    student = StudentRecord(student_no=no, name=name, answers=answers, source="csv")
    return class_session.grade_student(EXAM, student)  # ปรนัยล้วน ตรวจด้วยโค้ด ไม่เรียก LLM


def room_with_roster(conn, grade="ม.4", room="1", year=2569, entries=None):
    classroom = school_db.add_classroom(conn, year, grade, room)
    school_db.import_roster(conn, classroom.id, entries or [
        RosterEntry(number="1", name="นายเอ", code="s001"), RosterEntry(number="2", name="นางสาวบี")])
    return classroom


def test_new_database_is_created_then_upgraded_with_backup(temp_database, monkeypatch):
    with school_db.connect() as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == len(school_db.MIGRATIONS)
        school_db.add_classroom(conn, 2569, "ม.4", "1")

    original = list(school_db.MIGRATIONS)
    monkeypatch.setattr(school_db, "MIGRATIONS", original + ["ALTER TABLE classrooms ADD COLUMN note TEXT;"])
    with school_db.connect() as conn:  # โค้ดเวอร์ชันใหม่เปิดไฟล์เก่า → อัปเกรดให้
        assert conn.execute("PRAGMA user_version").fetchone()[0] == len(original) + 1
        assert [tuple(r) for r in conn.execute("SELECT grade, note FROM classrooms")] == [("ม.4", None)]
    backups = list((temp_database.parent / "backups").glob("school-before-v2-*.db"))
    assert len(backups) == 1

    monkeypatch.setattr(school_db, "MIGRATIONS", original)  # โค้ดเก่าเปิดไฟล์ใหม่ไม่ได้
    with pytest.raises(SchoolDbError, match="เวอร์ชันใหม่กว่า"):
        with school_db.connect():
            pass


def test_broken_file_gives_readable_error(temp_database):
    temp_database.parent.mkdir(parents=True)
    temp_database.write_bytes(b"this is not sqlite" * 100)
    with pytest.raises(SchoolDbError, match="เปิดฐานข้อมูล"):
        with school_db.connect():
            pass


def test_current_term_follows_thai_school_year():
    assert school_db.current_term(date(2026, 11, 20)) == (2569, 2)
    assert school_db.current_term(date(2026, 6, 1)) == (2569, 1)
    assert school_db.current_term(date(2027, 3, 1)) == (2569, 2)


def test_classrooms_and_roster_reimport():
    with school_db.connect() as conn:
        room = room_with_roster(conn, grade="ม. 4", room=" 1 ")
        assert (room.grade, room.room, room.label) == ("ม.4", "1", "ม.4/1 ปีการศึกษา 2569")
        assert school_db.add_classroom(conn, 2569, "ม.4", "1") == room  # ห้องเดิม ไม่สร้างซ้ำ
        with pytest.raises(SchoolDbError):
            school_db.add_classroom(conn, 2569, "ม.4", "")

        added, existing = school_db.import_roster(conn, room.id, [
            RosterEntry(number="2", name="นางสาวบี (แก้ชื่อ)"), RosterEntry(number="10", name="นายซี")])
        assert (added, existing) == (1, 1)
        assert [(e.number, e.name) for e in school_db.roster(conn, room.id)] == [
            ("1", "นายเอ"), ("2", "นางสาวบี (แก้ชื่อ)"), ("10", "นายซี")]  # เรียงเลขที่แบบตัวเลข

        next_year = school_db.add_classroom(conn, 2570, "ม.5", "1")
        school_db.import_roster(conn, next_year.id, [RosterEntry(number="5", name="นายเอ", code="s001")])
        assert school_db.counts(conn)["students"] == 3  # รหัสเดิม = คนเดิม


def test_save_and_load_session_with_question_scores():
    with school_db.connect() as conn:
        room = room_with_roster(conn)
        sitting_id = school_db.open_sitting(conn, EXAM, room.id, 2)
        assert school_db.open_sitting(conn, EXAM, room.id, 2) == sitting_id  # ห้องเดิม ข้อสอบเดิม = การสอบเดิม
        session = ClassSession(exam=EXAM, students=[
            graded("นายเอ", "1", {1: 0, 2: 1}),
            graded("นายเจ็ด", "7", {1: 2, 2: None}),  # ไม่อยู่ในรายชื่อ
            StudentRecord(student_no="2", name="นางสาวบี", answers={1: 0, 2: 0}, source="answer_sheet", reviewed=False),
        ])
        school_db.save_session(conn, sitting_id, session)
        school_db.save_session(conn, sitting_id, session)  # บันทึกซ้ำ = แทนที่ ไม่เพิ่มแถว

        sitting, loaded = school_db.load_session(conn, sitting_id)
        assert loaded == session
        assert (sitting.label, sitting.students, sitting.graded) == ("ชุด A · ม.4/1 ภาคเรียน 2/2569", 3, 2)
        rows = conn.execute(
            """SELECT sub.number, sub.student_id IS NOT NULL, qs.question_id, qs.choice, qs.is_correct, qs.points
               FROM question_scores qs JOIN submissions sub ON sub.id = qs.submission_id
               ORDER BY sub.position, qs.question_id""").fetchall()
        assert [tuple(r) for r in rows] == [
            ("1", 1, 1, 0, 1, 1.0), ("1", 1, 2, 1, 1, 1.0), ("7", 0, 1, 2, 0, 0.0), ("7", 0, 2, None, 0, 0.0)]

        with pytest.raises(SchoolDbError, match="ไม่พบการสอบ"):
            school_db.load_session(conn, 999)


def test_roster_imported_later_links_results_across_years():
    with school_db.connect() as conn:
        room = school_db.add_classroom(conn, 2569, "ม.4", "1")
        sitting_id = school_db.open_sitting(conn, EXAM, room.id, 1)
        school_db.save_session(conn, sitting_id, ClassSession(exam=EXAM, students=[graded("เอ", "1", {1: 0, 2: 1})]))
        assert school_db.student_history(conn, room.id, "1")[0]["ร้อยละ"] == 100  # ยังไม่มีรายชื่อ ใช้เลขที่ในห้อง

        school_db.import_roster(conn, room.id, [RosterEntry(number="1", name="นายเอ", code="s001")])
        next_year = room_with_roster(conn, grade="ม.5", year=2570,
                                     entries=[RosterEntry(number="3", name="นายเอ", code="s001")])
        exam_b = EXAM.model_copy(update={"title": "ชุด B"})
        later = school_db.open_sitting(conn, exam_b, next_year.id, 1)
        school_db.save_session(conn, later, ClassSession(exam=exam_b, students=[graded("นายเอ", "3", {1: 1, 2: 1})]))

        history = school_db.student_history(conn, next_year.id, "3")
        assert [(h["ปีการศึกษา"], h["ห้อง"], h["ข้อสอบ"], h["ร้อยละ"]) for h in history] == [
            (2569, "ม.4/1", "ชุด A", 100.0), (2570, "ม.5/1", "ชุด B", 50.0)]
        concepts = school_db.student_concepts(conn, next_year.id, "3")
        assert [(c["หัวข้อ"], c["ร้อยละ"], c["จำนวนข้อ"]) for c in concepts] == [("โมเดล", 50.0, 2), ("ข้อมูล", 100.0, 2)]


def test_classroom_and_grade_tables():
    with school_db.connect() as conn:
        room1 = room_with_roster(conn)
        room2 = school_db.add_classroom(conn, 2569, "ม.4", "2")
        s1 = school_db.open_sitting(conn, EXAM, room1.id, 2)
        s2 = school_db.open_sitting(conn, EXAM, room2.id, 2)
        school_db.save_session(conn, s1, ClassSession(exam=EXAM, students=[
            graded("นายเอ", "1", {1: 0, 2: 1}), graded("เจ็ด", "7", {1: 0, 2: 0})]))
        school_db.save_session(conn, s2, ClassSession(exam=EXAM, students=[graded("ดี", "1", {1: 1, 2: 0})]))

        table = school_db.classroom_table(conn, room1.id)
        assert table == [
            {"เลขที่": "1", "ชื่อ": "นายเอ", "ชุด A (ภาค 2)": 100.0, "เฉลี่ย": 100.0},
            {"เลขที่": "2", "ชื่อ": "นางสาวบี", "ชุด A (ภาค 2)": None, "เฉลี่ย": None},  # อยู่ในรายชื่อแต่ไม่มีผล
            {"เลขที่": "7", "ชื่อ": "เจ็ด", "ชุด A (ภาค 2)": 50.0, "เฉลี่ย": 50.0},
        ]
        assert school_db.grades(conn) == [(2569, "ม.4")]
        assert school_db.grade_exam_table(conn, 2569, "ม.4") == [{"ข้อสอบ": "ชุด A", "ม.4/1": 75.0, "ม.4/2": 0.0}]
        assert school_db.grade_concept_table(conn, 2569, "ม.4") == [
            {"หัวข้อ": "ข้อมูล", "ม.4/1": 50.0, "ม.4/2": 0.0, "ทั้งชั้น": 33.3},
            {"หัวข้อ": "โมเดล", "ม.4/1": 100.0, "ม.4/2": 0.0, "ทั้งชั้น": 66.7},
        ]


def test_backup_and_restore(temp_database, tmp_path):
    with school_db.connect() as conn:
        school_db.add_classroom(conn, 2569, "ม.4", "1")
    backup = school_db.backup_bytes()
    with school_db.connect() as conn:
        school_db.add_classroom(conn, 2569, "ม.4", "2")

    previous = school_db.restore(backup)
    with school_db.connect() as conn:
        assert [c.name for c in school_db.classrooms(conn)] == ["ม.4/1"]
    with closing(sqlite3.connect(previous)) as old:  # ข้อมูลก่อนกู้คืนถูกสำรองไว้
        assert old.execute("SELECT COUNT(*) FROM classrooms").fetchone()[0] == 2

    with pytest.raises(SchoolDbError, match="ไม่ใช่ไฟล์ฐานข้อมูล"):
        school_db.restore(b"hello")
    other = tmp_path / "other.db"
    with closing(sqlite3.connect(other)) as conn:
        conn.execute("CREATE TABLE notes (x)")
        conn.commit()
    with pytest.raises(SchoolDbError, match="ไม่ใช่ฐานข้อมูลของแอปนี้"):
        school_db.restore(other.read_bytes())
