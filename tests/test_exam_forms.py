"""ชุดสลับกันลอก: สลับลำดับข้อ/ตัวเลือก แล้วแปลงคำตอบกลับเป็นข้อสอบต้นฉบับได้ถูก"""

from src import answer_reader, class_session, csv_import, exam_forms
from src.schemas import Exam, ExtractedAnswer, ExtractedAnswerSheet, Question

COMMON = dict(source_ref="-", bloom_level="understand", difficulty="medium", explanation="", model_answer="",
              key_points=[], rubric=[], points=1)


def mcq(i: int, correct: int, choices=None) -> Question:
    return Question(**COMMON, id=i, type="mcq", concept=f"c{i}", question=f"คำถาม {i}",
                    choices=choices or [f"ตัวเลือก {i}-{j}" for j in range(4)], correct_choice=correct)


ESSAY = Question(**{**COMMON, "points": 5}, id=7, type="essay", concept="c7", question="อธิบาย", choices=[],
                 correct_choice=None)
TF = Question(**COMMON, id=6, type="true_false", concept="c6", question="จริงไหม", choices=["ถูก", "ผิด"],
              correct_choice=1)
ALL_OF_ABOVE = mcq(5, 3, ["แมว", "หมา", "นก", "ถูกทุกข้อ"])
EXAM = Exam(title="ชุดสลับ", spec_summary="", concepts=[], questions=[
    ESSAY, mcq(1, 0), mcq(2, 1), mcq(3, 2), mcq(4, 3), ALL_OF_ABOVE, TF,  # อัตนัยมาก่อนในต้นฉบับ
])
OBJECTIVE_IDS = [1, 2, 3, 4, 5, 6]


def test_forms_keep_sections_and_only_shuffle_safe_choices():
    forms = exam_forms.make_forms(EXAM, 3, seed=7)
    assert [f.code for f in forms] == ["ก", "ข", "ค"]
    assert forms[0].order == OBJECTIVE_IDS + [7] and forms[0].choices == {}  # ชุด ก = ต้นฉบับ (ปรนัยก่อน)
    for form in forms[1:]:
        assert sorted(form.order[:6]) == OBJECTIVE_IDS and form.order[6:] == [7]  # สลับเฉพาะภายในตอน
        assert set(form.choices) == {1, 2, 3, 4}  # "ถูกทุกข้อ" และถูก/ผิด ไม่สลับตัวเลือก
    assert forms[1].order != forms[0].order and forms[1].order != forms[2].order
    assert exam_forms.make_forms(EXAM, 3, seed=7) == forms  # seed เดิม ได้ชุดเดิม
    assert exam_forms.make_forms(EXAM, 1, seed=7) == forms[:1]


def test_form_exam_moves_the_key_with_the_choices():
    form = exam_forms.make_forms(EXAM, 2, seed=3)[1]
    printed = exam_forms.form_exam(EXAM, form)
    by_id = {q.id: q for q in EXAM.questions}
    assert [q.id for q in printed.questions] == list(range(1, 8))
    for q, base_id in zip(printed.questions, form.order):
        base = by_id[base_id]
        assert q.question == base.question
        if base.choices:
            assert q.choices[q.correct_choice] == base.choices[base.correct_choice]


def test_answers_round_trip_and_grade_the_same_on_every_form():
    full_marks = {q.id: q.correct_choice if q.choices else "ตอบ" for q in EXAM.questions}
    for form in exam_forms.make_forms(EXAM, 4, seed=11):
        printed = exam_forms.form_exam(EXAM, form)
        on_paper = {q.id: q.correct_choice if q.choices else "ตอบ" for q in printed.questions}
        assert exam_forms.to_base(form, on_paper) == full_marks
        assert exam_forms.to_form(form, full_marks) == on_paper


def test_reader_switches_to_the_form_printed_on_the_sheet(monkeypatch):
    exam = Exam(title="ปรนัย", spec_summary="", concepts=[], questions=[mcq(1, 0), mcq(2, 1), mcq(3, 2)])
    forms = exam_forms.make_forms(exam, 2, seed=5)
    calls = []

    def fake_read(printed: Exam, sheet):  # กระดาษจริงเป็นชุด ข ตอบถูกทุกข้อตามที่พิมพ์
        calls.append([q.question for q in printed.questions])
        answers = [ExtractedAnswer(question_id=q.id, answer="กขคง"[q.correct_choice], choice_text=q.choices[q.correct_choice],
                                   confidence="high", note="") for q in printed.questions]
        return ExtractedAnswerSheet(student_name="เอ", student_no="1", form_code="ข", answers=answers)

    monkeypatch.setattr(answer_reader, "read_answers", fake_read)
    sheet = answer_reader.AnswerSheetFile(name="a.png", pages=1, images=[])
    student = class_session.read_sheet(exam, sheet, forms)  # ไม่ระบุชุด → อ่านชุด ก ก่อน แล้วอ่านใหม่เป็นชุด ข
    assert len(calls) == 2 and student.form == "ข" and not student.import_notes
    assert student.answers == {1: 0, 2: 1, 3: 2}

    monkeypatch.setattr(answer_reader, "read_answers",
                        lambda printed, sheet: fake_read(printed, sheet).model_copy(update={"form_code": ""}))
    student = class_session.read_sheet(exam, sheet, forms, "ข")
    assert student.form == "ข" and any("อ่านรหัสชุด" in n for n in student.import_notes)


def test_csv_with_form_column_maps_back_to_the_original():
    exam = Exam(title="ปรนัย", spec_summary="", concepts=[], questions=[mcq(1, 0), mcq(2, 1), mcq(3, 2)])
    forms = exam_forms.make_forms(exam, 2, seed=5)
    template = csv_import.template_csv(exam, forms).decode("utf-8-sig")
    assert template.startswith("เลขที่,ชื่อ-นามสกุล,ชุด,ข้อ 1,ข้อ 2,ข้อ 3")
    printed_b = exam_forms.form_exam(exam, forms[1])
    row_b = ",".join("กขคง"[q.correct_choice] for q in printed_b.questions)
    data = template + "1,เอ,ก,ก,ข,ค\n" + f"2,บี,ข,{row_b}\n" + "3,ซี,,ก,ข,ค\n"
    result = csv_import.parse_responses(exam, data.encode("utf-8"), forms)
    students = {s.name: s for s in result.students}
    assert students["เอ"].answers == students["บี"].answers == {1: 0, 2: 1, 3: 2}
    assert (students["เอ"].form, students["บี"].form, students["ซี"].form) == ("ก", "ข", "ก")
    assert any("ไม่ระบุชุด" in n for n in students["ซี"].import_notes)


def test_csv_teacher_scores_follow_shuffled_essay_order():
    second = ESSAY.model_copy(update={"id": 8, "concept": "c8", "points": 10})
    exam = Exam(title="สองเรียงความ", spec_summary="", concepts=[], questions=[mcq(1, 0), ESSAY, second])
    forms = exam_forms.make_forms(exam, 2, seed=1)
    printed = exam_forms.form_exam(exam, forms[1])
    scores = {7: "4", 8: "9"}  # ข้อต้นฉบับ → คะแนน
    cells = [scores.get(base, "ก") for base in forms[1].order]
    assert forms[1].order[1:] == [8, 7] and [q.points for q in printed.questions[1:]] == [10, 5]  # ชุด ข สลับเรียงความ
    data = csv_import.template_csv(exam, forms).decode("utf-8-sig") + "1,เอ,ข," + ",".join(cells) + "\n"
    student = csv_import.parse_responses(exam, data.encode("utf-8"), forms).students[0]
    assert student.teacher_scores == {7: 4.0, 8: 9.0} and not student.import_notes


def test_choice_explanations_follow_shuffled_choices():
    from src import exam_forms
    from src.schemas import Exam, ExamForm
    q = mcq(1, 0).model_copy(update={"choice_explanations": ["หนึ่ง", "สอง", "สาม", "สี่"]})
    exam = Exam(title="ชุด", spec_summary="", concepts=[q.concept], questions=[q])
    form = ExamForm(code="ข", order=[1], choices={1: [2, 0, 3, 1]})
    shuffled = exam_forms.form_exam(exam, form).questions[0]
    assert shuffled.choice_explanations == ["สาม", "หนึ่ง", "สี่", "สอง"]
    assert shuffled.correct_choice == [2, 0, 3, 1].index(q.correct_choice)
