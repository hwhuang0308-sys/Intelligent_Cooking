import customtkinter as ctk
import pyttsx3
import threading
import time
import shared_data

class PopupAlert:
    def __init__(self, root_app):
        self.root = root_app
        self.popup_window = None
        self.label = None
        self.label_status = None
        self._hotkey_bound = False
        self._key_sink = None

        # NEW: small warning popup (non-blocking)
        self.small_window = None
        self.small_label = None

        # 全局锁：确保同一时间只有一个语音引擎在运行
        # 解决“第二句没声音”的核心机制
        self.speech_lock = threading.Lock()
        self.continue_event = threading.Event()
        self.quit_event = threading.Event()

        # NEW: global key bind (root) guard
        self._root_hotkeys_bound = False

    def _update_ui_task(self, text, status_text=None):
        """【GUI 任务】在主线程中更新文字（最大化 + 稳定热键焦点）"""

        # ✅ status_text=None 时不显示 "None"
        safe_status = "" if status_text is None else str(status_text)

        # ✅ 防御：如果你 __init__ 里忘了初始化
        if not hasattr(self, "_hotkey_bound"):
            self._hotkey_bound = False
        if not hasattr(self, "_key_sink"):
            self._key_sink = None

        # 1) 如果窗口不存在，创建窗口
        if self.popup_window is None or not self.popup_window.winfo_exists():
            self.popup_window = ctk.CTkToplevel(self.root)
            self.popup_window.title("系统通报")

            # ✅ 最大化（不覆盖任务栏）
            try:
                self.popup_window.state("zoomed")
            except Exception:
                # 兜底：铺满屏幕（不 fullscreen）
                w = self.popup_window.winfo_screenwidth()
                h = self.popup_window.winfo_screenheight()
                self.popup_window.geometry(f"{w}x{h}+0+0")

            # ✅ 置顶
            self.popup_window.attributes("-topmost", True)

            # ✅ 布局：两行，主内容占满，status 行不拉伸
            self.popup_window.grid_columnconfigure(0, weight=1)
            self.popup_window.grid_rowconfigure(0, weight=1)
            self.popup_window.grid_rowconfigure(1, weight=0)

            self.label = ctk.CTkLabel(
                self.popup_window,
                text=text,
                font=("SimHei", 200, "bold"),
                wraplength=1500,
                text_color="white",
                fg_color="transparent",
            )
            self.label.grid(row=0, column=0, sticky="nsew")

            self.label_status = ctk.CTkLabel(
                self.popup_window,
                text=safe_status,
                font=("SimHei", 60, "bold"),
                wraplength=1800,
                text_color="white",
                fg_color="transparent",
            )
            self.label_status.grid(row=1, column=0, sticky="s", pady=30)

            # ✅ 关键：隐藏的可聚焦控件，确保 Enter / q 立刻生效
            #   放屏幕外，不显示，但始终拿焦点
            self._key_sink = ctk.CTkEntry(self.popup_window, width=1, height=1)
            self._key_sink.place(x=-100, y=-100)

            # 处理窗口关闭
            def on_close():
                try:
                    self.popup_window.destroy()
                finally:
                    self.popup_window = None
                    self.label = None
                    self.label_status = None
                    self._hotkey_bound = False
                    self._key_sink = None

            self.popup_window.protocol("WM_DELETE_WINDOW", on_close)

            # ✅ 先绑定热键（绑定在 _key_sink 上最稳）
            self._bind_hotkeys_if_needed()

            # ✅ 再延迟抢焦点：解决“第一次弹出必须鼠标点一下”
            def _force_focus():
                try:
                    self.popup_window.lift()
                    self.popup_window.focus_force()
                    # 确保最大化状态保持
                    try:
                        self.popup_window.state("zoomed")
                    except Exception:
                        pass
                    self._key_sink.focus_set()
                except Exception:
                    pass

            # 80ms-120ms 都可以，给窗口管理器一点时间
            self.popup_window.after(80, _force_focus)

        else:
            # 2) 窗口已存在：更新文字 + status
            if self.label is not None:
                self.label.configure(text=text)

            if self.label_status is not None:
                self.label_status.configure(text=safe_status)

            # ✅ 每次更新都尽力保持最大化 + 置顶 + 焦点在 key_sink
            try:
                self.popup_window.lift()
                self.popup_window.attributes("-topmost", True)
                self.popup_window.state("zoomed")
                self.popup_window.focus_force()
                self.popup_window.grab_set()
            except Exception:
                pass

            try:
                if self._key_sink is not None and self._key_sink.winfo_exists():
                    self._key_sink.focus_set()
            except Exception:
                pass

    def update_status(self, status_str):
        """
        【对外接口】只更新底部状态栏文字，不改大标题，不发声
        """
        # 利用 after 调度到主线程安全更新
        self.root.after(0, self._inner_update_status, str(status_str))

    def _inner_update_status(self, status_str):
        """主线程执行的实际UI更新"""
        if self.label_status is not None:
            self.label_status.configure(text=status_str)
    def _bind_hotkeys_if_needed(self):
        # 不再用 _hotkey_bound 早退（每次都覆盖式绑定更稳）
        if self.popup_window is None or (not self.popup_window.winfo_exists()):
            return
        if self._key_sink is None or (not self._key_sink.winfo_exists()):
            return

        def on_enter(event=None):
            self.continue_event.set()
            return "break"  # ✅ 阻止事件继续传递

        def on_quit(event=None):
            # ✅ 退出优先级最高：先清掉 continue_event，避免被误当成 Enter
            try:
                self.continue_event.clear()
            except Exception:
                pass

            # 标记全局退出请求
            shared_data.COOKING_EXIT_REQUESTED.set()

            self.quit_event.set()
            return "break"

        # ✅ 绑定到 key_sink（最稳定：焦点通常在这里）
        self._key_sink.bind("<Return>", on_enter)
        self._key_sink.bind("<KP_Enter>", on_enter)
        self._key_sink.bind("<KeyPress-q>", on_quit)
        self._key_sink.bind("<KeyPress-Q>", on_quit)
        self._key_sink.bind("<Escape>", on_quit)
        self._key_sink.bind("<KeyRelease-Escape>", on_quit)

        # ✅ 也绑定到 popup_window（焦点丢失时兜底）
        self.popup_window.bind("<Return>", on_enter)
        self.popup_window.bind("<KP_Enter>", on_enter)
        self.popup_window.bind("<KeyPress-q>", on_quit)
        self.popup_window.bind("<KeyPress-Q>", on_quit)
        self.popup_window.bind("<Escape>", on_quit)
        self.popup_window.bind("<KeyRelease-Escape>", on_quit)

        self._hotkey_bound = True

        # ✅ 再补一层：绑定到 root（全局兜底，防 grab / other toplevel focus）
        try:
            if (not getattr(self, "_root_hotkeys_bound", False)) and self.root is not None:
                self.root.bind_all("<Escape>", on_quit)
                self.root.bind_all("<KeyRelease-Escape>", on_quit)
                self.root.bind_all("<KeyPress-q>", on_quit)
                self.root.bind_all("<KeyPress-Q>", on_quit)
                self._root_hotkeys_bound = True
        except Exception:
            pass

    def _speak_task(self, text):
        """
        【语音任务】在独立线程中运行
        每次都初始化新的 engine，规避状态卡死问题
        """
        # 获取锁：如果上一句还在读，这里会卡住等待，直到上一句释放锁
        with self.speech_lock:
            engine = None
            try:
                # print(f"正在播放: {text}") # 调试用

                # --- 核心修改：每次都全新初始化 ---
                engine = pyttsx3.init() #你这里每次调用都新建一个引擎，避免复用同一个引擎导致：状态残留（上一句播放结束但内部状态没释放）runAndWait 卡住第二句无声
                engine.setProperty('rate', 150)

                engine.say(text)
                engine.runAndWait()#开始执行语音队列里的所有朗读任务，并一直阻塞等待，直到全部读完才返回。

            except Exception as e:
                print(f"语音播放错误: {e}")
            finally:
                # 确保清理，防止占用驱动
                if engine:
                    try:
                        engine.stop()
                        del engine
                    except:
                        pass

                # 稍微休息一下，让音频驱动彻底释放
                time.sleep(0.5)

    def speak(self, text):
        """
        【对外接口】只播报语音，不改界面
        """
        if not text: return
        # 启动线程去读，复用你写好的逻辑
        t = threading.Thread(target=self._speak_task, args=(str(text),))
        t.start()
    def show(self, text, status_str=None):
        """
        【对外统一接口】
        子线程调用这个即可。
        """
        if not text: return
        text_str = str(text)
        # 1. 立即更新界面 (发送到主线程)
        self.root.after(0, self._update_ui_task, text_str, status_str)

        # 2. 启动一个临时线程去读这句话
        # 不会阻塞你的业务子线程，但会在内部排队播放
        t = threading.Thread(target=self._speak_task, args=(text_str,)) #若在短时间内出现多个线程，可能会出现争抢线程锁的问题，用 队列 Queue，并且只保留 一个固定语音线程：show(text) 只负责 queue.put(text)（按 put 的顺序入队）一个语音线程循环 queue.get()，按队列顺序播
        t.start()

    def close_popup(self):
        """线程安全：请求在主线程关闭弹窗，并释放 grab"""

        def _close():
            try:
                if self.popup_window is not None and self.popup_window.winfo_exists():
                    try:
                        self.popup_window.grab_release()
                    except Exception:
                        pass
                    try:
                        self.popup_window.destroy()
                    except Exception:
                        pass
            finally:
                self.popup_window = None
                self.label = None
                self.label_status = None
                self._key_sink = None
                self._hotkey_bound = False

        # 交给主线程执行
        self.root.after(0, _close)

    # =====================
    # Small warning popup
    # =====================
    def _show_small_warning_task(self, msg: str):
        """(GUI task) Create/update a small topmost window to show a short warning message."""
        if self.root is None or (hasattr(self.root, "winfo_exists") and not self.root.winfo_exists()):
            return

        text = "" if msg is None else str(msg)

        if self.small_window is None or not self.small_window.winfo_exists():
            self.small_window = ctk.CTkToplevel(self.root)
            self.small_window.title("报警")
            self.small_window.attributes("-topmost", True)

            # Small, non-modal window
            try:
                self.small_window.resizable(False, False)
            except Exception:
                pass

            # Put it near top-right
            try:
                sw = self.small_window.winfo_screenwidth()
                # a reasonable default size
                w, h = 520, 180
                x = max(0, sw - w - 40)
                y = 40
                self.small_window.geometry(f"{w}x{h}+{x}+{y}")
            except Exception:
                pass

            self.small_window.grid_columnconfigure(0, weight=1)
            self.small_window.grid_rowconfigure(0, weight=1)

            self.small_label = ctk.CTkLabel(
                self.small_window,
                text=text,
                font=("SimHei", 40, "bold"),
                wraplength=480,
                text_color="white",
            )
            self.small_label.grid(row=0, column=0, sticky="nsew", padx=20, pady=20)

            # ✅ 小窗也绑定退出热键：报警窗正在显示时，Esc 依然能退出做菜
            def _on_quit(event=None):
                try:
                    self.continue_event.clear()
                except Exception:
                    pass

                # 标记全局退出请求
                shared_data.COOKING_EXIT_REQUESTED.set()

                self.quit_event.set()
                return "break"

            try:
                self.small_window.bind("<Escape>", _on_quit)
                self.small_window.bind("<KeyRelease-Escape>", _on_quit)
                self.small_window.bind("<KeyPress-q>", _on_quit)
                self.small_window.bind("<KeyPress-Q>", _on_quit)
            except Exception:
                pass

            def on_close():
                try:
                    if self.small_window is not None and self.small_window.winfo_exists():
                        self.small_window.destroy()
                finally:
                    self.small_window = None
                    self.small_label = None

            self.small_window.protocol("WM_DELETE_WINDOW", on_close)
        else:
            if self.small_label is not None:
                self.small_label.configure(text=text)
            try:
                self.small_window.lift()
                self.small_window.attributes("-topmost", True)
            except Exception:
                pass

    def show_small_warning(self, msg: str) -> None:
        """Thread-safe: show/update the small warning window."""
        if getattr(self, "root", None) is None:
            return
        try:
            self.root.after(0, self._show_small_warning_task, str(msg))
        except Exception:
            # root may already be destroyed
            return

    def _close_small_warning_task(self) -> None:
        """(GUI task) Close the small warning window if it exists."""
        try:
            if self.small_window is not None and self.small_window.winfo_exists():
                self.small_window.destroy()
        except Exception:
            pass
        finally:
            self.small_window = None
            self.small_label = None

    def close_small_warning(self) -> None:
        """Thread-safe: close the small warning window."""
        if getattr(self, "root", None) is None:
            return
        try:
            self.root.after(0, self._close_small_warning_task)
        except Exception:
            return
