"""หน้าผู้เรียน: คะแนน คำแนะนำ และแบบฝึกเพิ่ม"""

from ui import result_tab
from ui.common import state

result_tab.render(state.llm_ready)
