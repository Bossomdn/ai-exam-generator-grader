"""หน้าครู 2: ออกข้อสอบ (ผู้เรียนใช้สร้างแบบฝึกได้ด้วย)"""

from ui import create_tab
from ui.common import state

create_tab.render(state.llm_ready)
