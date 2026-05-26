import time
import os
from datetime import datetime
import threading
from collections import deque
from contextlib import suppress
from pathlib import Path
import serial

__all__ = ["start_audio_sensor", "get_audio", "close_audio_sensor"]


# ========== 模块级全局状态（单例） ==========
_lock = threading.Lock()
_buffer = deque(maxlen=120)  # 会在 start_audio_sensor 里按 sample_period 动态调整

_thread = None
_stop_event = threading.Event()
_ser = None  # serial.Serial

_port = "COM8"
_baudrate = 9600
_timeout = 1.0

_sample_period = 0.5
_buffer_seconds = 60.0

_audio_log_enabled = True
base_dir = r"D:\python\cook\src"
today_tag = f"test_{datetime.now().month}_{datetime.now().day}"
day_root = os.path.join(base_dir, today_tag)
_audio_log_path = os.path.join(day_root, "audio", "audio_log.txt")


# ========== Modbus RTU 工具 ==========
def _crc16(data: bytes) -> int:
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            lsb = crc & 1
            crc >>= 1
            if lsb:
                crc ^= 0xA001
    return crc


def _read_once_db() -> float:
    """
    发送 Modbus-RTU 请求：读从机 0x01 的保持寄存器 0x0000，读取 1 个寄存器（2字节）
    响应数据按 big-endian 解析，并 /10 得到分贝值。
    """
    global _ser
    # 请求：01 03 00 00 00 01 + CRC(lo hi)
    request = bytearray([0x01, 0x03, 0x00, 0x00, 0x00, 0x01])
    crc = _crc16(request)
    request += crc.to_bytes(2, byteorder="little")

    _ser.reset_input_buffer()
    _ser.write(request)

    # 等待设备响应（你原来是 0.1s）
    time.sleep(0.1)

    # 标准响应长度 7 字节：addr(1) func(1) len(1) data(2) crc(2)
    resp = _ser.read(7)
    if len(resp) < 7:
        raise ValueError(f"响应长度不足: {len(resp)}")

    # 这里只做最基本解析（不校验 CRC / 功能码），保持与你原代码接近的“简化风格”
    raw = int.from_bytes(resp[3:5], byteorder="big")
    return raw / 10.0


# ========== 后台采样线程 ==========
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


def _sampling_loop():
    """
    后台线程：每 sample_period 秒采一次，写入 (timestamp, value) 到 deque
    """
    backoff = 0.5

    while not _stop_event.is_set():
        try:
            if _ser is None:
                _open_serial()
                backoff = 0.5

            value = _read_once_db()
            ts = time.time()

            _append_audio_log_line(ts, float(value))
            with _lock:
                _buffer.append((ts, value))

            # 正常采样睡眠
            time.sleep(_sample_period)

        except Exception as e:
            # 采样失败：关闭串口，退避后重试
            print("[sound_sensor] 采样错误：", e)
            _close_serial()
            time.sleep(backoff)
            backoff = min(backoff * 2, 5.0)

    _close_serial()


def start_audio_sensor(
    port="COM8",
    baudrate=9600,
    timeout=1.0,
    sample_period=0.5,
    buffer_seconds=60.0,
):
    """
    启动后台采样线程（只会启动一次）。
    队列长度会自动调整为约 buffer_seconds / sample_period 个点。
    """
    global _thread, _port, _baudrate, _timeout, _sample_period, _buffer_seconds, _buffer

    with _lock:
        _port = port
        _baudrate = baudrate
        _timeout = float(timeout)
        _sample_period = float(sample_period)
        _buffer_seconds = float(buffer_seconds)

        maxlen = max(1, int(_buffer_seconds / _sample_period + 0.5))
        if _buffer.maxlen != maxlen:
            _buffer = deque(_buffer, maxlen=maxlen)

    if _thread is not None and _thread.is_alive():
        return

    _stop_event.clear()
    _thread = threading.Thread(target=_sampling_loop, daemon=True)
    _thread.start()


def _wait_for_samples(duration: float, max_wait: float):
    """
    等待队列里至少有 duration 对应的样本数（尽量等到有数据再返回）
    """
    needed = max(1, int(duration / _sample_period + 0.5))
    start = time.time()
    while time.time() - start < max_wait:
        with _lock:
            if len(_buffer) >= needed:
                return
        time.sleep(min(0.05, _sample_period / 2))


def get_audio(start_offset: float, end_offset: float = 0.0) -> list[float]:
    """
    返回 (start_offset, end_offset] 之间的分贝值列表（offset 为相对“现在”的秒数，必须 ≤ 0）
    例：get_audio(-20, -10) -> 返回 20秒前到10秒前之间的采样点
    """
    # ---------- 参数检查 ----------
    if start_offset >= end_offset:
        raise ValueError("要求 start_offset < end_offset ≤ 0")
    if end_offset > 0:
        raise ValueError("end_offset 必须 ≤ 0")
    if start_offset < -_buffer_seconds:
        raise ValueError(f"仅支持查询最近 {_buffer_seconds:.0f} s 内的数据")

    # 确保采样线程已启动（你也可以选择在主线程显式 start_audio_sensor）
    start_audio_sensor(port=_port, baudrate=_baudrate, timeout=_timeout,
                       sample_period=_sample_period, buffer_seconds=_buffer_seconds)

    # 尽量等到窗口长度对应的样本数到位（第一次调用时很有用）
    duration = abs(start_offset)
    _wait_for_samples(duration, max_wait=duration + _sample_period)

    now = time.time()
    t_start = now + start_offset   # start_offset 是负数
    t_end = now + end_offset       # end_offset ≤ 0

    with _lock:
        # buffer 存 (ts, value)
        return [v for ts, v in list(_buffer) if (t_start <= ts <= t_end)]


def close_audio_sensor():
    """停止后台线程并关闭串口"""
    global _thread
    _stop_event.set()
    if _thread is not None:
        _thread.join(timeout=2.0)
        _thread = None
    _close_serial()

def _append_audio_log_line(ts: float, db: float) -> None:
    if not _audio_log_enabled or not _audio_log_path:
        return

    lt = time.localtime(ts)
    ms = int((ts - int(ts)) * 1000)
    ts_str = time.strftime("%Y-%m-%d %H:%M:%S", lt) + f".{ms:03d}"

    line = f"[{ts_str}] audio={db:.6f} dB\n"
    try:
        Path(_audio_log_path).parent.mkdir(parents=True, exist_ok=True)
        with open(_audio_log_path, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception as e:
        print("[sound_sensor] write log failed:", e)