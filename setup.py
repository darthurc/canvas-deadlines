# -*- coding: utf-8 -*-
"""
setup.py —— 第一次设置：问用户要那串日历订阅链接

为什么要有这个窗口：
    原来的流程是「用记事本打开 config.json，把链接粘到第 5 行两个引号中间」。
    对自己写的程序来说这没什么，但把这个东西拷给别人用的时候，这一步能劝退
    一半人 —— 粘错地方、忘了加引号、用 Word 打开把文件存坏了，什么都有。

    所以改成：双击 → 窗口弹出来 → 粘进去 → 点一下 → 检查、保存、拉数据，
    一步到位，全程不用碰 json。

这个窗口做的三件事（顺序很重要）：
    1. **先验证再保存**。下载一次订阅，真的解析出课程和作业了，才写进
       config.json。只保存不验证的话，打错一个字符会得到一个「配置好了
       但永远没数据」的状态，那比报错更难查。
    2. 保存链接（写 config.json）。
    3. 顺手拉第一次数据（复用 fetch.py 那套，不另写一份）。

安全：这串网址是凭证，所以
    * 输入框里的内容**不回显到任何日志**
    * 出错信息一律过 store.redact()
    * 保存前会明确告诉用户「别给任何人，包括我」
"""

from __future__ import annotations

import queue
import urllib.parse
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import canvas_api
import fetch
import store
import widget
from canvas_api import CanvasError

PAD = 16
WIN_W = 620


class SetupWindow:
    def __init__(self, root: tk.Tk, cfg: dict[str, Any]):
        self.root = root
        self.cfg = cfg
        self.c = widget.DARK if widget.system_prefers_dark() else widget.LIGHT
        self.ui, self.mono = widget.pick_font(root)
        self.base = 10
        self.queue: queue.Queue = queue.Queue()
        self.saved_url = ""
        self.busy = False
        self.finished = False              # 「完成」只算一次，防止连点两下装两遍
        self.var_auto = tk.BooleanVar(value=True)

        root.title("Canvas 截止日期 · 第一次设置")
        root.configure(bg=self.c["bg"])
        root.resizable(False, False)

        self._build()
        self._center()
        self._bring_to_front()

    def _bring_to_front(self) -> None:
        """
        把这个窗口顶到最前面。

        这个窗口是从别处被拉起来的（挂件发现没配置 → 启动 setup.py），
        所以系统不觉得它是「用户主动打开的程序」，可能就开在别的窗口后面。
        用户看到的现象是「双击了，没反应」—— 而窗口其实在底下等着。

        Windows 上一般不用管（新进程的窗口默认在前面），Mac 上不这么干
        窗口经常躲在后面。所以两边都顶一下，代价只是一闪而过。
        """
        try:
            self.root.lift()
            self.root.attributes("-topmost", True)
            self.root.after(400, lambda: self.root.attributes("-topmost", False))
            self.root.focus_force()
        except Exception:
            pass

    # ------------------------------------------------------------ 界面
    def _label(self, parent: tk.Misc, text: str, *, size: int = 0, muted: bool = False,
               bold: bool = False, wrap: int = WIN_W - PAD * 2, mono: bool = False) -> tk.Label:
        font = (self.mono if mono else self.ui, size or self.base)
        if bold:
            font = font + ("bold",)
        return tk.Label(
            parent, text=text, bg=self.c["bg"],
            fg=self.c["muted"] if muted else self.c["fg"],
            font=font, anchor="w", justify="left", wraplength=wrap,
        )

    def _build(self) -> None:
        c = self.c
        outer = tk.Frame(self.root, bg=c["bg"])
        outer.pack(fill="both", expand=True, padx=PAD, pady=PAD)

        self._label(
            outer, "粘贴 Canvas 日历订阅链接",
            size=self.base + 4, bold=True, wrap=WIN_W,
        ).pack(fill="x")

        self._label(
            outer,
            "只读。看不到成绩，改不了任何东西，不能替你交作业。",
            muted=True,
        ).pack(fill="x", pady=(6, 12))

        # ---- 怎么拿 ----
        steps = tk.Frame(outer, bg=c["surface"], highlightthickness=1,
                         highlightbackground=c["line"])
        steps.pack(fill="x")
        inner = tk.Frame(steps, bg=c["surface"])
        inner.pack(fill="x", padx=12, pady=12)

        tk.Label(
            inner, text="链接在哪里拿", bg=c["surface"], fg=c["fg"],
            font=(self.ui, self.base + 1, "bold"), anchor="w",
        ).pack(fill="x", pady=(0, 8))

        for n, text in (
            ("1", "在浏览器里登录 Canvas"),
            ("2", "点左边那条深色竖条上的 Calendar（日历）"),
            ("3", "进去后看右边那一栏，拉到最下面，点 Calendar Feed（日历订阅）"),
            ("4", "弹出的小窗里有一串网址，以 .ics 结尾 —— 复制它"),
        ):
            row = tk.Frame(inner, bg=c["surface"])
            row.pack(fill="x", pady=1)
            tk.Label(
                row, text=n, bg=c["surface"], fg=c["accent"],
                font=(self.mono, self.base, "bold"), width=3, anchor="w",
            ).pack(side="left")
            tk.Label(
                row, text=text, bg=c["surface"], fg=c["fg"],
                font=(self.ui, self.base), anchor="w", justify="left",
                wraplength=WIN_W - 90,
            ).pack(side="left", fill="x", expand=True)

        self.btn_open = tk.Button(
            inner, text="打开我的 Canvas 日历", command=self._open_canvas,
            bg=c["track"], fg=c["fg"], activebackground=c["line"],
            font=(self.ui, self.base), relief="flat", bd=0, cursor="hand2",
        )
        self.btn_open.pack(anchor="w", pady=(10, 0))

        # ---- 粘贴框 ----
        self._label(outer, "粘贴到这里：", muted=True).pack(
            fill="x", pady=(14, 4)
        )
        self.entry = tk.Entry(
            outer, font=(self.mono, self.base), bg=c["surface"], fg=c["fg"],
            insertbackground=c["fg"], relief="flat", bd=0,
            highlightthickness=1, highlightbackground=c["line"],
            highlightcolor=c["accent"],
        )
        self.entry.pack(fill="x", ipady=7)
        self.entry.bind("<Return>", lambda _e: self._on_check())
        # 粘完顺手把光标放到末尾，方便一眼看出有没有粘全
        self.entry.bind("<Control-v>", lambda _e: self.root.after(30, self._tail))

        self._label(
            outer,
            "这串网址等同于密码：谁拿到都能读你的课程日历。"
            "不要发到群里，不要提交到 GitHub。",
            muted=True, wrap=WIN_W - PAD * 2,
        ).pack(fill="x", pady=(6, 12))

        # ---- 主按钮 + 结果 ----
        self.btn_check = tk.Button(
            outer, text="检查并保存", command=self._on_check,
            bg=c["accent"], fg="#ffffff", activebackground=c["accent"],
            activeforeground="#ffffff", font=(self.ui, self.base + 1, "bold"),
            relief="flat", bd=0, cursor="hand2", state="disabled",
        )
        self.btn_check.pack(fill="x", ipady=8)
        # 输入框空着的时候按钮是灰的 —— 让人一眼知道「还差一步」
        self.entry.bind("<KeyRelease>", lambda _e: self._sync_button())
        self._sync_button()

        self.result = self._label(outer, "", wrap=WIN_W - PAD * 2)
        self.result.pack(fill="x", pady=(12, 0))

        # ---- 底部 ----
        bottom = tk.Frame(outer, bg=c["bg"])
        bottom.pack(fill="x", pady=(14, 0))
        self.btn_done = tk.Button(
            bottom, text="完成，开始用", command=self._done,
            bg=c["track"], fg=c["fg"], activebackground=c["line"],
            font=(self.ui, self.base), relief="flat", bd=0,
            cursor="hand2", state="disabled",
        )
        self.btn_done.pack(side="right", ipadx=14, ipady=6)
        tk.Button(
            bottom, text="先不弄", command=self.root.destroy,
            bg=c["bg"], fg=c["muted"], activebackground=c["bg"],
            font=(self.ui, self.base), relief="flat", bd=0, cursor="hand2",
        ).pack(side="right", padx=(0, 10))

        # 开机自启放在「完成」旁边，因为它改的就是「完成」干的事。
        # 默认勾上：不装的话，同学重启一次电脑就找不到挂件了，会以为程序坏了。
        tk.Checkbutton(
            bottom, text="同时设置开机自动启动", variable=self.var_auto,
            bg=c["bg"], fg=c["fg"], activebackground=c["bg"],
            activeforeground=c["fg"], selectcolor=c["surface"],
            font=(self.ui, self.base), relief="flat", bd=0,
            highlightthickness=0, cursor="hand2", anchor="w",
        ).pack(side="left")

        # 已经配过的话，把旧链接填进去（换链接时不用重新找）
        old = str((self.cfg.get("canvas") or {}).get("calendar_feed_url") or "").strip()
        if old:
            self.entry.insert(0, old)
            self._sync_button()

    def _center(self) -> None:
        self.root.update_idletasks()
        w = max(WIN_W, self.root.winfo_reqwidth())
        h = self.root.winfo_reqheight()
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        self.root.geometry(f"{w}x{h}+{(sw - w) // 2}+{max(0, (sh - h) // 2 - 40)}")

    def _tail(self) -> None:
        self.entry.icursor("end")
        self.entry.xview_moveto(1.0)
        self._sync_button()

    def _sync_button(self) -> None:
        if self.busy:
            return
        has = bool(self.entry.get().strip())
        self.btn_check.configure(state="normal" if has else "disabled")

    # ------------------------------------------------------------ 动作
    def _open_canvas(self) -> None:
        """直接把他送到 Canvas 日历页 —— 反正他马上要登录那儿去复制链接。"""
        base = str((self.cfg.get("canvas") or {}).get("base_url") or "").strip()
        if not base:
            return
        try:
            import webbrowser
            webbrowser.open(base.rstrip("/") + "/calendar")
        except Exception:
            pass

    def _on_check(self) -> None:
        url = self._clean(self.entry.get())
        if not url:
            return
        if not url.lower().startswith("http"):
            self._show_result(
                "这看着不像一个网址。应该是以 https:// 开头、以 .ics 结尾的一长串。",
                "bad",
            )
            return

        self.busy = True
        self.btn_check.configure(state="disabled", text="正在检查 ……")
        self.btn_done.configure(state="disabled")
        self._show_result("正在下载并解析订阅 ……", "info")

        # 网络请求扔到后台线程：墨大 Canvas 偶尔要十几秒，
        # 卡在主线程上窗口会「未响应」，看起来像崩了
        threading.Thread(target=self._work, args=(url,), daemon=True).start()
        self.root.after(120, self._poll)

    @staticmethod
    def _clean(raw: str) -> str:
        """常见手滑：复制时带上了引号、空格，或者粘成了 'xxx' 带单引号。"""
        return raw.strip().strip('"').strip("'").strip()

    def _work(self, url: str) -> None:
        """后台线程：下载 + 解析。只往 queue 里放结果，绝不碰界面。"""
        try:
            cfg = store.load_config(quiet=True)
            cfg.setdefault("canvas", {})["calendar_feed_url"] = url
            tasks, undated, meta = fetch.collect_from_feed(cfg)
            self.queue.put(("ok", url, tasks, undated, meta))
        except (CanvasError, ValueError) as exc:
            self.queue.put(("err", getattr(exc, "kind", "config"), str(exc)))
        except Exception as exc:                                  # noqa: BLE001
            self.queue.put(("err", "crash", f"{type(exc).__name__}: {exc}"))

    def _poll(self) -> None:
        """主线程：看后台干完没有。tkinter 不能从别的线程碰界面，只能这么传。"""
        try:
            item = self.queue.get_nowait()
        except queue.Empty:
            self.root.after(150, self._poll)
            return

        if item[0] == "auto":
            self._finish_autostart(item[1], item[2])
        elif item[0] == "ok":
            self._finish_ok(*item[1:])
        else:
            self._finish_err(item[1], item[2])

    def _finish_err(self, kind: str, detail: str) -> None:
        self.busy = False
        hint = canvas_api.hint_for(kind)
        # 出错信息里可能带着整串订阅链接（那等于一把钥匙），一律先抹掉
        self._show_result(f"没通过：{hint}\n\n{store.redact(detail)}", "bad")
        self.btn_check.configure(state="normal", text="再试一次")
        self._sync_button()

    def _finish_ok(self, url: str, tasks: list, undated: list, meta: dict) -> None:
        """验证通过 → 保存链接 → 拉第一次数据。"""
        lines = [f"链接有效 —— 找到 {meta.get('course_count', 0)} 门课、"
                 f"{meta.get('task_count', 0)} 个有截止时间的任务。"]

        # 把「最近的几个」列出来。这是让人放心的关键一步：
        # 他看到自己熟悉的作业名，才知道这东西真的读到了他的数据。
        hidden = set((self.cfg.get("display") or {}).get("hide_types") or [])
        shown = [t for t in tasks if (t.get("type") or "") not in hidden and t.get("due_utc")]
        shown.sort(key=lambda t: t["due_utc"])
        if shown:
            lines.append("")
            lines.append("最近几个：")
            for task in shown[:3]:
                lines.append(
                    f"   {task.get('due_display', '')}   "
                    f"{task.get('course_code', '')}  {task.get('title', '')}"
                )

        saved, err = self._save(url, tasks, undated, meta)
        if saved:
            lines.append("")
            lines.append("已保存。下次开机会自动更新。")
        else:
            lines.append("")
            lines.append(f"[警告] 链接没能写进 config.json：{err}")
            lines.append("       这次能看，但下次可能是空的。把这句话发给给你这个程序的人。")

        self._show_result("\n".join(lines), "ok" if saved else "warn")
        self.busy = False
        self.btn_check.configure(state="normal", text="重新检查")
        self.btn_done.configure(state="normal")
        self.saved_url = url

    def _save(self, url: str, tasks: list, undated: list, meta: dict) -> tuple[bool, str]:
        """
        写 config.json + 写第一次的数据。

        顺序：先 config 后数据。反过来的话，万一写 config 失败，
        挂件会拿着一份「没有链接」的配置去读一份看起来正常的数据 ——
        那种状态最难查。
        """
        try:
            cfg = store.load_config(quiet=True)
            canvas_cfg = cfg.setdefault("canvas", {})
            canvas_cfg["calendar_feed_url"] = url
            url = store.get_feed_url(cfg)
            canvas_cfg["calendar_feed_url"] = url
            parts = urllib.parse.urlsplit(url)
            canvas_cfg["base_url"] = f"{parts.scheme}://{parts.netloc}"
            # 设置向导验证的是日历订阅，不能让旧 token 在下一次刷新时抢占数据源。
            canvas_cfg["token"] = ""
            store.write_json(store.CONFIG_FILE, cfg)
        except Exception as exc:                                  # noqa: BLE001
            return False, store.redact(f"{type(exc).__name__}: {exc}")

        try:
            cfg = store.load_config(quiet=True)
            fetch.write_outputs(tasks, undated, meta, cfg, ok=True)
            store.append_log([
                f"第一次设置完成：{meta.get('task_count', 0)} 个任务，"
                f"数据源 {meta.get('source')}"
            ])
        except Exception as exc:                                  # noqa: BLE001
            return False, store.redact(f"{type(exc).__name__}: {exc}")

        return True, ""

    def _done(self) -> None:
        """点「完成」。可能要先装开机自启，再拉挂件。"""
        if self.finished:
            return
        self.finished = True

        if self.var_auto.get():
            # 装任务计划要起 PowerShell，好几秒。放在主线程上窗口会「未响应」，
            # 所以和检查链接走同一套「后台线程 + queue」。
            self.btn_done.configure(state="disabled", text="正在设置 ……")
            self._show_result("正在注册开机自启（3 个 Windows 任务计划）……", "info")
            threading.Thread(target=self._work_autostart, daemon=True).start()
            self.root.after(150, self._poll)
            return

        self._launch()

    def _work_autostart(self) -> None:
        """后台线程：注册任务计划。只往 queue 里放结果，绝不碰界面。"""
        lines: list[str] = []
        try:
            import autostart
            code = autostart.install(lines.append)
        except Exception as exc:                                  # noqa: BLE001
            code = 1
            lines.append(f"{type(exc).__name__}: {exc}")
        self.queue.put(("auto", code, "\n".join(lines)))

    def _finish_autostart(self, code: int, detail: str) -> None:
        if code == 0:
            self._show_result("已设置开机自启。挂件正在启动 ……", "ok")
            delay = 600
        else:
            # 装不上不影响程序本身，别让人以为前面白配了
            try:
                store.append_log(["开机自启安装失败：", *detail.splitlines()[-6:]])
            except Exception:
                pass
            self._show_result(
                "开机自启没设置成功 —— 不影响使用，只是重启后要自己双击一次这个程序。\n"
                "想再试：在挂件上右键 → 开机自启… → 开机自动启动。",
                "warn",
            )
            delay = 3500
        self.root.update_idletasks()
        self._center()
        self.root.after(delay, self._launch)

    def _launch(self) -> None:
        """把挂件拉起来 —— 不然用户会盯着一个「什么都没发生」的桌面。"""
        try:
            cmd, cwd = store.child_command("widget")
            subprocess.Popen(
                cmd,
                cwd=cwd,
                env=store.child_environment(),
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                close_fds=True,
            )
        except Exception:
            pass
        self.root.destroy()

    def _show_result(self, text: str, level: str) -> None:
        fg = {
            "ok": self.c["done"],
            "bad": self.c["bad_fg"],
            "warn": self.c["warn_fg"],
            "info": self.c["muted"],
        }[level]
        self.result.configure(text=text, fg=fg)
        self.root.update_idletasks()
        self._center()          # 结果有多有少，窗口高度跟着内容走


def main() -> int:
    try:
        return _run()
    except BaseException as exc:                              # noqa: BLE001
        # 这个窗口是别人的第一印象，**绝不能什么都不发生**。
        # 用 pythonw 启动时没有控制台，一个未捕获的异常会让进程直接消失 ——
        # 用户看到的就是「双击了，没反应」，然后合理地认为这东西是坏的。
        detail = f"{type(exc).__name__}: {exc}"
        try:
            import traceback
            store.ensure_dirs()
            store.append_log(["设置窗口崩溃：" + detail]
                             + traceback.format_exc().splitlines()[-6:])
        except Exception:
            pass
        try:
            root = tk.Tk()
            root.withdraw()
            from tkinter import messagebox
            messagebox.showerror(
                "设置窗口打不开",
                f"{detail}\n\n"
                "这一步是用来粘贴 Canvas 日历订阅链接的。\n"
                "你也可以手动打开 config.json，把链接填进 "
                "canvas.calendar_feed_url，或者把这句话发给给你这个程序的人。",
            )
            root.destroy()
        except Exception:
            pass
        return 1


def _run() -> int:
    widget.enable_dpi_awareness()          # 必须在 Tk() 之前；非 Windows 是空操作

    store.ensure_dirs()
    root = tk.Tk()
    # 注意：这里**故意不调** widget.suppress_dock_icon()。
    # 那个会把进程设成「附属模式」，而附属模式的窗口不一定会被激活到最前面 ——
    # 设置窗口里有个要粘贴链接的输入框，抢不到焦点就等于没法用。
    # 所以这个窗口保留正常模式，只额外把它顶到前面（见 _bring_to_front）。
    SetupWindow(root, store.load_config(quiet=True))
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
