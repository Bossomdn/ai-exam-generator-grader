"""สร้างไฟล์ PDF สำหรับพิมพ์: ข้อสอบ (ไม่มีเฉลย), เฉลย + rubric, รายงานผลรายคน

สร้างเป็น HTML แล้วให้เบราว์เซอร์ (Edge / Chrome ผ่าน Playwright) แปลงเป็น PDF
เพราะเบราว์เซอร์จัดวางสระ วรรณยุกต์ และตัดคำภาษาไทยได้ถูกต้อง ซึ่งไลบรารี PDF ทั่วไปทำไม่ได้
"""

import asyncio
import base64
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from functools import lru_cache
from html import escape
from pathlib import Path
from typing import Optional

from . import scoring
from .config import WEAK_THRESHOLD
from .schemas import OBJECTIVE_TYPES, TYPE_LABELS, ConceptMastery, Exam, LearningReport, Question, QuestionResult

FONT_DIR = Path(__file__).resolve().parent.parent / "assets" / "fonts"
# ลองตามลำดับ: Edge (มีใน Windows ทุกเครื่อง) → Chrome → Chromium ของ Playwright (`playwright install chromium`)
BROWSER_CHANNELS = ("msedge", "chrome", None)
ANSWER_LINES = {"short_answer": 4, "essay": 12}
SHORT_TYPE_LABELS = {"mcq": "ปรนัย", "true_false": "ถูก/ผิด", "short_answer": "ตอบสั้น", "essay": "เรียงความ"}
THAI_MONTHS = ["ม.ค.", "ก.พ.", "มี.ค.", "เม.ย.", "พ.ค.", "มิ.ย.", "ก.ค.", "ส.ค.", "ก.ย.", "ต.ค.", "พ.ย.", "ธ.ค."]

FOOTER = (
    '<div style="font-size:8px;width:100%;text-align:center;color:#666;">'
    '<span class="pageNumber"></span> / <span class="totalPages"></span></div>'
)

CSS = """
@page { size: A4; }
* { box-sizing: border-box; }
body { font-family: 'Sarabun', 'Leelawadee UI', Tahoma, sans-serif; font-size: 12pt; line-height: 1.6;
       color: #111; margin: 0; }
h1 { font-size: 17pt; text-align: center; margin: 0 0 1mm; }
h2 { font-size: 13.5pt; background: #ececec; padding: 1.2mm 3mm; margin: 6mm 0 3mm; break-after: avoid; }
h3 { font-size: 12pt; margin: 4mm 0 1mm; break-after: avoid; }
.subtitle { text-align: center; color: #444; margin-bottom: 5mm; }
.info { border: 1px solid #333; padding: 3mm 4mm; margin-bottom: 4mm; display: flex; flex-wrap: wrap; gap: 2mm 6mm; }
.field { display: inline-block; border-bottom: 1px dotted #333; min-width: 30mm; }
.field.wide { min-width: 70mm; }
.note { color: #444; margin: 0 0 3mm; }
.question { break-inside: avoid; margin-bottom: 4.5mm; }
.q-head { font-weight: 700; }
.points { font-weight: 400; color: #555; }
.choices { list-style: none; padding-left: 8mm; margin: 1mm 0 0; }
.choices li { margin: 0.6mm 0; }
.lines div { border-bottom: 1px solid #aaa; height: 8.5mm; }
.box { border: 1px solid #ccc; background: #fafafa; padding: 2mm 3mm; white-space: pre-wrap; }
table { border-collapse: collapse; width: 100%; margin: 1mm 0 3mm; }
th, td { border: 1px solid #999; padding: 1.4mm 2mm; vertical-align: top; text-align: left; }
th { background: #f0f0f0; }
td.num { text-align: center; white-space: nowrap; }
td.nowrap { white-space: nowrap; }
.key-item { margin-bottom: 5mm; }
tr { break-inside: avoid; }
.bar { background: #e5e7eb; height: 3mm; border-radius: 1mm; margin-top: 1mm; }
.bar > div { background: #4b5563; height: 100%; border-radius: 1mm; }
.weak { font-weight: 700; }
.block { border: 1px solid #ccc; padding: 2mm 4mm; margin-bottom: 3mm; break-inside: avoid; }
ul, ol { margin: 1mm 0 2mm; padding-left: 7mm; }
"""


class ExportError(RuntimeError):
    """สร้าง PDF ไม่สำเร็จ (ข้อความแสดงให้ผู้ใช้เห็นได้ตรง ๆ)"""


# ---------------- helpers ----------------

def strip_numbering(text: str) -> str:
    """ตัดเลขลำดับที่โมเดลใส่มาเอง (เช่น "1. ") เพื่อไม่ให้ซ้ำกับเลขที่เราใส่"""
    return re.sub(r"^\s*\d+[.)]\s*", "", text)


def _t(text: str) -> str:
    """escape ข้อความจาก LLM/ผู้ใช้ก่อนใส่ใน HTML และคงการขึ้นบรรทัดใหม่"""
    return escape(text).replace("\n", "<br>")


def _ul(items: list[str]) -> str:
    return "<ul>" + "".join(f"<li>{_t(i)}</li>" for i in items) + "</ul>" if items else ""


def thai_date(d: date) -> str:
    return f"{d.day} {THAI_MONTHS[d.month - 1]} {d.year + 543}"


@lru_cache(maxsize=1)
def _font_css() -> str:
    faces = []
    for weight, file in ((400, "Sarabun-Regular.ttf"), (700, "Sarabun-Bold.ttf")):
        path = FONT_DIR / file
        if path.exists():
            data = base64.b64encode(path.read_bytes()).decode("ascii")
            faces.append(
                f"@font-face {{ font-family: 'Sarabun'; font-weight: {weight}; "
                f"src: url(data:font/ttf;base64,{data}) format('truetype'); }}"
            )
    return "\n".join(faces)


def _page(title: str, body: str) -> str:
    return (
        f'<!doctype html><html lang="th"><head><meta charset="utf-8"><title>{escape(title)}</title>'
        f"<style>{_font_css()}{CSS}</style></head><body>{body}</body></html>"
    )


def _split(exam: Exam) -> tuple[list[Question], list[Question]]:
    objective = [q for q in exam.questions if q.type in OBJECTIVE_TYPES]
    subjective = [q for q in exam.questions if q.type not in OBJECTIVE_TYPES]
    return objective, subjective


def _points(questions: list[Question]) -> float:
    return sum(q.points for q in questions)


# ---------------- ข้อสอบ (ไม่มีเฉลย) ----------------

def _question_block(q: Question) -> str:
    head = f'<div class="q-head">ข้อ {q.id}. <span class="points">({q.points:g} คะแนน)</span></div>'
    body = f"<div>{_t(q.question)}</div>"
    if q.type == "mcq":
        body += '<ol class="choices">' + "".join(
            f"<li>{_t(scoring.choice_label(q, i))}</li>" for i in range(len(q.choices))
        ) + "</ol>"
    elif q.type == "true_false":
        body += '<div class="choices">(&nbsp;&nbsp;&nbsp;) ถูก&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;(&nbsp;&nbsp;&nbsp;) ผิด</div>'
    else:
        body += '<div class="lines">' + "<div></div>" * ANSWER_LINES.get(q.type, 4) + "</div>"
    return f'<div class="question">{head}{body}</div>'


def exam_html(exam: Exam) -> str:
    objective, subjective = _split(exam)
    total = _points(exam.questions)
    parts = [
        f"<h1>{_t(exam.title)}</h1>",
        f'<div class="subtitle">จำนวน {len(exam.questions)} ข้อ · คะแนนเต็ม {total:g} คะแนน</div>',
        '<div class="info">'
        '<span>ชื่อ-สกุล <span class="field wide"></span></span>'
        '<span>เลขที่ <span class="field"></span></span>'
        '<span>ชั้น/กลุ่ม <span class="field"></span></span>'
        f'<span>คะแนนที่ได้ <span class="field"></span> / {total:g}</span>'
        "</div>",
    ]
    section = 0
    if objective:
        section += 1
        parts.append(f"<h2>ตอนที่ {section} ปรนัย ({len(objective)} ข้อ {_points(objective):g} คะแนน)</h2>")
        note = "เลือกคำตอบที่ถูกต้องที่สุดเพียงคำตอบเดียว"
        if any(q.type == "true_false" for q in objective):
            note += " ข้อถูก/ผิดให้ทำเครื่องหมาย ✓ ในวงเล็บหน้าคำตอบ"
        parts.append(f'<p class="note">คำชี้แจง: {note}</p>')
        parts.extend(_question_block(q) for q in objective)
    if subjective:
        section += 1
        parts.append(f"<h2>ตอนที่ {section} อัตนัย ({len(subjective)} ข้อ {_points(subjective):g} คะแนน)</h2>")
        parts.append('<p class="note">คำชี้แจง: ตอบคำถามให้ชัดเจน ครบถ้วน และให้เหตุผลประกอบ</p>')
        parts.extend(_question_block(q) for q in subjective)
    return _page(exam.title, "".join(parts))


# ---------------- เฉลย + rubric ----------------

def answer_key_html(exam: Exam) -> str:
    objective, subjective = _split(exam)
    parts = [
        f"<h1>เฉลยและเกณฑ์การให้คะแนน</h1>",
        f'<div class="subtitle">{_t(exam.title)} · สำหรับผู้สอน (ไม่แจกผู้สอบ)</div>',
    ]
    if objective:
        rows = "".join(
            f'<tr><td class="num">{q.id}</td><td>{_t(scoring.choice_label(q, q.correct_choice))}</td>'
            f"<td>{_t(q.concept)}<br><small>{_t(q.source_ref)}</small></td><td>{_t(q.explanation)}</td></tr>"
            for q in objective
        )
        parts.append(
            "<h2>ตอนปรนัย</h2><table><tr><th>ข้อ</th><th>เฉลย</th><th>หัวข้อ / ที่มา</th><th>คำอธิบาย</th></tr>"
            f"{rows}</table>"
        )
    if subjective:
        parts.append("<h2>ตอนอัตนัย</h2>")
        for q in subjective:
            block = [
                f'<div class="key-item"><div class="q-head">ข้อ {q.id}. <span class="points">'
                f"({TYPE_LABELS[q.type]} · {q.points:g} คะแนน · {_t(q.concept)} · {_t(q.source_ref)})</span></div>",
                f"<div>{_t(q.question)}</div>",
                f'<h3>คำตอบตัวอย่าง</h3><div class="box">{_t(q.model_answer)}</div>',
            ]
            criteria = scoring.subjective_criteria(q)
            label = "เกณฑ์การให้คะแนน (rubric)" if q.type == "essay" and q.rubric else "ประเด็นที่ต้องมี"
            block.append(
                f"<h3>{label}</h3><table><tr><th>เกณฑ์</th><th>คะแนน</th><th>คำอธิบาย</th></tr>"
                + "".join(
                    f'<tr><td>{_t(name)}</td><td class="num">{max_pts:g}</td><td>{_t(desc)}</td></tr>'
                    for name, desc, max_pts in criteria
                )
                + "</table></div>"
            )
            parts.append("".join(block))
    return _page(f"เฉลย - {exam.title}", "".join(parts))


# ---------------- รายงานผลรายคน ----------------

def report_html(
    exam: Exam,
    results: list[QuestionResult],
    mastery: list[ConceptMastery],
    report: Optional[LearningReport],
    student_name: str = "",
    on: Optional[date] = None,
) -> str:
    t = scoring.totals(results)
    pct = 100 * t["score"] / t["max"] if t["max"] else 0
    questions = {q.id: q for q in exam.questions}
    parts = [
        "<h1>รายงานผลการสอบ</h1>",
        f'<div class="subtitle">{_t(exam.title)}</div>',
        f'<div class="info"><span>ชื่อผู้สอบ: <b>{_t(student_name.strip() or "-")}</b></span>'
        f"<span>วันที่: {thai_date(on or date.today())}</span></div>",
        "<table><tr><th>คะแนนรวม</th><th>ตอนปรนัย</th><th>ตอนอัตนัย</th></tr><tr>"
        f'<td class="num"><b>{t["score"]:g} / {t["max"]:g}</b> ({pct:.0f}%)</td>'
        f'<td class="num">{t["objective_score"]:g} / {t["objective_max"]:g}</td>'
        f'<td class="num">{t["subjective_score"]:g} / {t["subjective_max"]:g}</td></tr></table>',
    ]

    rows = []
    for m in mastery:
        weak = m.percent < WEAK_THRESHOLD
        status = '<span class="weak">ต้องเรียนเพิ่ม</span>' if weak else "ผ่าน"
        rows.append(
            f"<tr><td>{_t(m.concept)}</td><td class=\"num\">{m.points_awarded:g} / {m.max_points:g}</td>"
            f'<td style="width:26%">{m.percent:.0f}%<div class="bar"><div style="width:{min(m.percent, 100):.0f}%"></div></div></td>'
            f'<td class="nowrap">{status}</td><td class="nowrap">{_t(", ".join(m.source_refs))}</td></tr>'
        )
    parts.append(
        f"<h2>คะแนนรายหัวข้อ</h2><p class=\"note\">หัวข้อที่ได้ต่ำกว่า {WEAK_THRESHOLD:g}% ถือว่าต้องเรียนเพิ่ม</p>"
        "<table><tr><th>หัวข้อ</th><th>คะแนน</th><th>ร้อยละ</th><th>สถานะ</th><th>ที่มา</th></tr>"
        + "".join(rows) + "</table>"
    )

    if report:
        pct_by_concept = {m.concept: m.percent for m in mastery}
        parts.append(f"<h2>สรุปและคำแนะนำ</h2><p>{_t(report.overall_summary)}</p>")
        if report.strengths:
            parts.append("<h3>จุดแข็ง</h3>" + _ul(report.strengths))
        if report.weak_topics:
            parts.append("<h3>เรื่องที่ต้องเรียนเพิ่ม / ปรับปรุง</h3>")
            for w in report.weak_topics:
                concept_pct = pct_by_concept.get(w.concept)
                pct_text = f" — {concept_pct:.0f}%" if concept_pct is not None else ""
                review = f"<div><b>ควรทบทวน:</b> {_t(', '.join(w.review))}</div>" if w.review else ""
                parts.append(
                    f'<div class="block"><b>{_t(w.concept)}</b>{pct_text}<div>{_t(w.why)}</div>{review}{_ul(w.actions)}</div>'
                )
        if report.answer_writing_tips:
            parts.append("<h3>คำแนะนำการเขียนตอบอัตนัย</h3>" + _ul(report.answer_writing_tips))
        if report.study_plan:
            parts.append(
                "<h3>แผนการทบทวน</h3><ol>"
                + "".join(f"<li>{_t(strip_numbering(s))}</li>" for s in report.study_plan)
                + "</ol>"
            )

    detail_rows = []
    for r in results:
        q = questions[r.question_id]
        if q.type in OBJECTIVE_TYPES:
            mark = "ถูก" if r.is_correct else "ผิด"
            note = f"ตอบ: {_t(r.student_answer)} ({mark})<br>เฉลย: {_t(scoring.choice_label(q, q.correct_choice))}"
        else:
            note = _t(r.feedback)
            if r.unverified_evidence:
                note += "<br><i>หมายเหตุ: มีหลักฐานที่ผู้ตรวจอ้างแต่ไม่พบในคำตอบ ควรให้ผู้สอนตรวจซ้ำ</i>"
        detail_rows.append(
            f'<tr><td class="num">{r.question_id}</td><td class="nowrap">{SHORT_TYPE_LABELS[r.type]}</td><td>{_t(r.concept)}</td>'
            f'<td class="num">{r.points_awarded:g} / {r.max_points:g}</td><td>{note}</td></tr>'
        )
    parts.append(
        "<h2>รายละเอียดรายข้อ</h2><table><tr><th>ข้อ</th><th>ชนิด</th><th>หัวข้อ</th><th>คะแนน</th>"
        "<th>คำตอบ / ความเห็นผู้ตรวจ</th></tr>" + "".join(detail_rows) + "</table>"
    )
    return _page(f"รายงานผล - {exam.title}", "".join(parts))


# ---------------- HTML → PDF ----------------

async def _render(html: str) -> bytes:
    from playwright.async_api import Error as PlaywrightError
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = None
        for channel in BROWSER_CHANNELS:
            try:
                browser = await (p.chromium.launch(channel=channel) if channel else p.chromium.launch())
                break
            except PlaywrightError:
                continue
        if browser is None:
            raise ExportError(
                "ไม่พบเบราว์เซอร์สำหรับสร้าง PDF — ติดตั้ง Microsoft Edge หรือ Google Chrome "
                "หรือรัน `playwright install chromium`"
            )
        try:
            page = await browser.new_page()
            await page.set_content(html, wait_until="load")
            await page.evaluate("document.fonts.ready")
            return await page.pdf(
                format="A4",
                print_background=True,
                display_header_footer=True,
                header_template="<div></div>",
                footer_template=FOOTER,
                margin={"top": "15mm", "bottom": "17mm", "left": "15mm", "right": "15mm"},
            )
        finally:
            await browser.close()


def html_to_pdf(html: str) -> bytes:
    """รัน Playwright ใน thread แยกพร้อม event loop ของตัวเอง

    (Streamlit อาจตั้ง event loop แบบที่เปิด subprocess ไม่ได้บน Windows จึงไม่ใช้ loop ของ Streamlit)
    """
    def run() -> bytes:
        loop = asyncio.ProactorEventLoop() if sys.platform == "win32" else asyncio.new_event_loop()
        try:
            return loop.run_until_complete(_render(html))
        finally:
            loop.close()

    with ThreadPoolExecutor(max_workers=1) as pool:
        try:
            return pool.submit(run).result()
        except ExportError:
            raise
        except Exception as e:  # noqa: BLE001 — ข้อผิดพลาดจาก Playwright/เบราว์เซอร์
            raise ExportError(f"สร้าง PDF ไม่สำเร็จ: {e}") from e


def exam_pdf(exam: Exam) -> bytes:
    return html_to_pdf(exam_html(exam))


def answer_key_pdf(exam: Exam) -> bytes:
    return html_to_pdf(answer_key_html(exam))


def report_pdf(exam: Exam, results: list[QuestionResult], mastery: list[ConceptMastery],
               report: Optional[LearningReport], student_name: str = "") -> bytes:
    return html_to_pdf(report_html(exam, results, mastery, report, student_name))
