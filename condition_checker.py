from pathlib import Path
import cv2
import os
from datetime import datetime
import numpy as np
from utils import _now_tags
import get_api
import time
import cook_camera_new as cam
import cook_air as air
import cook_audio_new as audio
import cook_temperature_new as temp
import matplotlib
matplotlib.use("Agg")  # ✅ 后台保存图片，不弹窗（服务器/无GUI环境也能用）
import matplotlib.pyplot as plt

url = "rtsp://admin:admin@192.168.10.204:8554/live"
last_visual_step = None
last_visual_judge = 0
end_visual_judge = 0
visual_judge_time = None

last_temperature_step = None
last_temperature_judge = 0
end_temperature_judge = 0
temperature_judge_time = None

temperature_judge_accum = 0.0
temperature_judge_last_time = None

last_audio_step = None
end_audio_judge = 0

base_dir = r"D:\python\cook\src"
today_tag = f"test_{datetime.now().month}_{datetime.now().day}"   # 如 test_2_14
day_root = os.path.join(base_dir, today_tag)

save_vision_txt_path = os.path.join(day_root, "record", "vision_record.txt")
save_temp_txt_path   = os.path.join(day_root, "record", "temp_record.txt")
save_audio_txt_path  = os.path.join(day_root, "record", "audio_record.txt")
save_path_pics       = os.path.join(day_root, "pics", "check")
save_path_temp       = os.path.join(day_root, "temp")

# ✅ 建议顺手保证目录存在（避免首次运行报错）
os.makedirs(os.path.dirname(save_vision_txt_path), exist_ok=True)  # record
os.makedirs(save_path_pics, exist_ok=True)                         # pics/check
os.makedirs(save_path_temp, exist_ok=True)                         # temp


def check_visual_condition(duration: float, condition: str, now_step: str, do_text: str):
    print(f"调用视觉函数，当前步骤是：{now_step},时长:{duration},视觉条件：{condition}")
    now_ts, now_log, now_file = _now_tags()
    global last_visual_step, last_visual_judge, end_visual_judge, visual_judge_time
    if now_step != last_visual_step:
        last_visual_step = now_step
        last_visual_judge = 0
        end_visual_judge = 0
        visual_judge_time = None
    elif now_step == last_visual_step and end_visual_judge == 1:
        return 1
    frame = _wait_camera_frame()
    if frame is None:
        return 0
    pics_dir = Path(save_path_pics)  # 例如 D:\python\cook\src\test\pics\normal
    pics_dir.mkdir(parents=True, exist_ok=True)
    image_path = pics_dir / f"{now_step}_vision_{now_file}.jpg"

    cv2.imwrite(str(image_path), frame)
    prompt_1 = f"我现在正在进行炒菜，且正在执行的炒菜步骤为：{do_text}。请根据以下图像判断其是否满足如下条件：图像中存在炒菜步骤中所有涉及到的食材。若满足条件，请仅返回数字1；若不满足条件，请仅返回数字0。不要返回任何解释或其他内容，只输出1或0。"
    prompt_2 = f"我现在正在进行炒菜，且正在执行的炒菜步骤为：{do_text}。请根据以下图像判断其是否满足如下条件：<{condition}>。若满足条件，请仅返回数字1；若不满足条件，请仅返回数字0。不要返回任何解释或其他内容，只输出1或0。"

    while 1:
        visual_judge_1 = str(get_api.get_vision_answer_from_np(frame, prompt_1, model="gpt-5.2"))
        visual_judge_2 = str(get_api.get_vision_answer_from_np(frame, prompt_2, model="gpt-5.2"))

        visual_judge_1 = int(visual_judge_1)
        visual_judge_2 = int(visual_judge_2)

        visual_judge = visual_judge_1 and visual_judge_2
        if visual_judge == 0 or visual_judge ==1:
            break
    save_str = f'''
【---视觉判断---】
【步骤】：{now_step}
【数据】：{image_path}
【时间】：{now_log}
【提问】：{prompt_1}\n{prompt_2}
【回答】：{visual_judge}(p1:{visual_judge_1},p2:{visual_judge_2})
\n
'''
    now_time = time.time()
    with open(save_vision_txt_path, 'a', encoding='utf-8') as f:
        f.write(save_str)
    if visual_judge == 0:
        last_visual_judge = 0
        return 0
    elif visual_judge == 1 and last_visual_judge == 0:
        last_visual_judge = 1
        visual_judge_time = time.time()
        return 0
    elif visual_judge == 1 and last_visual_judge == 1 and (now_time - visual_judge_time < duration):
        return 0
    elif visual_judge == 1 and last_visual_judge == 1 and (now_time - visual_judge_time >= duration):
        end_visual_judge = 1
        save_str = f'''
【---视觉判断---】
【步骤】：{now_step}
【时间】：{now_log}
【备注】：已完成该步骤判定,直接返回1
\n
        '''
        with open(save_vision_txt_path, 'a', encoding='utf-8') as f:
            f.write(save_str)
        return 1


def check_audio_condition(duration: float, condition: float, now_step: str):
    print(f"调用声音函数，当前步骤是：{now_step},时长:{duration},阈值：{condition}")
    _, now_log, _ = _now_tags()
    global last_audio_step, end_audio_judge
    if now_step != last_audio_step:
        last_audio_step = now_step
        end_audio_judge = 0
    elif now_step == last_audio_step and end_audio_judge == 1:
        return 1

    side_window = 3
    now_audio_list = audio.get_audio(-side_window, 0) or []
    past_audio_list = audio.get_audio(-duration - side_window, -side_window) or []

    if len(now_audio_list) == 0 or len(past_audio_list) == 0:
        return 0

    now_audio = sum(now_audio_list) / len(now_audio_list)
    past_audio = sum(past_audio_list) / len(past_audio_list)
    save_str = f'''
【---声音强度判断---】
【步骤】：{now_step}
【时间】：{now_log}
【阈值】：{condition}
【时间间隔】：{duration}
【过去数值】：{past_audio}
【当前数值】：{now_audio}
\n
'''
    with open(save_audio_txt_path, 'a', encoding='utf-8') as f:
        f.write(save_str)
    if now_audio - past_audio > condition:
        end_audio_judge = 1
        save_str = f'''
【---声音强度判断---】
【步骤】：{now_step}
【时间】：{now_log}
【阈值】：{condition}
【时间间隔】：{duration}
【备注】：已判断完成
\n
        '''
        with open(save_audio_txt_path, 'a', encoding='utf-8') as f:
            f.write(save_str)
        return 1
    else:
        return 0


def check_temperature_condition(duration: float, condition: float, now_step: str):
    print(f"调用温度函数，当前步骤是：{now_step},时长:{duration},阈值：{condition}")
    now_ts, now_log, now_file = _now_tags()
    global last_temperature_step, last_temperature_judge, end_temperature_judge, temperature_judge_time
    if now_step != last_temperature_step:
        last_temperature_step = now_step
        last_temperature_judge = 0
        end_temperature_judge = 0
        temperature_judge_time = None
    elif now_step == last_temperature_step and end_temperature_judge == 1:
        return 1

    ratio = 0.2  # 提取温度最高的前20%点
    atemp = 0
    while 1:
        avg_temperature = temp.get_temperature(ratio)
        mat = temp.get_latest_matrix()
        if avg_temperature == None:
            atemp += 1
            print('未获得温度数据！')
        else:
            break
        if atemp >= 3:
            break

    # base_temp_path = Path(save_path_temp)
    # if not base_temp_path.exists():
    #     base_temp_path.mkdir(parents=True, exist_ok=True)
    #
    # if mat is not None:
    #     mat_np = np.array(mat, dtype=np.float32)
    #     csv_path = base_temp_path / f"temperature_{now_file}.csv"
    #     np.savetxt(csv_path, mat_np, delimiter=",", fmt="%.1f")
    #
    #     img_path = base_temp_path / f"heatmap_{now_file}.png"
    #     create_picture_heatmap(mat_np, str(img_path))

    if avg_temperature is not None:
        save_str = f'''
【---温度判断---】
【步骤】：{now_step}
【时间】：{now_log}
【阈值】：{condition}
【时间间隔】：{duration}
【当前数值】：{avg_temperature}
\n
        '''
        with open(save_temp_txt_path, 'a', encoding='utf-8') as f:
            f.write(save_str)
    else:
        return 0
    if avg_temperature >= condition:
        tempreature_judge = 1
    else:
        tempreature_judge = 0

    now_time = time.time()
    if tempreature_judge == 0:
        last_temperature_judge = 0
        return 0
    elif tempreature_judge == 1 and last_temperature_judge == 0:
        last_temperature_judge = 1
        temperature_judge_time = now_time
        return 0
    elif tempreature_judge == 1 and last_temperature_judge == 1 and (now_time - temperature_judge_time < duration):
        return 0
    elif tempreature_judge == 1 and last_temperature_judge == 1 and (now_time - temperature_judge_time >= duration):
        end_temperature_judge = 1
        save_str = f'''
【---温度判断---】
【步骤】：{now_step}
【时间】：{now_log}
【阈值】：{condition}
【时间间隔】：{duration}
【备注】：已判断完成
\n
        '''
        with open(save_temp_txt_path, 'a', encoding='utf-8') as f:
            f.write(save_str)
        return 1

def check_temperature_condition_2(
        duration: float,
        condition: float,
        now_step: str,
        *,
        # 你可以按实际标定调整：椭圆中心/半轴
        ellipse_center_y: float = None,
        ellipse_center_x: float = None,
        ellipse_radius_y: float = None,
        ellipse_radius_x: float = None,
        # 冷点比例（后20% = 最低20%）
        cold_ratio: float = 0.2,
        # 冷点阈值：默认用 condition；如需不同阈值可传入
        cold_threshold_ratio: float = 0.6,
):
    """
    【温度判定 v2】（基于固定机位：中心椭圆=食材区）
    判定 tempreature_judge=1 的条件：
      1) 食材表面温度均值（中心椭圆均值） >= condition
      AND
      2) 全矩阵最低20%均值（冷点20%） >= cold_threshold（默认=60,若传入参数则以参数为阈值）

    其余“持续 duration 秒才算完成”的逻辑保持不变。
    """
    print(f"调用温度函数2，当前步骤是：{now_step},时长:{duration},阈值：{condition}")
    now_ts, now_log, now_file = _now_tags()
    global last_temperature_step, last_temperature_judge, end_temperature_judge, temperature_judge_time, temperature_judge_accum, temperature_judge_last_time

    if now_step != last_temperature_step:
        last_temperature_step = now_step
        last_temperature_judge = 0
        end_temperature_judge = 0
        temperature_judge_time = None

        temperature_judge_accum = 0.0
        temperature_judge_last_time = None
    elif now_step == last_temperature_step and end_temperature_judge == 1:
        return 1

    atemp = 0
    mat = None
    food_mean = None
    cold_mean = None

    while 1:
        mat = temp.get_latest_matrix()
        if mat is None:
            atemp += 1
            print("未获得温度矩阵！")
        else:
            mat_np = np.array(mat, dtype=np.float32)

            # 1) 食材表面温度：中心椭圆均值
            food_mean = _ellipse_mean(
                mat_np,
                center_y=ellipse_center_y,
                center_x=ellipse_center_x,
                radius_y=ellipse_radius_y,
                radius_x=ellipse_radius_x
            )

            # 2) 冷点20%：全矩阵最低20%均值
            cold_mean = _cold20_mean(mat_np, ratio=cold_ratio)

            break

        if atemp >= 3:
            break
        time.sleep(0.05)

    # 保存 csv + heatmap（保持你原逻辑）
    # base_temp_path = Path(save_path_temp)
    # if not base_temp_path.exists():
    #     base_temp_path.mkdir(parents=True, exist_ok=True)
    #
    # if mat is not None:
    #     mat_np2 = np.array(mat, dtype=np.float32)
    #     csv_path = base_temp_path / f"temperature_{now_file}.csv"
    #     np.savetxt(csv_path, mat_np2, delimiter=",", fmt="%.1f")
    #
    #     img_path = base_temp_path / f"heatmap_{now_file}.png"
    #     create_picture_heatmap(mat_np2, str(img_path))
    cold_threshold = cold_threshold_ratio*condition
    if food_mean is not None and cold_mean is not None:
        save_str = f'''
【---温度判断2---】
【步骤】：{now_step}
【时间】：{now_log}
【阈值(食材均值)】：{condition}
【阈值(冷点{int(cold_ratio*100)}%)】：{cold_threshold}
【时间间隔】：{duration}
【食材椭圆均值】：{food_mean}
【冷点{int(cold_ratio*100)}%均值】：{cold_mean}
\n
        '''
        with open(save_temp_txt_path, 'a', encoding='utf-8') as f:
            f.write(save_str)
    else:
        return 0

    # ✅ 修改点：双阈值 AND 才算温度满足
    if (food_mean >= condition) and (cold_mean >= cold_threshold):
        tempreature_judge = 1
    else:
        tempreature_judge = 0

    now_time = time.time()
    if temperature_judge_last_time is None:
        dt = 0.0
    else:
        dt = now_time - temperature_judge_last_time
    temperature_judge_last_time = now_time

    if tempreature_judge == 1:
        temperature_judge_accum += dt

    if temperature_judge_accum >= duration:
        end_temperature_judge = 1
        save_str = f'''
【---温度判断2---】
【步骤】：{now_step}
【时间】：{now_log}
【阈值(食材均值)】：{condition}
【阈值(冷点{int(cold_ratio * 100)}%)】：{cold_threshold}
【时间间隔】：{duration}
【备注】：已判断完成
\n
        '''
        with open(save_temp_txt_path, 'a', encoding='utf-8') as f:
            f.write(save_str)
        return 1
    return 0

def create_picture_heatmap(temperature_matrix, save_path):
    plt.figure()
    plt.contourf(temperature_matrix, cmap='viridis')
    plt.colorbar()
    plt.savefig(save_path)
    plt.close()

def _wait_camera_frame(timeout_sec: float = 5.0, poll_sec: float = 0.05):
    t0 = time.time()
    while time.time() - t0 < timeout_sec:
        frame = cam.get_picture(copy=True)
        if frame is not None and frame.size > 0:
            return frame
        time.sleep(poll_sec)
    return None

def _ellipse_mask(h: int, w: int,
                  center_y: float = None, center_x: float = None,
                  radius_y: float = None, radius_x: float = None) -> np.ndarray:
    """
    生成中心椭圆区域 mask（True 表示椭圆内）
    默认：中心在矩阵中心；半轴为高/宽的 0.35（你可按机位/锅大小调整）
    """
    if center_y is None:
        center_y = (h - 1) / 2.0
    if center_x is None:
        center_x = (w - 1) / 2.0

    # 经验默认：中心食材区占中间偏大一点的椭圆
    if radius_y is None:
        radius_y = h * 0.35
    if radius_x is None:
        radius_x = w * 0.35

    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    norm = ((yy - center_y) / radius_y) ** 2 + ((xx - center_x) / radius_x) ** 2
    return norm <= 1.0


def _cold20_mean(mat_np: np.ndarray, ratio: float = 0.2) -> float:
    """
    计算全矩阵“最低 ratio”温度点的均值（冷点均值）。
    """
    flat = mat_np.reshape(-1)
    k = max(int(len(flat) * ratio), 1)
    # 取最小 k 个点
    lowk = np.partition(flat, k - 1)[:k]
    return float(np.mean(lowk))


def _ellipse_mean(mat_np: np.ndarray,
                  center_y: float = None, center_x: float = None,
                  radius_y: float = None, radius_x: float = None) -> float:
    """
    计算中心椭圆区域内温度点均值（食材表面温度均值）
    """
    h, w = mat_np.shape
    mask = _ellipse_mask(h, w, center_y=center_y, center_x=center_x,
                         radius_y=radius_y, radius_x=radius_x)
    vals = mat_np[mask]
    # 极端情况下 mask 为空，兜底返回全局均值
    if vals.size == 0:
        return float(np.mean(mat_np))
    return float(np.mean(vals))

