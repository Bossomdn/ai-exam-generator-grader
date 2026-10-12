"""Learning Advisor: อ่านผลตรวจ + คะแนนรายหัวข้อ แล้วบอกว่าต้องเรียนเพิ่มหรือปรับปรุงตรงไหน

ตัวเลขทั้งหมดคำนวณในโค้ด (scoring.concept_mastery) LLM ทำหน้าที่ตีความและเขียนคำแนะนำเท่านั้น
"""

import json

from .config import EFFORT, WEAK_THRESHOLD
from .llm import call_structured
from .schemas import ConceptMastery, Exam, LearningReport, QuestionResult

SYSTEM_PROMPT = f"""คุณเป็นที่ปรึกษาด้านการเรียนรู้ วิเคราะห์ผลสอบของผู้เรียน แล้วบอกอย่างเจาะจงว่าควรเรียนเพิ่มหรือปรับปรุงตรงไหน

- weak_topics: หัวข้อที่ mastery ต่ำกว่า {WEAK_THRESHOLD:g}% เรียงจากอ่อนที่สุด concept ต้องสะกดตรงกับข้อมูลที่ได้รับ
  ถ้าไม่มีหัวข้อใดต่ำกว่าเกณฑ์ ให้เลือกหัวข้อที่คะแนนต่ำสุด 1–2 หัวข้อเป็นเรื่องที่ควรต่อยอด
  - why: อ้างอิงข้อที่ผิดและความเข้าใจผิดที่พบจริงในผลตรวจ
  - review: ใช้เฉพาะ source_ref ที่มีอยู่ในข้อมูล ห้ามแต่งเลขสไลด์หรือหน้าขึ้นเอง
  - actions: สิ่งที่ทำได้จริง เช่น "ทบทวนความต่างระหว่าง precision กับ recall แล้วลองคำนวณจากตัวอย่างในสไลด์ 5"
- strengths: หัวข้อหรือทักษะที่ทำได้ดี
- answer_writing_tips: ถ้ามีข้ออัตนัย ให้คำแนะนำการเขียนตอบจากรูปแบบที่พบ (เช่น ขาดเหตุผลประกอบ ไม่ยกตัวอย่าง) ถ้าไม่มีให้เป็น []
- study_plan: ลำดับการทบทวน 3–5 ขั้น
- practice_prompt: เขียนคำสั่งภาษาธรรมดาสำหรับระบบออกข้อสอบ เพื่อสร้างแบบฝึกชุดใหม่ที่เน้นหัวข้อที่อ่อน ระบุชนิด จำนวนข้อ และระดับความยากให้เหมาะกับผลสอบ
- เขียนกับผู้เรียนโดยตรง ให้กำลังใจแต่ตรงไปตรงมา ใช้ภาษาเดียวกับข้อสอบ"""


def _result_summary(exam: Exam, results: list[QuestionResult]) -> list[dict]:
    questions = {q.id: q for q in exam.questions}
    summary = []
    for r in results:
        item = {
            "id": r.question_id,
            "type": r.type,
            "concept": r.concept,
            "source_ref": r.source_ref,
            "bloom_level": questions[r.question_id].bloom_level,
            "question": questions[r.question_id].question,
            "score": f"{r.points_awarded:g}/{r.max_points:g}",
        }
        if r.is_correct is not None:
            item["correct"] = r.is_correct
            item["student_answer"] = r.student_answer
        else:
            item["missed_key_points"] = r.missed_key_points
            item["misconceptions"] = r.misconceptions
            item["grader_feedback"] = r.feedback
        summary.append(item)
    return summary


def advise(exam: Exam, results: list[QuestionResult], mastery: list[ConceptMastery]) -> LearningReport:
    data = {
        "exam_title": exam.title,
        "concept_mastery": [m.model_dump(include={"concept", "percent", "source_refs"}) for m in mastery],
        "results": _result_summary(exam, results),
    }
    content = [{
        "type": "text",
        "text": f"<exam_results>\n{json.dumps(data, ensure_ascii=False, indent=1)}\n</exam_results>",
    }]
    return call_structured(system=SYSTEM_PROMPT, content=content, schema=LearningReport, effort=EFFORT["advise"])
