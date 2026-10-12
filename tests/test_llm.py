from src import sources
from src.llm import blocks_to_text


def test_blocks_to_text_keeps_material_and_request_in_order():
    content = sources.load_text("สไลด์ 1 เนื้อหา".encode("utf-8"), "notes.txt").content_blocks() + [
        {"type": "text", "text": "<user_request>\nปรนัย 3 ข้อ\n</user_request>"},
    ]
    text = blocks_to_text(content)
    assert text.index("<course_material") < text.index("<user_request>")
    assert "สไลด์ 1 เนื้อหา" in text and "ปรนัย 3 ข้อ" in text


def test_blocks_to_text_topic_only():
    assert blocks_to_text(sources.from_topic("ระบบสุริยะ").content_blocks()) == "<topic>\nระบบสุริยะ\n</topic>"
