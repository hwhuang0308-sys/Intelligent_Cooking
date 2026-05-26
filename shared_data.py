import threading
from dataclasses import dataclass
from typing import List, Dict

INVALID_DURATION_STR = "-9999"

INVALID_CONDITION_DATA = {
    'sensors': [],
    "logic": "%%%GARBAGE_DATA_FOR_INSTANT_STEP%%%"
}

@dataclass
class Step:
    is_continuous: int        # 1=持续, 0=瞬时
    recommended_duration: str # 持续步："0120"； 瞬时步：INVALID_DURATION_STR
    conditions: dict          # 持续步：{标准json}； 瞬时步：GARBAGE_CONDITION_DATA
    do_text: str              # "加入食盐..."

GLOBAL_RECIPE_STEPS: List[Step] = []

STEP_LOCK = threading.Lock()
CURRENT_EXEC_INDEX: int = 1

# =========================
# Global cooking exit flag (cross-thread)
# =========================
# UI/Popup/Executor 任意线程置位后，都应立即终止做菜流程
COOKING_EXIT_REQUESTED = threading.Event()

# =========================
# Small warning popup cooldown (cross-step)
# =========================
# 上次关闭“小报警窗”的时间戳（time.time()），用于跨步骤继承冷却期
SMALL_WARNING_LAST_CLOSE_TS: float = 0.0
# 保护该时间戳的锁（F4 线程会并发读写）
SMALL_WARNING_LOCK = threading.Lock()
