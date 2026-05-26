# temp_sensor_service.py
# 后台线程持续采集温度矩阵(24x32)，保存最近60秒到内存双端队列；
# 并实时分析是否需要翻炒（防糊锅算法）。
import os
import time
from datetime import datetime
from pathlib import Path
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import threading
from collections import deque
from contextlib import suppress
from typing import Optional, Tuple, Dict, Any
import cook_camera_new as cam
import serial
import struct
import numpy as np
import cv2

__all__ = [
    "start_temp_sensor",
    "get_temperature",
    "get_latest_heatmap",
    "get_stir_suggestion",
    "close_temperature_sensor"
]

# ====== 传感器协议常量 ======
FRAME_LEN = 7 + 768 * 2 + 16 * 2  # 1583 bytes
CMD = bytes([0x01, 0x65, 0x00, 0x00, 0x03, 0x11])


def crc16(data: bytes) -> int:
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            lsb = crc & 1
            crc >>= 1
            if lsb:
                crc ^= 0xA001
    return crc


# ====== [新增] 核心算法类：防糊锅监测器 ======
class Temperature_Monitor:
    def __init__(self, sample_period=0.5):
        # 参数配置
        # self.alpha = 0.3
        # self.history_len = 6  # 轨迹分析帧数
        # self.lag_frames = 6  # 形态对比间隔 (3秒 / 0.5s采样率 = 6帧)
        # self.noise_floor = 0.15 #质心位移阈值
        # self.corr_threshold = 0.5 #相关性阈值
        # self.temp_alert = 160.0
        # self.patience_seconds = 1.0
        #
        # self.patience_limit = int(self.patience_seconds / sample_period)
        #
        # # 运行时状态
        # self.ema_frame = None
        # self.static_counter = 0
        #
        # # 【修改点 1】: 这里改成 deque，并且必须指定 maxlen
        # # frame_buffer 需要存当前帧 + 滞后帧，所以长度是 lag_frames + 1
        # self.frame_buffer = deque(maxlen=self.lag_frames + 1)
        #
        # # centroid_buffer 只需要存 history_len 这么多
        # self.centroid_buffer = deque(maxlen=self.history_len)

        # ===== 参数配置 =====
        self.lag_frames = 6  # 持续判断窗口长度（帧数）
        self.lag_second = 3 #取3s前图像作相关性判断
        self.vision_corr_thresh = 0.89 #图像相关性阈值
        self.region_consistency_ratio = 1  # 窗口内同一区域占比阈值
        self.Ingredient_region_radius = 0.35  #食材区域半径
        self.center_ratio = 0.5  # 中心圆半径 = min(rx,ry)*center_ratio
        self.use_topk_ratio_for_region = 0.15  # 每个分区取 top15% 均值作为分区温度

        # ===== 运行时状态 =====
        self.hot_region_buffer = deque(maxlen=self.lag_frames)
        self.centroid_buffer = deque(maxlen=self.lag_frames)  # 仅调试参考，不参与判定

    def reset(self, sample_period):
        self.__init__(sample_period)

    # def process_frame(self, raw_matrix: np.ndarray):
    #     """
    #     核心计算逻辑
    #     """
    #     # --- 轨道 1：紧急熔断 (使用原始数据) ---
    #     raw_max = np.max(raw_matrix)
    #     if raw_max > 220.0:  # 绝对熔断阈值
    #         return True, f"【危险】瞬时高温 {int(raw_max)}°C！"
    #
    #     # --- 轨道 2：平滑处理 ---
    #     if self.ema_frame is None:
    #         self.ema_frame = raw_matrix.astype(float)
    #     else:
    #         self.ema_frame = self.alpha * raw_matrix + (1 - self.alpha) * self.ema_frame
    #
    #     # 【修改点 2】: 直接 append，deque 会自动把最老的数据挤出去
    #     self.frame_buffer.append(self.ema_frame.copy())
    #
    #     # 计算热力重心
    #     cy, cx = self._calc_centroid(self.ema_frame)
    #
    #     # 【修改点 3】: 直接 append，不用手写 pop
    #     self.centroid_buffer.append((cx, cy))
    #
    #     # 【修改点 4】: 判断数据是否攒满
    #     # 只要 deque 没填满 maxlen，说明启动时间还不够，直接返回
    #     if len(self.frame_buffer) < self.frame_buffer.maxlen or \
    #             len(self.centroid_buffer) < self.centroid_buffer.maxlen:
    #         return False, "初始化数据..."
    #
    #     # --- 算法判定 ---
    #
    #     # A. 轨迹协方差分析 (判断宏观位移)
    #     # np.array 可以直接把 deque 转成矩阵
    #     traj_points = np.array(self.centroid_buffer)
    #     try:
    #         cov_matrix = np.cov(traj_points, rowvar=False)
    #         eigenvalues, _ = np.linalg.eig(cov_matrix)
    #         movement_magnitude = np.sqrt(np.max(eigenvalues))
    #     except:
    #         movement_magnitude = 0.0
    #
    #     is_static_pos = movement_magnitude < self.noise_floor
    #
    #     # B. 形态相关性分析 (判断微观纹理)
    #     curr_vec = self.ema_frame.flatten()
    #
    #     # 【修改点 5】: 获取最老的帧
    #     # deque[0] 永远是当前队列里最老的那一帧 (即 3 秒前的帧)
    #     prev_vec = self.frame_buffer[0].flatten()
    #
    #     if np.std(curr_vec) < 1e-6 or np.std(prev_vec) < 1e-6:
    #         correlation = 1.0
    #     else:
    #         correlation = np.corrcoef(curr_vec, prev_vec)[0, 1]
    #         if np.isnan(correlation): correlation = 1.0
    #
    #     is_pattern_locked = correlation > self.corr_threshold
    #
    #     # C. 综合判定 (注意：温度判断改回用 raw_max 防止 EMA 滞后)
    #     if is_static_pos and is_pattern_locked:
    #         self.static_counter += 1
    #     else:
    #         self.static_counter = 0
    #
    #     debug_msg = f"Tmax={raw_max:.1f} Mov={movement_magnitude:.2f} Corr={correlation:.3f}"
    #
    #     if self.static_counter > self.patience_limit:
    #         return True, f"【警告】高温静止! {debug_msg}"
    #
    #     return False, f"正常: {debug_msg}"

    def process_frame(
            self,
            raw_matrix: np.ndarray,
            *,
            ellipse_center_y: float = None,
            ellipse_center_x: float = None,
            ellipse_radius_y: float = None,
            ellipse_radius_x: float = None,
            roi_x1: int = None,
            roi_y1: int = None,
            roi_x2: int = None,
            roi_y2: int = None

    ):
        """
        受热不均检测：
        1) 取椭圆食材区（剔除锅边）
        2) 椭圆内按椭圆中心分五区（左上/右上/左下/右下/中心）
        3) 各区计算 top-k 均值，得到当前最热区
        4) 最近 lag_frames 内若最热区持续同一区域（占比>=阈值）
        """
        if raw_matrix is None:
            return False, "温度矩阵为空"

        mat = np.asarray(raw_matrix, dtype=np.float32)
        if mat.ndim != 2:
            return False, f"温度矩阵维度错误: shape={mat.shape}"

        # 调试参考：温度加权质心（不参与判定）
        cy_cent, cx_cent = self._calc_centroid(mat)
        self.centroid_buffer.append((cx_cent, cy_cent))

        # 椭圆食材区内五分区 top-k 均值
        hottest_region, region_scores, region_counts = self._get_hottest_region_in_ellipse(
            mat,
            ellipse_center_y=ellipse_center_y,
            ellipse_center_x=ellipse_center_x,
            ellipse_radius_y=ellipse_radius_y,
            ellipse_radius_x=ellipse_radius_x,
        )
        self.hot_region_buffer.append(hottest_region)

        # 缓冲未满（窗口还没攒够）
        if len(self.hot_region_buffer) < self.hot_region_buffer.maxlen:
            return False, (
                f"初始化数据... "
                f"HotNow={hottest_region} "
                f"RegionTopK[中心={region_scores['中心']:.1f},左上={region_scores['左上']:.1f},右上={region_scores['右上']:.1f},"
                f"左下={region_scores['左下']:.1f},右下={region_scores['右下']:.1f}] "
                f"Centroid=({cx_cent:.2f},{cy_cent:.2f})"
            )

        # 判断窗口内最热区是否持续同一区域
        is_persistent, dominant_region, dominant_ratio = self._is_region_persistent()

        debug_msg = (
            f"HotNow={hottest_region} HotWin={dominant_region}({dominant_ratio:.2f}) "
            f"RegionTopK[中心={region_scores['中心']:.1f},左上={region_scores['左上']:.1f},右上={region_scores['右上']:.1f},"
            f"左下={region_scores['左下']:.1f},右下={region_scores['右下']:.1f}] "
            f"Centroid=({cx_cent:.2f},{cy_cent:.2f}) "
        )

        if is_persistent:
            # 1) 计算 “3 秒前” 的时间跨度（与你温度采样一致：lag_frames * sample_period）
            # 你温度采样周期是 0.5s，所以 6帧=3秒；这里写死也行，但建议用全局 _sample_period
            lag_seconds = self.lag_second

            # 2) 获取当前帧和严格 lag_seconds 前的帧
            frame_now = cam.get_picture(copy=False)
            frame_prev = cam.get_picture_seconds_ago_strict(lag_seconds, copy=False)

            if frame_now is None or frame_prev is None:
                # 没拿到视觉帧就不能做 AND，建议先不报警（也可返回一个提示信息）
                return False, f"正常(视觉帧不足): {debug_msg}"

            # 3) ROI 裁剪（你自己填锅的矩形框）
            if None in (roi_x1, roi_y1, roi_x2, roi_y2):
                return False, f"正常(ROI未设置): {debug_msg}"
            x1, y1, x2, y2 = roi_x1, roi_y1, roi_x2, roi_y2
            roi_now = frame_now[y1:y2, x1:x2]
            roi_prev = frame_prev[y1:y2, x1:x2]

            if roi_now.size == 0 or roi_prev.size == 0 or roi_now.shape[:2] != roi_prev.shape[:2]:
                return False, f"正常(ROI无效): {debug_msg}"

            # 4) 灰度 + 轻微模糊（抗噪/抗蒸汽）
            g_now = cv2.cvtColor(roi_now, cv2.COLOR_BGR2GRAY)
            g_prev = cv2.cvtColor(roi_prev, cv2.COLOR_BGR2GRAY)
            g_now = cv2.GaussianBlur(g_now, (5, 5), 0)
            g_prev = cv2.GaussianBlur(g_prev, (5, 5), 0)

            # 5) OpenCV 模板匹配相似度（同尺寸 -> res 是 1x1）
            score = float(cv2.matchTemplate(g_now, g_prev, cv2.TM_CCOEFF_NORMED)[0, 0])

            # 6) 判断“是否没翻炒”（相似度高 -> 画面没变化）
            vision_corr_thresh = self.vision_corr_thresh  # 先从0.95试，你后面可调
            no_stir = (score >= vision_corr_thresh)
            if not no_stir:
                print(f"vision_score:{score:.3f} no_stir={no_stir} ")
            if no_stir:
                try:
                    ts_float = time.time()
                    t = time.localtime(ts_float)
                    ms = int((ts_float - int(ts_float)) * 1000)
                    ts_log = time.strftime("%Y-%m-%d %H:%M:%S", t) + f".{ms:03d}"
                    log_path = os.path.join(save_path_temp, "stir_warning_log.txt")

                    log_text = (
                        f"[{ts_log}] 警告！{dominant_region}区域受热不均，请均匀翻炒！\n"
                        f"{debug_msg}\n"
                        f"VisionCorr={score:.3f} Th={vision_corr_thresh:.2f}\n"
                        f"{'=' * 80}\n"
                    )

                    with open(log_path, "a", encoding="utf-8") as f:
                        f.write(log_text)
                except Exception as e:
                    print(f"[temp_sensor] 写入报警日志失败: {e}")
                return True, f"{dominant_region}区域受热不均，请均匀翻炒！"

        return False, f"正常: {debug_msg}"

    def _get_hottest_region_in_ellipse(
            self,
            mat: np.ndarray,
            *,
            ellipse_center_y: float = None,
            ellipse_center_x: float = None,
            ellipse_radius_y: float = None,
            ellipse_radius_x: float = None,
    ):
        """
        五分区（四象限 + 中心圆）：
        1) 先取椭圆食材区 ellipse_mask
        2) 在椭圆内再取中心圆 center_mask
        3) 中心=中心圆；四象限=椭圆去掉中心圆再按 cx/cy 分
        4) 每区计算 top-k 均值，返回最热区
        """
        h, w = mat.shape

        # 椭圆参数
        cy = ellipse_center_y if ellipse_center_y is not None else (h - 1) / 2.0
        cx = ellipse_center_x if ellipse_center_x is not None else (w - 1) / 2.0
        ry = ellipse_radius_y if ellipse_radius_y is not None else h * self.Ingredient_region_radius
        rx = ellipse_radius_x if ellipse_radius_x is not None else w * self.Ingredient_region_radius

        ry = max(float(ry), 1e-6)
        rx = max(float(rx), 1e-6)

        yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)

        # 1) 椭圆食材区
        ellipse_mask = (((yy - cy) / ry) ** 2 + ((xx - cx) / rx) ** 2) <= 1.0

        # 2) 中心圆（在椭圆内）
        center_r = min(rx, ry) * float(getattr(self, "center_ratio", 0.5))
        center_r = max(center_r, 1e-6)
        center_mask = ellipse_mask & (((yy - cy) ** 2 + (xx - cx) ** 2) <= (center_r ** 2))

        # 3) 四象限（椭圆内且不在中心圆）
        outer_mask = ellipse_mask & (~center_mask)

        masks = {
            "中心": center_mask,
            "左上": outer_mask & (yy < cy) & (xx < cx),
            "右上": outer_mask & (yy < cy) & (xx >= cx),
            "左下": outer_mask & (yy >= cy) & (xx < cx),
            "右下": outer_mask & (yy >= cy) & (xx >= cx),
        }

        region_scores = {}
        region_counts = {}

        for name, m in masks.items():
            vals = mat[m]
            region_counts[name] = int(vals.size)

            if vals.size == 0:
                region_scores[name] = -np.inf
                continue

            k = max(1, int(vals.size * self.use_topk_ratio_for_region))
            topk = np.partition(vals, -k)[-k:]
            region_scores[name] = float(np.mean(topk))

        hottest_region = max(region_scores, key=region_scores.get)
        return hottest_region, region_scores, region_counts

    def _is_region_persistent(self):
        """
        判断最近 lag_frames 内最热区是否持续同一区域
        返回：(是否持续, 主导区域, 主导占比)
        """
        if len(self.hot_region_buffer) < self.hot_region_buffer.maxlen:
            return False, "", 0.0

        counts = {}
        for r in self.hot_region_buffer:
            counts[r] = counts.get(r, 0) + 1

        dominant_region = max(counts, key=counts.get)
        ratio = counts[dominant_region] / len(self.hot_region_buffer)
        return ratio >= self.region_consistency_ratio, dominant_region, ratio

    def _calc_centroid(self, matrix):
        """计算热力重心"""
        # 为了提高灵敏度，减去一个基底温度，让高温点权重更大
        base_temp = np.min(matrix)
        weights = matrix - base_temp
        weights[weights < 0] = 0  # 钳位

        total_mass = np.sum(weights)
        if total_mass == 0: return 12.0, 16.0  # 返回中心默认值

        h, w = matrix.shape
        y_idxs, x_idxs = np.mgrid[:h, :w]

        cx = np.sum(x_idxs * weights) / total_mass
        cy = np.sum(y_idxs * weights) / total_mass
        return cy, cx


# ====== 模块级全局状态 ======
_lock = threading.Lock()
_buffer = deque(maxlen=120)

_thread = None
_stop_event = threading.Event()
_ser = None

_port = "COM4"
_baudrate = 115200
_timeout = 2.0
_sample_period = 0.5
_buffer_seconds = 60.0

base_dir = r"D:\python\cook\src"
today_tag = f"test_{datetime.now().month}_{datetime.now().day}"
day_root = os.path.join(base_dir, today_tag)

# 你希望 csv+heatmap 都放到 temp 目录
save_path_temp = os.path.join(day_root, "temp")
Path(save_path_temp).mkdir(parents=True, exist_ok=True)

# [新增] 全局监测对象和结果状态
_monitor = Temperature_Monitor()
_analysis_result = {"need_stir": False, "msg": ""}


# ====== 串口操作 (保持不变) ======
def _open_serial():
    global _ser
    _close_serial()
    _ser = serial.Serial(_port, _baudrate, timeout=_timeout)


def _close_serial():
    global _ser
    if _ser is not None:
        with suppress(Exception):
            _ser.close()
        _ser = None


def _query_frame() -> Optional[bytes]:
    req = CMD + struct.pack("<H", crc16(CMD))
    _ser.reset_input_buffer()
    _ser.write(req)
    buf = bytearray()
    start = time.time()
    while len(buf) < FRAME_LEN and (time.time() - start) < 2.0:
        chunk = _ser.read(FRAME_LEN - len(buf))
        if chunk:
            buf += chunk
        else:
            break
    return bytes(buf) if len(buf) == FRAME_LEN else None


def _parse_frame_to_matrix(frame: Optional[bytes]) -> Optional[np.ndarray]:
    if frame is None or len(frame) != FRAME_LEN: return None
    start = 37
    raw = frame[start: start + 768 * 2]
    if len(raw) != 768 * 2: return None
    temps = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 10.0
    return temps.reshape(24, 32)


# ====== [修改] 后台采样线程 ======
def _sampling_loop():
    global _analysis_result
    backoff = 0.5

    while not _stop_event.is_set():
        try:
            if _ser is None:
                _open_serial()
                backoff = 0.5

            frame = _query_frame()
            mat = _parse_frame_to_matrix(frame)

            if mat is not None:
                ts = time.time()

                _save_matrix_and_heatmap(mat, ts)
                # 1. 存入历史队列
                with _lock:
                    _buffer.append((ts, mat.copy()))

                    # 2. [新增] 实时运行防糊锅算法
                    # 注意：process_frame 计算量极小(24x32矩阵)，不会阻塞采样
                    need_stir, msg = _monitor.process_frame(mat, roi_x1=420, roi_y1=70, roi_x2=1500, roi_y2=1010)

                    # 更新全局状态
                    _analysis_result["need_stir"] = need_stir
                    _analysis_result["msg"] = msg

            time.sleep(_sample_period)

        except Exception as e:
            print("[temp_sensor] 采样错误：", e)
            _close_serial()
            time.sleep(backoff)
            backoff = min(backoff * 2, 5.0)

    _close_serial()


def start_temp_sensor(
        port: str = "COM4",
        baudrate: int = 115200,
        timeout: float = 2.0,
        sample_period: float = 0.5,
        buffer_seconds: float = 60.0,
) -> None:
    """启动后台采样线程"""
    global _thread, _port, _baudrate, _timeout, _sample_period, _buffer_seconds, _buffer, _monitor

    with _lock:
        _port = port
        _baudrate = int(baudrate)
        _timeout = float(timeout)
        _sample_period = float(sample_period)
        _buffer_seconds = float(buffer_seconds)

        maxlen = max(1, int(_buffer_seconds / _sample_period + 0.5))
        if _buffer.maxlen != maxlen:
            _buffer = deque(_buffer, maxlen=maxlen)

    if _thread is not None and _thread.is_alive():
        return

    with _lock:
        _monitor.reset(sample_period=_sample_period)

    _stop_event.clear()
    _thread = threading.Thread(target=_sampling_loop, daemon=True)
    _thread.start()


# ====== Getters ======

def get_latest_matrix(copy: bool = True) -> Optional[np.ndarray]:
    with _lock:
        if not _buffer: return None
        mat = _buffer[-1][1]
        return mat.copy() if copy else mat


def get_temperature(ratio: float = 0.1) -> Optional[float]:
    # (保持原有代码不变)
    if ratio <= 0 or ratio > 1: raise ValueError("ratio 必须在 (0, 1]")
    start_temp_sensor(port=_port, baudrate=_baudrate, timeout=_timeout,
                      sample_period=_sample_period, buffer_seconds=_buffer_seconds)
    mat = get_latest_matrix(copy=False)
    if mat is None: return None
    flat = mat.reshape(-1)
    k = max(int(len(flat) * ratio), 1)
    topk = np.partition(flat, -k)[-k:]
    return float(np.mean(topk))


# ====== [新增] 翻炒建议接口 ======
def get_stir_suggestion() -> Tuple[bool, str]:
    with _lock:
        # 返回副本防止外部修改
        return _analysis_result["need_stir"], _analysis_result["msg"]


def get_latest_heatmap(
        size: Optional[Tuple[int, int]] = None,
        colormap: int = cv2.COLORMAP_VIRIDIS,
        copy: bool = True,
        grid: bool = True,
        grid_thickness: int = 2,
        grid_color: Tuple[int, int, int] = (0, 0, 0),
) -> Optional[np.ndarray]:
    # (保持原有代码不变，为节省篇幅略去函数体，逻辑与你提供的完全一致)
    mat = get_latest_matrix(copy=copy)
    if mat is None: return None
    mmin, mmax = float(np.min(mat)), float(np.max(mat))
    if mmax - mmin < 1e-6:
        gray = np.zeros_like(mat, dtype=np.uint8)
    else:
        gray = ((mat - mmin) / (mmax - mmin) * 255.0).astype(np.uint8)
    heat = cv2.applyColorMap(gray, colormap)
    if size is not None: heat = cv2.resize(heat, size, interpolation=cv2.INTER_LANCZOS4)
    if grid:
        h, w = heat.shape[:2]
        x1, x2 = w // 3, (2 * w) // 3
        y1, y2 = h // 3, (2 * h) // 3
        for x in (x1, x2): cv2.line(heat, (x, 0), (x, h - 1), grid_color, grid_thickness)
        for y in (y1, y2): cv2.line(heat, (0, y), (w - 1, y), grid_color, grid_thickness)
    heat = add_colorbar(heat, mmin, mmax, colormap)  # 需确保 add_colorbar 函数存在
    return heat


def add_colorbar(heat, vmin, vmax, colormap, bar_width=60, ticks=5):
    h, w = heat.shape[:2]
    gradient = np.linspace(255, 0, h).astype(np.uint8).reshape(h, 1)
    gradient = np.repeat(gradient, bar_width, axis=1)
    bar = cv2.applyColorMap(gradient, colormap)
    out = np.hstack([heat, bar])
    for i in range(ticks):
        y = int(i * (h - 1) / (ticks - 1))
        val = vmax - (vmax - vmin) * i / (ticks - 1)
        cv2.putText(out, f"{val:.1f}", (w + 5, y + 5), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1)
    cv2.putText(out, "", (w + 10, h - 10), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 2)
    return out

def create_picture_heatmap(temperature_matrix: np.ndarray, save_path: str) -> None:
    plt.figure()
    plt.contourf(temperature_matrix, cmap="viridis")
    plt.colorbar()
    plt.savefig(save_path)
    plt.close()

def _save_matrix_and_heatmap(mat: np.ndarray, ts: float) -> None:
    # 文件名：精确到毫秒，避免同一秒覆盖
    lt = time.localtime(ts)
    ms = int((ts - int(ts)) * 1000)
    now_file = time.strftime("%Y%m%d_%H%M%S", lt) + f"_{ms:03d}"

    base_temp_path = Path(save_path_temp)
    base_temp_path.mkdir(parents=True, exist_ok=True)

    mat_np = np.array(mat, dtype=np.float32)

    csv_path = base_temp_path / f"temperature_{now_file}.csv"
    np.savetxt(csv_path, mat_np, delimiter=",", fmt="%.1f")

    img_path = base_temp_path / f"heatmap_{now_file}.png"
    create_picture_heatmap(mat_np, str(img_path))

def close_temperature_sensor() -> None:
    global _thread
    _stop_event.set()
    if _thread is not None:
        _thread.join(timeout=2.0)
        _thread = None
    _close_serial()


# ====== 自测 ======
if __name__ == "__main__":
    start_temp_sensor(port="COM13", sample_period=0.5, buffer_seconds=60)
    print("传感器已启动，采集数据中...")

    try:
        while True:
            # 模拟机器人主循环
            time.sleep(0.5)

            # 1. 获取温度
            temp = get_temperature(ratio=0.1)

            # 2. [新增] 获取翻炒建议
            need_stir, msg = get_stir_suggestion()

            # 打印状态
            ts_str = time.strftime("%H:%M:%S")
            temp_str = f"{temp:.1f}°C" if temp else "N/A"
            status = "【🔴 需要翻炒】" if need_stir else "【🟢 状态良好】"

            print(f"[{ts_str}] Temp: {temp_str} | {status} -> {msg}")

            # 显示热力图
            img = get_latest_heatmap(size=(600, 440))
            if img is not None:
                cv2.imshow("Heatmap Monitor", img)
                if cv2.waitKey(1) & 0xFF == 27: break

    except KeyboardInterrupt:
        pass
    finally:
        close_temperature_sensor()
        cv2.destroyAllWindows()