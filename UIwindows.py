# -*- coding: utf-8 -*-
import time

import customtkinter as ctk
import threading
import os
import re
from get_api import get_text_answer,get_text_answer_force_websearch,get_text_answer_stream,call_gemini
import cv2
import numpy as np
import cook_camera_new as cam
import cook_air as air
import cook_audio_new as audio
import cook_temperature_new as temp
import Daemon_thread as Daemon
import recipe_executor as executor
from utils import parse_recipe_into_global,read_py_file
from popup_windows import PopupAlert
import shared_data
from shared_data import (
    Step,
    GLOBAL_RECIPE_STEPS,
    STEP_LOCK,
    INVALID_DURATION_STR,
    INVALID_CONDITION_DATA
)
class RecipeGeneratorApp:
    def __init__(self):
        # ==================== 设置外观 ====================
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")

        # ==================== 初始化主窗口 ====================
        self.root = ctk.CTk()
        self.root.title("智能烹饪系统")
        self.root.geometry("1280x700")
        self.root.minsize(900, 650)
        self.alert = PopupAlert(self.root)

        # ✅ 全局兜底：在主窗口捕获 Esc，直接发起退出
        try:
            self.root.bind_all("<Escape>", self._on_global_escape)
            self.root.bind_all("<KeyRelease-Escape>", self._on_global_escape)
        except Exception:
            pass

        # ==================== 用户数据存储（结构化信息） ====================
        self.user_info = {
            "seasoning_list": "",  # 1. 调料
            "ingredient_list": "",  # 2. 食材
            "dining_preferences": "",  # 3. 用餐偏好
            "history_feedback": "",  # 4. 历史有价值信息（用户评价反馈）
            "health_status": "",  # 5. 个人健康情况
            "additional_comment": ""  # 6. 补充意见
        }

        # ==================== 历史反馈 ====================
        self.log_file = "feedback_history.txt"
        self.load_feedback_history()
        # ==================== 步骤配置 ====================
        self.current_step = 0
        self.step_prompts = [
            "请输入您现有的调料：\n",
            "请输入您现有的食材：\n",
            "请输入您的用餐偏好：\n",
            "请输入您的个人健康情况：",
            "您是否还有补充？\n"
        ]
        self.step_keys = ["seasoning_list", "ingredient_list", "dining_preferences", "health_status", "additional_comment"]

        # ==================== 生成的菜谱存储 ====================
        self.generated_dish_name = None
        self.std_recipe = None
        self.customed_recipe = None
        self.generated_recipe = None
        self.adjustments = None

        # 创建UI
        self.create_widgets()

        air.start_air_sensor(port="COM11", buffer_seconds=60, expected_hz=1.0)
        cam.start_camera(
            "rtsp://admin:admin@192.168.43.167:8554/live",
            threshold=1,  # 先用0保证一直有帧；用清晰度过滤再改回合适阈值
            maxlen=60
        )
        audio.start_audio_sensor(
            port="COM10",
            baudrate=9600,
            timeout=1.0,
            sample_period=0.5,  # 0.5s采一次
            buffer_seconds=60.0  # 保存最近60秒 => 120点
        )
        temp.start_temp_sensor(
            port="COM4",
            sample_period=0.5,  # 0.5秒采一次（=2Hz）
            buffer_seconds=60,  # 保存60秒
            timeout=2.0
        )
    def load_feedback_history(self):
        """从本地日志读取历史反馈"""
        try:
            if os.path.exists(self.log_file):
                with open(self.log_file, "r", encoding="utf-8") as f:
                    content = f.read().strip()
                    if content:
                        self.user_info["history_feedback"] = content
                        print(f"[日志] 已加载历史反馈记录")
            else:
                 # 文件不存在，创建新文件
                with open(self.log_file, "w", encoding="utf-8") as f:
                    pass
                print(f"[日志] 创建新反馈历史记录文件")
        except Exception as e:
            print(f"[日志] 读取历史反馈失败: {e}")

    def save_feedback_to_log(self, dish_name, feedback):
        """将反馈保存到本地日志"""
        try:
            import datetime
            timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            log_entry = f'*时间：{timestamp}；菜品：{dish_name}；反馈意见：{feedback}\n*'
            with open(self.log_file, "a", encoding="utf-8") as f:
                f.write(log_entry)

            print(f"[日志] 反馈已保存到 {self.log_file}")
        except Exception as e:
            print(f"[日志] 保存反馈失败: {e}")
    def create_widgets(self):
        """创建主界面组件"""

        # ========== 标题 ==========
        self.title_label = ctk.CTkLabel(
            self.root,
            text="🤖 智能烹饪系统",
            font=("Microsoft YaHei", 32, "bold")
        )
        self.title_label.pack(pady=25)

        # ========== 进度条区域 ==========
        # self.progress_frame = ctk.CTkFrame(self.root)
        # self.progress_frame.pack(fill="x", padx=50, pady=10)
        #
        # self.progress_label = ctk.CTkLabel(
        #     self.progress_frame,
        #     text="步骤 1/5",
        #     font=("Microsoft YaHei", 14)
        # )
        # self.progress_label.pack(side="left", padx=15)
        #
        # self.progress_bar = ctk.CTkProgressBar(self.progress_frame, width=400)
        # self.progress_bar.pack(side="left", fill="x", expand=True, padx=15)
        # self.progress_bar.set(0.2)

        # ========== 主内容框架 ==========
        self.main_frame = ctk.CTkFrame(self.root)
        self.main_frame.pack(fill="both", expand=True, padx=50, pady=20)

        # ========== 提示标签 ==========
        self.prompt_label = ctk.CTkLabel(
            self.main_frame,
            text=self.step_prompts[0],
            font=("Microsoft YaHei", 60),
            wraplength=750,
            justify="center"
        )
        self.prompt_label.pack(pady=35)

        # ========== 输入框 ==========
        self.input_textbox = ctk.CTkTextbox(
            self.main_frame,
            height=250,
            font=("Microsoft YaHei", 28),
            wrap="word"
        )
        self.input_textbox.pack(fill="x", padx=40, pady=15)

        # ========== 按钮框架 ==========
        self.button_frame = ctk.CTkFrame(self.main_frame, fg_color="transparent")
        self.button_frame.pack(pady=30,expand=False,fill="none")
        # ========== 添加：上一步按钮 ==========
        self.prev_button = ctk.CTkButton(
            self.button_frame,
            text="上一步",
            command=self.prev_step,
            width=120,
            height=40,
            font=("Microsoft YaHei", 16,'bold'),
            fg_color="#6c757d",
            hover_color="#5a6268",
            corner_radius=25,
            state="disabled"  # 第一步时禁用
        )

        # ========== 下一步按钮 ==========
        self.next_button = ctk.CTkButton(
            self.button_frame,
            text="下一步",
            command=self.next_step,
            width=120,
            height=40,
            font=("Microsoft YaHei", 16,'bold'),
            corner_radius=25
        )
        self.prev_button.pack(side="left", padx=5, expand=False, fill="none")
        self.next_button.pack(side="left", padx=5,expand=False,fill="none")


        # ========== 状态标签 ==========
        self.status_label = ctk.CTkLabel(
            self.root,
            text="",
            font=("Microsoft YaHei", 12),
            text_color="gray"
        )
        self.status_label.pack(pady=10)

        # ========== 绑定回车键 ==========
        self.input_textbox.bind("<Control-Return>", lambda e: self.next_step())

    def _on_global_escape(self, event=None):
        """UI 主线程兜底退出：无论当前焦点在哪，Esc 都应立刻结束做菜流程。"""
        try:
            shared_data.COOKING_EXIT_REQUESTED.set()
        except Exception:
            pass
        try:
            if hasattr(self, 'alert') and self.alert:
                self.alert.quit_event.set()
                self.alert.continue_event.clear()
                self.alert.close_small_warning()
                self.alert.close_popup()
        except Exception:
            pass
        return "break"

    def prev_step(self):
        """返回上一步"""
        if self.current_step <= 0:
            return

        # 更新用户数据
        current_input = self.input_textbox.get("1.0", "end-1c").strip()
        if self.current_step < len(self.step_keys):
            self.user_info[self.step_keys[self.current_step]] = current_input

        # 返回上一步
        self.current_step -= 1

        # 更新提示文字
        self.prompt_label.configure(text=self.step_prompts[self.current_step])

        # 恢复上一步的输入内容
        self.input_textbox.delete("1.0", "end")
        prev_content = self.user_info[self.step_keys[self.current_step]]
        if prev_content:
            self.input_textbox.insert("1.0", prev_content)

        # 按钮状态更新
        if self.current_step == 0:
            self.prev_button.configure(state="disabled")

        # 恢复"下一步"按钮文字
        if self.current_step == len(self.step_prompts) - 1:
            self.next_button.configure(text="确定")
        else:
            self.next_button.configure(text="下一步")
    def next_step(self):
        """处理下一步操作"""
        # 获取当前输入
        current_input = self.input_textbox.get("1.0", "end-1c").strip()

        # 保存输入到对应的字段
        if self.current_step < len(self.step_keys):
            self.user_info[self.step_keys[self.current_step]] = current_input

        # 清空输入框
        self.input_textbox.delete("1.0", "end")

        # 更新步骤
        self.current_step += 1
        # 检查是否完成所有输入步骤
        if self.current_step >= len(self.step_prompts):
            self.start_recipe_generation()
            return
        #恢复上一步按钮
        self.prev_button.configure(state="normal")
        # 更新提示文字
        self.prompt_label.configure(text=self.step_prompts[self.current_step])

        # ========== 添加这段：恢复之前保存的内容 ==========
        if self.current_step < len(self.step_keys):
            saved_content = self.user_info[self.step_keys[self.current_step]]
            if saved_content:
                self.input_textbox.insert("1.0", saved_content)

        # 更新进度条
        # progress = (self.current_step + 1) / len(self.step_prompts)
        # self.progress_bar.set(progress)
        # self.progress_label.configure(text=f"步骤 {self.current_step + 1}/{len(self.step_prompts)}")

        # 更新提示文字
        self.prompt_label.configure(text=self.step_prompts[self.current_step])

        # 最后一步改变按钮文字
        if self.current_step == len(self.step_prompts) - 1:
            self.next_button.configure(text="确定")

    def normalize_seasoning_list(self):
        """
        对 self.user_info['seasoning_list'] 做一次性规范化：
        - 统一分隔符
        - 去重
        - 补充清水 / 食用油（只补一次）
        """
        raw = self.user_info.get("seasoning_list") or ""

        # 1. 统一分隔符（把“以及、和、，、;”等都转成 '、'）
        for sep in ["以及", "和", "，", ",", ";", "；", "\n"]:
            raw = raw.replace(sep, "、")

        # 2. 拆分 + 去空
        items = [x.strip() for x in raw.split("、") if x.strip()]

        # 3. 去重（保持顺序）
        seen = set()
        normalized = []
        for x in items:
            if x not in seen:
                normalized.append(x)
                seen.add(x)

        # 4. 补充默认调料（只补一次）
        for extra in ["清水", "食用油"]:
            if extra not in seen:
                normalized.append(extra)

        # 5. 回写为干净列表字符串
        self.user_info["seasoning_list"] = "、".join(normalized)
    def format_user_info(self):
        formatted = f"""                           
现有调料：{self.user_info['seasoning_list']}；
现有食材：{self.user_info['ingredient_list']}；
用餐偏好：{self.user_info['dining_preferences'] or '无'}；
健康状况：{self.user_info['health_status'] or '健康，无特殊情况'}；
补充意见：{self.user_info['additional_comment'] or '无'}。
"""
        return formatted

    def start_recipe_generation(self):
        """开始菜谱生成流程"""
        # 禁用按钮防止重复点击
        self.next_button.configure(state="disabled")
        self.input_textbox.configure(state="disabled")
        self.prompt_label.configure(text="正在为您生成定制菜谱，请稍候...")
        self.status_label.configure(text="AI正在分析您的需求...")

        # 显示格式化的用户信息
        print("\n" + "=" * 60)
        print("用户输入的结构化信息：")
        print(self.format_user_info())
        print("=" * 60 + "\n")

        # 使用线程进行API调用，避免阻塞UI
        thread = threading.Thread(target=self.generate_recipe)
        thread.daemon = True #当主程序退出时，线程会立即终止
        thread.start()

    def generate_recipe(self):
        """生成菜谱（在后台线程中运行）- 这是核心循环"""
        self.normalize_seasoning_list()
        try:
            while True:
                # 格式化用户数据
                user_info = self.format_user_info()
                # ==================== 第一步：生成菜名 ====================
                dish_name_prompt = f"""
你是专业的烹饪营养师，也是中餐资深厨师。请在满足所有约束条件的前提下，选择一道可实际烹饪的菜品，并仅输出最终确定的菜名。
菜品选择必须同时满足以下约束：
输出内容仅包含菜名，不得输出任何解释、描述或附加文字。
输出菜名长度不得超过6个汉字。
该菜在烹饪过程中不得使用调料列表{self.user_info['seasoning_list']}以外的任何调料。
该菜在烹饪过程中不得使用食材列表{self.user_info['ingredient_list']}以外的任何食材。
用户当前健康状况为{self.user_info['health_status'] or '健康'}，在合理烹饪条件下，用户食用该菜后不得导致健康状况恶化。
用户用餐偏好为{self.user_info['dining_preferences'] or '无'}，在满足安全性与可行性的前提下，所选菜品应尽可能符合上述偏好。若偏好中明确指定某道菜或食材，而该菜或食材在常规做法下存在健康风险，必须另选符合所有约束的菜品。
用户的补充意见为{self.user_info['additional_comment'] or '无'}，在不违反前述约束的前提下，应尽可能予以满足。
    """

                # 更新状态
                self.root.after(0, lambda: self.status_label.configure(text="正在生成菜名..."))

                # ========== 调用API获取菜名 ==========
                dish_name = get_text_answer(dish_name_prompt,"gpt-5.2",reason="medium",verbosity="low")
                self.generated_dish_name = dish_name.strip()

                print(f"[生成的菜名] {self.generated_dish_name}")

                # ==================== 第二步：根据菜名生成详细菜谱 ====================
                std_recipe_prompt = f"""
你是专业的烹饪营养师，也是中餐资深厨师。请联网搜索{self.generated_dish_name}的菜谱，输出一份严格遵循输出模板格式的菜谱。
输出的菜谱必须同时满足以下约束：
输出内容仅包含以下三个板块：所需食材、所需调料、烹饪步骤，且顺序不得更改。
在所需食材与所需调料中，每一项必须给出明确数值 + 单位，不得出现“适量”“少许”等模糊表述，每项单独占一行。
在烹饪步骤中，按执行顺序逐行给出具体操作步骤，每一步单独占一行。
只输出菜谱本体，不得输出任何引用标注、链接和解释性语句。
输出结果必须严格遵循以下格式与规范，输出模板如下：
所需食材：
（食材名）（重量）
所需调料：
（调料名）（容积或重量)
烹饪步骤：
（步骤一）
（步骤二）
    """

                # 更新状态
                self.root.after(0, lambda: self.status_label.configure(text="正在生成详细菜谱..."))

                # ========== 调用API获取完整菜谱 ==========
                generated_std_recipe = get_text_answer_force_websearch(std_recipe_prompt,"gpt-5.2",reason="low",verbosity="low")
                generated_std_recipe = re.sub(r'\n\s*\n', '\n', generated_std_recipe)
                self.std_recipe = generated_std_recipe

                print(f"[生成的标准化菜谱]\n{self.std_recipe}")
                customed_recipe_prompt = f"""
你是专业的烹饪营养师，也是中餐资深厨师。以下将给出{self.generated_dish_name}的菜谱，你来结合用户个性化需求与输出格式规范化需求来修改，输出修改后的菜谱。
修改前的菜谱如下：{self.std_recipe}
修改后的菜谱必须同时满足以下全部约束条件：
修改后的菜谱中涉及的调料不得超出调料列表{self.user_info['seasoning_list']}。
修改后的菜谱中涉及的食材不得超出食材列表{self.user_info['ingredient_list']}。
若修改前菜谱中涉及的调料名出现与上述调料列表中含义相同但名称不同的别名（如：食盐=盐，白砂糖=糖，植物油=食用油等），必须优先选择并替换为调料列表中已存在的那一个名称，禁止输出任何不在调料列表中的别名。
若修改前菜谱中涉及的食材名出现与上述食材列表中含义相同但名称不同的别名（如：番茄=西红柿，马铃薯=土豆等），必须优先选择并替换为食材列表中已存在的那一个名称，禁止输出任何不在食材列表中的别名。
用户当前健康状况为{self.user_info['health_status'] or '健康'}，修改后的菜谱必须保证用户吃完按该谱烹饪的菜后健康状况不会恶化，尤其是当修改前的菜谱会导致用户健康状况恶化时更要注意此事。
修改后的菜谱的烹饪流程应安全、合理，符合常规中餐烹饪逻辑与操作顺序。
用户用餐偏好为{self.user_info['dining_preferences'] or '无'}，修改后的菜谱尽可能符合上述偏好。
修改后的菜谱在不违反前述约束的前提下尽可能满足用户补充意见：{self.user_info['additional_comment'] or '无'}。
输出内容仅包含以下三个板块：所需食材、所需调料、烹饪步骤，且顺序不得更改。
在所需食材与所需调料中，每一项必须给出明确数值 + 单位，不得出现“适量”“少许”等模糊表述，每项单独占一行。
在烹饪步骤中，按执行顺序逐行给出具体操作步骤，每一步单独占一行。
输出结果必须严格遵循以下格式与规范，不得包含任何额外说明性文字。输出模板如下：
所需食材：
（食材名）（重量）
所需调料：
（调料名）（容积或重量)
烹饪步骤：
（步骤一）
（步骤二）
"""
                adjustments_prompt = f"""
原菜谱{self.std_recipe}根据以下用户信息{user_info}进行定制化调整后，得到调整后的新菜谱为{self.customed_recipe}。请生成一段不超过300个汉字的段落，必须结合用户信息如实说明我们从原菜谱到新菜谱的改动是出于哪些用户情况的考虑，表述应尽可能言简意赅。如果新菜谱相比原菜谱未作出任何改动，则只需要输出字符串‘定制化菜谱与标准菜谱之间无本质区别’即可。
                """
                self.customed_recipe = get_text_answer(customed_recipe_prompt,"gpt-5.2",reason="medium",verbosity="low")
                self.adjustments = get_text_answer(adjustments_prompt, "gpt-5.2", reason="medium",verbosity="low")
                # self.adjustments = call_gemini(adjustments_prompt)
                self.customed_recipe = re.sub(r'\n\s*\n', '\n', self.customed_recipe)
                print(f"[生成的个性化菜谱]\n{self.customed_recipe}")
                ingredients_check_prompt = f"""
已知菜谱：{self.customed_recipe}和食材清单：{self.user_info['ingredient_list']}，我的要求如下：
菜谱明确使用了食材清单外的食材（你必须根据菜谱中的所需食材和所需调料字段界定，什么是食材以及什么是调料）
菜谱明确存在某食材用量超过食材清单现有量（若食材清单中某食材未提及现有量则视为该食材现有量充足）
上述两条要求都不满足，输出1，任意一条要求满足输出0，不得输出任何其他字符。
"""

                seasonings_check_prompt = f"""
已知菜谱：{self.customed_recipe}和调料清单：{self.user_info['seasoning_list']}，我的要求如下：
菜谱明确使用了调料清单外的食材（你必须根据菜谱中的所需食材和所需调料字段界定，什么是食材以及什么是调料）
菜谱明确存在某调料用量超过调料清单现有量（若调料清单中某调料未提及现有量则视为该调料现有量充足）
上述两条要求都不满足，输出1，任意一条要求满足输出0，不得输出任何其他字符。
"""
                health_check_prompt = f"""
已知菜谱：{self.customed_recipe}和用户健康状况：{self.user_info['health_status'] or '健康'}，我的要求如下：
用户食用执行该菜谱烹饪的菜后健康状况明确会发生恶化。
上述要求符合，输出0，否则输出1，不得输出任何其他字符。
"""
                check_1 = get_text_answer(ingredients_check_prompt,"gpt-5.2",reason="high",verbosity="low").strip()
                print(check_1)
                if check_1 != "1":
                    continue
                check_2 = get_text_answer(seasonings_check_prompt, "gpt-5.2", reason="high",verbosity="low").strip()
                print(check_2)
                if check_2 != "1":
                    continue
                check_3 = get_text_answer(health_check_prompt, "gpt-5.2", reason="high",verbosity="low").strip()
                print(check_3)
                if check_3 != "1":
                    continue
                self.generated_recipe =self.customed_recipe
                if self.generated_recipe:
                    break
            # 在主线程中显示结果对话框

            self.root.after(0, self.show_recipe_dialog)

        except Exception as e:
            error_msg = f"生成失败：{str(e)}"
            print(f"[错误] {error_msg}")
            self.root.after(0, lambda: self.status_label.configure(text=error_msg))
            self.root.after(0, lambda: self.next_button.configure(state="normal"))
            self.root.after(0, lambda: self.input_textbox.configure(state="normal"))

    def show_recipe_dialog(self):
        """显示菜谱确认对话框（子窗口）"""
        self.status_label.configure(text="✅ 菜谱生成完成！请确认是否接受")

        # ==================== 创建子窗口 ====================
        self.recipe_dialog = ctk.CTkToplevel(self.root)
        self.recipe_dialog.title("定制菜谱")
        self.recipe_dialog.geometry("1060x650")
        self.recipe_dialog.minsize(750, 650)
        self.recipe_dialog.transient(self.root)  # 附属窗口
        self.recipe_dialog.grab_set()  # 模态窗口

        # 居中显示
        self.recipe_dialog.update_idletasks()
        x = (self.recipe_dialog.winfo_screenwidth() - 750) // 2
        y = (self.recipe_dialog.winfo_screenheight() - 650) // 2
        self.recipe_dialog.geometry(f"750x650+{x}+{y}")

        # ========== 菜名标签 ==========
        dish_label = ctk.CTkLabel(
            self.recipe_dialog,
            text=f"🍳 推荐菜品：{self.generated_dish_name}",
            font=("Microsoft YaHei", 24, "bold")
        )
        dish_label.pack(side="top", pady=20)

        # ========== 询问标签 ==========
        ask_label = ctk.CTkLabel(
            self.recipe_dialog,
            text="📋 这是为您定制的菜谱，是否接受？",
            font=("Microsoft YaHei", 14),
            text_color="lightblue"
        )
        ask_label.pack(side="top", pady=5)

        # ==================== 1) 底部：按钮区域（固定在最下面） ====================
        button_frame = ctk.CTkFrame(self.recipe_dialog, fg_color="transparent")
        button_frame.pack(side="bottom", pady=25, fill="x", expand=False)

        button_frame.configure(height=70)
        button_frame.pack_propagate(False)

        button_inner = ctk.CTkFrame(button_frame, fg_color="transparent")
        button_inner.pack(anchor="center")

        yes_button = ctk.CTkButton(
            button_inner,  # ✅ 放到 button_inner 里，才能居中
            text="✅ Yes - 接受菜谱",
            command=self.on_accept_recipe,
            width=180,
            height=45,
            font=("Microsoft YaHei", 15, "bold"),
            fg_color="#28a745",
            hover_color="#218838",
            corner_radius=10
        )
        yes_button.pack(side="left", padx=20)

        no_button = ctk.CTkButton(
            button_inner,  # ✅ 放到 button_inner 里，才能居中
            text="❌ No - 重新生成",
            command=self.on_reject_recipe,
            width=180,
            height=45,
            font=("Microsoft YaHei", 15, "bold"),
            fg_color="#dc3545",
            hover_color="#c82333",
            corner_radius=10
        )
        no_button.pack(side="left", padx=20)

        # ==================== 2) 中间：内容区域（占据剩余空间） ====================
        recipe_frame = ctk.CTkFrame(self.recipe_dialog)
        recipe_frame.pack(side="top", fill="both", expand=True, padx=30, pady=15)

        # ✅ 关键：先把“固定高度”的 difference 区域放到底部固定住
        # 这样上面的 recipe_textbox 才会去吃“剩余空间”，而不会把 difference 挤没
        diff_frame = ctk.CTkFrame(recipe_frame, fg_color="transparent")
        diff_frame.pack(side="bottom", fill="x", expand=False, padx=5, pady=(0, 5))

        difference_textbox = ctk.CTkTextbox(
            diff_frame,
            font=("Microsoft YaHei", 13),
            wrap="word",
            height=120
        )
        difference_textbox.pack(fill="x", expand=False)
        difference_textbox.insert("1.0", self.adjustments)
        difference_textbox.configure(state="disabled")

        # 上面的菜谱框：吃掉剩余空间、可拉伸
        recipe_textbox = ctk.CTkTextbox(
            recipe_frame,
            font=("Microsoft YaHei", 13),
            wrap="word"
        )
        recipe_textbox.pack(side="top", fill="both", expand=True, padx=5, pady=(5, 10))
        recipe_textbox.insert("1.0", self.customed_recipe)
        recipe_textbox.configure(state="disabled")

    def on_accept_recipe(self):
        """用户点击Yes - 接受菜谱，循环结束"""
        self.recipe_dialog.destroy()

        # ==================== 保存最终菜谱（供后续使用） ====================
        self.final_result = {
            "dish_name": self.generated_dish_name,
            "recipe_line": self.generated_recipe,
            "user_info": self.user_info.copy()
        }

        # 显示最终结果
        self.show_final_result()

        # 打印保存的菜谱（供后续功能使用）
        # print("\n" + "🎉" * 30)
        # print("用户接受的最终菜谱已保存：")
        # print(f"菜名：{self.final_result['dish_name']}")
        # print("菜谱内容：")
        # print(self.final_result['recipe_line'])
        # print("🎉" * 30 + "\n")

    def on_reject_recipe(self):
        """用户点击No - 拒绝菜谱，进入重新生成流程"""
        self.recipe_dialog.destroy()
        self.show_rejection_dialog()

    def show_rejection_dialog(self):
        #显示拒绝理由输入对话框
        # ==================== 创建拒绝理由窗口 ====================
        self.rejection_dialog = ctk.CTkToplevel(self.root)
        self.rejection_dialog.title("请输入拒绝理由")
        self.rejection_dialog.geometry("650x550")
        self.rejection_dialog.minsize(550, 450)
        self.rejection_dialog.transient(self.root)
        self.rejection_dialog.grab_set()

        # 确保窗口在最前面
        self.rejection_dialog.lift()
        self.rejection_dialog.focus_force()

        # 居中显示
        self.rejection_dialog.update_idletasks()
        x = (self.rejection_dialog.winfo_screenwidth() - 650) // 2
        y = (self.rejection_dialog.winfo_screenheight() - 550) // 2
        self.rejection_dialog.geometry(f"650x550+{x}+{y}")

        # ========== 主框架 ==========
        main_frame = ctk.CTkFrame(self.rejection_dialog)
        main_frame.pack(fill="both", expand=True, padx=20, pady=20)

        # ==================== 1. 顶部：提示标签 ====================
        label = ctk.CTkLabel(
            main_frame,
            text=f"您不满意「{self.generated_dish_name}」\n\n请告诉我们您拒绝的理由或者更多要求，我们将为您推荐更合适的菜品。",
            font=("Microsoft YaHei", 14),
            wraplength=480,
            justify="left"
        )
        label.pack(side="top", pady=(10, 10), fill="x")

        # ==================== 2. 底部：按钮区域 (优先固定位置) ====================
        # 注意：先 pack 底部，保证无论窗口多小，按钮都在最下面
        button_frame = ctk.CTkFrame(main_frame, fg_color="transparent")
        button_frame.pack(side="bottom", pady=(10, 10), fill="x", expand=False)

        # 创建一个内部容器用于 Grid 布局居中
        button_inner = ctk.CTkFrame(button_frame, fg_color="transparent")
        button_inner.pack(anchor="center")

        # ========== 提交按钮 ==========
        submit_button = ctk.CTkButton(
            button_inner,
            text="提交并重新生成",
            command=self.on_submit_rejection,
            width=200,
            height=45,
            font=("Microsoft YaHei", 15, "bold"),
            fg_color="#28a745",
            hover_color="#218838",
            corner_radius=10
        )
        submit_button.grid(row=0, column=0, padx=10)

        # ========== 取消按钮 ==========
        cancel_button = ctk.CTkButton(
            button_inner,
            text="❌ 取消",
            command=self.on_cancel_rejection,
            width=120,
            height=45,
            font=("Microsoft YaHei", 15, 'bold'),
            fg_color="#6c757d",
            hover_color="#5a6268",
            corner_radius=10
        )
        cancel_button.grid(row=0, column=1, padx=10)

        # ==================== 3. 底部上方：提示文字 ====================
        # 放在按钮上面 (side=bottom 会堆叠在之前 pack 的元素上方)
        tip_label = ctk.CTkLabel(
            main_frame,
            text="💡 提示：按 Ctrl+Enter 或点击按钮提交，按 Esc 取消",
            font=("Microsoft YaHei", 11),
            text_color="gray"
        )
        tip_label.pack(side="bottom", pady=(0, 5))

        # ==================== 4. 中间：输入框 (填充剩余所有空间) ====================
        self.rejection_textbox = ctk.CTkTextbox(
            main_frame,
            # height=120, # 不需要强制高度，因为它会自动填充
            font=("Microsoft YaHei", 13),
            wrap="word"
        )
        # fill="both", expand=True 是关键，它会占据 Label 和 Buttons 剩下的所有空间
        self.rejection_textbox.pack(side="top", fill="both", expand=True, padx=20, pady=5)

        # ========== 绑定快捷键 ==========
        self.rejection_textbox.bind("<Control-Return>", lambda e: self.on_submit_rejection())
        self.rejection_dialog.bind("<Escape>", lambda e: self.on_cancel_rejection())

        # 设置焦点到输入框
        self.rejection_textbox.focus_set()

    def on_cancel_rejection(self):
        """取消拒绝，返回菜谱确认对话框"""
        self.rejection_dialog.destroy()
        # 重新显示菜谱对话框
        self.show_recipe_dialog()

    def on_submit_rejection(self):
        """提交拒绝理由，将信息补入第6点补充信息，重新进入生成循环"""
        rejection_reason = self.rejection_textbox.get("1.0", "end-1c").strip()

        # ==================== 将拒绝理由添加到补充信息（第6点） ====================
        if rejection_reason:
            self.user_info[
                "additional_comment"] += f"\n[用户拒绝反馈] 用户不接受菜品「{self.generated_dish_name}」的理由是{rejection_reason}"
        else:
            self.user_info[
                "additional_comment"] += f"\n[用户拒绝反馈] 用户不满意「{self.generated_dish_name}」，请推荐其他不同的菜品"

        print(f"[用户拒绝] 菜品：{self.generated_dish_name}，理由：{rejection_reason or '未说明'}")

        # 关闭对话框
        self.rejection_dialog.destroy()

        # 更新主界面状态
        self.prompt_label.configure(text="🔄 正在根据您的反馈重新生成菜谱...")
        self.status_label.configure(text="AI正在重新分析您的需求...")

        # ==================== 重新进入生成循环 ====================
        thread = threading.Thread(target=self.generate_recipe)
        thread.daemon = True
        thread.start()

    def show_final_result(self):
            """显示最终结果"""
            # 更新主界面文字
            self.prompt_label.configure(text="您的定制菜谱已生成完成！")
            self.status_label.configure(text="正在将菜谱转化为程序谱...")

            # 1. 如果存在下一步按钮，先销毁（防止重复）
            if hasattr(self, 'next_button') and self.next_button.winfo_exists():
                self.next_button.destroy()

            # ==================== 核心修改：调整布局逻辑 ====================

#             # 2. 强制 输入框 占据剩余的所有空间 (变身“弹簧”)
#             # 注意：这里使用 pack_configure 来修改已存在的 pack 属性，而不是重新 pack
#             self.input_textbox.pack_configure(fill="both", expand=True)
#
#             # 3. 强制 按钮框架 固定在底部
#             # side="bottom" 确保它沉底，fill="x" 确保它横向铺满（为了让内部内容居中）
#             # expand=False 禁止它抢占纵向空间
#             self.button_frame.pack_configure(side="bottom", fill="x", expand=False, pady=20)
#
#             # ==================== 更新内容 ====================
#
#             # 在输入框中显示最终菜谱
#             self.input_textbox.configure(state="normal")
#             self.input_textbox.delete("1.0", "end")
#             final_display = f"""
# 🎊 最终选定菜谱
# {self.generated_recipe}
#     """
#             self.input_textbox.insert("1.0", final_display)
#             self.input_textbox.configure(state="disabled")
#             # self.input_textbox.configure(height=350) # 删除这行，让 layout 自动决定高度

            # 清空按钮框架中的所有子组件 (清除旧按钮)
            for widget in self.button_frame.winfo_children():
                widget.destroy()

            # # ==================== 创建按钮内部容器 ====================
            # # 技巧：在 button_frame 里再放一个 frame，用来专门 grid 按钮
            # # 这样 button_frame 负责铺满底部，inner_frame 负责在中间仅仅包裹两个按钮
            # inner_btn_frame = ctk.CTkFrame(self.button_frame, fg_color="transparent")
            # inner_btn_frame.pack(anchor="center")
            #
            # # ==================== 创建两个并排的按钮 ====================
            # # 注意：master 变成了 inner_btn_frame
            #
            # # 反馈按钮
            # self.feedback_button = ctk.CTkButton(
            #     inner_btn_frame,  # 放入内部容器
            #     text="📝 提交反馈",
            #     command=self.show_feedback_dialog,
            #     width=160,
            #     height=42,
            #     font=("Microsoft YaHei", 14, 'bold'),
            #     fg_color="#28a745",
            #     hover_color="#218838",
            #     corner_radius=15
            # )
            # self.feedback_button.grid(row=0, column=0, padx=10)
            #
            # # 重新开始按钮
            # self.restart_button = ctk.CTkButton(
            #     inner_btn_frame,  # 放入内部容器
            #     text="🔄 重新开始",
            #     command=self.restart,
            #     width=160,
            #     height=42,
            #     font=("Microsoft YaHei", 14, 'bold'),
            #     # fg_color="#3B82F6", # 保持你的原有配色注释
            #     # hover_color="#2563EB",
            #     corner_radius=15
            # )
            # self.restart_button.grid(row=0, column=1, padx=10)

            thread = threading.Thread(target=self.generate_cooking_structure_stream)
            thread.daemon = True
            thread.start()
    def generate_cooking_structure_stream(self):
        try:
            programmatic_prompt = f"""
请将我提供的菜谱转换为具备条件触发机制的结构化操作流程，统一采用格式“第x步，do yyy，until zzz”，其中x是步骤编号，yyy是当前烹饪操作，zzz是当前烹饪操作终止的条件，当前操作终止后执行x+1步，生成操作流程必须满足的约束如下：
yyy的烹饪操作分为持续动作和瞬时动作，持续动作会在当前步骤中循环执行，直至满足until中的zzz条件则终止；瞬时动作执行完成后自动跳转下一步（所有一次性动作必须单独作为瞬时动作写成一步），不需要生成until中的zzz条件，即持续动作语句须以“until ...”结尾，瞬时动作为无法通过现有传感器（视觉传感器、温度传感器和声音传感器）判定结束时刻的一次性动作。视觉传感器和温度传感器位于锅具上方，只能对锅中食材和锅具状态进行判定（例如无法对腌制、切菜、搅匀调汁等在锅具外的食材处理动作进行判定，此类动作必须为瞬时动作），声音传感器位于锅具旁，可以获得锅具声音分贝强度变化。若能通过以上现有传感器判定动作何时停止，该动作即为持续动作，否则为瞬时动作。
调节火力（如调大火、中火、小火和关火）必须明确定义为瞬时动作，并单独作为一步列出。严禁将火力调节与烹饪动作合并，例如禁止输出“大火翻炒直至食材表面变黄”，任何涉及加热食材动作前一步必须为调火动作。
对于yyy中添加调味品或食用油的操作，yyy中的添加操作应明确标注所用调味品或食用油的具体用量，以便于操作。
生成的结构化操作流程中，除瞬时操作中的添加调料动作可连续执行多个调料的添加，其他烹饪操作中只能包含一个最小可独立执行的烹饪动作，禁止在任何步骤中（即yyy中）同时执行两个或以上的操作（包括准备工作），即使为同属准备类操作如“切菜”、“切肉”等，也必须各自单独作为一步操作进行拆分，禁止添加任何解释性或背景性文字（如“备用”、“为了……”，“至……”“将……”等无实质意义的文字）。整体流程应简洁规范、结构一致、表达精准，确保可直接用于自动化控制系统的执行逻辑中。
对于持续动作中的zzz必须满足约束如下：
zzz由总判定条件和推荐时长组成，总判定条件和推荐时长之间通过逗号分隔，其中总判定条件由子判定条件组成，子判定条件为：<视觉判定条件:******>、<温度判定条件:******>、<声音判定条件:******>。
各子判定条件的表达应严格遵循上述模板，“******”代表实际需要判定的条件，为保证判定条件使用的稳定性，各子判定条件中的“******”应严格按照以下模板对aaa和bbb进行填充：“[aaa s]时长内满足条件[bbb]”。aaa表示判定持续时间，单位为秒；bbb表示在判定持续时间需要判定的条件。
对于[bbb]，视觉判定条件：bbb 为对视觉状态的自然语言描述（仅限锅内可见状态），禁止臆造不可见信息。声音判定条件：bbb 只能是“声音强度变化量大于等于X dB”。温度判定条件：bbb 必须明确“对象 + 阈值”，且只允许“温度大于等于某一具体温度值”的形式；对象只能从以下三类中选择其一：
A) 空锅温度大于等于 T ℃
B) 食用油温度大于等于 T ℃
C) 锅中食材表面温度大于等于 T ℃
示例：
<温度判定条件:[5 s]时长内满足条件[空锅温度大于等于180℃]>
<温度判定条件:[3 s]时长内满足条件[食用油温度大于等于160℃]>
<温度判定条件:[10 s]时长内满足条件[锅中食材表面温度大于等于75℃]>。
对于实际的某一持续动作，根据实际的判定需求，选择适当数量的子判定条件进行组合，当总判定条件包含两个或以上子判定条件时，各子判定条件之间必须且只能通过“and”或“or”逻辑运算符连接，其中“and”表示逻辑运算中的“且”关系，“or”表示逻辑运算中的“或”关系，若逻辑关系较为复杂，可添加括号组成更为复杂的总判定条件,总判定条件与推荐时长之间通过逗号分隔。
需要注意的是，使用的传感器有且仅有视觉传感器（视觉图像）、温度传感器（温度数值）和声音传感器（声音强度数值）这三个，这意味着子判定条件的生成应符合传感器的实际测量条件，即仅可以从视觉、温度数值、声音强度分贝值（不可以出现如：爆裂声，滋滋声等声音强度传感器无法测量的特征）来构建子判定条件。传感器数值范围约束（必须遵守，防止生成离谱阈值）：
温度阈值 T（℃）必须在 [0, 160] 范围内。
A) 对“空锅温度”：建议 T 在 [120, 130] 之间选取。
B) 对“食用油温度”：建议 T 在 [140, 150] 之间选取。
C) 对“锅中食材表面温度”：建议 T 在 [60, 70] 之间选取（具体按食材与目标熟度设定）。
声音阈值 X（dB）必须在 [3, 8] 范围内。
视觉判定条件不输出数值，只描述锅内可见食材状态，且必须只能是锅中食材的形状发生明显变化的状态。
所有子判定条件应主要以温度判定条件作为主判定条件构建，可以从视觉或声音判定选择其一作为辅助判定条件。若选择视觉作为辅助判定条件，则该判定条件不可干扰主判定条件判定，即如果主判定条件若判定成功则总判定条件也应为判定成功，当前持续步骤完成。
在整个烹饪流程中，视觉判定条件和声音判定条件的累计出现次数均须大于等于1，但均不超过3次。
推荐时长的输出应符合以下模板:<推荐时长: **s>。输出的推荐时长的应为准确的时间长度避免模糊表达（如：约50s）。
接下来，请以严格遵循上述要求，将菜谱：{self.customed_recipe}转化为结构化操作流程。
"""
            programmatic_recipe = get_text_answer_stream(programmatic_prompt,"gpt-5.2","medium","low").strip()
            programmatic_recipe = re.sub(r'\n\s*\n', '\n', programmatic_recipe)
            print(f"[生成的程序谱]:\n{programmatic_recipe}")
            def update_ui():
                self.status_label.configure(text="正在准备烹饪流程...")
            self.root.after(0, update_ui)
            action_prompt = f"""
请把下方的程序谱，改写为严格遵循给定烹饪动作模板中动作固定表述格式的动作谱。
程序谱：{programmatic_recipe}
改写成动作谱时必须遵循的原则：
对于包含until的步骤，只允许修改do与until之间的内容，until以及其后的所有内容必须逐字符保持不变，不得增加、删除和修改任何字符；对于不包含until的步骤，只修改do之后的内容。
改写每一步时，从烹饪动作模板列表中选择与原步骤动作语义最一致的动词，根据该动词的动作固定表述格式进行修改。（对于焖煮等加热锅中食材但无需具体动作的烹饪步骤统一归为加热；对于翻匀等加热同时需要对锅中食材进行具体动作的烹饪步骤统一归为炒。）
动作谱中不得出现任何不在烹饪动作模板中的动词。
若原步骤包含多个动作，必须拆成多步，每步只包含一个模板动作。
若原步骤同时添加多种调料，则必须拆分为多个步骤，每次只添加一种调料。
动作谱中每一步的动作表述必须以模板动词开头，且整句必须严格符合该动词在模板中的动作固定表述格式，不得增删字段和调换顺序。
烹饪动作模板中动作固定表述格式定义的尖括号（如`<A>`、`<B>`）仅代表参数槽位，在生成最终动作表述时，必须去除这些尖括号，直接将参数内容与模板文字拼接。
将原步骤动作表述按烹饪动作模板中的动作固定表述格式改写并填充槽位参数时，只能基于该步原文及其相邻前后步骤中出现的信息进行推理补全，补全结果必须能由这些文本信息逻辑推出且对应关系明确。
动作谱中，在步骤中涉及锅对象时，只能使用‘锅’或‘空锅’，严禁使用‘炒锅’一词。
仅输出动作谱，不输出任何解释、总结或额外文字。
烹饪动作模板：
清洗；清洗<A>；A=食材（需要被清洗的食材对象）
切；切<A>成<B>；A=食材（被切的食材对象），B=形状/规格（切后的食材形状或规格）
剔除；剔除<A>从<B>；A=部位（要去除的食材部位），B=食材（需要被去除某部位的食材）
剖开；剖开<A>；A=食材（需要被剖开的食材对象）
沥干；沥干<A>；A=食材（需要沥干的食材对象）
抓拌；抓拌<A>；A=食材（需要被抓拌的食材对象）
搅匀；搅匀<A>；A=液体（需要被搅匀的调味汁）
静置；静置<A>；A=食材（需要被静置食材对象）
加热；加热<A>；A=食材（需要被加热的锅中食材对象）
炒；炒<A>；A=食材（被炒的食材对象）
炸；炸<A>；A=食材（被炸的食材对象）
加入；加入<A>到<B>；A=食材/调料（加入的食材或调料），B=食材/容器（加入到的目标食材或容器）
取出；取出<A>从<B>到<C>；A=食材（被取出的对象），B=容器（取出食材的来源容器），C=位置（取出的食材去向位置）
调火；调火至<A>；A=火力（烹饪需要用到的火力，有大火、中火、小火、关闭）
            """

            action_recipe = get_text_answer_stream(action_prompt, "gpt-5.2", "medium", "low").strip()
            action_recipe = re.sub(r'\n\s*\n', '\n', action_recipe)
            self.input_textbox.configure(state="normal")
            print(f"[生成的动作谱]:\n{action_recipe}")

              # 回主线程启动
            threading.Thread(target=self._parse, args=(action_recipe,), daemon=True).start()

        except Exception as e:
            error_msg = f"生成失败：{str(e)}"
            print(f"[错误] {error_msg}")
            self.root.after(0, lambda: self.status_label.configure(text=error_msg))

    def _parse(self,action_text: str):
        parse_recipe_into_global(action_text)
        print(GLOBAL_RECIPE_STEPS)
        self.root.after(0, self.start_cooking)
    def start_cooking(self):
        #plan.txt 路径（你自己放哪都行）
        #启动守护线程
        self._abn_thread = threading.Thread(
            target=Daemon.abnormal_monitor_loop,
            kwargs=dict(interval_sec=2.0, cooldown_sec=10.0),
            daemon=True
        )
        self._abn_thread.start()

        #启动执行器线程
        self._exec_thread = threading.Thread(
            target=executor.executor_main,
            args=(self.alert,),
            daemon=True
        )
        self._exec_thread.start()

        # ✅ 重要：启动监工，做菜结束后切换到“烹饪结束 + 退出按钮”界面
        self.root.after(500, self.check_cooking_thread)

    def check_cooking_thread(self):
        """
        【监工函数】
        周期性检查执行器线程是否存活。
        """
        if self._exec_thread.is_alive():
            # 还在跑，500ms 后再来看
            self.root.after(200, self.check_cooking_thread)
        else:
            # 线程死了（说明做菜结束了，或者按Q退出了）
            try:
                Daemon.stop_all_event.set()
            except Exception:
                pass

            # ✅ 先尝试关闭弹窗，确保无干扰
            if hasattr(self, 'alert') and self.alert:
                self.alert.close_popup()
                self.alert.close_small_warning()

            # ✅ 立即显示结束界面
            try:
                self.show_cooking_finished_ui()
            except Exception as e:
                print(f"[UI Error] show_cooking_finished_ui failed: {e}")

            # ✅ 启动完全独立的线程去慢慢关闭传感器
            # 使用 threading.Thread 确保它立刻 detach，不阻塞当前 UI 帧
            def _cleanup_task():
                # 稍微 sleep 一下让 UI 先渲染出来（可选，但推荐）
                time.sleep(0.1)

                try:
                    temp.close_temperature_sensor()
                    print(" -> 温度传感器已关闭")
                except Exception: pass

                try:
                    audio.close_audio_sensor()
                    print(" -> 声音传感器已关闭")
                except Exception: pass

                try:
                    cam.close_camera()
                    print(" -> 摄像头已关闭")
                except Exception: pass

                try:
                    air.close_air_sensor()
                    print(" -> 空气质量传感器已关闭")
                except Exception: pass

            t = threading.Thread(target=_cleanup_task, daemon=True)
            t.start()


    def show_cooking_finished_ui(self):
        """
        【结束界面】
        1. 关闭弹窗
        2. 清空主界面
        3. 显示结束语
        """
        print("[UI] 正在切换至结束界面...")

        # ---------------- 1. 关闭系统弹窗 ----------------
        # 这一步非常关键，防止弹窗卡在最前面关不掉
        if hasattr(self, 'alert') and self.alert:
            self.alert.close_popup()

        # ---------------- 2. 清空主界面 ----------------
        # 暴力清空 main_frame 里的所有东西 (输入框、PromptLabel 等)
        # 这也会顺带把里面的 button_frame 销毁掉 (因为它也是 main_frame 的子控件)
        try:
            for widget in self.main_frame.winfo_children():
                widget.destroy()
        except Exception:
            pass

        # ---------------- 3. 重建按钮容器 ----------------
        # 因为上面把 main_frame 清空了，button_frame 肯定没了，所以这里直接重建
        # 不需要判断 exists 了，直接建个新的
        try:
            self.button_frame = ctk.CTkFrame(self.main_frame, fg_color="transparent")
            self.button_frame.pack(side="bottom", pady=30, fill="none")
        except Exception:
            pass

        # ---------------- 4. 绘制"烹饪结束"界面 ----------------

        # 更新顶部状态栏
        try:
            self.status_label.configure(text="✨ 烹饪结束，设备已安全待机")
        except Exception:
            pass

        # 显示大字结束语
        finish_label = ctk.CTkLabel(
            self.main_frame,
            text="烹饪结束",
            font=("Microsoft YaHei", 120, "bold"),
            text_color="#28a745"
        )
        finish_label.pack(expand=True, pady=(80, 20))

        # 显示副标题
        sub_label = ctk.CTkLabel(
            self.main_frame,
            text="",
            font=("Microsoft YaHei", 16),
            text_color="gray"
        )
        sub_label.pack(pady=10)

        # 添加退出按钮
        exit_btn = ctk.CTkButton(
            self.button_frame,
            text="退出程序",
            command=self.root.quit,
            width=200,
            height=50,
            font=("Microsoft YaHei", 18, "bold"),
            fg_color="#dc3545",
            hover_color="#c82333",
            corner_radius=25
        )
        exit_btn.pack(pady=20)



    def show_feedback_dialog(self):
        """显示反馈对话框（用于收集历史有价值信息）"""
        feedback_dialog = ctk.CTkToplevel(self.root)
        feedback_dialog.title("📝 菜谱反馈")
        feedback_dialog.geometry("650x550")
        feedback_dialog.minsize(650, 550)
        feedback_dialog.transient(self.root)
        feedback_dialog.grab_set()

        # 居中
        feedback_dialog.update_idletasks()
        x = (feedback_dialog.winfo_screenwidth() - 650) // 2
        y = (feedback_dialog.winfo_screenheight() - 550) // 2
        feedback_dialog.geometry(f"650x550+{x}+{y}")

        main_frame = ctk.CTkFrame(feedback_dialog)
        main_frame.pack(fill="both", expand=True, padx=20, pady=20)

        label = ctk.CTkLabel(
            main_frame,
            text=f"请对「{self.generated_dish_name}」这道菜进行评价：\n（您的反馈将作为历史有价值信息保存）",
            font=("Microsoft YaHei", 14)
        )
        label.pack(pady=20)

        feedback_textbox = ctk.CTkTextbox(main_frame, height=150)
        feedback_textbox.pack(fill="both", expand=True, padx=20, pady=10)
        feedback_textbox.focus_set()

        def submit_feedback():
            # 获取输入内容
            feedback = feedback_textbox.get("1.0", "end-1c").strip()
            if feedback:
                # 保存到历史反馈信息（第4点）
                self.save_feedback_to_log(self.generated_dish_name, feedback)
                print(f"\n✅ 已保存用户反馈：{feedback}")
            feedback_dialog.destroy()

        # ==================== 按钮框架 (布局核心修复) ====================
        # 1. 创建按钮框架：side="bottom" 确保它沉底
        button_frame = ctk.CTkFrame(main_frame, fg_color="transparent")
        button_frame.pack(side="bottom", fill="x", pady=15)

        # 2. 内部容器：用于居中固定大小
        inner_btn_frame = ctk.CTkFrame(button_frame, fg_color="transparent")
        inner_btn_frame.pack(anchor="center")

        # 3. 提交按钮 (Grid布局)
        submit_btn = ctk.CTkButton(
            inner_btn_frame,
            text="✅ 提交反馈",
            command=submit_feedback,
            width=150,
            height=40,
            font=("Microsoft YaHei", 14, 'bold'),
            fg_color="#28a745",
            hover_color="#218838"
        )
        submit_btn.grid(row=0, column=0, padx=10)

        # 4. 取消按钮 (Grid布局)
        cancel_btn = ctk.CTkButton(
            inner_btn_frame,
            text="取消",
            command=feedback_dialog.destroy,
            width=100,
            height=40,
            font=("Microsoft YaHei", 14, 'bold'),
            fg_color="#6c757d",
            hover_color="#5a6268"
        )
        cancel_btn.grid(row=0, column=1, padx=10)

        # ==================== 关键修复步骤 ====================
        # 强制调整布局顺序：先把输入框“拿下来”，再“放回去”
        # 这样能保证 按钮(Button) 在布局队列中排在 输入框(Textbox) 之前
        # 从而确保无论窗口怎么缩小，按钮都优先保留显示空间，不会被挤消失
        feedback_textbox.pack_forget()
        feedback_textbox.pack(side="top", fill="both", expand=True, padx=20, pady=10)

        # ==================== 绑定快捷键 ====================
        feedback_textbox.bind("<Control-Return>", lambda e: submit_feedback())
        feedback_dialog.bind("<Escape>", lambda e: feedback_dialog.destroy())

    def restart(self):
        """重新开始整个流程"""
        # 保留历史反馈信息
        history = self.user_info.get("history_feedback", "")

        # 重置用户数据
        self.user_info = {
            "seasoning_list": "",
            "ingredient_list": "",
            "dining_preferences": "",
            "history_feedback": "",  # 保留历史反馈
            "health_status": "",
            "additional_comment": ""
        }
        # 重新读取日志文件
        self.load_feedback_history()

        self.current_step = 0
        self.std_recipe = None
        self.generated_recipe = None
        self.generated_dish_name = None

        # ==================== 重置UI内容 ====================
        self.prompt_label.configure(text=self.step_prompts[0])
        self.status_label.configure(text="")

        # ==================== 核心修复：还原布局属性 ====================
        # 1. 还原输入框：
        # 在 show_final_result 中我们设为了 fill="both", expand=True
        # 这里要改回初始状态：fill="x", expand=False, 并且恢复固定高度
        self.input_textbox.configure(state="normal", height=250)
        self.input_textbox.delete("1.0", "end")
        # 强制改回布局 (关键步骤)
        self.input_textbox.pack_configure(fill="x", expand=False, side="top", pady=15)

        # 2. 还原按钮框架：
        # 在 show_final_result 中我们设为了 side="bottom", fill="x"
        # 这里要改回初始状态：side="top", fill="none" (这样它才会根据内容收缩并居中)
        self.button_frame.pack_configure(side="top", fill="none", expand=False, pady=30)

        # ==================== 清空并重建按钮 ====================
        # 销毁当前按钮框架中的所有按钮 (包括 inner_btn_frame)
        for widget in self.button_frame.winfo_children():
            widget.destroy()

        # 重新创建按钮
        # 上一步按钮
        self.prev_button = ctk.CTkButton(
            self.button_frame,
            text="上一步",
            command=self.prev_step,
            width=120,
            height=40,
            font=("Microsoft YaHei", 16,'bold'),
            fg_color="#6c757d",
            hover_color="#5a6268",
            corner_radius=25,
            state="disabled"
        )

        # 下一步按钮
        self.next_button = ctk.CTkButton(
            self.button_frame,
            text="下一步",
            command=self.next_step,
            width=120,
            height=40,
            font=("Microsoft YaHei", 16,'bold'),
            corner_radius=25
        )

        # 建议这里改为 side="left"，这样两个按钮会紧挨着居中显示
        # 因为 button_frame 已经是 fill="none" (自动收缩) 且默认居中了
        self.prev_button.pack(side="left", padx=5)
        self.next_button.pack(side="left", padx=5)

    def run(self):
        """运行应用"""
        self.root.mainloop()


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║                                  主程序入口                                   ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

if __name__ == "__main__":
    app = RecipeGeneratorApp()
    app.run()
