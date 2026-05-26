import re
import os
import time
from datetime import datetime
import threading
from typing import Any, Dict, List, Optional
import cv2
import get_api
import cook_camera_new
import utils
import shared_data
import cook_air  # NEW: use smoke concentration to gate vision checks
from shared_data import (
    Step,
    GLOBAL_RECIPE_STEPS,
    STEP_LOCK,
    INVALID_DURATION_STR,
    INVALID_CONDITION_DATA
)
from utils import _now_tags

abnormal_event = threading.Event()
stop_all_event = threading.Event()

abnormal_lock = threading.Lock()
abnormal_info: Dict[str, Any] = {}

def _create_emergency_steps_via_parser(abn_type: str):
    """
    直接写自然语言指令，调用 parser 转为标准 Step 对象。
    这样既支持瞬时动作(关火)，也支持持续动作(持续喷水)。
    """
    raw_strings = []

    if abn_type == "fire":
        raw_strings.append("应急步骤，do 立刻关火")
        raw_strings.append("应急步骤，do 立刻用锅盖盖住灭火")

    elif abn_type == "burnt":
        # 糊锅：先停止加热并处理锅底，后续你也可以再加“加水/翻炒/换锅”等动作
        raw_strings.append("应急步骤，do 发生糊锅，立即关火")

    else:
        raw_strings.append("应急步骤，do 暂停当前操作，检查安全")

    # === 调用 Parser 转换 ===
    step_objects = []
    for line in raw_strings:
        # 调用 parser.py 里的通用解析函数
        step_obj = utils.parse_single_line(line)
        step_objects.append(step_obj)

    return step_objects


# ================== 线程B：异常检测（着火示例） ==================
def abnormal_monitor_loop(
    *,
    interval_sec: float = 0.5,
    cooldown_sec: float = 10.0,
    base_dir: str = r'D:\python\cook\src',
    normal_save_every_sec: float = 0.5,
    smoke_trigger_value: float = 9.0,          # NEW: only do vision check when smoke >= this
    burnt_cooldown_sec: "Optional[float]" = None,    # NEW: cooldown for burnt-pot; default to cooldown_sec
) -> None:
    """
    - 着火(1)：保存到 abnormal（每次触发都存），并触发应急插入
    - 糊锅(1)：仅当空气传感器浓度==smoke_trigger_value 时才进行视觉判断；触发后同样插入应急步骤
    """
    today_tag = f"test_{datetime.now().month}_{datetime.now().day}"
    day_root = os.path.join(base_dir, today_tag)

    save_dir_abnormal = os.path.join(day_root, "pics", "abnormal")
    save_dir_normal = os.path.join(day_root, "pics", "normal")
    os.makedirs(save_dir_abnormal, exist_ok=True)
    os.makedirs(save_dir_normal, exist_ok=True)

    # 着火 prompt（保留你原来的）
    prompt_fire = "请判断给出的烹饪照片中是否发生着火。若着火请仅返回数字1；否则仅返回数字0。不要返回任何解释或其他内容，只输出数字1或0。"

    # NEW: 糊锅 prompt
    prompt_burnt = "请判断给出的烹饪照片中是否出现‘糊锅/糊底/烧焦’迹象（例如锅底发黑、食材明显烧焦、锅内冒大量焦糊烟等）。若发生糊锅请仅返回数字1；否则仅返回数字0。不要返回任何解释或其他内容，只输出数字1或0。"

    last_fire_trigger_ts = 0.0
    last_burnt_trigger_ts = 0.0

    if burnt_cooldown_sec is None:
        burnt_cooldown_sec = cooldown_sec

    # last_normal_save_ts = 0.0

    while True:
        if stop_all_event.is_set():
            return

        time.sleep(interval_sec)

        now_ts = time.time()

        # 1) 拿最新画面
        frame = cook_camera_new.get_picture(copy=True)
        if frame is None:
            continue

        # 2)（可选）未异常时的 normal 保存逻辑，你之前注释了，这里保持不动
        # _, _, now_file = _now_tags()
        # if now_ts - last_normal_save_ts >= normal_save_every_sec:
        #     cv2.imwrite(os.path.join(save_dir_normal, f"normal_{now_file}.jpg"), frame)
        #     last_normal_save_ts = now_ts

        # 3) 着火检测（你现在人为注释掉了 API 调用，这里沿用你的做法：不主动请求）
        # cooldown：着火触发后的一段时间内不重复触发
        if now_ts - last_fire_trigger_ts >= cooldown_sec:
            try:
                # ans_fire = str(get_api.get_vision_answer_from_np(frame, prompt_fire, model="gpt-5.2")).strip()
                ans_fire = "0"  # 你当前注释掉了，保持默认不触发
            except Exception as e:
                print(f"[ABN-fire] API 调用失败：{e}")
                ans_fire = "0"

            if ans_fire == "1":
                _, _, now_file = _now_tags()
                img_path = os.path.join(save_dir_abnormal, f"fire_{now_file}.jpg")
                cv2.imwrite(img_path, frame)

                last_fire_trigger_ts = time.time()

                emergency_steps = _create_emergency_steps_via_parser("fire")
                target_pos = shared_data.CURRENT_EXEC_INDEX
                with STEP_LOCK:
                    for step in reversed(emergency_steps):
                        GLOBAL_RECIPE_STEPS.insert(target_pos, step)

                abnormal_event.set()
                print("[ABN] 检测到着火异常：已触发中断标志。")

        # 4) NEW：糊锅检测（只在 smoke==10 时请求视觉）
        # cooldown：糊锅触发后的一段时间内不重复触发
        if now_ts - last_burnt_trigger_ts < burnt_cooldown_sec:
            continue

        smoke = None
        try:
            smoke = cook_air.get_latest()
        except Exception as e:
            # 空气传感器没开/串口异常时，不做糊锅视觉请求
            print(f"[ABN-burnt] 读取烟雾浓度失败：{e}")
            continue

        # 只有浓度>=阈值才进行视觉判断（按你的需求：>=9）
        if smoke is None or float(smoke) < float(smoke_trigger_value):
            continue

        try:
            ans_burnt = str(get_api.get_vision_answer_from_np(frame, prompt_burnt, model="gpt-5.2")).strip()
        except Exception as e:
            print(f"[ABN-burnt] API 调用失败：{e}")
            continue

        if ans_burnt == "1":
            _, _, now_file = _now_tags()
            img_path = os.path.join(save_dir_abnormal, f"burnt_{now_file}.jpg")
            cv2.imwrite(img_path, frame)

            last_burnt_trigger_ts = time.time()

            emergency_steps = _create_emergency_steps_via_parser("burnt")
            target_pos = shared_data.CURRENT_EXEC_INDEX
            with STEP_LOCK:
                for step in reversed(emergency_steps):
                    GLOBAL_RECIPE_STEPS.insert(target_pos, step)

            abnormal_event.set()
            print("[ABN] 检测到糊锅异常：已触发中断标志。")

