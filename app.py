# -*- coding: utf-8 -*-
"""
app.py —— 打包成单个 exe 之后的**唯一入口**，靠第一个参数分派

没打包的时候，每个功能是一个独立的 .py，靠 .bat 双击哪个来决定跑哪个：

    run-widget.bat   → pythonw widget.py
    run-fetch.bat    → python  fetch.py
    首次设置.bat      → pythonw setup.py
    安装开机自启.bat  → powershell autostart-install.ps1

打包之后只剩一个 exe，没有 .py 文件可以让别人「双击那个」了，
所以改成子命令：

    CanvasDeadlines.exe                → 没配过就弹设置窗口，配过就出挂件
    CanvasDeadlines.exe widget         → 直接出挂件
    CanvasDeadlines.exe fetch --quiet  → 拉一次数据（任务计划用这个）
    CanvasDeadlines.exe setup          → 强制弹设置窗口
    CanvasDeadlines.exe check          → 检查订阅，结果写成文本并打开记事本
    CanvasDeadlines.exe autostart install    → 装开机自启
    CanvasDeadlines.exe autostart uninstall  → 卸开机自启

`store.child_command()` 就是照着这套名字生成命令行的
（它把 "fetch" 翻译成 `[exe, "fetch"]`，把 "widget" 翻译成 `[exe, "widget"]`）。

**为什么 no-args 走 widget 而不是 setup**：
双击 exe 是最常见的动作，应该是「出现挂件」。而「还没配过链接」这件事
widget.main() 自己已经会判断了（它会把 setup 拉起来然后自己退出），
所以这里不用重复一遍。

**这个 exe 是 --noconsole 编出来的**：没有控制台，`print()` 全部石沉大海。
所以 check / autostart 的结果不能靠打印 —— 见下面 _report()。
好消息是任务计划跑 `fetch --quiet` 时不会闪黑框，这本来就是要的效果。
"""

from __future__ import annotations

import io
import os
import subprocess
import sys
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import store


# --------------------------------------------------------------------------
# 结果怎么给用户看
# --------------------------------------------------------------------------
def _has_console() -> bool:
    """
    有没有地方能接收打印。
    打包成 --noconsole 之后 sys.stdout 是 None，print() 不会报错但什么也不显示；
    手动从 cmd 里跑一个 windowed exe 也是同样情况（Windows 不会给你接上）。
    """
    return sys.stdout is not None


def _report(title: str, body: str) -> None:
    """
    把一段文字给用户看。有控制台就打印，没有就写文件 + 打开记事本。

    为什么是记事本而不是自己画个窗口：
    这些内容（检查报告、任务计划列表）本来就是**给人复制走的** ——
    出问题时用户要把它发给我。记事本可以直接全选复制，比只读窗口顺手。
    """
    if _has_console():
        print(body)
        return

    mac = sys.platform == "darwin"

    try:
        store.ensure_dirs()
        path = store.OUT_DIR / "last_report.txt"
        # utf-8-sig（带 BOM）：这个文件就是给人用记事本打开、然后复制粘贴给我的，
        # 存成不带 BOM 的 utf-8 时中文在有些编辑器里会变乱码。
        # Mac 上没有这个顾虑（TextEdit 两种都认），就不加 BOM 了。
        store.write_text(path, body, encoding="utf-8" if mac else "utf-8-sig")
        subprocess.Popen(
            ["open", "-t", str(path)] if mac else ["notepad.exe", str(path)],
            close_fds=True,
        )
    except Exception:
        # 连记事本都打不开就只剩最后一招：弹个框。至少别什么都不发生。
        try:
            import tkinter as tk
            from tkinter import messagebox
            root = tk.Tk()
            root.withdraw()
            messagebox.showinfo(title, body[:3000])
            root.destroy()
        except Exception:
            pass


# --------------------------------------------------------------------------
# 子命令
# --------------------------------------------------------------------------
def _cmd_check(rest: list[str]) -> int:
    """
    `check` —— 只验证订阅能不能用，不写任何数据文件。

    原来是 run-check.bat 跑 `python fetch.py --check`，报告直接打在控制台上。
    exe 没有控制台，所以这里把 stdout 抓进内存，再写成文件打开。
    """
    buf = io.StringIO()
    code = 1
    try:
        with redirect_stdout(buf):
            import fetch
            code = fetch.run(["--check", *rest])
    except BaseException as exc:                              # noqa: BLE001
        buf.write(f"\n[崩溃] {type(exc).__name__}: {exc}\n")

    text = buf.getvalue()
    # 报错信息里可能带着订阅链接（那等于一把钥匙），一律先抹掉再给用户看
    text = store.redact(text)
    header = "Canvas 截止日期 —— 订阅检查\n" + "=" * 62 + "\n"
    body = header + text + (
        "\n" + "=" * 62 + "\n"
        f"结论：{'看起来没问题' if code == 0 else '没通过（见上面的 [没通过] / [崩溃]）'}\n"
        "\n请把这些内容跟 Canvas 网页上显示的课程、日期核对一遍。\n"
    )
    _report("订阅检查", body)
    return code


def _cmd_autostart(rest: list[str]) -> int:
    """`autostart install` / `autostart uninstall`。"""
    if not rest or rest[0] not in ("install", "uninstall"):
        _report("开机自启",
                "用法：CanvasDeadlines.exe autostart install\n"
                "      CanvasDeadlines.exe autostart uninstall\n")
        return 2

    lines: list[str] = []

    def emit(message: str = "") -> None:
        lines.append(message)

    import autostart
    code = autostart.install(emit) if rest[0] == "install" else autostart.uninstall(emit)
    header = ("Canvas 截止日期 —— 开机自启\n"
              + ("安装" if rest[0] == "install" else "卸载") + "\n"
              + "=" * 62 + "\n")
    _report("开机自启", header + "\n".join(lines))
    return code


def _cmd_widget(rest: list[str]) -> int:
    sys.argv = [sys.argv[0], *rest]        # widget.main() 用 argparse 读 sys.argv
    import widget
    return widget.main()


def _cmd_setup(rest: list[str]) -> int:
    sys.argv = [sys.argv[0], *rest]
    import setup
    return setup.main()


def _cmd_fetch(rest: list[str]) -> int:
    """
    拉一次数据。**故意什么都不报** —— 它每天被任务计划叫起来一次，
    成功时弹个记事本出来是骚扰。

    失败了用户也会知道：fetch.py 会把原因写进 deadlines.json 的错误块，
    挂件顶上那条状态栏读到就变红并写明原因。日志另有一份在 out\\fetch_log.txt。
    """
    import fetch
    return fetch.main(rest)


def _cmd_diagnose(rest: list[str]) -> int:
    """离线检查界面和时区资源，不读取订阅凭证，不联网。"""
    import tkinter as tk
    import timeutil
    root = tk.Tk()
    root.withdraw()
    try:
        zone = timeutil.get_zone(timeutil.FALLBACK_TZ)
        result = {"ok": True, "tk": root.tk.eval("info patchlevel"), "timezone": zone.key}
        store.write_json(store.OUT_DIR / "diagnostic.json", result)
    finally:
        root.destroy()
    if "--quiet" not in rest:
        _report("离线检查", "界面和时区资源检查通过。")
    return 0


_USAGE = (
    "Canvas 截止日期桌面挂件\n"
    "\n"
    "  双击这个文件就行 —— 没配置过会弹出设置窗口，配置过就直接出挂件。\n"
    "\n"
    "  命令行用法（一般用不上）：\n"
    "    CanvasDeadlines.exe widget                 启动挂件\n"
    "    CanvasDeadlines.exe setup                  重新配置订阅链接\n"
    "    CanvasDeadlines.exe fetch [--quiet]        立刻拉一次数据\n"
    "    CanvasDeadlines.exe check                  检查订阅链接对不对\n"
    "    CanvasDeadlines.exe autostart install      装开机自启\n"
    "    CanvasDeadlines.exe autostart uninstall    卸开机自启\n"
)

_COMMANDS = {
    "widget": _cmd_widget,
    "setup": _cmd_setup,
    "fetch": _cmd_fetch,
    "diagnose": _cmd_diagnose,
    "check": _cmd_check,
    "autostart": _cmd_autostart,
}


def main(argv: list[str] | None = None) -> int:
    # Tcl 的旧版 Windows 路径处理在重定向目录中可能打不开绝对路径。
    # 使用同一资源的相对路径，不改变程序的工作目录或用户数据位置。
    if getattr(sys, "frozen", False):
        for key in ("TCL_LIBRARY", "TK_LIBRARY"):
            value = os.environ.get(key)
            if value:
                try:
                    os.environ[key] = os.path.relpath(value)
                except ValueError:  # 不同盘符仍使用原路径
                    pass
    store.setup_console()

    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        # 双击 exe。widget.main() 自己会判断「还没配过」并把 setup 拉起来。
        return _cmd_widget([])

    name, rest = args[0], args[1:]
    if name in ("-h", "--help", "/?", "help"):
        _report("用法", _USAGE)
        return 0

    handler = _COMMANDS.get(name)
    if handler is None:
        # 不认识的参数：可能是老用户习惯了 `--reset` 那种写法。
        # 不弹错误框（开机自启万一传错参数就变成弹框骚扰），记一笔日志就好。
        if name.startswith("-"):
            return _cmd_widget(args)
        _report("用法", _USAGE)
        return 2

    return handler(rest)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except BaseException as exc:                              # noqa: BLE001
        # 没有控制台，一个未捕获的异常会让进程静默消失 ——
        # 用户看到的是「双击了，没反应」。至少留下一条日志。
        try:
            import traceback
            store.ensure_dirs()
            store.append_log(["程序崩溃：" + f"{type(exc).__name__}: {exc}"]
                             + traceback.format_exc().splitlines()[-8:])
        except Exception:
            pass
        try:
            import tkinter as tk
            from tkinter import messagebox
            root = tk.Tk()
            root.withdraw()
            messagebox.showerror("Canvas 截止日期",
                                 f"出错了：{type(exc).__name__}: {exc}\n\n"
                                 "详细记录在 out\\fetch_log.txt 里。")
            root.destroy()
        except Exception:
            pass
        raise SystemExit(1)
