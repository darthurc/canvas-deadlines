# -*- coding: utf-8 -*-
r"""
autostart.py —— 注册 / 撤销开机自启

两个平台两套机制，互不相干，按 sys.platform 分派：

    Windows —— 3 个任务计划（schtasks / Register-ScheduledTask），下面这一大半
    macOS   —— 3 个 launchd agent（~/Library/LaunchAgents/*.plist），见文件后半

**为什么 Mac 上不用 cron**：cron 在 macOS 上已经半废弃，而且笔记本合盖时
错过的任务它**不会补跑**。launchd 会 —— 系统唤醒后发现 StartCalendarInterval
的时间点已经过去，就立刻补一次。这正是这条需求的核心（早上 7:47 电脑多半还合着盖）。

用法：
    python autostart.py install
    python autostart.py uninstall

（下面这段 Windows 部分的说明保留原样。）

以前这一步是两个 .ps1 文件（`autostart-install.ps1` / `autostart-uninstall.ps1`），
由对应的两个 .bat 双击调用。之所以改成 Python 现生成：

    那两个 ps1 里**写死了** `旧电脑的 Python 路径`。
    我自己这台机器上没问题，别人拷过去就是「找不到 pythonw.exe，退出」。
    打包成 exe 之后更彻底 —— 连 Python 都没有了，那个路径根本不存在。

所以改成这里现算「该用什么程序、带什么参数」：
    没打包 → pythonw.exe + fetch.py
    打包后 → 这个 exe 自己 + 子命令（见 app.py）
这个区别只由 store.child_command() 决定，别的地方不用知道。

**为什么还是要借 PowerShell，不直接用 schtasks.exe**：
关键是 StartWhenAvailable —— 笔记本早上 7:47 合着盖的时候任务不会跑，
有了这个设置，开机后 Windows 会补跑一次。schtasks.exe 没有对应的开关。
所以这里生成一段 ps1 脚本（写到临时文件里再执行，避免命令行引号地狱），
跑完删掉。

    用法：  python autostart.py install
            python autostart.py uninstall
"""

from __future__ import annotations

import argparse
import os
import plistlib
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))

import store

PREFIX = "CanvasDeadlines"
DAILY_AT = "07:47"          # 故意避开整点：很多定时任务挤在整点，机器可能还没醒

# Mac 上对应的东西。label 反过来写（com.域名.名字）是 macOS 的惯例，
# 这个字符串会显示在「系统设置 → 通用 → 登录项」里给用户看。
MAC_LABEL_PREFIX = "com.canvasdeadlines"
MAC_AGENTS_DIR = Path.home() / "Library" / "LaunchAgents"

Emit = Callable[[str], None]


# --------------------------------------------------------------------------
# 拼 PowerShell
# --------------------------------------------------------------------------
def _ps_quote(text: str) -> str:
    """PowerShell 单引号字符串里，单引号要写两遍。路径带引号时用得上。"""
    return "'" + str(text).replace("'", "''") + "'"


def _action(what: str, *args: str) -> tuple[str, str, str]:
    """
    把「跑哪个程序、带什么参数」交给 store 去算，这里只管拼成 ps1 要的形式。
    返回 (可执行文件, 参数字符串, 工作目录)。
    """
    cmd, cwd = store.child_command(what, *args)
    exe = cmd[0]
    # PowerShell 的 -Argument 是一个字符串，带空格的路径要自己加引号
    arg = " ".join(f'"{a}"' for a in cmd[1:])
    return exe, arg, cwd


def _build_install_script() -> str:
    fetch_exe, fetch_arg, cwd = _action("fetch", "--quiet")
    widget_exe, widget_arg, _ = _action("widget")

    return f"""$ErrorActionPreference = 'Stop'

$Prefix = {_ps_quote(PREFIX)}
$UserId = "$env:USERDOMAIN\\$env:USERNAME"
$Cwd    = {_ps_quote(cwd)}
$Exe    = {_ps_quote(fetch_exe)}
$DailyAt = {_ps_quote(DAILY_AT)}

Write-Host ''
Write-Host '=== 注册开机自启 ===' -ForegroundColor Cyan
Write-Host ''

if (-not (Test-Path $Exe)) {{
    Write-Host "[错误] 找不到这个程序：$Exe" -ForegroundColor Red
    Write-Host '       自启任务要靠它来跑，找不到就没法装。'
    exit 1
}}

Write-Host "  程序：$Exe"
Write-Host "  数据：$Cwd"
Write-Host "  用户：$UserId"
Write-Host ''

$principal = New-ScheduledTaskPrincipal -UserId $UserId -LogonType Interactive -RunLevel Limited

$baseSettings = @{{
    AllowStartIfOnBatteries    = $true    # 笔记本用电池时也跑
    DontStopIfGoingOnBatteries = $true
    StartWhenAvailable         = $true    # 错过了就补跑 —— 关键就这一条
    MultipleInstances          = 'IgnoreNew'
    ExecutionTimeLimit         = (New-TimeSpan -Minutes 10)
}}

$fetchAction  = New-ScheduledTaskAction -Execute {_ps_quote(fetch_exe)} `
                    -Argument {_ps_quote(fetch_arg)} -WorkingDirectory $Cwd
$widgetAction = New-ScheduledTaskAction -Execute {_ps_quote(widget_exe)} `
                    -Argument {_ps_quote(widget_arg)} -WorkingDirectory $Cwd

$dailyTrigger = New-ScheduledTaskTrigger -Daily -At $DailyAt
Register-ScheduledTask -TaskName "$Prefix-Daily" `
    -Action $fetchAction -Trigger $dailyTrigger -Principal $principal `
    -Settings (New-ScheduledTaskSettingsSet @baseSettings) `
    -Description '每天早上从 Canvas 拉取所有截止日期' -Force | Out-Null
Write-Host "  [OK] $Prefix-Daily    每天 $DailyAt 拉取数据" -ForegroundColor Green

$logonTrigger = New-ScheduledTaskTrigger -AtLogOn -User $UserId
$logonTrigger.Delay = 'PT2M'
Register-ScheduledTask -TaskName "$Prefix-Logon" `
    -Action $fetchAction -Trigger $logonTrigger -Principal $principal `
    -Settings (New-ScheduledTaskSettingsSet @baseSettings) `
    -Description '登录后拉一次 Canvas 截止日期（等 WiFi 连上再跑）' -Force | Out-Null
Write-Host "  [OK] $Prefix-Logon    登录后 2 分钟拉取数据" -ForegroundColor Green

# 挂件是常驻的，执行时限必须设成「无限制」，否则 10 分钟后会被杀掉
$widgetSettings = @{{}}
$baseSettings.Keys | ForEach-Object {{ $widgetSettings[$_] = $baseSettings[$_] }}
$widgetSettings.ExecutionTimeLimit = (New-TimeSpan -Seconds 0)   # PT0S = 不限制

$widgetTrigger = New-ScheduledTaskTrigger -AtLogOn -User $UserId
Register-ScheduledTask -TaskName "$Prefix-Widget" `
    -Action $widgetAction -Trigger $widgetTrigger -Principal $principal `
    -Settings (New-ScheduledTaskSettingsSet @widgetSettings) `
    -Description '登录时启动 Canvas 截止日期桌面挂件' -Force | Out-Null
Write-Host "  [OK] $Prefix-Widget   登录时启动挂件" -ForegroundColor Green

Write-Host ''
Write-Host '已注册的任务：' -ForegroundColor Cyan
Get-ScheduledTask -TaskName "$Prefix-*" -ErrorAction SilentlyContinue |
    Select-Object TaskName, State |
    Format-Table -AutoSize |
    Out-String -Width 200 |
    Write-Host

Write-Host '  这些任务都在你的用户身份下运行，不需要管理员权限。'
Write-Host '  重启一次电脑就能看到挂件自己出现。'
Write-Host ''
Write-Host '  想立刻看效果（不用重启）：在挂件上右键 → 立即刷新。'
Write-Host ''
"""


def _build_uninstall_script() -> str:
    return f"""$ErrorActionPreference = 'Stop'
$Prefix = {_ps_quote(PREFIX)}

Write-Host ''
Write-Host '=== 撤销开机自启 ===' -ForegroundColor Cyan
Write-Host ''

$found = Get-ScheduledTask -TaskName "$Prefix-*" -ErrorAction SilentlyContinue
if (-not $found) {{
    Write-Host '  没有找到已注册的任务，可能之前就没装过。' -ForegroundColor Yellow
    Write-Host ''
    exit 0
}}

foreach ($task in $found) {{
    try {{
        Unregister-ScheduledTask -TaskName $task.TaskName -Confirm:$false
        Write-Host "  [已删除] $($task.TaskName)" -ForegroundColor Green
    }}
    catch {{
        Write-Host "  [删除失败] $($task.TaskName)：$($_.Exception.Message)" -ForegroundColor Red
    }}
}}

Write-Host ''
Write-Host '  配置和数据都没动，还留在原来的文件夹里。'
Write-Host '  挂件如果还开着，在它上面右键 → 退出。'
Write-Host ''
"""


# --------------------------------------------------------------------------
# 执行
# --------------------------------------------------------------------------
def _run_powershell(script: str, emit: Emit) -> int:
    """
    把脚本写进临时文件再让 PowerShell 执行。

    不直接把多行脚本塞进 `-Command`：那要跟 cmd.exe 和 PowerShell 两层引号
    搏斗，路径里带个单引号就崩了。临时文件没这个问题。

    编码必须是 utf-8-sig：Windows PowerShell 5.1 读 .ps1 时，
    没有 BOM 就按系统 ANSI（中文机器上是 GBK）解码，脚本里的中文会变乱码。

    开头那句 [Console]::OutputEncoding 也是同一个问题的另一半：
    脚本打印中文时，PowerShell 默认按控制台的 OEM 代码页（这里是 GBK）往
    stdout 写，而我这边按 utf-8 读 —— 结果就是「[OK] ���Ĳ���」。
    实测加上这一句就对了。
    """
    tmp = None
    try:
        fd, name = tempfile.mkstemp(prefix="canvas-autostart-", suffix=".ps1")
        os.close(fd)
        tmp = Path(name)
        tmp.write_text(
            "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8\n" + script,
            encoding="utf-8-sig",
        )

        proc = subprocess.run(
            ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
             "-File", str(tmp)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except FileNotFoundError:
        emit("  [错误] 找不到 powershell.exe —— 这个功能要靠它注册任务计划。")
        return 1
    except OSError as exc:
        emit(f"  [错误] 执行失败：{exc}")
        return 1
    finally:
        if tmp is not None:
            try:
                tmp.unlink()
            except OSError:
                pass

    out = (proc.stdout or "").strip()
    err = (proc.stderr or "").strip()
    if out:
        emit(out)
    if proc.returncode != 0:
        emit("")
        emit(f"  [失败] PowerShell 返回了错误（退出码 {proc.returncode}）。")
        if err:
            emit(err)
        emit("  提示：这一步不需要管理员权限，但如果被组策略拦住了，")
        emit("        可以在开始菜单搜「任务计划程序」，手动看一眼。")
        return proc.returncode or 1
    return 0


def install_windows(emit: Emit = print) -> int:
    """注册 3 个任务计划。可以重复运行（-Force 会覆盖旧的）。"""
    return _run_powershell(_build_install_script(), emit)


def uninstall_windows(emit: Emit = print) -> int:
    """删掉 3 个任务计划。可以重复运行（没有也不报错）。"""
    return _run_powershell(_build_uninstall_script(), emit)


# ==========================================================================
# macOS：launchd
# ==========================================================================
# 三个 agent，跟 Windows 那边一一对应：
#
#   com.canvasdeadlines.daily    每天 07:47 拉一次（StartCalendarInterval）
#   com.canvasdeadlines.logon    登录后 90 秒拉一次（等 WiFi 连上）
#   com.canvasdeadlines.widget   登录时启动挂件（一直在后台跑着）
#
# 关于 launchd 的三个坑，都在下面代码里绕过了：
#   1. **plist 要写成 ~/Library/LaunchAgents/名字.plist**，文件名不重要，
#      真正起作用的是里面的 Label，而且两者最好保持一致，不然排查时会对不上。
#   2. **光写文件不生效**，必须 `launchctl bootstrap gui/$UID 路径` 把它交给
#      launchd。换了新机器/新 macOS 版本之后 bootstrap 偶尔不认，退回老命令
#      `launchctl load -w`（已经标了废弃，但一直还能用）。
#   3. **重复安装要先 bootout**，否则 bootstrap 会报「service already loaded」
#      直接失败 —— 用户重跑一次安装脚本就卡在这儿，很难自己看出来。


def _launchd_domain() -> str:
    """launchd 的「GUI 会话」域名。挂件要显示窗口，必须挂在这个域里。"""
    return f"gui/{os.getuid()}"


def _plist_path(label: str) -> Path:
    return MAC_AGENTS_DIR / f"{label}.plist"


def _run(args: list[str], timeout: float = 20.0) -> tuple[int, str]:
    """跑一条命令，返回 (退出码, 输出)。找不到命令/超时都当成失败，不抛异常。"""
    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        return 127, f"找不到命令：{args[0]}"
    except subprocess.TimeoutExpired:
        return 124, f"命令超时：{' '.join(args)}"
    except OSError as exc:
        return 126, f"执行失败：{exc}"
    text = ((proc.stdout or "") + (proc.stderr or "")).strip()
    return proc.returncode, text


def _sh_quote(text: str) -> str:
    """把一段文本包成 shell 里安全的单引号字符串。"""
    return "'" + str(text).replace("'", "'\\''") + "'"


def _log_paths(name: str) -> tuple[Path, Path]:
    """
    launchd 的 stdout / stderr 落盘位置。

    这两个文件很重要：launchd 跑的东西**没有终端**，连「模块导入失败」这种
    最基础的问题都只能从这里看。fetch.py 自己的日志（out/fetch_log.txt）
    只有进到 fetch.run() 之后才开始写，之前崩掉是空的。
    """
    log_dir = store.STATE_DIR / "launchd"
    log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir / f"{name}.log", log_dir / f"{name}.err"


def _mac_jobs() -> list[dict[str, Any]]:
    """
    三个 agent 的定义。放在一个函数里，安装和卸载都用同一份 label 列表，
    免得改了一处忘了另一处 —— 那样会剩下一个删不掉的僵尸任务。
    """
    fetch_cmd, cwd = store.child_command("fetch", "--quiet")
    widget_cmd, _ = store.child_command("widget")

    daily_out, daily_err = _log_paths("daily")
    logon_out, logon_err = _log_paths("logon")
    widget_out, widget_err = _log_paths("widget")

    hour, minute = (int(part) for part in DAILY_AT.split(":"))

    return [
        {
            "label": f"{MAC_LABEL_PREFIX}.daily",
            "title": "每天拉取",
            "desc": f"每天 {DAILY_AT} 从 Canvas 拉取所有截止日期",
            # StartCalendarInterval：launchd 会在系统唤醒后补跑错过的时间点，
            # 这是选 launchd 而不是 cron 的唯一理由。
            "plist": {
                "Label": f"{MAC_LABEL_PREFIX}.daily",
                "ProgramArguments": fetch_cmd,
                "WorkingDirectory": cwd,
                "StartCalendarInterval": {"Hour": hour, "Minute": minute},
                "RunAtLoad": False,
                "ProcessType": "Background",
                "StandardOutPath": str(daily_out),
                "StandardErrorPath": str(daily_err),
            },
        },
        {
            "label": f"{MAC_LABEL_PREFIX}.logon",
            "title": "登录后拉取",
            "desc": "登录后 90 秒拉一次（等 WiFi 连上再跑）",
            # launchd 没有「延迟 N 秒再跑」这种键，所以拿 sh 睡一下。
            # 不睡的话，登录瞬间网还没连上，会白白失败一次。
            "plist": {
                "Label": f"{MAC_LABEL_PREFIX}.logon",
                "ProgramArguments": [
                    "/bin/sh", "-c",
                    "sleep 90; exec " + " ".join(_sh_quote(a) for a in fetch_cmd),
                ],
                "WorkingDirectory": cwd,
                "RunAtLoad": True,
                "ProcessType": "Background",
                "StandardOutPath": str(logon_out),
                "StandardErrorPath": str(logon_err),
            },
        },
        {
            "label": f"{MAC_LABEL_PREFIX}.widget",
            "title": "挂件",
            "desc": "登录时启动桌面挂件",
            "plist": {
                "Label": f"{MAC_LABEL_PREFIX}.widget",
                "ProgramArguments": widget_cmd,
                "WorkingDirectory": cwd,
                "RunAtLoad": True,
                # 挂件是 GUI，不能标 Background（那会降优先级，还可能拿不到窗口）
                "LimitLoadToSessionType": "Aqua",
                "StandardOutPath": str(widget_out),
                "StandardErrorPath": str(widget_err),
            },
        },
    ]


def _bootout(label: str) -> None:
    """把已经注册的 agent 摘掉。没注册过就什么都不做（不报错）。"""
    _run(["launchctl", "bootout", f"{_launchd_domain()}/{label}"])
    _run(["launchctl", "bootout", _launchd_domain(), str(_plist_path(label))])


def install_mac(emit: Emit = print) -> int:
    """写 3 个 plist 并交给 launchd。可以重复运行。"""
    if sys.platform != "darwin":
        emit("  这个分支只在 macOS 上跑得起来。")
        return 2

    emit("=== 注册开机自启（launchd）===")
    emit()

    try:
        MAC_AGENTS_DIR.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        emit(f"  [错误] 建不了这个目录：{MAC_AGENTS_DIR}")
        emit(f"         {exc}")
        return 1

    emit(f"  程序：{store.child_command('fetch', '--quiet')[0][0]}")
    emit(f"  数据：{store.ROOT}")
    emit(f"  位置：{MAC_AGENTS_DIR}")
    emit()

    failures = 0
    for job in _mac_jobs():
        label = job["label"]
        path = _plist_path(label)

        # 先摘掉旧的：重复安装时 bootstrap 会因为「已经加载了」直接失败。
        # 摘完等一下下 —— bootout 是异步生效的，紧跟着 bootstrap 偶尔会撞上。
        _bootout(label)
        time.sleep(0.4)

        try:
            with path.open("wb") as fh:
                plistlib.dump(job["plist"], fh, fmt=plistlib.FMT_XML)
        except OSError as exc:
            emit(f"  [失败] 写不了 {path.name}：{exc}")
            failures += 1
            continue

        code, note = _run(["launchctl", "bootstrap", _launchd_domain(), str(path)])
        if code != 0:
            # 新版 bootstrap 偶尔不认，退回老命令（load -w 已经标废弃但还能用）
            code2, note2 = _run(["launchctl", "load", "-w", str(path)])
            if code2 != 0:
                emit(f"  [失败] {job['title']}（{label}）没注册上")
                if note:
                    emit(f"         bootstrap：{note.splitlines()[0] if note else ''}")
                if note2:
                    emit(f"         load：{note2.splitlines()[0] if note2 else ''}")
                failures += 1
                continue
        emit(f"  [OK] {label}")
        emit(f"       {job['desc']}")

    emit()
    if failures:
        emit(f"  有 {failures} 个没装上。把上面的输出原样发我。")
        emit()
        return 1

    emit("  这些 agent 都在你的用户身份下运行，不需要管理员密码。")
    emit("  重启一次（或者注销再登录）就能看到挂件自己出现。")
    emit()
    emit("  想立刻看效果（不用重启）：双击「Mac-立即刷新.command」。")
    emit()
    emit("  注：文件夹挪了位置、或者换了 Python，要重新跑一次这个安装。")
    emit("      因为 plist 里记的是当前这个 python 的绝对路径。")
    emit()
    return 0


def uninstall_mac(emit: Emit = print) -> int:
    """摘掉 3 个 agent 并删掉 plist。可以重复运行。"""
    if sys.platform != "darwin":
        emit("  这个分支只在 macOS 上跑得起来。")
        return 2

    emit("=== 撤销开机自启（launchd）===")
    emit()

    found = 0
    for job in _mac_jobs():
        label = job["label"]
        path = _plist_path(label)
        had_file = path.exists()

        _bootout(label)

        if had_file:
            try:
                path.unlink()
                found += 1
                emit(f"  [已删除] {path.name}")
            except OSError as exc:
                emit(f"  [删除失败] {path.name}：{exc}")
        else:
            emit(f"  [没有] {path.name}（本来就没装）")

    emit()
    if not found:
        emit("  没有找到已注册的 agent，可能之前就没装过。")
    emit("  配置和数据都没动，还留在原来的文件夹里。")
    emit("  挂件如果还开着，在它上面右键 → 退出。")
    emit()
    return 0


# ==========================================================================
# 分派
# ==========================================================================
def install(emit: Emit = print) -> int:
    """注册开机自启。可以重复运行。"""
    if sys.platform == "darwin":
        return install_mac(emit)
    return install_windows(emit)


def uninstall(emit: Emit = print) -> int:
    """撤销开机自启。可以重复运行（没有也当成功）。"""
    if sys.platform == "darwin":
        return uninstall_mac(emit)
    return uninstall_windows(emit)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Canvas 截止日期 —— 开机自启")
    parser.add_argument("action", choices=["install", "uninstall"],
                        help="install = 装，uninstall = 卸")
    args = parser.parse_args(argv)
    store.setup_console()
    return install() if args.action == "install" else uninstall()


if __name__ == "__main__":
    raise SystemExit(main())
