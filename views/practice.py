"""หน้าผู้เรียน: ทำข้อสอบในแอป หรืออัปโหลดกระดาษคำตอบ 1 คน"""

from ui import take_tab
from ui.common import state

take_tab.render(state.llm_ready)
