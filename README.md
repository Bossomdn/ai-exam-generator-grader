# Multi-Agent System for Automated Assessment Generation and Evaluative Essay Grading

ระบบสร้างข้อสอบ (ปรนัย + อัตนัย) พร้อมเฉลยจากสไลด์/เอกสาร/หัวข้อ ตามคำสั่งภาษาธรรมดา
แล้วตรวจคำตอบ ให้คะแนน และบอกว่าควรเรียนเพิ่มหรือปรับปรุงเรื่องไหน

> สถานะ: **Prototype** — flow ครบแล้ว แต่ยังใช้ agent แบบเรียงลำดับธรรมดา (ยังไม่ใช้ LangGraph)

## วิธีติดตั้งและรัน

```powershell
python -m venv .venv
.\.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
streamlit run app.py
```

เลือกโมเดลได้ 2 แบบด้วย `LLM_PROVIDER` ในไฟล์ `.env`

| ค่า | ใช้อะไร | ค่าใช้จ่าย | ต้องเตรียม |
|---|---|---|---|
| `ollama` (ค่าเริ่มต้น) | `gemma4:12b` รันในเครื่อง | ฟรี | ติดตั้ง [Ollama](https://ollama.com/download) แล้วรัน `ollama pull gemma4:12b` (~8GB, ควรมี GPU 8GB+) |
| `claude` | `claude-opus-5-5` ผ่าน API | ~$0.1–0.3 ต่อรอบ | ใส่ `ANTHROPIC_API_KEY` จาก https://console.anthropic.com |

ทดสอบ (ไม่ต้องใช้โมเดล): `python -m pytest`

## วิธีใช้

1. **สร้างข้อสอบ** — อัปโหลดสไลด์ (.pptx / .pdf / .txt) หรือใช้เนื้อหาตัวอย่าง หรือพิมพ์แค่หัวข้อ
   แล้วพิมพ์ว่าอยากได้ข้อสอบแบบไหน เช่น *"ปรนัย 5 ข้อ ถูก/ผิด 2 ข้อ อัตนัยตอบสั้น 1 ข้อ เรียงความ 1 ข้อ ระดับกลาง"*
2. **ทำข้อสอบ** — ตอบในแอปแล้วกดส่งตรวจ
3. **ผลและคำแนะนำ** — คะแนนรวม/แยกตอน, คะแนนรายหัวข้อ, เรื่องที่ต้องเรียนเพิ่ม + สไลด์ที่ควรทบทวน,
   และปุ่มสร้างแบบฝึกเพิ่มเฉพาะเรื่องที่อ่อน

## การทำงานของแต่ละส่วน

| ส่วน | ไฟล์ | ใช้ LLM? |
|---|---|---|
| อ่านเนื้อหา (PPTX รายสไลด์ + โน้ต, PDF, TXT, หัวข้อ) | `src/sources.py` | ไม่ |
| ออกข้อสอบ + เฉลย + rubric + แท็กหัวข้อ/สไลด์ | `src/generator.py` | ใช่ (effort high) |
| ตรวจปรนัย, รวมคะแนน, คะแนนรายหัวข้อ, ตรวจหลักฐาน | `src/scoring.py` | ไม่ |
| ตรวจอัตนัยตามเกณฑ์ พร้อมหลักฐานจากคำตอบ | `src/grader.py` | ใช่ (ทีละข้อ, ขนานกัน) |
| วิเคราะห์จุดอ่อนและคำแนะนำ | `src/advisor.py` | ใช่ |
| เรียกโมเดล (Ollama / Claude) + structured output + จัดการ error | `src/llm.py` | – |

แนวคิดหลัก
- **ตัวเลขทุกตัวคำนวณด้วยโค้ด** (คะแนนปรนัย คะแนนรวม คะแนนรายหัวข้อ) LLM ใช้เฉพาะงานที่ต้องตีความ
- grader ต้องคัดลอก **หลักฐานจากคำตอบ** มาประกอบคะแนน โค้ดตรวจว่ามีอยู่จริง ถ้าไม่พบจะเตือนให้ผู้สอนตรวจซ้ำ
- คำตอบนักเรียนถูกห่อด้วย `<student_answer>` และสั่งให้ถือเป็นข้อมูล (กัน prompt injection)
- ทุก agent ได้ผลลัพธ์เป็น JSON ตาม Pydantic schema (Ollama: `format=<schema>`, Claude: structured output + server-side fallback)
- Ollama อ่าน PDF เองไม่ได้ ระบบจึงแยกข้อความจาก PDF ก่อน (เห็นเฉพาะข้อความ ไม่เห็นรูป/ตาราง)

## ขั้นต่อไป (roadmap)

1. แยกเป็น multi-agent บน LangGraph: Request Planner → Content Analyzer → Generator → Reviewer (ลูปแก้ข้อที่ไม่ผ่าน)
2. ตรวจเรียงความด้วยคณะ grader 3 มุมมอง + Moderator
3. ประเมินระบบ: เทียบคะแนนกับมนุษย์ (QWK/MAE), ทดสอบการชี้จุดอ่อน, ทดสอบ prompt injection
