"""หน้าครู 3: สอบบนกระดาษและตรวจทั้งห้อง → วิเคราะห์ข้อสอบจากผลจริง"""

from ui import class_tab
from ui.common import state

class_tab.render(state.llm_ready)
