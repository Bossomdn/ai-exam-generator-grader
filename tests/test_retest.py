"""สอบซ่อม: เพดานคะแนนหลังซ่อม, เลือกคนไม่ผ่าน, ข้อสอบชุดซ่อมไม่ซ้ำข้อเดิม, ตารางคะแนนหลังซ่อมในฐานข้อมูล"""

import pytest

from src import class_session, retest, school_db, scoring
from src.generation_graph import GenerationResult
from src.schemas import ClassSession, Exam, Question, RosterEntry, StudentRecord
from src.sources import from_topic

COMMON = dict(source_ref="-", bloom_level="understand", difficulty="medium", explanation="", model_answer="",
              key_points=[], rubric=[], points=1, concept="ระบบหายใจ")


def mcq(i: int, question: str) -> Question:
    return Question(**COMMON, id=i, type="mcq", question=question, choices=["ก1", "ข1", "ค1", "ง1"], correct_choice=0)


EXAM = Exam(title="ระบบหายใจ", spec_summary="ปรนัย 4 ข้อ", concepts=["ระบบหายใจ"], questions=[
    mcq(1, "อวัยวะใดแลกเปลี่ยนแก๊ส"), mcq(2, "กะบังลมทำงานอย่างไรขณะหายใจเข้า"),
    mcq(3, "ถุงลมมีลักษณะอย่างไร"), mcq(4, "ปัจจัยใดเพิ่มอัตราการหายใจ"),
])


def graded(name: str, no: str, right: int) -> StudentRecord:
    """ตอบถูก right ข้อจาก 4 ข้อ"""
    answers = {q.id: 0 if q.id <= right else 1 for q in EXAM.questions}
    return class_session.grade_student(EXAM, StudentRecord(student_no=no, name=name, answers=answers, source="csv"))


@pytest.mark.parametrize("first, again, expected", [
    (25, 100, 50),   # ซ่อมได้เต็ม → ได้แค่เกณฑ์ผ่าน
    (25, 40, 40),    # ซ่อมได้ต่ำกว่าเกณฑ์ → ใช้คะแนนซ่อม
    (25, 0, 25),     # ซ่อมได้น้อยกว่าเดิม → ไม่ลดลง
    (25, None, 25),  # ยังไม่ได้ซ่อม
])
def test_after_retest_is_capped_at_the_pass_mark(first, again, expected):
    assert scoring.after_retest(first, again, 50) == expected


def test_below_pass_and_duplicate_check():
    session = ClassSession(exam=EXAM, students=[graded("เอ", "1", 4), graded("บี", "2", 1), graded("ซี", "3", 2)])
    assert [s.name for s in retest.below_pass(session, 50)] == ["บี"]  # 50% พอดีถือว่าผ่าน
    again = EXAM.model_copy(update={"questions": [mcq(1, "อวัยวะใดทำหน้าที่แลกเปลี่ยนแก๊ส"), mcq(2, "เมื่อออกกำลังกายหนัก ร่างกายปรับตัวอย่างไร")]})
    assert retest.duplicates(EXAM, again) == {1: 1}


def test_make_retest_sends_duplicates_back_to_the_generator(monkeypatch):
    calls = {}

    def fake_generation(source, *, spec, on_step=None):
        calls["spec"] = spec
        exam = EXAM.model_copy(update={"title": spec.title, "questions": [mcq(1, "ถุงลมมีลักษณะอย่างไร"),
                                                                        mcq(2, "คำถามใหม่ทั้งหมด")]})
        return GenerationResult(exam=exam, spec=spec, quality=[], rounds=0, trace=[])

    def fake_revision(source, spec, exam, issues, suggestions, *, prior_steps=(), on_step=None):
        calls["issues"] = issues
        return GenerationResult(exam=exam, spec=spec, quality=[], rounds=1, trace=list(prior_steps))

    monkeypatch.setattr(retest, "run_generation", fake_generation)
    monkeypatch.setattr(retest, "run_revision", fake_revision)
    result = retest.make_retest(from_topic("ระบบหายใจ"), EXAM)
    assert calls["spec"].title == "ระบบหายใจ (ชุดสอบซ่อม)"
    assert "ห้ามใช้คำถาม" in calls["spec"].focus and "ถุงลมมีลักษณะอย่างไร" in calls["spec"].focus
    assert calls["issues"] == {1: ["คำถามซ้ำหรือเกือบซ้ำกับข้อ 3 ของข้อสอบครั้งแรก"]}
    assert [s.agent for s in result.trace] == ["ตรวจข้อซ้ำ (โค้ด)"]


def test_retest_scores_flow_into_room_student_and_grade_views():
    with school_db.connect() as conn:
        room = school_db.add_classroom(conn, 2569, "ม.4", "1")
        school_db.import_roster(conn, room.id, [RosterEntry(number="1", name="เอ"), RosterEntry(number="2", name="บี"),
                                                RosterEntry(number="3", name="ซี")])
        first_id = school_db.open_sitting(conn, EXAM, room.id, 1)
        first = ClassSession(exam=EXAM, students=[graded("เอ", "1", 4), graded("บี", "2", 1), graded("ซี", "3", 0)])
        school_db.save_session(conn, first_id, first)

        retest_exam = EXAM.model_copy(update={"title": "ระบบหายใจ (ชุดสอบซ่อม)"})
        retest_id = school_db.open_retest(conn, retest_exam, first_id, ["2", "3"])
        [sitting] = school_db.retests(conn, first_id)
        assert (sitting.id, sitting.retest_of, sitting.takers, sitting.pass_percent) == (retest_id, first_id, ("2", "3"), 50.0)
        assert sitting.label.startswith("ระบบหายใจ (ชุดสอบซ่อม) (สอบซ่อม)")
        again = ClassSession(exam=retest_exam, students=[graded("บี", "2", 4), graded("ซี", "3", 1)])
        school_db.save_session(conn, retest_id, again)

        table = {r["ชื่อ"]: r for r in school_db.classroom_table(conn, room.id)}
        title = "ระบบหายใจ (ภาค 1)"
        assert [(table[n][title], table[n][f"{title} สอบซ่อม"], table[n][f"{title} หลังซ่อม"], table[n]["เฉลี่ย"])
                for n in ("เอ", "บี", "ซี")] == [(100.0, None, 100.0, 100.0), (25.0, 100.0, 50.0, 50.0),
                                                 (0.0, 25.0, 25.0, 25.0)]
        history = school_db.student_history(conn, room.id, "2")
        assert [(h["ข้อสอบ"], h["ร้อยละ"], h["หลังสอบซ่อม"]) for h in history] == [
            ("ระบบหายใจ", 25.0, 50.0), ("ระบบหายใจ (ชุดสอบซ่อม) (สอบซ่อม)", 100.0, None)]
        # รายชั้นเทียบห้องจากการสอบครั้งแรกเท่านั้น
        assert school_db.grade_exam_table(conn, 2569, "ม.4") == [{"ข้อสอบ": "ระบบหายใจ", "ม.4/1": round(125 / 3, 1)}]

        summary = retest.summary(first, [again], 50)
        assert [(r["ชื่อ"], r["ครั้งแรก"], r["สอบซ่อม"], r["หลังซ่อม"], r["ผ่านเกณฑ์"]) for r in summary] == [
            ("บี", 25.0, 100.0, 50.0, "✓"), ("ซี", 0.0, 25.0, 25.0, "✗")]


def test_personal_retest_uses_only_student_weak_concepts(monkeypatch):
    questions = [q.model_copy(update={"concept": "A" if q.id <= 2 else "B"}) for q in EXAM.questions]
    exam = EXAM.model_copy(update={"concepts": ["A", "B"], "questions": questions})
    student = class_session.grade_student(exam, StudentRecord(name="บี", answers={1: 0, 2: 0, 3: 1, 4: 1}, source="csv"))
    assert retest.weak_concepts(student, 50) == ["B"]
    other = class_session.grade_student(exam, StudentRecord(name="ซี", answers={1: 1, 2: 1, 3: 0, 4: 0}, source="csv"))
    assert retest.weak_concepts(other, 50) == ["A"]
    spec = retest.retest_spec(exam, retest.weak_concepts(student, 50))
    assert spec.concepts == ["B"] and sum(s.count for s in spec.sections) == 4
    assert "วัดเฉพาะหัวข้อ B" in spec.focus and "วัดหัวข้อเดิม" not in spec.focus  # คำสั่งไม่ขัดกันเอง

    def fake_generation(source, *, spec, on_step=None):
        return GenerationResult(exam=exam, spec=spec, quality=[], rounds=0, trace=[])
    monkeypatch.setattr(retest, "run_generation", fake_generation)
    from src.llm import LLMError
    with pytest.raises(LLMError, match="หัวข้อนอกเหนือ"):
        retest.make_retest(from_topic("ระบบหายใจ"), exam, concepts=["B"])
