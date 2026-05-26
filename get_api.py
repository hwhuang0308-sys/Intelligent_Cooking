# -*- coding: utf-8 -*-
from __future__ import annotations
import httpx
import time
import ssl
import os
import numpy as np
from openai import OpenAI,BadRequestError,RateLimitError,APIConnectionError,APITimeoutError,APIStatusError,APIError
http_client = httpx.Client(timeout=httpx.Timeout(connect=5.0, read=300.0, write=200.0, pool=5.0))
api_key = "sk-XXXXXXXXXXXXXXXXXXXXXX"
client = OpenAI(api_key=api_key,base_url="https://api.vectorengine.ai/v1")
client_2 = OpenAI(api_key=api_key,base_url="https://api.vectorengine.ai/v1",http_client=http_client)
client_3 = OpenAI(api_key=api_key,base_url="https://api.vectorengine.ai/v1",timeout=60.0)
RETRYABLE = (ssl.SSLError, httpx.TransportError, APITimeoutError, APIError)

def get_text_answer(prompt: str, model: str, reason: str = "medium", verbosity: str = "low") -> str:
    response = client.responses.create(
        model=model,
        input=prompt,
        reasoning={"effort": reason},
        text={"verbosity": verbosity},
    )

    return response.output_text

schema = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "sensors": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "type": {"type": "string", "enum": ["visual", "temperature", "audio", "time"]},
                    "duration": {"type": "number"},
                    "condition": {"type": "string"},
                },
                "required": ["type", "duration", "condition"],
            },
        },
        "logic": {"type": "string"},
    },
    "required": ["sensors", "logic"],
}
def get_text_answer_json(prompt: str, model: str, reason: str = "medium", verbosity: str = "low") -> str:
    response = client.responses.create(
        model=model,
        input=prompt,
        reasoning={"effort": reason},
        text={
            "verbosity": verbosity,
            "format": {
                "type": "json_schema",
                "name": "cooking_condition_parse",
                "schema": schema,
                "strict": True
            }
        },
    )
    return response.output_text
def get_text_answer_force_websearch(
    prompt: str,
    model: str = "gpt-5.2",
    reason: str = "medium",
    verbosity: str = "low",
    log_fn=print,  # 你也可以传 logging.warning / logger.info 等
) -> str:
    attempt = 0

    while True:
        attempt += 1
        ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
        try:
            resp = client_2.responses.create(
                model=model,
                input=prompt,
                reasoning={"effort": reason},
                text={"verbosity": verbosity},
                tools=[{"type": "web_search"}],
                tool_choice="required",
            )
            return resp.output_text

        except BadRequestError as e:
            # 参数错：不重试（重试没用）
            log_fn(f"[{ts}] attempt={attempt} FAILED ({type(e).__name__}) -> NOT retry (400 bad request). err={e}")
            raise

        except APIConnectionError as e:
            # 连接失败：立刻重试（你要的“马上重新运行”）
            log_fn(
                f"[{ts}] attempt={attempt} FAILED ({type(e).__name__}) -> retry immediately (connection failure). err={e}")
            continue

        except APITimeoutError as e:
            # 超时：你若也希望立刻重试，就 continue
            log_fn(f"[{ts}] attempt={attempt} FAILED ({type(e).__name__}) -> retry (timeout). err={e}")
            continue

        except RateLimitError as e:
            # 限流：严格来说不建议立刻重试，否则更容易一直429
            # 但如果你坚持“立刻”，也可以 continue；这里我给一个很短sleep更稳
            log_fn(f"[{ts}] attempt={attempt} FAILED ({type(e).__name__}) -> retry (rate limit). err={e}")
            time.sleep(1.0)
            continue

        except APIStatusError as e:
            # 其它 4xx/5xx：一般可以重试（尤其 5xx）
            log_fn(
                f"[{ts}] attempt={attempt} FAILED ({type(e).__name__}) -> retry (status={getattr(e, 'status_code', None)}). err={e}")
            continue

        except Exception as e:
            # 兜底：未知错误也记录并重试
            log_fn(f"[{ts}] attempt={attempt} FAILED ({type(e).__name__}) -> retry (unknown). err={repr(e)}")
            continue
def get_text_answer_stream(prompt: str, model: str, verbosity: str = "low", reason: str = "none") -> str:
    last_err = None
    for attempt in range(5):
        try:
            stream = client_3.responses.create(
                model=model,
                input=[{"role": "user", "content": prompt}],
                text={"verbosity": verbosity},
                stream=True,
                reasoning={"effort": reason},
            )

            buf = []
            for event in stream:
                if event.type == "response.output_text.delta":
                    if event.delta:
                        buf.append(event.delta)
                elif event.type == "error":
                    raise RuntimeError(f"Streaming error: {event}")
            return "".join(buf)

        except RETRYABLE as e:
            last_err = e
            time.sleep(0.8 * (2 ** attempt))
            # 继续重试会建立新连接
            continue

    raise last_err

from typing import Optional
import base64

def get_vision_answer_from_np(
    image_np,                      # numpy.ndarray
    prompt: str,
    model: str,
    reason: str = "none",
    verbosity: str = "low",
    color_space: str = "BGR",      # "BGR" (OpenCV 默认) 或 "RGB"
    max_side: Optional[int] = 1024,  # 缩放最大边，避免太大
    image_format: str = "jpeg",    # "jpeg" 或 "png"
    jpeg_quality: int = 85,        # 1~100
) -> str:
    """
    输入：numpy 图像 + prompt
    输出：response.output_text (字符串)

    - 使用 OpenAI Responses API 的 input_text + input_image(data URL) 方式
    - image_np 通常是 OpenCV 读到的 BGR uint8 (H,W,3)
    """

    import numpy as np

    # 1) 基础检查
    if image_np is None:
        raise ValueError("image_np is None")
    if not hasattr(image_np, "shape"):
        raise TypeError(f"image_np must be a numpy ndarray-like, got {type(image_np)}")

    img = image_np

    # 2) 统一成 uint8
    if img.dtype != np.uint8:
        # 常见情况：float32 [0,1] 或其他范围 -> 映射到 [0,255]
        img_min = float(np.min(img))
        img_max = float(np.max(img))
        if img_max - img_min < 1e-12:
            img_u8 = np.zeros_like(img, dtype=np.uint8)
        else:
            img_u8 = ((img - img_min) / (img_max - img_min) * 255.0).clip(0, 255).astype(np.uint8)
        img = img_u8

    # 3) 处理通道：灰度 -> 3通道
    if img.ndim == 2:
        img = np.stack([img, img, img], axis=-1)
    elif img.ndim == 3 and img.shape[2] == 1:
        img = np.repeat(img, 3, axis=2)
    elif img.ndim != 3 or img.shape[2] not in (3, 4):
        raise ValueError(f"Unsupported image shape: {img.shape}, expected HxWx3/4 or HxW")

    # 4) 颜色空间：如果是 RGB，需要转成 OpenCV 习惯的 BGR 再编码（保证颜色不乱）
    try:
        import cv2
    except ImportError as e:
        raise ImportError("需要安装 opencv-python 才能直接处理 numpy 图像编码") from e

    if color_space.upper() == "RGB":
        if img.shape[2] == 3:
            img = cv2.cvtColor(img, cv2.COLOR_RGB2BGR)
        elif img.shape[2] == 4:
            img = cv2.cvtColor(img, cv2.COLOR_RGBA2BGRA)
    elif color_space.upper() != "BGR":
        raise ValueError("color_space must be 'BGR' or 'RGB'")

    # 5) 可选缩放：限制最大边，减少请求体积/速度更快
    if max_side is not None:
        h, w = img.shape[:2]
        m = max(h, w)
        if m > max_side:
            scale = max_side / float(m)
            new_w = int(round(w * scale))
            new_h = int(round(h * scale))
            img = cv2.resize(img, (new_w, new_h), interpolation=cv2.INTER_AREA)

    # 6) numpy -> bytes（内存编码）
    fmt = image_format.lower().strip()
    if fmt not in ("jpeg", "jpg", "png"):
        raise ValueError("image_format must be 'jpeg'/'jpg' or 'png'")

    if fmt in ("jpeg", "jpg"):
        encode_ext = ".jpg"
        params = [int(cv2.IMWRITE_JPEG_QUALITY), int(jpeg_quality)]
        mime = "image/jpeg"
    else:
        encode_ext = ".png"
        params = []
        mime = "image/png"

    ok, buf = cv2.imencode(encode_ext, img, params)
    if not ok:
        raise RuntimeError("cv2.imencode failed")

    img_b64 = base64.b64encode(buf.tobytes()).decode("utf-8")
    data_url = f"data:{mime};base64,{img_b64}"

    # 7) 调用 Responses API（你项目里已有 client）
    response = client.responses.create(
        model=model,
        input=[
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": prompt},
                    {"type": "input_image", "image_url": data_url},
                ],
            }
        ],
        reasoning={"effort": reason},
        text={"verbosity": verbosity},
    )

    return response.output_text.strip()

def call_gemini(prompt: str) -> str:
    client = OpenAI(api_key="sk-XXXXXXXXXXX", base_url="https://api.vectorengine.ai/v1")

    completion = client.chat.completions.create(
        model="gemini-3-pro-preview",  # 此处以 deepseek-r1 为例，可按需更换模型名称。
        messages=[{"role": "user", "content": prompt}],
    )

    return completion.choices[0].message.content
