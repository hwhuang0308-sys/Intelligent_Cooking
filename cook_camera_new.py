import time
import threading
from collections import deque
from datetime import datetime
from pathlib import Path
import cv2
import os

# -------- 全局共享状态（模块级单例）--------
_lock = threading.Lock()
_buffer = deque(maxlen=30)

_thread = None
_stop_event = threading.Event()
_cap = None

_rtsp_url = None
_threshold = 120
_backend = cv2.CAP_FFMPEG

# 保存图像目录
base_dir = r"D:\python\cook\src"
today_tag = f"test_{datetime.now().month}_{datetime.now().day}"
day_root = os.path.join(base_dir, today_tag)
save_path_pics = os.path.join(day_root, "pics", "camera")

# 确保保存图像目录存在
Path(save_path_pics).mkdir(parents=True, exist_ok=True)

def _focus_measure(gray):
    """拉普拉斯方差衡量清晰度，值越大越清晰"""
    return cv2.Laplacian(gray, cv2.CV_64F).var()

def save_frame(frame, timestamp):
    """
    保存图像到指定目录，并使用时间戳命名文件。
    """
    # 文件名：时间戳
    try:
        ts = timestamp
        lt = time.localtime(ts)
        ms = int((ts - int(ts)) * 1000)
        now_file = time.strftime("%Y%m%d_%H%M%S", lt) + f"_{ms:03d}"
        img_path = os.path.join(save_path_pics, f"camera_{now_file}.jpg")
        cv2.imwrite(img_path, frame)
    except Exception as e:
        print(f"Error saving frame: {e}")

def start_camera(rtsp_url, threshold=180.0, maxlen=30):
    """
    启动后台采集线程（只会启动一次）。
    rtsp_url: RTSP 地址
    threshold: 清晰度阈值
    maxlen: 队列最大长度（只保留最新 maxlen 张）
    """
    global _thread, _rtsp_url, _threshold, _buffer

    with _lock:
        _rtsp_url = rtsp_url
        _threshold = float(threshold)
        if _buffer.maxlen != maxlen:
            _buffer = deque(_buffer, maxlen=maxlen)

    # 已经在运行就不重复启动
    if _thread is not None and _thread.is_alive():
        return

    _stop_event.clear()
    _thread = threading.Thread(target=_capture_loop, daemon=True)
    _thread.start()


def close_camera():
    """停止采集线程并释放资源"""
    global _thread
    _stop_event.set()
    if _thread is not None:
        _thread.join(timeout=2.0)
    _release_cap()


def get_picture(copy=True):
    """
    获取最新一张缓存图像（BGR numpy 数组）。
    队列为空返回 None。
    copy=True 返回副本（更安全）；copy=False 更快但别修改返回的图像。
    """
    with _lock:
        if not _buffer:
            return None
        ts, frame = _buffer[-1]
        return frame.copy() if copy else frame

def get_picture_seconds_ago_strict(seconds: float, copy=True):
    """
    严格：返回 <= (now - seconds) 的最近一帧。
    如果 buffer 覆盖不到 seconds 秒前，返回 None。
    """
    target_ts = time.time() - float(seconds)
    return get_picture_at_or_before(target_ts, copy=copy)

def get_picture_at_or_before(target_ts: float, copy=True):
    """
    严格：返回 buffer 中 timestamp <= target_ts 的最近一帧（向下取整/floor）。
    如果不存在（全部都比 target_ts 新），返回 None。
    """
    with _lock:
        if not _buffer:
            return None

        # _buffer 里必须是 (ts, frame)
        item0 = _buffer[0]
        if not (isinstance(item0, tuple) and len(item0) == 2):
            raise RuntimeError("_buffer 不是 (ts, frame) 结构，请先改入队格式。")

        best_frame = None
        best_ts = None

        # buffer 按时间递增入队，遍历找到最后一个 ts <= target_ts
        for ts, frame in _buffer:
            if ts <= target_ts:
                best_ts = ts
                best_frame = frame
            else:
                # 后面的 ts 更大了，直接停止
                break

        if best_frame is None:
            return None

        return best_frame.copy() if copy else best_frame

# -------- 内部函数 --------
def _open_cap():
    global _cap

    _release_cap()
    cap = cv2.VideoCapture(_rtsp_url, _backend)
    if not cap.isOpened():
        return False

    # 尝试减小缓冲（不保证生效）
    try:
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
    except Exception:
        pass

    _cap = cap
    return True


def _release_cap():
    global _cap
    if _cap is not None:
        try:
            _cap.release()
        finally:
            _cap = None


# def _capture_loop():
#     """
#     后台线程：持续读 RTSP，清晰度达标就放入 deque（只保留最新 30 张）
#     断流会自动重连
#     """
#     backoff = 0.5
#
#     while not _stop_event.is_set():
#         if _cap is None:
#             if _rtsp_url is None:
#                 time.sleep(0.2)
#                 continue
#             if not _open_cap():
#                 time.sleep(backoff)
#                 backoff = min(backoff * 2, 5.0)
#                 continue
#             backoff = 0.5
#
#         ok, frame = _cap.read()
#         if not ok or frame is None:
#             _release_cap()
#             time.sleep(backoff)
#             backoff = min(backoff * 2, 5.0)
#             continue
#
#         gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
#         score = _focus_measure(gray)
#
#         if score >= _threshold:
#             # copy 一份存入队列，避免后续被覆盖
#             with _lock:
#                 _buffer.append(frame.copy())
#                 # print("PUSH", len(_buffer))
#
#     _release_cap()

def _capture_loop():
    """
    后台线程：持续读 RTSP，把“尽量最新”的帧放入 deque（只保留最新 maxlen 张）
    - 通过多次 grab() 丢弃积压帧，只 retrieve() 最新一帧（追帧）
    - 连续一段时间拿不到新帧则强制重连（防止拉流僵住）
    - 断流自动重连
    """
    global _cap

    backoff = 0.5

    # 每轮最多跳过多少帧（3~15 可调，动作大/延迟大就调大）
    grab_skip = 8

    # 超过该秒数没有“成功读到帧”就重连
    stall_timeout = 2.0
    last_ok_time = time.time()
    last_save_time = time.time()

    while not _stop_event.is_set():
        # 如果长时间没有成功读到帧，强制重连（重要：要放在循环开头才会生效）
        if _cap is not None and (time.time() - last_ok_time) > stall_timeout:
            _release_cap()
            time.sleep(backoff)
            backoff = min(backoff * 2, 5.0)
            continue

        # 确保 cap 已打开
        if _cap is None:
            if _rtsp_url is None:
                time.sleep(0.2)
                continue
            if not _open_cap():
                time.sleep(backoff)
                backoff = min(backoff * 2, 5.0)
                continue
            backoff = 0.5
            last_ok_time = time.time()

        # 追帧：尽量丢掉积压帧
        ok = True
        for _ in range(grab_skip):
            if _stop_event.is_set():
                break
            if not _cap.grab():
                ok = False
                break

        if not ok or _stop_event.is_set():
            _release_cap()
            time.sleep(backoff)
            backoff = min(backoff * 2, 5.0)
            continue

        ok, frame = _cap.retrieve()
        if not ok or frame is None:
            _release_cap()
            time.sleep(backoff)
            backoff = min(backoff * 2, 5.0)
            continue

        last_ok_time = time.time()

        # 始终缓存最新帧（保留最近 maxlen 张）
        with _lock:
            _buffer.append((last_ok_time, frame.copy()))
        #保存图像
        if last_ok_time - last_save_time >= 0.5:
            save_frame(frame, last_ok_time)
            last_save_time = last_ok_time

    _release_cap()
