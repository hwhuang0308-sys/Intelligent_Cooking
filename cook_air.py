from __future__ import annotations
import os
from datetime import datetime
import time
import threading
from collections import deque
from contextlib import suppress
from typing import Optional, Deque, Tuple, Dict, Any
from pathlib import Path
import serial


# =========================
#   ZP16 协议常量（与你原代码一致）
# =========================
MODE_ACTIVE = 0x40
CMD_SWITCH_MODE = 0x78


def _calculate_checksum(frame9: bytes) -> int:
    """
    校验值 = (取反(字节1+字节2+...+字节7)) + 1
    注意：这里传入应当是 9 字节帧（含起始与校验位），按 ZP16 规则只算 [1:8]
    """
    s = sum(frame9[1:8]) & 0xFF
    chk = ((~s) & 0xFF) + 1
    return chk & 0xFF


def _build_switch_active_cmd() -> bytes:
    # FF 00 78 40 00 00 00 00 48
    cmd = bytearray([0xFF, 0x00, CMD_SWITCH_MODE, MODE_ACTIVE, 0, 0, 0, 0, 0])
    cmd[8] = _calculate_checksum(cmd)
    return bytes(cmd)


def _parse_active_frame(frame9: bytes) -> Optional[Dict[str, Any]]:
    """
    主动上传帧格式（9字节）：
    FF  gas_type  unit  decimal  conc_hi  conc_lo  fs_hi  fs_lo  checksum
    """
    if len(frame9) != 9:
        return None
    if frame9[0] != 0xFF:
        return None
    if frame9[8] != _calculate_checksum(frame9):
        return None

    gas_type = frame9[1]
    unit = frame9[2]
    decimal = frame9[3]
    conc_raw = frame9[4] * 256 + frame9[5]
    fs_raw = frame9[6] * 256 + frame9[7]

    scale = 10 ** decimal
    concentration = conc_raw / scale
    full_scale = fs_raw / scale

    # unit=0x11 通常表示 mg/m3（你原代码写死了 'mg/m³'）
    return {
        "ts": time.time(),
        "concentration": concentration,
        "full_scale": full_scale,
        "gas_type": gas_type,
        "unit": "mg/m³",
        "unit_code": unit,
        "decimal": decimal,
    }


def _read_one_frame_sync(ser: serial.Serial) -> Optional[bytes]:
    """
    更稳的读法：先同步到 0xFF，再读满剩余 8 字节，得到 9 字节帧
    """
    # 读到起始 0xFF
    b = ser.read(1)
    if not b:
        return None
    # 可能读到的不是 0xFF，就一直找
    while b and b[0] != 0xFF:
        b = ser.read(1)
        if not b:
            return None

    rest = ser.read(8)
    if len(rest) != 8:
        return None
    return b + rest


# =========================
#   后台线程管理器（像 audio 一样）
# =========================
_lock = threading.Lock()
_buffer: Deque[Tuple[float, Dict[str, Any]]] = deque()  # (ts, data)
_latest: Optional[Dict[str, Any]] = None

_thread: Optional[threading.Thread] = None
_stop_event = threading.Event()
_ser: Optional[serial.Serial] = None

_port = "/dev/ttyUSB2"
_baudrate = 9600
_timeout = 1.0
_buffer_seconds = 60.0

# 传感器主动上传默认 1Hz（官方手册写每 1s 一次）
_expected_hz = 1.0
_air_log_enabled = True

base_dir = r"D:\python\cook\src"
today_tag = f"test_{datetime.now().month}_{datetime.now().day}"
day_root = os.path.join(base_dir, today_tag)
_air_log_path = os.path.join(day_root, "air", "air_log.txt")


def start_air_sensor(
    port: str = "/dev/ttyUSB2",
    baudrate: int = 9600,
    timeout: float = 1.0,
    buffer_seconds: float = 60.0,
    expected_hz: float = 1.0,
) -> None:
    """
    启动后台采集线程（可重复调用；已启动则直接返回）
    """
    global _thread, _port, _baudrate, _timeout, _buffer_seconds, _expected_hz

    _port = port
    _baudrate = baudrate
    _timeout = timeout
    _buffer_seconds = float(buffer_seconds)
    _expected_hz = float(expected_hz)

    # 重新配置 buffer maxlen（按 60秒 * 1Hz ≈ 60点，给 2倍冗余）
    maxlen = max(10, int(_buffer_seconds * _expected_hz * 2) + 10)
    with _lock:
        # 重新建一个同 maxlen 的 deque，并尽量保留已有数据
        old = list(_buffer)
        _buffer.clear()
        _buffer.extend(old[-maxlen:])
        _buffer.__init__(maxlen=maxlen)  # type: ignore

    if _thread is not None and _thread.is_alive():
        return

    _stop_event.clear()
    _thread = threading.Thread(target=_capture_loop, daemon=True)
    _thread.start()


def _open_serial() -> bool:
    global _ser
    try:
        _ser = serial.Serial(_port, _baudrate, timeout=_timeout)
        # 切到主动上传（发一次即可；即使已经是 active，也不会坏）
        cmd = _build_switch_active_cmd()
        _ser.reset_input_buffer()
        _ser.write(cmd)
        time.sleep(0.1)
        return True
    except Exception as e:
        print("[zp16] open serial failed:", e)
        _ser = None
        return False


def _close_serial():
    global _ser
    if _ser is not None:
        with suppress(Exception):
            _ser.close()
    _ser = None


def _capture_loop():
    """
    后台线程：持续读 ZP16 主动上传帧，存入 deque（保存最近 buffer_seconds）
    """
    global _latest

    backoff = 0.5
    while not _stop_event.is_set():
        if _ser is None:
            if not _open_serial():
                time.sleep(backoff)
                backoff = min(backoff * 2, 5.0)
                continue
            backoff = 0.5

        try:
            frame = _read_one_frame_sync(_ser)
            if frame is None:
                # timeout/断流：重连
                _close_serial()
                time.sleep(backoff)
                backoff = min(backoff * 2, 5.0)
                continue

            data = _parse_active_frame(frame)
            if data is None:
                # 校验失败/异常帧：继续读
                continue

            ts = data["ts"]

            _append_air_log_line(ts, float(data["concentration"]))
            with _lock:
                _latest = data
                _buffer.append((ts, data))

                # 顺便按时间清理（即使 maxlen 冗余，也确保只保留 60 秒）
                cutoff = time.time() - _buffer_seconds
                while _buffer and _buffer[0][0] < cutoff:
                    _buffer.popleft()

        except Exception as e:
            print("[zp16] read error:", e)
            _close_serial()
            time.sleep(backoff)
            backoff = min(backoff * 2, 5.0)

    _close_serial()


def get_air(start_offset: float, end_offset: float = 0.0) -> list[float]:
    """
    像 audio 一样：获取 (now+start_offset) 到 (now+end_offset) 之间的浓度序列
    - start_offset/end_offset 都是 ≤0 的“相对当前时刻”的偏移（秒）
    - 例：get_air(-20, -10) -> 取最近 20~10 秒之间的数据点
    """
    if start_offset >= end_offset:
        raise ValueError("要求 start_offset < end_offset ≤ 0")
    if end_offset > 0:
        raise ValueError("end_offset 必须 ≤ 0")
    if start_offset < -_buffer_seconds:
        raise ValueError(f"仅支持查询最近 {_buffer_seconds:.0f} s 内的数据")

    now = time.time()
    t0 = now + start_offset
    t1 = now + end_offset

    with _lock:
        return [d["concentration"] for ts, d in _buffer if (t0 <= ts <= t1)]


def get_latest() -> Optional[float]:
    """
    获取最新一次浓度值（只取一个最新数据）
    """
    with _lock:
        if _latest is None:
            return None
        return float(_latest["concentration"])


def close_air_sensor() -> None:
    """
    关闭后台线程与串口
    """
    global _thread
    if _thread is None:
        return
    _stop_event.set()
    _thread.join(timeout=2.0)
    _thread = None

def _append_air_log_line(ts: float, concentration: float) -> None:
    if not _air_log_enabled or not _air_log_path:
        return

    # 生成你截图那种时间戳格式：YYYY-mm-dd HH:MM:SS.mmm
    lt = time.localtime(ts)
    ms = int((ts - int(ts)) * 1000)
    ts_str = time.strftime("%Y-%m-%d %H:%M:%S", lt) + f".{ms:03d}"

    line = f"[{ts_str}] air={concentration:.6f} mg/m³\n"
    try:
        Path(_air_log_path).parent.mkdir(parents=True, exist_ok=True)
        with open(_air_log_path, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception as e:
        print("[zp16] write log failed:", e)

if __name__ == "__main__":
    # 配置你的串口端口，确保你在 Linux 下有权限读取该端口
    # 常用命令: sudo chmod 666 /dev/ttyUSB2
    TEST_PORT = "COM14"

    print(f"🚀 [测试模式] 正在启动 ZP16 传感器 (Port: {TEST_PORT})...")

    # 1. 启动传感器
    try:
        start_air_sensor(port=TEST_PORT, buffer_seconds=60.0)

        # 给一点时间让串口打开并收到第一帧数据
        print("⏳ 等待传感器预热和数据同步 (约 2 秒)...")
        time.sleep(2)

        print("\n🟢 开始监测数据 (按 Ctrl+C 停止):")
        print("-" * 50)
        print(f"{'时间戳':<20} | {'浓度 (mg/m³)':<15} | {'状态指示'}")
        print("-" * 50)

        # 2. 循环读取最新值
        while True:
            # 获取最新一次读数
            val = get_latest()
            current_time = time.strftime("%H:%M:%S", time.localtime())

            if val is not None:
                # 简单的可视化进度条
                bar_len = int(val * 50)  # 根据浓度调整比例
                bar = "█" * bar_len
                print(f"{current_time:<20} | {val:<15.4f} | {bar}")
            else:
                print(f"{current_time:<20} | {'等待数据...':<15} |")

            # 传感器大概 1秒1次，这里 sleep 1秒避免刷屏太快
            time.sleep(1)

    except KeyboardInterrupt:
        print("\n\n🛑 用户中断，正在停止测试...")

        # 3. 测试历史数据读取功能 (get_air)
        print("-" * 50)
        print("📊 [Buffer 测试] 读取过去 10 秒内的历史数据:")
        try:
            # 读取从 "现在-10秒" 到 "现在" 的数据
            history_data = get_air(start_offset=-10, end_offset=0)
            print(f"   采集到的数据点数量: {len(history_data)} 个")
            print(f"   平均浓度: {sum(history_data) / len(history_data):.4f} mg/m³" if history_data else "   无数据")
            print(f"   原始列表: {[round(x, 3) for x in history_data]}")
        except Exception as e:
            print(f"   读取历史数据出错: {e}")
        print("-" * 50)

    finally:
        # 4. 关闭资源
        close_air_sensor()
        print("👋 传感器线程已关闭，串口已释放。")