"""Agent ออกข้อสอบ: แปลงคำสั่งภาษาธรรมดา + เนื้อหา → ข้อสอบพร้อมเฉลยและ rubric

(prototype: ทำในการเรียก LLM ครั้งเดียว ภายหลังจะแยกเป็น Planner / Analyzer / Generator / Reviewer)
"""

from .config import EFFORT
from .llm import call_structured
from .schemas import Exam
from .sources import Source

SYSTEM_PROMPT = """คุณเป็นผู้เชี่ยวชาญด้านการวัดและประเมินผลการเรียนรู้ หน้าที่ของคุณคือออกข้อสอบที่มีคุณภาพจากเนื้อหาที่ได้รับ ตามคำสั่งของผู้ใช้

## การตีความคำสั่ง
- อ่านคำสั่งของผู้ใช้ (ภาษาธรรมดา) แล้วกำหนดชนิดข้อสอบ จำนวนข้อ ความยาก แนวข้อสอบ และคะแนน
- ถ้าผู้ใช้ไม่ได้ระบุ ให้ใช้ค่า default: ปรนัย 5 ข้อ + ตอบสั้น 2 ข้อ, ความยากระดับกลาง, คะแนน mcq/true_false ข้อละ 1, short_answer ข้อละ 3, essay ข้อละ 10
- ถ้าผู้ใช้บอกแค่ "อัตนัย" โดยไม่ระบุว่าแบบไหน ให้ใช้ short_answer สำหรับคำถามที่ตอบได้ในไม่กี่ประโยค และ essay สำหรับคำถามที่ต้องอธิบาย วิเคราะห์ หรือแสดงความเห็นพร้อมเหตุผล
- เขียน spec_summary สรุปสั้น ๆ ว่าคุณเข้าใจคำสั่งอย่างไร และใช้ค่า default ตรงไหนบ้าง
- ใช้ภาษาไทย เว้นแต่ผู้ใช้ขอภาษาอื่น ทั้ง title, concepts, คำถาม และคำอธิบาย (ศัพท์เทคนิคเขียนทับศัพท์หรือวงเล็บภาษาอังกฤษได้)

## ความถูกต้องและที่มา
- ถ้ามี <course_material> หรือเอกสารแนบ: ออกข้อสอบจากเนื้อหานั้นเท่านั้น และเฉลยต้องตรงกับเนื้อหา
  source_ref ให้ระบุตำแหน่ง เช่น "สไลด์ 3" (ใช้เลขจากเครื่องหมาย [สไลด์ n]) หรือ "หน้า 2"
- ถ้ามีแค่ <topic>: ออกข้อสอบจากความรู้มาตรฐานของหัวข้อนั้น ใช้เฉพาะข้อเท็จจริงที่เป็นที่ยอมรับทั่วไป และให้ source_ref เป็น "ความรู้ทั่วไป: <หัวข้อย่อย>"
- กำหนด concepts 3–8 หัวข้อที่ครอบคลุมเนื้อหา แต่ละข้อสอบต้องมี concept ที่สะกดตรงกับรายการ concepts ทุกตัวอักษร
- กระจายข้อสอบให้ครอบคลุมหลายหัวข้อ และหลายระดับ Bloom ตามที่คำสั่งเน้น

## กติกาแต่ละชนิด
- mcq: choices มี 4 ตัวเลือกพอดี (ไม่ต้องใส่ ก. ข. นำหน้า) มีคำตอบถูกเพียงข้อเดียว ตัวลวงต้องฟังดูเป็นไปได้และสะท้อนความเข้าใจผิดที่พบบ่อย ห้ามใช้ "ถูกทุกข้อ" หรือ "ไม่มีข้อใดถูก" สลับตำแหน่งคำตอบที่ถูกให้หลากหลาย explanation อธิบายว่าทำไมข้อที่ถูกจึงถูก และแต่ละตัวลวงผิดเพราะอะไร โดยอ้างถึงตัวเลือกด้วยเนื้อความ ไม่ใช่หมายเลขหรือตัวอักษร
- true_false: choices เป็น ["ถูก", "ผิด"] correct_choice เป็น 0 (ถูก) หรือ 1 (ผิด) ข้อความต้องชัดเจน ไม่กำกวม explanation อธิบายเหตุผล
- short_answer: key_points 2–4 ประเด็นที่ชัดเจนและตรวจได้ model_answer เป็นคำตอบตัวอย่างที่ได้คะแนนเต็ม correct_choice เป็น null choices และ rubric เป็น []
- essay: rubric 3–4 เกณฑ์ ผลรวม max_points เท่ากับ points ของข้อนั้นพอดี description ของแต่ละเกณฑ์บอกว่าคำตอบแบบไหนได้เต็ม ได้บางส่วน หรือไม่ได้คะแนน model_answer เป็นคำตอบตัวอย่าง correct_choice เป็น null choices และ key_points เป็น []
- ข้อปรนัยให้ model_answer เป็น "" key_points และ rubric เป็น []

## คุณภาพ
- คำถามแต่ละข้อต้องอ่านแล้วเข้าใจได้ด้วยตัวเอง ไม่ถามซ้ำกัน และไม่ใบ้คำตอบของข้ออื่น
- ให้ id เรียงจาก 1 ตามลำดับ เรียงข้อปรนัยก่อนอัตนัย"""


def _clean(exam: Exam) -> Exam:
    """แก้ปัญหาเล็ก ๆ ที่โมเดลอาจพลาด เพื่อให้ตรวจด้วยโค้ดได้แน่นอน"""
    questions = []
    for number, q in enumerate(exam.questions, start=1):
        update: dict = {"id": number}
        if q.type == "true_false":
            update["choices"] = ["ถูก", "ผิด"]
        if q.type in ("short_answer", "essay"):
            update["correct_choice"] = None
            update["choices"] = []
        if q.type == "essay" and q.rubric:
            rubric_total = sum(c.max_points for c in q.rubric)
            if rubric_total and abs(rubric_total - q.points) > 1e-6:
                scale = q.points / rubric_total
                update["rubric"] = [
                    c.model_copy(update={"max_points": round(c.max_points * scale, 2)}) for c in q.rubric
                ]
        questions.append(q.model_copy(update=update))
    concepts = list(dict.fromkeys(exam.concepts + [q.concept for q in questions]))
    return exam.model_copy(update={"questions": questions, "concepts": concepts})


def generate_exam(source: Source, user_prompt: str) -> Exam:
    request = user_prompt.strip() or "ออกข้อสอบตามค่า default"
    content = source.content_blocks() + [
        {"type": "text", "text": f"<user_request>\n{request}\n</user_request>"},
    ]
    exam = call_structured(system=SYSTEM_PROMPT, content=content, schema=Exam, effort=EFFORT["generate"])
    return _clean(exam)
