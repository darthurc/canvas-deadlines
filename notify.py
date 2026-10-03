# -*- coding: utf-8 -*-
"""
notify.py —— 桌面通知

设计原则：**通知失败绝不能影响数据更新。**
拉数据才是这个工具的核心价值，弹不弹通知是锦上添花。
所以这里的每一个函数都吞掉所有异常，最坏情况是「没弹通知」，而不是「今天没拉到数据」。

Windows 上两条路径：
    1. winotify（如果装了）—— 干净、纯 Python
    2. PowerShell 调 Windows 原生 Toast API —— 不用装任何东西，但 fiddly
macOS 上一条：osascript 调系统的通知中心（零依赖，系统自带）。

哪条都失败就静默跳过，并且把原因记进日志（这样你翻 fetch_log.txt 时知道是通知没弹，
而不是数据没拉到 —— 这两个问题的处理方式完全不同）。

去重：state/notified.json 记 {任务key: "2026-09-23"}。
同一个任务一天最多提醒一次。没有这层的话，你每天早上开机会被同一个作业刷屏，
然后就再也不看通知了 —— 那比不弹还糟。
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

import store
import timeutil

# 下面这一整段（PS_APP_ID / _PS_SCRIPT / _via_winotify / _via_powershell）
# 只在 Windows 上用得到，Mac 走 _via_osascript。留着不影响 Mac 跑 ——
# 它们不会被执行，也 import 不到任何 Windows 专有的东西。
#
# PowerShell 需要知道「这条通知是谁发的」。Windows 要求用已注册的 AppUserModelID，
# 用 PowerShell 自己的就行 —— 这样通知会显示成来自 Windows PowerShell，
# 不需要我们去注册什么应用，也不需要管理员权限。
PS_APP_ID = r"{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe"

_PS_SCRIPT = r"""
param([string]$XmlPath, [string]$AppId)
$ErrorActionPreference = "Stop"
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType=WindowsRuntime] > $null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType=WindowsRuntime] > $null
$xml = New-Object Windows.Data.Xml.Dom.XmlDocument
$xml.LoadXml((Get-Content -Path $XmlPath -Raw -Encoding UTF8))
$toast = New-Object Windows.UI.Notifications.ToastNotification $xml
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($AppId).Show($toast)
"""


# --------------------------------------------------------------------------
# 发送
# --------------------------------------------------------------------------
def _via_winotify(title: str, body: str) -> bool:
    """winotify 装了就优先用这条。"""
    try:
        from winotify import Notification  # type: ignore
    except ImportError:
        return False
    try:
        toast = Notification(app_id="Canvas 截止日期", title=title, msg=body)
        toast.show()
        return True
    except Exception:
        return False


def _via_powershell(title: str, body: str) -> bool:
    """
    不装任何东西的兜底。把 XML 落到临时文件再让 PowerShell 读，
    避免中文经过命令行参数时被编码搞坏（Windows 命令行传中文是个老坑）。
    """
    xml = (
        '<toast duration="long"><visual><binding template="ToastGeneric">'
        f"<text>{escape(title)}</text>"
        f"<text>{escape(body)}</text>"
        "</binding></visual></toast>"
    )

    tmp_dir = store.STATE_DIR / "notify"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    xml_path = tmp_dir / "toast.xml"
    ps_path = tmp_dir / "toast.ps1"

    try:
        xml_path.write_text(xml, encoding="utf-8")
        ps_path.write_text(_PS_SCRIPT, encoding="utf-8")
        proc = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive",
             "-ExecutionPolicy", "Bypass",
             "-File", str(ps_path),
             "-XmlPath", str(xml_path),
             "-AppId", PS_APP_ID],
            capture_output=True, timeout=25,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return proc.returncode == 0
    except Exception:
        return False


# macOS 的通知：osascript 一行搞定。
#
# **正文和标题当成参数传，不拼进脚本字符串** —— AppleScript 的字符串字面量
# 不能跨行、还得分清 " 和 \ 的转义，把中文作业名拼进去迟早出事。
# 用 `on run argv` 接参数就完全没有转义问题（这是 osascript 官方支持的用法：
# -e 后面的额外参数会原样进 argv）。
#
# 一个改不掉的观感问题：Mac 上通知的署名是「脚本编辑器」（Script Editor），
# 因为通知是它代发的。要换成别的名字得打一个 .app 包并注册 bundle id ——
# 这跟「不打 .app」的路线冲突，不值得，先这样。
_OSA_SCRIPT = (
    "on run argv\n"
    "    display notification (item 1 of argv) with title (item 2 of argv)\n"
    "end run"
)


def _via_osascript(title: str, body: str) -> bool:
    try:
        proc = subprocess.run(
            ["osascript", "-e", _OSA_SCRIPT, body, title],
            capture_output=True, timeout=20,
        )
        return proc.returncode == 0
    except Exception:
        return False


def notify(title: str, body: str) -> tuple[bool, str]:
    """
    发一条通知。返回 (成功了吗, 说明)。
    调用方拿这个说明去写日志 —— 但不该因为它失败就中断流程。
    """
    if sys.platform == "darwin":
        if _via_osascript(title, body):
            return True, "osascript"
        return False, "osascript 没发出去（通知没弹，但数据不受影响）"

    if _via_winotify(title, body):
        return True, "winotify"
    if _via_powershell(title, body):
        return True, "powershell"
    return False, "两条路径都失败了（通知没弹，但数据不受影响）"


# --------------------------------------------------------------------------
# 去重
# --------------------------------------------------------------------------
def _load_notified() -> dict[str, str]:
    data = store.read_json(store.NOTIFIED_FILE, default={}) or {}
    return data if isinstance(data, dict) else {}


def _save_notified(data: dict[str, str]) -> None:
    # 顺手清掉超过 30 天的记录，不然这个文件会一直长
    today = datetime.now()
    pruned = {
        key: day for key, day in data.items()
        if _days_ago(day, today) <= 30
    }
    store.write_json(store.NOTIFIED_FILE, pruned)


def _days_ago(day: str, today: datetime) -> float:
    try:
        when = datetime.strptime(day, "%Y-%m-%d")
    except (ValueError, TypeError):
        return 0.0
    return (today - when).total_seconds() / 86400.0


# --------------------------------------------------------------------------
# 业务逻辑
# --------------------------------------------------------------------------
def notify_urgent(tasks: list[dict[str, Any]], notify_hours: float) -> list[str]:
    """
    找出「没交 且 快到期」的任务，每个每天提醒一次。
    返回写进日志的说明文字。

    刻意不提醒的两种情况：
        * 非日历订阅的 submitted=None：不猜测提交状态。
          日历订阅的待处理作业可以提醒，但必须说明尚未核对提交状态。
        * 已经过期很久的（超过 7 天）—— 那已经不是「快到期」，是「已经错过了」，
          该在挂件上红着脸显示，而不是天天弹窗烦你
    """
    if notify_hours <= 0:
        return ["通知已关闭（display.notify_hours = 0）"]

    now = timeutil.now_utc()
    today = datetime.now().strftime("%Y-%m-%d")
    notified = _load_notified()
    messages: list[str] = []
    fired = 0

    for task in tasks:
        unknown_feed = (
            task.get("source") == "feed"
            and task.get("submitted") is None
            and task.get("needs_action") is True
        )
        if task.get("excused") or task.get("submitted") is True:
            continue
        if task.get("submitted") is not False and not unknown_feed:
            continue
        due = timeutil.parse_iso(task.get("due_utc"))
        if due is None:
            continue

        left = timeutil.remaining_seconds(due, now)
        if left < -7 * 86400:
            continue
        if left > notify_hours * 3600:
            continue

        key = task.get("key") or task.get("title") or "?"
        if notified.get(key) == today:
            continue

        if left < 0:
            title = f"已经过期：{task.get('title', '')}"
            body = (f"{task.get('course_code', '')} · 截止 "
                    f"{task.get('due_display', '')}\n{timeutil.short_remaining(due, now)}")
        else:
            title = f"{timeutil.short_remaining(due, now)}后截止"
            body = (f"{task.get('course_code', '')} · {task.get('title', '')}\n"
                    f"{task.get('due_display', '')}")

        if unknown_feed:
            body += "\n日历订阅无法确认是否已交，请核对并手动标记。"
        ok, how = notify(title, body)
        messages.append(
            f"通知{'成功' if ok else '失败'}（{how}）：{title} / {task.get('course_code', '')}"
        )
        if ok:
            notified[key] = today
            fired += 1
        else:
            # 失败就不记去重，下次还有机会弹
            break

    if fired:
        _save_notified(notified)
    elif not messages:
        messages.append(f"{notify_hours:g} 小时内没有需要提醒的未交任务")
    return messages


def test_notification() -> int:
    """`python notify.py` 直接跑，用来确认通知这条路在你机器上是通的。"""
    store.setup_console()
    print("=" * 58)
    print("  测试桌面通知")
    print("=" * 58)
    ok, how = notify(
        "Canvas 截止日期 · 测试",
        "如果你看到这条，说明通知是通的。\n（这条是手动测试，不是真的有作业要交）",
    )
    if ok:
        print(f"\n  ✓ 通知已发出（走的 {how}）")
        print("  看看屏幕上有没有弹出来"
              "（Windows 在右下角，Mac 在右上角）。")
        if sys.platform == "darwin":
            print("\n  没弹的话去 系统设置 → 通知，找到「脚本编辑器」，")
            print("  把「允许通知」打开。macOS 的通知权限只认第一次问你的那个回答，")
            print("  拒过一次就不会再问了。")
        return 0
    print("\n  ✗ 没发出去。")
    print("  这不影响拉数据和挂件，只是没有弹窗提醒。")
    print("  想修的话：双击拉数据那个文件，再翻 out/fetch_log.txt 看具体报错。")
    return 1


if __name__ == "__main__":
    raise SystemExit(test_notification())
