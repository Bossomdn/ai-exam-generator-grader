import io

import pytest
from PIL import Image

from src import answer_reader, export
from src.schemas import ExtractedAnswer, Question

COMMON = dict(concept="c", source_ref="สไลด์ 1", bloom_level="understand", difficulty="medium",
              points=1, explanation="", model_answer="", key_points=[], rubric=[])
MCQ = Question(id=1, type="mcq", question="q", choices=["Supervised", "Unsupervised", "RL", "Semi"],
               correct_choice=1, **COMMON)
TF = Question(id=2, type="true_false", question="q", choices=["ถูก", "ผิด"], correct_choice=0, **COMMON)
ESSAY = Question(id=3, type="essay", question="q", choices=[], correct_choice=None, **COMMON)


def ans(text: str, choice_text: str = "") -> ExtractedAnswer:
    return ExtractedAnswer(question_id=1, answer=text, choice_text=choice_text, confidence="high", note="")


@pytest.mark.parametrize("text, expected", [
    ("ข", 1), ("ข.", 1), ("(ค)", 2), (" ง ", 3), ("B", 1), ("c)", 2), ("Unsupervised", 1),
    ("", None), ("คำตอบคือ ข", None), ("จ", None), ("E", None),
])
def test_mcq_answer_to_choice_index(text, expected):
    assert answer_reader.to_form_value(MCQ, ans(text)) == expected


@pytest.mark.parametrize("text, expected", [("ถูก", 0), ("ผิด", 1), ("True", 0), ("F", 1), ("?", None)])
def test_true_false_answer(text, expected):
    assert answer_reader.to_form_value(TF, ans(text)) == expected


def test_subjective_answer_is_text_and_missing_is_empty():
    assert answer_reader.to_form_value(ESSAY, ans("  คำตอบยาว  ")) == "คำตอบยาว"
    assert answer_reader.to_form_value(ESSAY, None) == ""
    assert answer_reader.to_form_value(MCQ, None) is None


def _image_bytes(fmt: str) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (400, 300), "white").save(buf, format=fmt)
    return buf.getvalue()


def test_choice_text_used_when_letter_missing():
    assert answer_reader.to_form_value(MCQ, ans("", "Unsupervised")) is None  # ไม่ได้ตอบ
    assert answer_reader.to_form_value(MCQ, ans("?", "Unsupervise")) == 1


def test_consistency_warning_when_letter_and_choice_text_disagree():
    assert answer_reader.consistency_warning(MCQ, ans("ค", "Unsupervised")) is not None
    assert answer_reader.consistency_warning(MCQ, ans("ข", "Unsupervised")) is None
    assert answer_reader.consistency_warning(MCQ, ans("ข", "")) is None  # เทียบไม่ได้ ไม่เตือน
    assert answer_reader.consistency_warning(ESSAY, ans("ข้อความ")) is None


def test_scanned_pdf_and_photo_become_page_images():
    pdf = answer_reader.load_answer_sheet("scan.pdf", _image_bytes("PDF"))
    assert pdf.pages == 1 and pdf.images[0].startswith(b"\x89PNG") and not pdf.truncated
    photo = answer_reader.load_answer_sheet("photo.jpg", _image_bytes("JPEG"))
    assert len(photo.images) == 1


def test_typed_pdf_is_rendered_as_image_too():
    try:
        data = export.html_to_pdf("<p>ชื่อ นายทดสอบ</p><p>ข้อ 1. ข</p>")
    except export.ExportError as e:
        pytest.skip(f"ไม่มีเบราว์เซอร์สำหรับสร้าง PDF ทดสอบ: {e}")
    sheet = answer_reader.load_answer_sheet("typed.pdf", data)
    assert sheet.pages == 1 and len(sheet.images) == 1
    width, height = Image.open(io.BytesIO(sheet.images[0])).size
    assert max(width, height) == answer_reader.MAX_IMAGE_SIDE  # เรนเดอร์ละเอียดแล้วย่อเหลือขนาดสูงสุด


def test_bad_files_raise_sheet_error():
    with pytest.raises(answer_reader.SheetError):
        answer_reader.load_answer_sheet("answers.docx", b"x")
    with pytest.raises(answer_reader.SheetError):
        answer_reader.load_answer_sheet("broken.pdf", b"not a pdf")
    with pytest.raises(answer_reader.SheetError):
        answer_reader.load_answer_sheet("broken.png", b"not an image")


def _multipage_pdf(pages: int) -> bytes:
    images = [Image.new("RGB", (200, 280), "white") for _ in range(pages)]
    buf = io.BytesIO()
    images[0].save(buf, format="PDF", save_all=True, append_images=images[1:])
    return buf.getvalue()


def test_split_whole_class_pdf_into_students():
    sheets = answer_reader.split_pdf("ห้อง 5-1.pdf", _multipage_pdf(6), pages_per_student=2)
    assert [s.name for s in sheets] == ["ห้อง 5-1 คนที่ 1", "ห้อง 5-1 คนที่ 2", "ห้อง 5-1 คนที่ 3"]
    assert all(s.pages == 2 and len(s.images) == 2 for s in sheets)
    with pytest.raises(answer_reader.SheetError, match="ไม่ลงตัว"):
        answer_reader.split_pdf("x.pdf", _multipage_pdf(5), pages_per_student=2)


def test_review_warning_covers_each_reason():
    assert "ไม่พบคำตอบ" in answer_reader.review_warning(MCQ, None)
    assert "จับคู่กับตัวเลือกไม่ได้" in answer_reader.review_warning(MCQ, ans("จ"))
    assert "อาจอ่านเครื่องหมายผิด" in answer_reader.review_warning(MCQ, ans("ค", "Unsupervised"))
    low = ExtractedAnswer(question_id=1, answer="ข", choice_text="", confidence="low", note="เลือน")
    assert answer_reader.review_warning(MCQ, low) == "อ่านไม่ค่อยออก — เลือน · กรุณาตรวจทาน"
    assert answer_reader.review_warning(MCQ, ans("ข", "Unsupervised")) is None
