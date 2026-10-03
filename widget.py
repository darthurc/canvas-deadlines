# -*- coding: utf-8 -*-
"""
widget.py —— 桌面挂件（主界面）

一个贴着桌面的小窗口：无标题栏、可拖动、半透明，抬头就能看到最近几个 deadline
和实时倒计时。倒计时是纯本地算术，每秒走一次，不联网。

为什么挂件不自己联网：
    网络会卡、会超时、会抛异常。让挂件只读 out\\deadlines.json，
    它就永远不会因为网络问题白屏或卡死。刷新是 fetch.py 独立进程的事，
    挂件只用 subprocess 把它叫起来，然后等文件变化。

用法：
    python widget.py              启动
    python widget.py --reset      把窗口位置恢复成默认（拖到屏幕外找不回来时用）
"""

from __future__ import annotations

import argparse
import os
import socket
import subprocess
import sys
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import messagebox
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import store
import timeutil

# 单实例锁用的本地端口。挂件开两份没意义，还会互相覆盖窗口位置。
LOCK_PORT = 8791

# 进度条的参考窗口：7 天。也就是「还有一周」时条是空的，马上要交时是满的。
BAR_WINDOW_HOURS = 168.0

# 剩余时间少于这么多小时的任务，整行放大 —— 快到点了就该更显眼。
# 默认 72 小时 = 3 天。可以用 config.json 的 display.emphasize_hours 改，设 0 关掉。
EMPHASIZE_HOURS = 72.0
EMPHASIZE_BUMP = 2          # 放大几个字号

# 一键清理之后还能点「撤销」的秒数
UNDO_SECONDS = 12

# 窗口还没画出来时（winfo_height() 只会返回 1），拿它当高度的估值来夹取位置。
# 比真实高度略大一点没关系 —— 夹取宁可是保守的。
FALLBACK_HEIGHT = 420

LIGHT = {
    "bg": "#faf9f7", "surface": "#ffffff", "fg": "#24211e", "muted": "#6f6862",
    "line": "#e3ded8", "accent": "#b4552d", "track": "#eeebe6",
    "past": "#c0392b", "urgent": "#c0392b", "soon": "#c2610f", "week": "#a07800",
    "later": "#9a9188", "done": "#2e7d52",
    "warn_bg": "#faf2d8", "warn_fg": "#8a6a00",
    "bad_bg": "#fbe4e1", "bad_fg": "#a5281c",
}
DARK = {
    "bg": "#1a1816", "surface": "#232120", "fg": "#ece8e3", "muted": "#a09890",
    "line": "#383432", "accent": "#e08a5f", "track": "#302d2b",
    "past": "#e57365", "urgent": "#e57365", "soon": "#e09a52", "week": "#d9c06a",
    "later": "#8f877f", "done": "#6fbf94",
    "warn_bg": "#332f1c", "warn_fg": "#d9c06a",
    "bad_bg": "#3a2320", "bad_fg": "#e57365",
}


# --------------------------------------------------------------------------
# 平台适配
# --------------------------------------------------------------------------
def enable_dpi_awareness() -> None:
    """
    高分屏上不声明 DPI 感知的话，Windows 会把窗口位图拉伸，字全是糊的。
    必须在创建 Tk() 之前调用，晚了不生效。
    """
    if sys.platform != "win32":
        return
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)   # PROCESS_SYSTEM_DPI_AWARE
    except Exception:
        try:
            import ctypes
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def system_prefers_dark() -> bool:
    """
    系统是不是深色模式。读不到就当浅色（浅色在任何系统上都还能看，
    猜错成深色反而可能出现深底黑字）。
    """
    if sys.platform == "darwin":
        # macOS 没有注册表，规矩是「有 AppleInterfaceStyle 这个键就是深色」——
        # 浅色时这个键**不存在**（不是等于 Light），所以不能靠它的值判断。
        try:
            proc = subprocess.run(
                ["defaults", "read", "-g", "AppleInterfaceStyle"],
                capture_output=True, text=True, timeout=5,
            )
            return proc.returncode == 0 and "dark" in (proc.stdout or "").lower()
        except Exception:
            return False

    if sys.platform != "win32":
        return False
    try:
        import winreg
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
        )
        with key:
            value, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
            return value == 0
    except Exception:
        return False


def suppress_dock_icon() -> None:
    """
    把进程改成「附属模式」，让它不出现在程序坞里、也不抢焦点。

    为什么需要：挂件是个贴在桌面上的小条，但 macOS 会把它当成一个正常的
    Python 程序 —— 程序坞里多一个 Python 图标，启动时还要抢一次焦点
    （正打着字突然跳走）。这两件事都很难看。

    做法：通过 ctypes 直接调 Objective-C 运行时把 NSApplication 的
    activationPolicy 设成 1（NSApplicationActivationPolicyAccessory）。
    不用装 pyobjc —— 那又是一个 pip 依赖，而「Mac 上零 pip 依赖」是
    这条路线的核心约束。

    **这段我（写代码的这个 AI）没法验证** —— 手上没有 Mac，GUI 这半边
    只能靠实机试。所以整段包在 try 里：失败了就退回一个普通的 Python 进程，
    挂件照常能用，只是程序坞里多个图标。不影响功能。

    必须在 Tk() 之后调用（Tk 初始化时才会创建 NSApplication）。
    """
    if sys.platform != "darwin":
        return
    try:
        import ctypes
        import ctypes.util

        lib = ctypes.util.find_library("objc")
        if not lib:
            return
        objc = ctypes.cdll.LoadLibrary(lib)

        objc.objc_getClass.restype = ctypes.c_void_p
        objc.objc_getClass.argtypes = [ctypes.c_char_p]
        objc.sel_registerName.restype = ctypes.c_void_p
        objc.sel_registerName.argtypes = [ctypes.c_char_p]
        objc.objc_msgSend.restype = ctypes.c_void_p
        objc.objc_msgSend.argtypes = [ctypes.c_void_p, ctypes.c_void_p]

        cls = objc.objc_getClass(b"NSApplication")
        if not cls:
            return
        app = objc.objc_msgSend(cls, objc.sel_registerName(b"sharedApplication"))
        if not app:
            return

        # setActivationPolicy: 收一个 NSInteger，得换一份 argtypes 再调
        objc.objc_msgSend.argtypes = [
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_long,
        ]
        objc.objc_msgSend(
            app, objc.sel_registerName(b"setActivationPolicy:"), 1,
        )
    except Exception:
        pass


def pick_font(root: tk.Tk) -> tuple[str, str]:
    """
    挑一个实际存在的字体。不能硬写字体名 —— 系统上没有的话 tkinter 会静默
    退回默认字体，中文可能变成方块。
    """
    from tkinter import font as tkfont
    families = set(tkfont.families(root))
    for name in ("Microsoft YaHei UI", "Microsoft YaHei", "PingFang SC", "SimHei"):
        if name in families:
            ui = name
            break
    else:
        ui = "TkDefaultFont"
    # Menlo 是 macOS 上的等宽首选，Consolas 是 Windows 上的。
    # 两张表都列着，各自挑得到各自的，成一个平台无关的列表。
    for name in ("Consolas", "Cascadia Mono", "Menlo", "SF Mono", "Courier New"):
        if name in families:
            mono = name
            break
    else:
        mono = ui
    return ui, mono


def display_width(text: str) -> int:
    """估算显示宽度：中日韩字符占两个英文字符的宽度。"""
    return sum(2 if ord(ch) > 0x2E7F else 1 for ch in text)


def ellipsize(text: str, limit: int) -> str:
    """按显示宽度截断并加省略号。挂件窄，长作业名必须截，不然会把窗口撑开。"""
    text = (text or "").strip()
    if display_width(text) <= limit:
        return text
    out, used = [], 0
    for ch in text:
        w = 2 if ord(ch) > 0x2E7F else 1
        if used + w > limit - 1:
            break
        out.append(ch)
        used += w
    return "".join(out) + "…"


# --------------------------------------------------------------------------
# 挂件
# --------------------------------------------------------------------------
class Widget:
    def __init__(self, root: tk.Tk, cfg: dict[str, Any], reset: bool = False):
        self.root = root
        self.cfg = cfg
        self.window_cfg = cfg.get("window", {})
        self.display_cfg = cfg.get("display", {})
        self.state = store.load_widget_state()

        self.theme_name = self.window_cfg.get("theme") or "auto"
        self.c = self._palette()

        self.ui_font, self.mono_font = pick_font(root)
        self.base_size = int(self.window_cfg.get("font_size") or 10)

        self.payload: dict[str, Any] = {}
        self.mtime: float | None = None
        self.tickers: list[tuple[tk.Label, datetime]] = []
        self.show_all = bool(self.state.get("show_all", False))
        self.show_submitted = bool(
            self.state.get("show_submitted", self.display_cfg.get("show_submitted", False))
        )
        self.topmost = bool(self.state.get("topmost", self.window_cfg.get("topmost", False)))
        self.fetching = False
        self._fetch_process = None
        self._fetch_error = None
        self._fetch_initial_mtime = None
        self._drag_origin: tuple[int, int] | None = None

        # 上一次「一键清理」清了哪些任务 —— 用来做撤销。
        # None 表示当前没什么可撤销的，清理条也就不用显示撤销状态。
        self._undo_tasks: list[dict[str, Any]] | None = None
        self._undo_job: str | None = None

        self._setup_window(reset)
        self._build_chrome()
        self._build_menu()

        self.reload(force=True)
        self._tick()
        self._poll_file()

    # ------------------------------------------------------------ 外观
    def _palette(self) -> dict[str, str]:
        if self.theme_name == "dark":
            return DARK
        if self.theme_name == "light":
            return LIGHT
        return DARK if system_prefers_dark() else LIGHT

    def _setup_window(self, reset: bool) -> None:
        root = self.root
        width = int(self.window_cfg.get("width") or 310)

        root.overrideredirect(True)                    # 去掉标题栏，像个挂件
        root.configure(bg=self.c["line"])              # 外层当 1px 边框用
        try:
            root.attributes("-alpha", float(self.window_cfg.get("alpha") or 0.96))
        except tk.TclError:
            pass
        self._apply_topmost()

        pos = None if reset else self.state.get("position")
        if isinstance(pos, (list, tuple)) and len(pos) == 2:
            x, y = self._clamp_to_screen(int(pos[0]), int(pos[1]), width)
        else:
            x, y = self._default_position(width)
        root.geometry(f"{width}x1+{x}+{y}")

    def _apply_topmost(self) -> None:
        try:
            self.root.attributes("-topmost", self.topmost)
        except tk.TclError:
            pass

    def _work_area(self) -> tuple[int, int, int, int]:
        """屏幕的**可用区域**（去掉任务栏），返回 (左, 上, 右, 下)。"""
        try:
            import ctypes
            class _Rect(ctypes.Structure):
                _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                            ("right", ctypes.c_long), ("bottom", ctypes.c_long)]
            rect = _Rect()
            if ctypes.windll.user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(rect), 0):
                if rect.right > rect.left and rect.bottom > rect.top:
                    return rect.left, rect.top, rect.right, rect.bottom
        except Exception:
            # 非 Windows（Mac 版源码也跑这一份）没有 ctypes.windll，直接走下面的兜底
            pass
        return 0, 0, self.root.winfo_screenwidth(), self.root.winfo_screenheight()

    def _screen_size(self) -> tuple[int, int]:
        """可用区域的宽高（不含任务栏）。"""
        left, top, right, bottom = self._work_area()
        return right - left, bottom - top

    def _widget_size(self, width: int | None = None,
                     height: int | None = None) -> tuple[int, int]:
        """
        挂件实际占的宽高。窗口还没画出来时 winfo_* 只会给 1，
        那就退回配置里的宽度 / 一个保守的高度估值。
        """
        w = width or self.root.winfo_width()
        h = height or self.root.winfo_height()
        if not w or w <= 1:
            w = int(self.window_cfg.get("width") or 310)
        if not h or h <= 1:
            h = FALLBACK_HEIGHT
        return int(w), int(h)

    def _default_position(self, width: int) -> tuple[int, int]:
        left, top, right, bottom = self._work_area()
        _, h = self._widget_size(width, None)
        return right - width - 28, bottom - h - 40

    def _clamp_to_screen(self, x: int, y: int, width: int | None = None,
                         height: int | None = None) -> tuple[int, int]:
        """
        把窗口夹回可用区域（去掉任务栏）以内 —— 整个挂件都要看得见。

        没有这层的话：外接屏拔掉、或者把挂件拖到屏幕外再关掉，
        下次启动它会从那个非法坐标还原，你会以为它根本没启动。
        """
        left, top, right, bottom = self._work_area()
        w, h = self._widget_size(width, height)

        max_x = right - w
        max_y = bottom - h

        # 可用区域比挂件还小时（极端情况），退化成左上角，别算出比 left 还小的值
        x = left if max_x < left else max(left, min(x, max_x))
        y = top if max_y < top else max(top, min(y, max_y))
        return x, y

    # ------------------------------------------------------------ 骨架
    def _build_chrome(self) -> None:
        c, size = self.c, self.base_size

        self.body = tk.Frame(self.root, bg=c["bg"])
        self.body.pack(fill="both", expand=True, padx=1, pady=1)

        # ---- 标题栏（也是拖动把手）----
        self.header = tk.Frame(self.body, bg=c["bg"])
        self.header.pack(fill="x", padx=10, pady=(8, 0))

        self.title_label = tk.Label(
            self.header, text="Canvas 倒计时", bg=c["bg"], fg=c["fg"],
            font=(self.ui_font, size, "bold"), anchor="w",
        )
        self.title_label.pack(side="left")

        self.btn_menu = tk.Label(
            self.header, text="≡", bg=c["bg"], fg=c["muted"],
            font=(self.ui_font, size + 4), cursor="hand2",
        )
        self.btn_menu.pack(side="right", padx=(6, 0))
        self.btn_menu.bind("<Button-1>", self._show_menu)

        self.btn_refresh = tk.Label(
            self.header, text="↻", bg=c["bg"], fg=c["muted"],
            font=(self.ui_font, size + 3), cursor="hand2",
        )
        self.btn_refresh.pack(side="right")
        self.btn_refresh.bind("<Button-1>", lambda _e: self.start_fetch())

        for widget in (self.header, self.title_label):
            widget.bind("<Button-1>", self._drag_start)
            widget.bind("<B1-Motion>", self._drag_move)
            widget.bind("<ButtonRelease-1>", self._drag_end)
            widget.bind("<Button-3>", self._show_menu)

        # ---- 状态栏：数据新鲜度 / 出错提示 ----
        self.status = tk.Label(
            self.body, text="", bg=c["bg"], fg=c["muted"], anchor="w", justify="left",
            font=(self.ui_font, max(7, size - 2)), wraplength=int(self.window_cfg.get("width") or 310) - 24,
        )
        self.status.pack(fill="x", padx=10, pady=(1, 6))
        self.status.bind("<Button-3>", self._show_menu)

        self.sep = tk.Frame(self.body, bg=c["line"], height=1)
        self.sep.pack(fill="x", padx=10)

        # ---- 过期一键清理条 ----
        # 只在「有过期没标的」时候出现（见 _refresh_cleanup_bar）。
        # 为什么要有这个东西：日历订阅拿不到提交状态，所以刚装上的头几天
        # 会有一堆过期作业堆在最上面，把真正要看的挤没了。这事只在右键
        # 菜单里的话你根本发现不了 —— 看不见的入口等于没有。
        self.cleanup_bar = tk.Frame(self.body, bg=c["warn_bg"], cursor="hand2")
        self.cleanup_label = tk.Label(
            self.cleanup_bar, text="", bg=c["warn_bg"], fg=c["warn_fg"],
            anchor="w", justify="left", cursor="hand2",
            font=(self.ui_font, max(7, size - 1)),
            wraplength=int(self.window_cfg.get("width") or 310) - 30,
        )
        self.cleanup_label.pack(fill="x", padx=8, pady=4)
        for widget in (self.cleanup_bar, self.cleanup_label):
            widget.bind("<Button-1>", self._on_cleanup_click)
        # 注意：这里不 pack。要不要显示由 _refresh_cleanup_bar 决定。

        # ---- 任务区 ----
        self.tasks_frame = tk.Frame(self.body, bg=c["bg"])
        self.tasks_frame.pack(fill="both", expand=True, padx=10, pady=(6, 10))
        self.tasks_frame.bind("<Button-3>", self._show_menu)
        self.tasks_frame.bind("<Button-1>", self._drag_start)
        self.tasks_frame.bind("<B1-Motion>", self._drag_move)
        self.tasks_frame.bind("<ButtonRelease-1>", self._drag_end)

    # ------------------------------------------------------------ 菜单
    def _build_menu(self) -> None:
        c = self.c
        self.menu = tk.Menu(
            self.root, tearoff=0,
            bg=c["surface"], fg=c["fg"], activebackground=c["accent"],
            activeforeground="#ffffff", bd=0, font=(self.ui_font, self.base_size - 1),
        )
        self.menu.add_command(label="立即刷新", command=self.start_fetch)
        self.menu.add_separator()

        self.var_top = tk.BooleanVar(value=self.topmost)
        self.menu.add_checkbutton(
            label="窗口置顶", variable=self.var_top, command=self._toggle_topmost,
        )
        self.var_all = tk.BooleanVar(value=self.show_all)
        self.menu.add_checkbutton(
            label="显示全部（不只前几个）", variable=self.var_all, command=self._toggle_all,
        )
        self.var_sub = tk.BooleanVar(value=self.show_submitted)
        self.menu.add_checkbutton(
            label="显示已提交的", variable=self.var_sub, command=self._toggle_submitted,
        )

        # 这一项的文字和可用状态每次弹菜单前现算（见 _refresh_bulk_item），
        # 因为「有几个过期的」是随着时间自己变的
        self.menu.add_separator()
        self.menu.add_command(label="把已过期的都标记为已交", command=self._mark_all_overdue)
        self._bulk_index = self.menu.index("end")
        self.menu.add_separator()

        self.menu.add_command(label="打开完整列表（网页）", command=self._open_html)
        self.menu.add_command(label="打开数据文件夹", command=self._open_folder)
        self.menu.add_command(
            label="打开配置文件", command=lambda: self._open_path(store.CONFIG_FILE),
        )

        # 开机自启。以前是两个 .bat，但打包成 exe 之后没有 .bat 可双击了，
        # 而任务计划里写死的是「这个程序 + 子命令」（见 store.child_command），
        # 所以干脆挂到菜单里 —— 这样收程序的人根本不用碰命令行。
        autostart_menu = tk.Menu(
            self.menu, tearoff=0,
            bg=c["surface"], fg=c["fg"], activebackground=c["accent"],
            activeforeground="#ffffff", bd=0, font=(self.ui_font, self.base_size - 1),
        )
        autostart_menu.add_command(
            label="开机自动启动（每天自动刷新）",
            command=lambda: self._spawn_autostart("install"),
        )
        autostart_menu.add_command(
            label="取消开机自动启动",
            command=lambda: self._spawn_autostart("uninstall"),
        )
        self.menu.add_cascade(label="开机自启…", menu=autostart_menu)

        self.menu.add_separator()
        self.menu.add_command(label="退出", command=self.root.destroy)

    def _show_menu(self, event: tk.Event) -> None:
        self._refresh_bulk_item()
        try:
            self.menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.menu.grab_release()

    # -------------------------------------------------- 批量清理已过期
    def _overdue_unmarked(self) -> list[dict[str, Any]]:
        """
        列出「已经过期、但还没标成已交」的任务。

        只算挂件本来会显示的类型 —— 上课时间表不算，它没有「交」这回事。
        """
        hidden = set(self.display_cfg.get("hide_types") or [])
        now = timeutil.now_utc()
        out: list[dict[str, Any]] = []
        for task in self.payload.get("tasks") or []:
            if (task.get("type") or "") in hidden:
                continue
            if not task.get("needs_action") or task.get("manual_submitted"):
                continue
            due = timeutil.parse_iso(task.get("due_utc"))
            if due is not None and due < now:
                out.append(task)
        return out

    def _refresh_bulk_item(self) -> None:
        """弹菜单前把「把已过期的都标记为已交」那项的文字和灰/亮更新一下。"""
        if not hasattr(self, "_bulk_index"):
            return
        count = len(self._overdue_unmarked())
        self.menu.entryconfigure(
            self._bulk_index,
            label=f"把已过期的都标记为已交（{count} 个）" if count else "把已过期的都标记为已交",
            state="normal" if count else "disabled",
        )

    def _mark_all_overdue(self) -> None:
        """
        把已经过期的任务一次性标成「已交」。

        为什么需要这个：日历订阅拿不到提交状态，所以刚装上的时候，
        过去这些天交过的作业**全都**显示成「已过期没交」，
        而且它们按时间排序全挤在最上面，把真正要看的那个挤没了。
        一个个右键点太烦，而烦的结果就是你不再看这个挂件 —— 那它就白做了。

        只动**已经过期**的，未来的一个都不碰：这个工具的存在意义就是
        盯住还没到期的那些，让一个批量按钮误伤它们是不可接受的。

        **故意不弹确认框。** 清理这件事本身很安全（只碰过期的，而且随时能
        撤销），但每点一次都拦一个框，用两天就会烦到不点它，然后那堆过期
        任务永远挡在最前面。不弹框的代价是可能点错，所以留了两条退路：
        12 秒内再点一下那条就撤销（_undo_cleanup），之后还能右键单行取消。
        """
        targets = self._overdue_unmarked()
        if not targets:
            return

        done: list[dict[str, Any]] = []
        for task in targets:
            key = task.get("key")
            if not key:
                continue
            store.set_marked(key, True, title=task.get("title") or "")
            # 跟 _toggle_marked 保持一致：submitted 是 True（你标的），不是 None
            task["submitted"] = True
            task["manual_submitted"] = True
            task["needs_action"] = False
            done.append(task)

        if not done:
            return

        self._undo_tasks = done
        self._arm_undo_timeout()
        self.render()

    def _undo_cleanup(self) -> None:
        """撤销上一次批量清理：标记全去掉，那些任务回到「过期没交」。"""
        tasks = self._undo_tasks or []
        self._clear_undo_timeout()
        self._undo_tasks = None

        for task in tasks:
            key = task.get("key")
            if not key:
                continue
            store.set_marked(key, False)
            # 跟 _toggle_marked 取消标记时保持一致：submitted 回到 None（未知），
            # 不能写成 False —— 那等于凭空断言「你没交」
            task["submitted"] = None
            task["manual_submitted"] = False
            task["needs_action"] = True
        self.render()

    def _arm_undo_timeout(self) -> None:
        """撤销窗口到点后自动关掉。after 的 id 要留着，点撤销时得能取消它。"""
        self._clear_undo_timeout()
        self._undo_job = self.root.after(UNDO_SECONDS * 1000, self._expire_undo)

    def _clear_undo_timeout(self) -> None:
        if self._undo_job is not None:
            try:
                self.root.after_cancel(self._undo_job)
            except Exception:
                pass
            self._undo_job = None

    def _expire_undo(self) -> None:
        self._undo_job = None
        self._undo_tasks = None
        self._refresh_cleanup_bar()

    # -------------------------------------------------- 过期一键清理条
    def _on_cleanup_click(self, _event: tk.Event) -> str:
        """点那条提示。刚清理完时点它是撤销，其余情况是清理。"""
        if self._undo_tasks is not None:
            self._undo_cleanup()
        else:
            self._mark_all_overdue()
        return "break"

    def _refresh_cleanup_bar(self) -> None:
        """决定顶上那条显示什么、显示不显示。三种状态：待清理 / 可撤销 / 藏起来。"""
        if self._undo_tasks is not None:
            self.cleanup_label.configure(
                text=f"已清理 {len(self._undo_tasks)} 个过期任务　·　点这里撤销"
            )
            self._pack_cleanup_bar()
            return

        count = len(self._overdue_unmarked())
        if count and self._feed_marks_manual():
            self.cleanup_label.configure(
                text=f"{count} 个已过期没交　·　点这里全部标记为已交"
            )
            self._pack_cleanup_bar()
        else:
            self.cleanup_bar.pack_forget()
            self._fit_height()

    def _pack_cleanup_bar(self) -> None:
        """显示清理条。已经显示着就什么都不做，不然每渲染一次会抖一下。"""
        if self.cleanup_bar.winfo_manager():
            return
        # before= 是为了让它待在状态栏和任务列表之间。
        # 直接 pack 会按调用顺序追加到最下面，跑到任务列表底下去了。
        self.cleanup_bar.pack(fill="x", padx=10, pady=(6, 0), before=self.tasks_frame)
        self._fit_height()

    def _fit_height(self) -> None:
        """窗口高度跟着内容走。清理条出现/消失、撤销到时，都要重算一次。"""
        self.root.update_idletasks()
        w = int(self.window_cfg.get("width") or 310)
        h = self.body.winfo_reqheight()
        # 高度变了就可能顶到任务栏下面去，顺手夹一次
        x, y = self._clamp_to_screen(self.root.winfo_x(), self.root.winfo_y(), w, h)
        self.root.geometry(f"{w}x{h}+{x}+{y}")

    def _feed_marks_manual(self) -> bool:
        """
        提交状态是不是只能靠你手动标。

        走日历订阅时是的（接口压根不给）。哪天走回 API token，提交状态就是
        真的，「标记为已交」这类东西一律不该出现 —— 那等于让你骗自己。
        """
        return (self.payload.get("fetch") or {}).get("source") == "calendar_feed"

    # ------------------------------------------------------------ 逐行菜单
    def _show_task_menu(self, event: tk.Event, task: dict[str, Any]) -> None:
        """
        点在某一行上弹的菜单。比全局菜单多了「标记为已交」这一项。

        为什么必须有这个：日历订阅拿不到 Canvas 的提交状态，
        所以「交了没」这件事只能由你自己标。没有它的话，
        所有作业会永远显示成待办，用两天你就不信这个挂件了。
        """
        c = self.c
        menu = tk.Menu(
            self.root, tearoff=0,
            bg=c["surface"], fg=c["fg"], activebackground=c["accent"],
            activeforeground="#ffffff", bd=0, font=(self.ui_font, self.base_size - 1),
        )

        marked = bool(task.get("manual_submitted"))
        menu.add_command(
            label="取消「已交」标记" if marked else "标记为已交",
            command=lambda: self._toggle_marked(task),
        )
        url = task.get("url")
        if url:
            menu.add_command(label="在 Canvas 里打开", command=lambda: self._open_url(url))
        menu.add_separator()

        # 全局菜单那几项也搬过来 —— 右键一处能办完所有事，
        # 不用记住「点行」和「点空白」弹的东西不一样
        menu.add_command(label="立即刷新", command=self.start_fetch)
        menu.add_command(
            label="只显示前几个" if self.show_all else "显示全部",
            command=self._menu_toggle_all,
        )
        menu.add_command(
            label="隐藏已交的" if self.show_submitted else "显示已交的",
            command=self._menu_toggle_submitted,
        )

        overdue = len(self._overdue_unmarked())
        if overdue:
            menu.add_command(
                label=f"把已过期的都标记为已交（{overdue} 个）",
                command=self._mark_all_overdue,
            )
        menu.add_separator()
        menu.add_command(label="打开完整列表（网页）", command=self._open_html)
        menu.add_command(label="打开数据文件夹", command=self._open_folder)
        menu.add_command(label="打开配置文件", command=lambda: self._open_path(store.CONFIG_FILE))
        menu.add_separator()
        menu.add_command(label="退出", command=self.root.destroy)

        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _menu_toggle_all(self) -> None:
        """勾选框的状态存在 var_all 里，直接调 _toggle_all 会读到旧值，所以先翻过来。"""
        self.var_all.set(not self.show_all)
        self._toggle_all()

    def _menu_toggle_submitted(self) -> None:
        self.var_sub.set(not self.show_submitted)
        self._toggle_submitted()

    def _toggle_marked(self, task: dict[str, Any]) -> None:
        """
        标记 / 取消「已交」。

        就地改内存里的数据并立刻重画 —— 点一下必须马上看到变化，
        不能等下一次联网拉取（那可能是明天早上）。下次 fetch 会从
        state\\marked.json 把这个标记重新应用一遍，所以这里改了不会漂。
        """
        key = task.get("key")
        if not key:
            return

        was_marked = bool(task.get("manual_submitted"))
        store.set_marked(key, not was_marked, title=task.get("title") or "")

        # 取消标记时 submitted 回到 None（未知），跟 fetch 的产出保持一致 ——
        # 不能写成 False，那等于凭空断言「你没交」
        task["submitted"] = None if was_marked else True
        task["manual_submitted"] = not was_marked
        # 刚标记 → 不用催了；取消标记 → 回到「要催」
        task["needs_action"] = was_marked
        self.render()

    def _toggle_topmost(self) -> None:
        self.topmost = bool(self.var_top.get())
        self._apply_topmost()
        store.save_widget_state(topmost=self.topmost)

    def _toggle_all(self) -> None:
        self.show_all = bool(self.var_all.get())
        store.save_widget_state(show_all=self.show_all)
        self.render()

    def _toggle_submitted(self) -> None:
        self.show_submitted = bool(self.var_sub.get())
        store.save_widget_state(show_submitted=self.show_submitted)
        self.render()

    # ------------------------------------------------------------ 拖动
    def _drag_start(self, event: tk.Event) -> None:
        self._drag_origin = (event.x_root - self.root.winfo_x(),
                             event.y_root - self.root.winfo_y())

    def _drag_move(self, event: tk.Event) -> None:
        if not self._drag_origin:
            return
        x = event.x_root - self._drag_origin[0]
        y = event.y_root - self._drag_origin[1]
        self.root.geometry(f"+{x}+{y}")

    def _drag_end(self, _event: tk.Event) -> None:
        if not self._drag_origin:
            return
        self._drag_origin = None
        # 先夹回可用区域再存：存下非法位置的话，下次启动挂件就"消失"了。
        x, y = self._clamp_to_screen(self.root.winfo_x(), self.root.winfo_y())
        self.root.geometry(f"+{x}+{y}")
        store.save_widget_state(position=[x, y])

    # ------------------------------------------------------------ 数据
    def reload(self, force: bool = False) -> None:
        """读 deadlines.json。文件没变就什么也不做。"""
        try:
            stamp = store.DEADLINES_FILE.stat().st_mtime
        except OSError:
            stamp = None

        if not force and stamp is not None and stamp == self.mtime:
            return
        self.mtime = stamp

        payload = store.read_json(store.DEADLINES_FILE, default=None)
        self.payload = payload if isinstance(payload, dict) else {}
        if self._fetch_process is None:
            self.fetching = False
        self._fetch_error = None
        self.render()

    def _poll_file(self) -> None:
        """
        每 3 秒看一眼文件有没有变。
        不做成「定时重新拉取」是故意的：拉取由任务计划负责，
        挂件只负责显示。这样挂件永远不会因为网络慢而卡住。
        """
        try:
            self.reload()
            if self._fetch_process is not None:
                result = self._fetch_process.poll()
                if result is not None:
                    self._fetch_process = None
                    self.fetching = False
                    if self.mtime == self._fetch_initial_mtime:
                        self._fetch_error = "刷新未生成新数据，请检查日志后重试。"
                    elif result != 0 and (self.payload.get("fetch") or {}).get("ok", True):
                        self._fetch_error = "刷新进程异常退出，请检查日志。"
                    self._render_status()
        except Exception:
            pass
        self.root.after(3000, self._poll_file)

    def start_fetch(self) -> None:
        """叫起一个 fetch.py 子进程。不等待 —— 挂件不能因为网络慢而卡住。"""
        if self.fetching:
            return
        self.fetching = True
        self._fetch_error = None
        self._fetch_initial_mtime = self.mtime
        self._set_status("warn", "正在拉取…")

        cmd, cwd = store.child_command("fetch", "--quiet")
        try:
            self._fetch_process = subprocess.Popen(
                cmd, cwd=cwd,
                env=store.child_environment(),
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                close_fds=True,
            )
        except Exception as exc:
            self.fetching = False
            self._fetch_error = f"启动拉取失败：{store.redact(str(exc))}"
            self._set_status("bad", self._fetch_error)

    # ------------------------------------------------------------ 渲染
    def _visible_tasks(self) -> list[dict[str, Any]]:
        """
        选出该显示哪些。三类：
            1. needs_action  → 确定没交，要催
            2. submitted 是 None → 状态未知（讨论区、页面这类没有「提交」概念的东西）。
               它们有截止时间，也有事要做，所以照样显示，但不假装它「没交」。
            3. show_submitted 打开时 → 已交的也显示，排最后
        """
        tasks = self.payload.get("tasks") or []
        hidden = set(self.display_cfg.get("hide_types") or [])

        chosen: list[tuple[dict[str, Any], int]] = []
        for task in tasks:
            if (task.get("type") or "") in hidden:
                continue
            if task.get("needs_action") or task.get("submitted") is None:
                chosen.append((task, 0))
            elif self.show_submitted:
                chosen.append((task, 1))

        # 组内按截止时间升序 —— 已过期未交的自然排到最前
        chosen.sort(key=lambda pair: (pair[1], pair[0].get("due_utc") or "9999"))

        if not self.show_all:
            limit = int(self.display_cfg.get("top_n") or 5)
            chosen = chosen[:limit]
        return [task for task, _ in chosen]

    def _count_hidden(self) -> tuple[int, int]:
        """返回 (被 top_n 截掉的数量, 没有截止日期的数量)。用在底部提示行。"""
        tasks = self.payload.get("tasks") or []
        hidden = set(self.display_cfg.get("hide_types") or [])
        shown = len(self._visible_tasks())

        total_relevant = 0
        for task in tasks:
            if (task.get("type") or "") in hidden:
                continue
            if task.get("needs_action") or task.get("submitted") is None or self.show_submitted:
                total_relevant += 1

        undated = len(self.payload.get("undated") or [])
        return max(0, total_relevant - shown), undated

    def render(self) -> None:
        c = self.c
        for child in self.tasks_frame.winfo_children():
            child.destroy()
        self.tickers = []

        self._render_status()

        tasks = self._visible_tasks()
        meta = self.payload.get("fetch") or {}

        if not self.payload:
            self._message("还没有数据。\n右键 → 立即刷新，或者双击拉数据那个文件。")
        elif not tasks:
            if not meta.get("ok", True):
                self._message("这次拉取失败了，而且没有上一次的数据可以显示。\n右键 → 立即刷新 再试一次。")
            else:
                days = int(self.display_cfg.get("future_days") or 180)
                self._message(f"未来 {days} 天没有待处理的截止任务。")

        for task in tasks:
            self._render_task(task)

        self._render_footer(len(tasks))
        # 放在 footer 之后、量高度之前 —— 它自己会 pack 出来，
        # 高度得算上它那一条
        self._refresh_cleanup_bar()

        self._fit_height()

    def _render_footer(self, shown: int) -> None:
        """
        底部一行小字，告诉你「还有东西没显示」。
        没有这层的话，截断到前 5 个之后，第 6 个任务就等于不存在 ——
        而它可能正好是最近的那个。
        """
        if not shown:
            return
        clipped, undated = self._count_hidden()

        parts = []
        if clipped:
            parts.append(f"还有 {clipped} 个未显示（右键 → 显示全部）")
        if undated:
            parts.append(f"{undated} 个没有截止日期")

        # 日历订阅拿不到提交状态，必须明确告诉你「已交」得自己标。
        # 不说的话你会以为挂件知道，然后奇怪为什么交了的作业还在催。
        # （有过期任务时上面那条清理条已经说得很清楚了，这里不重复）
        if self._feed_marks_manual():
            overdue = len(self._overdue_unmarked())
            if not overdue:
                parts.append("右键某一行可标记「已交」")

        if not parts:
            return

        tk.Label(
            self.tasks_frame, text=" · ".join(parts), bg=self.c["bg"], fg=self.c["muted"],
            font=(self.ui_font, max(7, self.base_size - 2)), anchor="w", justify="left",
            wraplength=int(self.window_cfg.get("width") or 310) - 28,
        ).pack(fill="x", pady=(2, 0))

    def _message(self, text: str) -> None:
        tk.Label(
            self.tasks_frame, text=text, bg=self.c["bg"], fg=self.c["muted"],
            font=(self.ui_font, max(7, self.base_size - 1)), justify="left", anchor="w",
            wraplength=int(self.window_cfg.get("width") or 310) - 28,
        ).pack(fill="x", pady=(2, 4))

    def _render_status(self) -> None:
        """
        状态栏是这个工具最要紧的地方。

        一个看起来正常、其实数据已经放了三天没更新的看板，比没有看板更危险 ——
        你会以为这周没作业。所以「数据新鲜吗」必须一直显示在脸上。
        """
        meta = self.payload.get("fetch") or {}
        stale_hours = float(self.display_cfg.get("stale_hours") or 36)

        if self.fetching:
            self._set_status("warn", "正在拉取…")
            return
        if self._fetch_error:
            self._set_status("bad", self._fetch_error)
            return
        if not meta:
            self._set_status("bad", "还没有数据")
            return
        if not meta.get("ok", True):
            kind = meta.get("error_kind") or ""
            hint = meta.get("hint") or "拉取失败"
            # 配置类的提示要看走的是哪条路 —— 现在是日历订阅，
            # 还写「token」的话你会去找一个根本不存在的东西
            on_feed = meta.get("source") == "calendar_feed"
            if kind == "config":
                what = "日历订阅链接" if on_feed else "token"
                self._set_status("bad", f"还没填{what} —— 右键 → 打开配置文件")
            elif kind == "auth":
                self._set_status("bad", "token 失效了 —— 右键 → 打开配置文件换一串")
            else:
                self._set_status("bad", f"⚠ {hint}")
            return

        when = timeutil.parse_stamp(meta.get("time"))
        if when:
            age_h = (datetime.now() - when).total_seconds() / 3600.0
            if age_h > stale_hours:
                self._set_status(
                    "bad",
                    f"⚠ 数据已过期（{timeutil.human_age(age_h * 3600)}没更新）"
                    "—— 右键 → 立即刷新",
                )
                return
            stamp = f"更新于 {timeutil.human_age(age_h * 3600)}"
        else:
            stamp = "更新时间未知"

        if meta.get("token_warn"):
            used = meta.get("token_days_used") or 0
            self._set_status("warn", f"{stamp} · token 已用 {used:.0f} 天，该换了")
            return

        extra = ""
        if meta.get("source") == "fallback":
            extra = " · 逐课程拉取"
        self._set_status("ok", stamp + extra)

    def _set_status(self, level: str, text: str) -> None:
        c = self.c
        colors = {
            "ok": (c["bg"], c["muted"]),
            "warn": (c["warn_bg"], c["warn_fg"]),
            "bad": (c["bad_bg"], c["bad_fg"]),
        }[level]
        self.status.configure(text=text, bg=colors[0], fg=colors[1])

    def _emphasize_hours(self) -> float:
        """
        剩余多少小时以内要放大字号。config.json 的 display.emphasize_hours。

        这里不能用 `cfg.get(...) or 默认值` 那个写法 —— 那样设成 0 会被
        当成「没填」而拿到默认的 72，等于关不掉。
        """
        raw = self.display_cfg.get("emphasize_hours", EMPHASIZE_HOURS)
        try:
            return float(raw)
        except (TypeError, ValueError):
            return EMPHASIZE_HOURS

    def _render_task(self, task: dict[str, Any]) -> None:
        c = self.c
        width = int(self.window_cfg.get("width") or 310)
        inner = width - 28

        due = timeutil.parse_iso(task.get("due_utc"))
        left = timeutil.remaining_seconds(due) if due else None
        needs = bool(task.get("needs_action"))
        unknown = task.get("submitted") is None

        # 日历订阅这条路，**每一条**的提交状态都是未知（接口压根不给）。
        # 那样的话每行都挂一个「状态未知」只是噪音 —— 改成在底部统一说一次。
        from_feed = self._feed_marks_manual()

        # 还该有倒计时的：要催的 + 状态未知的。已交的不再走秒。
        live = (needs or unknown) and due is not None

        # 「快到点了」的整行放大 —— 三天内该交的，字比别的粗一号，
        # 扫一眼就知道哪个是要紧的，不用去读倒计时。
        # 已经在走秒的（live）才算，已交的和已过期的不放大：前者不用催，
        # 后者已经晚了，放大只会把真正要交的挤下去。
        size = self.base_size
        if live and left is not None and 0 < left <= self._emphasize_hours() * 3600:
            size += EMPHASIZE_BUMP

        # 颜色。状态未知的用中性灰 —— 绝不能用绿色，
        # 绿色在别处代表「已交」，用在这里会让你以为它已经做完了。
        if needs and left is not None:
            tint = c[timeutil.urgency(left)]
        elif unknown:
            tint = c["later"]
        else:
            tint = c["done"]

        row = tk.Frame(self.tasks_frame, bg=c["bg"])
        row.pack(fill="x", pady=(0, 7))

        # 左侧色条：一眼看出紧急程度的那个「●」，其实是一条竖线更省地方，
        # 但圆点在视觉上更容易被扫到，所以用一个小方块 label 顶在行首
        head = tk.Frame(row, bg=c["bg"])
        head.pack(fill="x")

        tk.Label(
            head, text="●", bg=c["bg"], fg=tint,
            font=(self.ui_font, max(7, size - 3)),
        ).pack(side="left", padx=(0, 4))

        tk.Label(
            head, text=task.get("course_code") or "?", bg=c["bg"], fg=c["muted"],
            font=(self.mono_font, max(7, size - 2), "bold"),
        ).pack(side="left")

        # 放大过的行，同样的像素宽度能放下的字符更少。不按比例缩一下，
        # 长作业名会顶到窗口边缘被切掉（窗口宽度是写死的，不会跟着撑开）
        budget = int((inner - 10) * self.base_size / size)
        title = ellipsize(task.get("title") or "", budget)
        tk.Label(
            head, text="  " + title, bg=c["bg"], fg=c["fg"],
            font=(self.ui_font, size), anchor="w",
        ).pack(side="left", fill="x", expand=True)

        line2 = tk.Frame(row, bg=c["bg"])
        line2.pack(fill="x", padx=(13, 0), pady=(0, 2))

        if due is None:
            tk.Label(
                line2, text="没有设置截止时间", bg=c["bg"], fg=c["muted"],
                font=(self.ui_font, max(7, size - 2)),
            ).pack(side="left")
        else:
            if live:
                timer = tk.Label(
                    line2, text="还剩 " + timeutil.countdown(due),
                    bg=c["bg"], fg=tint,
                    font=(self.mono_font, max(7, size - 1), "bold" if needs else "normal"),
                )
                timer.pack(side="left")
                self.tickers.append((timer, due))
                if unknown and not from_feed:
                    # 不能让它看起来像「没交」，也不能像「交了」——直接说不知道
                    tk.Label(
                        line2, text=" 状态未知", bg=c["bg"], fg=c["muted"],
                        font=(self.ui_font, max(7, size - 2)),
                    ).pack(side="left")
            else:
                if task.get("manual_submitted"):
                    state_text = "已交（你标的）"
                elif task.get("submitted"):
                    state_text = "已交"
                else:
                    state_text = "已过期"
                tk.Label(
                    line2, text=state_text, bg=c["bg"], fg=tint,
                    font=(self.mono_font, max(7, size - 1)),
                ).pack(side="left")

            tk.Label(
                line2, text=task.get("due_display") or "", bg=c["bg"], fg=c["muted"],
                font=(self.mono_font, max(7, size - 2)),
            ).pack(side="right")

        # 进度条：越紧急填得越满，把「还有多久」变成一个不用读字的信号
        if live:
            bar_bg = tk.Frame(row, bg=c["track"], height=3, width=inner)
            bar_bg.pack(fill="x", padx=(13, 0), pady=(1, 0))
            bar_bg.pack_propagate(False)
            frac = timeutil.progress_fraction(left, BAR_WINDOW_HOURS)
            fill = tk.Frame(bar_bg, bg=tint)
            fill.place(x=0, y=0, relwidth=max(0.02, frac), relheight=1)

        # 双击打开 Canvas 上那个作业的页面。要递归绑到所有子控件上 ——
        # 否则你正好点在文字上时事件被那个 label 吃掉，双击就没反应，
        # 这种「有时灵有时不灵」的毛病最难查。
        url = task.get("url")
        if url:
            self._bind_recursive(
                row, "<Double-Button-1>",
                lambda _e, u=url: self._open_url(u), cursor="hand2",
            )

        self._bind_recursive(row, "<Button-3>", lambda e, t=task: self._show_task_menu(e, t))
        self._bind_recursive(row, "<Button-1>", self._drag_start)
        self._bind_recursive(row, "<B1-Motion>", self._drag_move)
        self._bind_recursive(row, "<ButtonRelease-1>", self._drag_end)

    @staticmethod
    def _bind_recursive(widget: tk.Misc, sequence: str, func, cursor: str | None = None) -> None:
        try:
            widget.bind(sequence, func)
            if cursor:
                widget.configure(cursor=cursor)   # type: ignore[call-arg]
        except tk.TclError:
            pass
        for child in widget.winfo_children():
            Widget._bind_recursive(child, sequence, func, cursor)

    # ------------------------------------------------------------ 每秒走秒
    def _tick(self) -> None:
        """
        只改倒计时那几个 label 的文字，不重建 DOM。
        重建的话窗口会闪、你正在拖的位置会跳。
        """
        for label, due in self.tickers:
            try:
                label.configure(text="还剩 " + timeutil.countdown(due))
            except tk.TclError:
                pass
        self.root.after(1000, self._tick)

    # ------------------------------------------------------------ 杂项
    def _open_url(self, url: str) -> None:
        try:
            import webbrowser
            webbrowser.open(url)
        except Exception:
            pass

    def _open_path(self, path: Path) -> None:
        try:
            os.startfile(str(path))          # noqa: S606  (Windows 专用)
        except Exception as exc:
            messagebox.showerror("打不开", f"{path}\n\n{exc}")

    def _open_html(self) -> None:
        if not store.HTML_FILE.exists():
            messagebox.showinfo(
                "还没有完整列表",
                "先拉一次数据（右键 → 立即刷新），就会生成 out\\deadlines.html。",
            )
            return
        self._open_path(store.HTML_FILE)

    def _open_folder(self) -> None:
        try:
            os.startfile(str(store.OUT_DIR))
        except Exception as exc:
            messagebox.showerror("打不开", str(exc))

    def _spawn_autostart(self, action: str) -> None:
        """
        装/卸开机自启。实际动手的是另一个进程（autostart.py），不在这里做 ——
        注册任务计划要跑 PowerShell，会卡好几秒，卡在挂件线程上窗口就假死了。
        结果由那个进程自己显示（打包后没有控制台，它会用记事本弹出来）。
        """
        cmd, cwd = store.child_command("autostart", action)
        try:
            subprocess.Popen(
                cmd, cwd=cwd,
                env=store.child_environment(),
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                close_fds=True,
            )
        except Exception as exc:
            messagebox.showerror("启动失败", f"{exc}")


# --------------------------------------------------------------------------
# 单实例
# --------------------------------------------------------------------------
def acquire_lock() -> socket.socket | None:
    """
    挂件开两份没意义，还会互相覆盖窗口位置。
    用一个本地端口当锁：占得上就说明没有别的实例。
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", LOCK_PORT))
        sock.listen(1)
        return sock
    except OSError:
        sock.close()
        return None


def main() -> int:
    parser = argparse.ArgumentParser(description="Canvas 截止日期桌面挂件")
    parser.add_argument("--reset", action="store_true",
                        help="把窗口位置恢复成默认（拖出屏幕找不回来时用）")
    args = parser.parse_args()

    enable_dpi_awareness()        # 函数自己判断平台，非 Windows 是空操作

    # 还没配置过就别启动挂件了 —— 弹那个「第一次设置」的窗口，让人粘链接。
    # 不这么做的话，一个刚拿到这个文件夹的人双击之后会看到一个红着一条
    # 「还没填日历订阅链接 —— 右键 → 打开配置文件」的小条，然后要自己去
    # 用记事本编辑 json。那一步能劝退一半人。
    # （配好之后这个分支永远不会再进来。）
    if not sys.argv[1:]:                      # 带 --reset 之类参数时跳过，那是老用户
        try:
            cfg_now = store.load_config(quiet=True)
            canvas_now = cfg_now.get("canvas") or {}
            if not str(canvas_now.get("calendar_feed_url") or "").strip() \
                    and not str(canvas_now.get("token") or "").strip():
                cmd, cwd = store.child_command("setup")
                subprocess.Popen(
                    cmd, cwd=cwd,
                    env=store.child_environment(),
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                    close_fds=True,
                )
                return 0
        except Exception:
            pass                              # 读配置出问题就照常启动，由挂件自己报错

    lock = acquire_lock()
    if lock is None:
        # 已经有一个在跑了，安静退出。
        #
        # 这里**不能**弹对话框：开机自启那个任务会在登录时拉起挂件，
        # 如果你已经开着，第二个实例就会弹一个「已经在运行了」的框
        # 在后台干等人点确定 —— 没人点它就永远不退，白占一个进程
        # （而且用 pythonw 跑，那个框还可能藏在你根本没看的地方）。
        # 记一笔日志就够了，反正屏幕上本来就有一个挂件。
        try:
            store.append_log(["挂件已经在运行，这次启动跳过。"])
        except Exception:
            pass
        return 0

    store.ensure_dirs()
    cfg = store.load_config(quiet=True)

    root = tk.Tk()
    root.title("Canvas 倒计时")
    suppress_dock_icon()          # macOS：别在程序坞里露脸（必须在 Tk() 之后）
    try:
        Widget(root, cfg, reset=args.reset)
    except Exception as exc:
        import traceback
        traceback.print_exc()
        try:
            store.append_log([f"挂件启动失败：{type(exc).__name__}: {exc}"])
        except Exception:
            pass
        messagebox.showerror("挂件启动失败", f"{type(exc).__name__}: {exc}\n\n"
                                             f"详情已写进 {store.LOG_FILE}")
        return 1

    try:
        root.mainloop()
    finally:
        try:
            lock.close()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
