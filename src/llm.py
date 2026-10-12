"""จุดเดียวที่เรียกโมเดล: ส่ง prompt แล้วได้ผลลัพธ์เป็น Pydantic object ที่ validate แล้ว

รองรับ 2 ผู้ให้บริการ เลือกด้วย LLM_PROVIDER ใน .env
- ollama: รันโมเดลในเครื่อง ฟรี (structured output ผ่าน `format=<JSON schema>`)
- claude: Claude API (structured output ผ่าน `output_format=<Pydantic model>`)
"""

import base64
import json
from typing import TypeVar

import anthropic
import ollama
from pydantic import BaseModel, ValidationError

from . import config
from .sources import pdf_to_text

T = TypeVar("T", bound=BaseModel)

_claude: anthropic.Anthropic | None = None
_ollama: ollama.Client | None = None


class LLMError(RuntimeError):
    """ข้อผิดพลาดที่แสดงให้ผู้ใช้เห็นได้ตรง ๆ"""


def call_structured(*, system: str, content: list[dict], schema: type[T], effort: str) -> T:
    if config.PROVIDER == "ollama":
        return _call_ollama(system, content, schema)
    return _call_claude(system, content, schema, effort)


def check_ready() -> tuple[bool, str]:
    """ตรวจว่าพร้อมเรียกโมเดลหรือยัง คืน (พร้อมไหม, ข้อความสถานะ)"""
    if config.PROVIDER == "ollama":
        try:
            installed = {m.model for m in _get_ollama().list().models}
        except ConnectionError:
            return False, "ยังไม่ได้เปิด Ollama — เปิดโปรแกรม Ollama ก่อน แล้วรีเฟรชหน้านี้"
        if config.OLLAMA_MODEL not in installed and f"{config.OLLAMA_MODEL}:latest" not in installed:
            return False, f"ยังไม่ได้โหลดโมเดล — รัน `ollama pull {config.OLLAMA_MODEL}`"
        return True, "Ollama พร้อมใช้งาน (ฟรี รันในเครื่อง)"
    if config.has_api_key():
        return True, "พบ Claude API key"
    return False, "ยังไม่ได้ตั้ง ANTHROPIC_API_KEY — คัดลอก `.env.example` เป็น `.env` แล้วใส่ key จากนั้นรีสตาร์ตแอป"


# ---------------- Ollama ----------------

def _get_ollama() -> ollama.Client:
    global _ollama
    if _ollama is None:
        _ollama = ollama.Client(host=config.OLLAMA_HOST)
    return _ollama


def blocks_to_text(content: list[dict]) -> str:
    """แปลง content block รูปแบบ Claude เป็นข้อความล้วนสำหรับโมเดลที่รับได้แค่ข้อความ"""
    parts = []
    for block in content:
        if block["type"] == "text":
            parts.append(block["text"])
        elif block["type"] == "document":
            text = pdf_to_text(base64.standard_b64decode(block["source"]["data"]))
            parts.append(f'<course_material name="{block.get("title", "")}">\n{text}\n</course_material>')
    return "\n\n".join(parts)


def _call_ollama(system: str, content: list[dict], schema: type[T]) -> T:
    json_schema = schema.model_json_schema()
    # Ollama แนะนำให้ใส่ schema ใน prompt ด้วย นอกจากการบังคับผ่าน format
    system_full = (
        f"{system}\n\nตอบกลับเป็น JSON ที่ตรงกับ schema นี้เท่านั้น:\n"
        f"{json.dumps(json_schema, ensure_ascii=False)}"
    )
    try:
        response = _get_ollama().chat(
            model=config.OLLAMA_MODEL,
            messages=[
                {"role": "system", "content": system_full},
                {"role": "user", "content": blocks_to_text(content)},
            ],
            format=json_schema,
            think=config.OLLAMA_THINK,
            options={"num_ctx": config.OLLAMA_NUM_CTX, "temperature": 0.3},
        )
    except ConnectionError as e:
        raise LLMError("เชื่อมต่อ Ollama ไม่ได้ — เปิดโปรแกรม Ollama ก่อน") from e
    except ollama.ResponseError as e:
        if e.status_code == 404:
            raise LLMError(f"ยังไม่ได้โหลดโมเดล — รัน `ollama pull {config.OLLAMA_MODEL}`") from e
        raise LLMError(f"Ollama ตอบกลับด้วยข้อผิดพลาด: {e.error}") from e

    if response.done_reason == "length":
        raise LLMError("ผลลัพธ์ยาวเกินขีดจำกัด ลองลดจำนวนข้อ หรือเพิ่ม OLLAMA_NUM_CTX ใน .env")
    try:
        return schema.model_validate_json(response.message.content)
    except ValidationError as e:
        raise LLMError("โมเดลในเครื่องตอบกลับไม่ตรงรูปแบบ ลองใหม่อีกครั้ง") from e


# ---------------- Claude ----------------

def _get_claude() -> anthropic.Anthropic:
    global _claude
    if _claude is None:
        _claude = anthropic.Anthropic()
    return _claude


def _call_claude(system: str, content: list[dict], schema: type[T], effort: str) -> T:
    try:
        with _get_claude().beta.messages.stream(
            model=config.MODEL,
            max_tokens=config.MAX_TOKENS,
            system=system,
            messages=[{"role": "user", "content": content}],
            output_format=schema,
            output_config={"effort": effort},
            betas=[config.FALLBACK_BETA],
            fallbacks="default",
        ) as stream:
            response = stream.get_final_message()
    except anthropic.AuthenticationError as e:
        raise LLMError("API key ไม่ถูกต้อง ตรวจสอบ ANTHROPIC_API_KEY ในไฟล์ .env") from e
    except anthropic.RateLimitError as e:
        raise LLMError("เรียก API ถี่เกินไป รอสักครู่แล้วลองใหม่") from e
    except anthropic.APIStatusError as e:
        raise LLMError(f"Claude API ตอบกลับด้วยข้อผิดพลาด {e.status_code}: {e.message}") from e
    except anthropic.APIConnectionError as e:
        raise LLMError("เชื่อมต่อ Claude API ไม่ได้ ตรวจสอบอินเทอร์เน็ต") from e
    except anthropic.AnthropicError as e:  # เช่น ยังไม่ได้ตั้ง API key
        raise LLMError(f"เรียก Claude ไม่สำเร็จ: {e}") from e

    if response.stop_reason == "refusal":
        raise LLMError("โมเดลปฏิเสธคำขอนี้ ลองปรับเนื้อหาหรือคำสั่งใหม่")
    if response.stop_reason == "max_tokens":
        raise LLMError("ผลลัพธ์ยาวเกินขีดจำกัด ลองลดจำนวนข้อลง")
    parsed = response.parsed_output
    if parsed is None:
        raise LLMError("อ่านผลลัพธ์จากโมเดลไม่ได้ ลองใหม่อีกครั้ง")
    return parsed
