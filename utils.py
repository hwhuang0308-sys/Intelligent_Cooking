# -*- coding: utf-8 -*-
import re
import os
import json
import time

import get_api
from typing import Tuple, Optional
from shared_data import (
    Step,
    GLOBAL_RECIPE_STEPS,
    STEP_LOCK,
    INVALID_DURATION_STR,
    INVALID_CONDITION_DATA
)


def read_py_file(file_path: str) -> str:
    """读取指定路径的 Python 文件内容并返回为字符串"""
    with open(file_path, 'r', encoding='utf-8') as f:
        content = f.read()
    return content


def parse_recipe_into_global(action_recipe_str: str) -> None:
    """
    【主解析入口】
    将包含 50+ 步的长字符串解析为 Step 对象列表，并存入 GLOBAL_RECIPE_STEPS。
    """
    print("[Parser] 开始解析 Action Recipe...")

    # 1. 按行分割，去掉空行
    raw_lines = [line.strip() for line in action_recipe_str.split('\n') if line.strip()]

    steps_list = []

    # 2. 逐行解析
    for i, line in enumerate(raw_lines):
        try:
            step_obj = parse_single_line(line)
            steps_list.append(step_obj)
        except Exception as e:
            print(f"[Parser Error] 第 {i + 1} 行解析失败: {line} \n原因: {e}")
            # 可以选择跳过或报错，这里演示跳过
            continue

    # 3. 解析完成后，一次性写入全局变量 (加锁)
    with STEP_LOCK:
        GLOBAL_RECIPE_STEPS.clear()
        GLOBAL_RECIPE_STEPS.extend(steps_list)

    print(f"[Parser] 解析成功！共存入 {len(GLOBAL_RECIPE_STEPS)} 个步骤到全局变量。")
    # 打印前几个看看对不对
    if len(GLOBAL_RECIPE_STEPS) > 0:
        print(f"Sample Step 1: {GLOBAL_RECIPE_STEPS[0]}")


def parse_single_line(line: str) -> Step:
    """
    【修复版单行解析逻辑】
    采用强切分逻辑：以 'until' 为界，左边是动作，右边是条件。
    彻底防止条件内容混入 do_text。

    兼容应急步骤：支持行首出现 "应急步骤" 之类的业务前缀，例如：
      "应急步骤，do 发生糊锅，立即关火"
    最终 do_text 只保留动作内容（不包含“应急步骤”）。
    """
    # 0. 预处理：统一标点，防止 LLM 混用中英文标点
    line = line.replace('，', ',').replace('：', ':').strip()

    # === A. 核心逻辑：寻找 until 进行切分 ===
    # 使用正则查找 until，确保忽略大小写且匹配单词边界
    match_until = re.search(r'\buntil\b', line, re.IGNORECASE)

    if match_until:
        # >>>>> 判定为：持续动作 (Continuous) <<<<<
        is_continuous = 1
        cut_index = match_until.start()

        # 1. 物理切割字符串
        left_part = line[:cut_index].strip()
        right_part = line[cut_index:].strip()

        # 2. 从右半部分(right_part) 提取 推荐时长
        dur_match = re.search(r"推荐时长\s*[:]\s*(\d+)", right_part)
        if dur_match:
            seconds = int(dur_match.group(1))
            rec_duration = f"{seconds:04d}"
        else:
            rec_duration = "0060"  # 默认值，防止空

        # 3. 从右半部分(right_part) 提取 判定条件
        cond_match = re.search(r"until\s*(.*?)(?=\s*[,]\s*<推荐时长|$)", right_part, re.IGNORECASE)

        if cond_match:
            raw_condition_str = cond_match.group(1).strip()
            conditions = _call_api_to_convert_condition(raw_condition_str)
        else:
            backup_str = re.sub(r"until\s*", "", right_part, flags=re.IGNORECASE)
            backup_str = re.sub(r"[,]\s*<推荐时长.*", "", backup_str).strip()
            conditions = _call_api_to_convert_condition(backup_str)

    else:
        # >>>>> 判定为：瞬时动作 (Instant) <<<<<
        is_continuous = 0
        left_part = line  # 整行都是动作

        rec_duration = INVALID_DURATION_STR
        conditions = INVALID_CONDITION_DATA

    # === B. 清洗 do_text (只处理 left_part) ===
    # 此时 left_part 绝对不包含 until 及后面的内容，安全！

    # 0) 去掉行首的“应急步骤”业务前缀（不影响正常“第X步”）
    left_part = re.sub(r'^\s*应急步骤\s*[,\s]*', '', left_part).strip()

    # 1) 去掉行首的“第xx步/步骤”之类编号前缀（更精确，避免 .*? 吞掉 do）
    #    例如："第7步, do加热空锅" / "第7步do加热空锅" / "步骤, do..."
    left_part = re.sub(r'^\s*(第\s*\d+\s*步|\d+\s*步|步骤)\s*[,：:\s]*', '', left_part).strip()

    # 2) 去掉紧贴写法的 do：支持 "do加热" "DO加入" "do 加热"
    #    只删除位于开头的 do，避免误删正文中的 'do'
    left_part = re.sub(r'^\s*do\s*', '', left_part, flags=re.IGNORECASE).strip()

    do_text = left_part

    # 3) 统一输出标点：把英文逗号转回中文逗号（便于 UI/播报一致）
    do_text = do_text.replace(',', '，')

    # 4) 去掉可能残留的末尾标点
    do_text = do_text.strip(" ，,：:")

    # 4. 【兜底】防止切完变空
    if not do_text:
        do_text = "执行操作" if not is_continuous else "持续执行当前操作"

    return Step(
        is_continuous=is_continuous,
        recommended_duration=rec_duration,
        conditions=conditions,
        do_text=do_text
    )

def extract_first_float(condition: object, *, default: Optional[float] = None) -> float:
    """
    从 condition 文本中提取第一个浮点数（阈值）。
    例如：
      "温度大于等于95℃" -> 95.0
      "声音强度变化量大于10dB" -> 10.0
      ">= 180 ℃" -> 180.0

    若提取失败：
      - default 不为 None：返回 default
      - default 为 None：抛 ValueError
    """
    s = "" if condition is None else str(condition)

    num_re = re.compile(
        r"[-+]?(?:\d+\.\d+|\d+\.|\.\d+|\d+)(?:[eE][-+]?\d+)?"
    )
    m = num_re.search(s)
    if not m:
        if default is not None:
            return float(default)
        raise ValueError(f"Cannot find a number in condition: {s!r}")

    return float(m.group(0))
def _call_api_to_convert_condition(raw_str: str) -> dict:
    """
    【真实 LLM 解析】
    将复杂的自然语言条件字符串解析为标准 。
    处理复杂的与或非逻辑及括号嵌套。

    输入示例: "<视觉判定条件:[10s]A> and (<温度判定:[5s]B> or <声音:[2s]C>)"
    输出示例:
    {
        "sensors": [
            {"type": "visual", "condition": "A", "duration": 10.0},
            {"type": "temperature", "condition": "B", "duration": 5.0},
            {"type": "audio", "condition": "C", "duration": 2.0}
        ],
        "logic": "{0} and ({1} or {2})"
    }
    """

    # 1. 构造 Prompt (提示词)
    # 核心思想：告诉 LLM 把具体条件提取出来放进 list，
    # 然后在 logic 字符串里用 {0}, {1} 占位符还原原来的逻辑关系。
    prompt = f"""
你是一个严格的语义解析器。已知判定条件字符串：{raw_str}，请将该字符串解析为结构化结果，输出一个JSON对象。输出对象中包含以下要素：
sensors: 按原文从左到右出现顺序，列出所有子判定条件对象，每个对象包含 type、duration、condition。
logic: 保留原文逻辑关系结构，将每个子判定条件整体替换为 {{i}} 占位符，{{i}} 占位符表示第 i 个子判定条件。
以下为必须满足的约束：
1.判定条件字符串中包含一个或多个以尖括号'<>'包裹的子判定条件。你需要抽取该字符串中所有子判定条件，每个‘<>’中包含的所有内容（包含尖括号）视为一个子判定条件。
2.需要提取的子判定条件为：<视觉判定条件:******>、<温度判定条件:******>、<声音判定条件:******>。“******”代表实际需要判定的条件，且格式为：“[aaa s]时长内满足条件[bbb]”。aaa表示判定持续时间，单位为秒；bbb表示在判定持续时间需要判定的条件。视觉判定条件中是对视觉状态的自然语言描述， 温度判定条件中是大于等于某一具体温度数值， 声音判定条件中是声音强度变化量大于某一数值。
3.子判定条件对象中的type必须由子判定条件尖括号内的类别字段决定：出现“视觉判定条件”则 type="visual"，出现“温度判定条件”则 type="temperature"，出现“声音判定条件”则 type="audio",不得使用其它type值。
4.子判定条件对象中的 duration 必须从该子判定条件内部的时长段 "[aaa s]" 中提取数值 aaa。
5.子判定条件对象中的 condition 必须从该子判定条件内部的条件段 "满足条件[bbb]" 中提取 bbb 的内容，提取时不包含外层方括号；condition 必须保留原始语义信息与符号单位，不得做同义改写、数值换算、单位替换或语义概括
6.当同一个子判定条件中同时出现多个方括号段时，duration 只能来自表示时长的第一段 "[aaa s]"，而 condition 只能来自 "满足条件[bbb]" 这一段；不得把时长方括号内容误提取为 condition，也不得把条件方括内容误提取为 duration。
7.sensors列表必须严格按照原文中子判定条件出现的从左到右顺序排列，若出现重复type的子判定条件也必须重复保留在列表中，不允许去重、合并或重排。
8.logic必须以判定条件字符串为基础构建：将原文中每一个子判定条件整体替换为对应的占位符 {{i}}，其中 i 为该子判定条件在 sensors 列表中的0基索引，且logic中的每一个{{i}}都必须能在 sensors[i] 找到对应对象，反之 sensors 中每个对象也必须在 logic 中出现至少一次。
9.logic中必须保留判定条件字符串的逻辑连接词、逻辑结构和括号层级，不得改变组合优先级。
10.logic中占位符、括号与逻辑词之间必须且仅使用单个空格分隔，不输出多余换行、制表符或中文空格，以保证 logic 稳定可解析。
11.必须严格根据判定条件字符串进行输出，不得新增任何没有的子判定条件和逻辑关系。
"""

    try:
        # 2. 调用 API
        # print(f"[LLM] Parsing condition: {raw_str[:30]}...") # 调试用
        response_text = get_api.get_text_answer_json(prompt,"gpt-5.1","medium","low")

        # 3. 清洗数据 (防止模型有时候会加 ```json ... ```)
        clean_json_str = response_text.strip()
        if clean_json_str.startswith("```"):
            # 去掉开头 ```json 和结尾 ```
            clean_json_str = re.sub(r"^```json\s*", "", clean_json_str)
            clean_json_str = re.sub(r"^```\s*", "", clean_json_str)
            clean_json_str = re.sub(r"\s*```$", "", clean_json_str)

        # 4. 转为字典
        result_dict = json.loads(clean_json_str)

        # 简单校验字段是否存在
        if "sensors" not in result_dict or "logic" not in result_dict:
            raise ValueError("Missing 'sensors' or 'logic' fields")

        return result_dict

    except Exception as e:
        print(f"[Parser Error] LLM 解析条件失败: {e}")
        print(f"原始输入: {raw_str}")
        # 兜底返回：如果解析挂了，为了不崩程序，返回一个永远不满足的条件，或者直接报错
        # 这里返回一个空的，逻辑为 False (0)，意味着只能靠推荐时长结束
        return {
            "sensors": [],
            "logic": "0"
        }

def _now_tags():
    ts_float = time.time()
    t = time.localtime(ts_float)
    ms = int((ts_float - int(ts_float)) * 1000)
    ts_log = time.strftime("%Y-%m-%d %H:%M:%S", t) + f".{ms:03d}"
    ts_file = time.strftime("%Y%m%d_%H%M%S", t) + f"_{ms:03d}"
    return ts_float, ts_log, ts_file
