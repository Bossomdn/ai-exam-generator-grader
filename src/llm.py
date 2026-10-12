"""จุดเดียวที่เรียก Claude: ส่ง prompt แล้วได้ผลลัพธ์เป็น Pydantic object ที่ validate แล้ว"""

from typing import TypeVar

import anthropic
from pydantic import BaseModel

from .config import FALLBACK_BETA, MAX_TOKENS, MODEL

T = TypeVar("T", bound=BaseModel)

_client: anthropic.Anthropic | None = None


class LLMError(RuntimeError):
    """ข้อผิดพลาดที่แสดงให้ผู้ใช้เห็นได้ตรง ๆ"""


def _get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        _client = anthropic.Anthropic()
    return _client


def call_structured(*, system: str, content: list[dict], schema: type[T], effort: str) -> T:
    try:
        with _get_client().beta.messages.stream(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            system=system,
            messages=[{"role": "user", "content": content}],
            output_format=schema,
            output_config={"effort": effort},
            betas=[FALLBACK_BETA],
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
