# -*- coding: utf-8 -*-

import re
import time
import threading
from dataclasses import dataclass
from typing import Any, Dict, List, Optional
from utils import extract_first_float
import condition_checker
import Daemon_thread  # 共享事件 + 计划读写 + 异常插入
import cook_temperature_new as temp
from shared_data import Step, GLOBAL_RECIPE_STEPS, STEP_LOCK
import shared_data

# =========================================================================
# 全局变量
# =========================================================================
GLOBAL_STEP_STATUS = []

def check_one(idx: int) -> bool:
    """
    检查 GLOBAL_STEP_STATUS[idx-1] 是否为 1
    """
    global GLOBAL_STEP_STATUS
    list_idx = idx - 1
    if GLOBAL_STEP_STATUS[list_idx] == 1:
        return True
    return False


def evaluate_condition_expression(raw_text: str, expression_data: dict, idx: int) -> int:
    """
    依次阻塞调用传感器检测函数，返回 1 或 0
    """
    sensors_cfg = expression_data.get("sensors", [])
    logic_str = expression_data.get("logic", "")
    step_idx_str = str(idx)
    current_results = []

    for i, cfg in enumerate(sensors_cfg):
        s_type = cfg.get("type")
        condition = cfg.get("condition")
        duration = float(cfg.get("duration", 0))

        is_met = 0
        try:
            if s_type == "visual":
                is_met = condition_checker.check_visual_condition(duration, str(condition), step_idx_str, raw_text)
            elif s_type == "temperature":
                if "食材" in condition:
                    condition = extract_first_float(condition)
                    is_met = condition_checker.check_temperature_condition_2(duration, float(condition), step_idx_str)
                else:
                    condition = extract_first_float(condition)
                    is_met = condition_checker.check_temperature_condition(duration, float(condition), step_idx_str)
            elif s_type == "audio":
                condition = extract_first_float(condition)
                is_met = condition_checker.check_audio_condition(duration, float(condition), step_idx_str)
        except Exception as e:
            print(f"[Sensor Error] {s_type} error: {e}")
            is_met = 0

        current_results.append(1 if is_met else 0)

    try:
        safe_results = [int(r) for r in current_results]
        final_expr = logic_str.format(*safe_results)
        return 1 if bool(eval(final_expr)) else 0
    except Exception as e:
        print(f"[Eval Error] {e}")
        return 0


# =========================================================================
# 任务函数
# =========================================================================

def function_1(idx: int, step_data) -> None:
    """
    【持续步任务 - 线程函数】
    循环 evaluate，满足或超时修改全局状态，然后退出线程。
    """
    global GLOBAL_STEP_STATUS

    expression_data = step_data.conditions
    raw_text = step_data.do_text

    recommended_duration = float(step_data.recommended_duration)

    start_time = time.time()

    while True:
        if idx - 1 < len(GLOBAL_STEP_STATUS):
            if GLOBAL_STEP_STATUS[idx - 1] == 1:
                return
        # 1. 核心判定 (串行阻塞)
        result = evaluate_condition_expression(raw_text, expression_data, idx)
        elapsed = time.time() - start_time

        # 2. 检查结果
        if result == 1 or elapsed >= recommended_duration:
            # ✅ 修改全局状态
            if idx - 1 < len(GLOBAL_STEP_STATUS):
                GLOBAL_STEP_STATUS[idx - 1] = 1
            break  # 退出线程

        time.sleep(0.1)


def function_2(idx: int, step_data: Step) -> None:
    """
    【超时看门狗线程】
    职责：单纯计时，超过 recommended_duration 则强行置位。
    """
    global GLOBAL_STEP_STATUS

    # 1. 解析推荐时长

    limit_time = float(step_data.recommended_duration)


    start_time = time.time()
    list_idx = idx - 1

    while True:
        # A. 检查是否已经被 function_1 完成了
        # 如果逻辑判定先成功了，这个看门狗就可以提前下班了，避免误报超时
        if list_idx < len(GLOBAL_STEP_STATUS):
            if GLOBAL_STEP_STATUS[list_idx] == 1:
                return

                # B. 检查是否超时
        elapsed = time.time() - start_time
        if elapsed >= limit_time:

            # 强制置位
            if list_idx < len(GLOBAL_STEP_STATUS):
                GLOBAL_STEP_STATUS[list_idx] = 1
            return  # 任务结束

        # C. 减少 CPU 占用，0.1秒检查一次
        time.sleep(0.05)

def function_3(idx: int, alert) -> None:
    """
    【瞬时步任务 - 线程函数】
    循环监听按键，满足修改全局状态，然后退出线程。
    (注意：这里不需要再嵌套 _monitor_input_thread 了，function_3 本身就是线程任务)

    约定：
    - Enter => continue_event
    - Esc/q => quit_event
    """
    global GLOBAL_STEP_STATUS
    list_idx = idx - 1

    while True:
        if list_idx < len(GLOBAL_STEP_STATUS):
            if GLOBAL_STEP_STATUS[list_idx] == 1:
                break

        # ✅ 最高优先级：Esc/q 退出
        if getattr(alert, "quit_event", None) is not None and alert.quit_event.is_set():
            # 不在这里 clear，让主控去 clear 并做收尾
            print(f"[Thread-F3] 检测到 Quit(Esc/q)，置位并退出。")
            if list_idx < len(GLOBAL_STEP_STATUS):
                GLOBAL_STEP_STATUS[list_idx] = 1
            break

        # 检查 Enter
        if alert.continue_event.is_set():
            alert.continue_event.clear()
            print(f"[Thread-F3] 检测到 Enter，置位并退出。")
            if list_idx < len(GLOBAL_STEP_STATUS):
                GLOBAL_STEP_STATUS[list_idx] = 1
            break  # 退出线程

        time.sleep(0.05)


def function_4(idx: int, step_data, alert) -> None:
    """
    【受热不均匀检测报警线程】
    - 不再覆盖主弹窗 do_text
    - need_stir=True 时：弹出一个“小报警窗”显示 msg
    - need_stir=False 且满足最短展示时间后：自动关闭小报警窗

    新增约束：
    1) 一旦当前步骤结束（执行器进入下一步），必须立刻解除报警并退出本线程。
    2) 解除报警（关闭小窗）后的 3 秒内不得再次报警（防止频繁弹窗）。

    """
    global GLOBAL_STEP_STATUS

    MIN_UI_DISPLAY_TIME = 3.0
    COOLDOWN_AFTER_CLOSE_SEC = 3.0

    last_stir_state = False
    warning_start_time = 0.0
    raw_do_text = step_data.do_text

    # 读取全局冷却时间戳（跨步骤继承）
    def _get_global_close_ts() -> float:
        try:
            lock = getattr(shared_data, "SMALL_WARNING_LOCK", None)
            ts_name = "SMALL_WARNING_LAST_CLOSE_TS"
            if lock is None:
                return float(getattr(shared_data, ts_name, 0.0))
            with lock:
                return float(getattr(shared_data, ts_name, 0.0))
        except Exception:
            return 0.0

    def _set_global_close_ts(ts: float) -> None:
        try:
            lock = getattr(shared_data, "SMALL_WARNING_LOCK", None)
            ts_name = "SMALL_WARNING_LAST_CLOSE_TS"
            if lock is None:
                setattr(shared_data, ts_name, float(ts))
                return
            with lock:
                setattr(shared_data, ts_name, float(ts))
        except Exception:
            return

    print(f"[Thread-F4] 受热均匀检测已启动 (Step {idx})")

    while True:
        # 0) root 存活检测
        try:
            if getattr(alert, "root", None) is None:
                break
            if not alert.root.winfo_exists():
                break
        except Exception:
            break

        # 1) 只要执行器已经进入下一步（CURRENT_EXEC_INDEX 改变），立刻解除并退出
        try:
            if getattr(shared_data, "CURRENT_EXEC_INDEX", None) != idx:
                try:
                    if last_stir_state:
                        alert.close_small_warning()
                        _set_global_close_ts(time.time())
                finally:
                    return
        except Exception:
            pass

        # 2) 步骤结束：立刻解除报警并退出（双保险）
        if idx - 1 < len(GLOBAL_STEP_STATUS) and GLOBAL_STEP_STATUS[idx - 1] == 1:
            try:
                if last_stir_state:
                    alert.close_small_warning()
                    _set_global_close_ts(time.time())
            except Exception:
                pass
            return

        # 3) 读取温度报警建议
        try:
            need_stir, msg = temp.get_stir_suggestion()
        except Exception as e:
            print(f"[F4 Error] 温度服务异常: {e}")
            need_stir, msg = False, ""

        current_time = time.time()

        # 冷却期：使用“全局关闭时间戳”，跨步骤继承 3 秒冷却
        last_close_ts = _get_global_close_ts()
        in_cooldown = (current_time - last_close_ts) < COOLDOWN_AFTER_CLOSE_SEC

        if need_stir:
            if not last_stir_state:
                if not in_cooldown:
                    # 第一次进入报警态：弹小窗 + 语音提示一次
                    try:
                        alert.show_small_warning(msg or "请均匀翻炒")
                    except Exception:
                        pass

                    try:
                        alert.speak(msg or "请均匀翻炒")
                    except Exception:
                        pass

                    last_stir_state = True
                    warning_start_time = current_time
            else:
                # 报警持续：只更新小窗文字（不重复播报）
                try:
                    alert.show_small_warning(msg or "请均匀翻炒")
                except Exception:
                    pass

        else:
            # need_stir=False ：如果之前在报警态 -> 满足最短展示时间后关闭
            if last_stir_state:
                if current_time - warning_start_time >= MIN_UI_DISPLAY_TIME:
                    try:
                        alert.close_small_warning()
                    except Exception:
                        pass
                    last_stir_state = False
                    _set_global_close_ts(current_time)

        time.sleep(0.1)

    # 退出前收尾：关闭小窗
    try:
        if last_stir_state:
            alert.close_small_warning()
            _set_global_close_ts(time.time())
    except Exception:
        pass

# =========================================================================
# 执行函数 (主线程运行)
# =========================================================================

def run_continuous_step(idx: int, step_data, alert) -> None:
    """
    【持续步 - 主控】
    1. 显示 UI
    2. 启动 function_1 线程
    3. 循环 check_one
    """
    raw_text = step_data.do_text
    alert.show(raw_text, status_str="")
    print("\n" + "=" * 90)
    print(f"【第{idx}步】(持续) >>> {raw_text}")
    print("=" * 90 + "\n")

    # 1. 启动子线程执行 (F1)
    t1 = threading.Thread(
        target=function_1,
        args=(idx, step_data),  # function_1 只需要 idx 和 step_data
        daemon=True
    )
    t1.start()

    # 2. 启动超时看门狗线程 (F2)
    t2 = threading.Thread(
        target=function_2,
        args=(idx, step_data),  # function_2 也只需要 idx 和 step_data
        daemon=True
    )
    t2.start()

    # 3. 启动受热不均匀报警线程 (F4)
    if "炒" in raw_text:
        t3 = threading.Thread(
            target=function_4,
            args=(idx, step_data, alert),
            daemon=True
        )
        t3.start()

    # 4. 主线程死循环检查 GLOBAL_STEP_STATUS
    while True:
        # ✅ 全局退出（最强兜底）：任何地方置位 COOKING_EXIT_REQUESTED 都立即停止
        if shared_data.COOKING_EXIT_REQUESTED.is_set():
            print("[EXEC] Exit requested (COOKING_EXIT_REQUESTED). Stop cooking.")
            try:
                alert.close_popup()
                alert.close_small_warning()
            except:
                pass
            return

        # ✅ 全局退出：Esc/q 会置位 quit_event
        if getattr(alert, "quit_event", None) is not None and alert.quit_event.is_set():
            shared_data.COOKING_EXIT_REQUESTED.set()
            # 不在这里 clear，让主控去 clear 并做收尾
            print(f"[EXEC] Quit requested. Stop cooking.")
            return

        if check_one(idx):
            print(f"[Main] 第{idx}步 确认完成，进入下一步。")
            return  # 跳出，执行器会去执行 idx+1
        if Daemon_thread.abnormal_event.is_set():
            Daemon_thread.abnormal_event.clear()
            if idx - 1 < len(GLOBAL_STEP_STATUS):
                GLOBAL_STEP_STATUS[idx - 1] = 1
            return
        time.sleep(0.1)


def run_instant_step(idx: int, step_data, alert) -> None:
    """
    【瞬时步 - 主控】
    1. 显示 UI
    2. 启动 function_3 线程
    3. 循环 check_one
    """
    raw_text = step_data.do_text
    print("\n" + "=" * 90)
    print(f"【第{idx}步】(瞬时) >>> {raw_text}")
    print("=" * 90 + "\n")

    alert.continue_event.clear()
    alert.quit_event.clear()

    # 保持你现有的应急识别逻辑（按你的业务需求可自行调整）
    if "发生糊锅" in raw_text:
        is_emergency = True
    else:
        is_emergency = False

    status = "应急步骤：按 Esc 结束做菜" if is_emergency else "按 Enter 进入下一步"
    alert.show(raw_text, status_str=status)

    # 1. 启动子线程执行 function_3
    t = threading.Thread(
        target=function_3,
        args=(idx, alert),
        daemon=True
    )
    t.start()

    # 2. 主线程死循环检查 GLOBAL_STEP_STATUS
    while True:
        # ✅ 全局退出（最强兜底）：UI/Popup 任意线程发起退出
        if shared_data.COOKING_EXIT_REQUESTED.is_set():
            print("[EXEC] Exit requested (COOKING_EXIT_REQUESTED). Stop cooking.")
            try:
                alert.close_popup()
                alert.close_small_warning()
            except:
                pass
            return

        # ✅ 全局退出：Esc/q
        if getattr(alert, "quit_event", None) is not None and alert.quit_event.is_set():
            shared_data.COOKING_EXIT_REQUESTED.set()
            # 不在这里 clear，让主控去 clear 并做收尾
            print(f"[EXEC] Quit requested. Stop cooking.")
            return

        if check_one(idx):
            return                         # 跳出，执行器会去执行 idx+1
        if Daemon_thread.abnormal_event.is_set():
            Daemon_thread.abnormal_event.clear()
            if idx - 1 < len(GLOBAL_STEP_STATUS):
                GLOBAL_STEP_STATUS[idx - 1] = 1
            return
        time.sleep(0.1)


# -------------------- 执行器主循环（线程入口） --------------------

def executor_main(alert) -> None:
    """
    线程入口：
    - idx 逐步执行
    - idx 逐步执行
    """
    global GLOBAL_STEP_STATUS

    with STEP_LOCK:
        GLOBAL_STEP_STATUS = [0] * len(GLOBAL_RECIPE_STEPS)

    idx = 1

    while True:
        # ✅ 全局退出（最强兜底）：UI/Popup 任意线程发起退出
        if shared_data.COOKING_EXIT_REQUESTED.is_set():
            print("[EXEC] Exit requested (COOKING_EXIT_REQUESTED). Executor exit.")
            try:
               alert.close_popup()
               alert.close_small_warning()
               Daemon_thread.stop_all_event.set()
            except:
                pass
            return

        # ✅ 全局退出：如果已经收到 quit_event，就直接退出 executor_main
        if getattr(alert, "quit_event", None) is not None and alert.quit_event.is_set():
            shared_data.COOKING_EXIT_REQUESTED.set()
            # 不在这里 clear，让主控去 clear 并做收尾
            print(f"[EXEC] Quit requested. Stop cooking.")
            return

        shared_data.CURRENT_EXEC_INDEX = idx
        with STEP_LOCK:

            current_recipe_len = len(GLOBAL_RECIPE_STEPS)

            if idx > current_recipe_len:
                # alert.show("全部步骤已完成")
                print("[EXEC] All steps done.")
                return

            step_data = GLOBAL_RECIPE_STEPS[idx - 1]

        now_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
        print(f"[{now_str}] ENTER STEP {idx}/{current_recipe_len}")

        global_status_len = len(GLOBAL_STEP_STATUS)
        diff = current_recipe_len - global_status_len

        if diff > 0:
            insert_pos = idx - 1
            for _ in range(diff):
                GLOBAL_STEP_STATUS.insert(insert_pos, 0)

        if step_data.is_continuous:
            run_continuous_step(idx, step_data, alert)
        else:
            run_instant_step(idx, step_data, alert)

        # ✅ 关键点：每一步结束后，再次检查是否请求了退出。如果是，则立即 return，不再执行 idx += 1
        if shared_data.COOKING_EXIT_REQUESTED.is_set():
            print("[EXEC] Exit requested before incrementing step index. Executor exit.")
            return

        idx += 1
