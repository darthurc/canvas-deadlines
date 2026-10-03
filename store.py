# -*- coding: utf-8 -*-
"""
store.py —— 路径常量、配置读写、JSON 原子读写

这一层不联网、不打印业务信息，只负责三件事：
    1. 文件放哪（别在别的模块里硬编码路径）
    2. 配置怎么合并（缺键自动补默认值，以后加新设置不会弄坏旧 config.json）
    3. 写坏了怎么救（原子写 + 损坏先备份再从默认值重建，绝不静默丢数据）

照搬 photo-cleaner 的 store.py 套路，那几个坑已经踩过了。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Any

def _find_root() -> Path:
    """
    数据放哪儿。

    打包成 exe 之后，`__file__` 指向的是 PyInstaller 解压出来的临时目录
    （%TEMP%\\_MEIxxxxxx），那个目录每次运行都不一样、退出就被删掉。
    要是还把 config.json / state / out 写在那儿，用户会看到：
        「刚配好的链接，关掉再开又没了」
    —— 而且是**下一次运行**才发作，最难查的那种 bug。

    所以打包之后一律以 **exe 自己所在的目录**为准：用户把 exe 放哪儿，
    config.json 就生成在哪儿，跟他双击的那个文件在一起，看得见摸得着。

    Mac 上多一层：`Foo.app` 是个**目录**，数据写进 Contents/MacOS/ 就等于写在
    包里面 —— 下次重新拷贝/替换这个 app，数据和配置一起消失。所以打包成 .app
    之后一律挪到 ~/Library/Application Support/ 下。
    （源码版不走这条路：源码放在哪个文件夹，数据就放在哪个文件夹，
    跟 Windows 上一样，看得见摸得着。）
    """
    if getattr(sys, "frozen", False):                 # PyInstaller 打出来的 exe
        if sys.platform == "darwin":
            return Path.home() / "Library" / "Application Support" / "CanvasDeadlines"
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def _find_assets() -> Path:
    """
    只读的模板文件（ui.html）在哪儿。

    这个跟 _find_root 是**相反**的：ui.html 是跟着程序走的资源，
    不是用户数据，所以要待在包里 —— 打包后它在 sys._MEIPASS 里。
    """
    bundled = getattr(sys, "_MEIPASS", None)
    return Path(bundled) if bundled else Path(__file__).resolve().parent


ROOT = _find_root()
ASSETS = _find_assets()
STATE_DIR = ROOT / "state"
OUT_DIR = ROOT / "out"

CONFIG_FILE = ROOT / "config.json"
WIDGET_STATE_FILE = STATE_DIR / "widget.json"
NOTIFIED_FILE = STATE_DIR / "notified.json"
TOKEN_STATE_FILE = STATE_DIR / "token.json"

# 日历订阅拿不到「交没交」的状态，所以由你自己在挂件上右键标记。
# 这个文件记的就是你标过的那些：任务 key → 标记信息。
MARKED_FILE = STATE_DIR / "marked.json"

# 排查用：把订阅原文的结构摘要写这儿（只记属性名和头两个事件，不记全量）
FEED_DEBUG_FILE = STATE_DIR / "feed_debug.txt"

DEADLINES_FILE = OUT_DIR / "deadlines.json"
HTML_FILE = OUT_DIR / "deadlines.html"
TEXT_FILE = OUT_DIR / "deadlines.txt"
LOG_FILE = OUT_DIR / "fetch_log.txt"


# --------------------------------------------------------------------------
# 默认配置
# --------------------------------------------------------------------------
# 这里就是「配置模板」。config.json 不存在时自动写出去，存在时缺的键从这儿补。
# JSON 不支持注释，所以用 "_说明" / "_xxx说明" 这种键当行内文档 —— 读取时会被忽略。
DEFAULT_CONFIG: dict[str, Any] = {
    "_说明": "改完这个文件，下次拉取就会生效（不用重启什么）。"
             "必须填的只有 canvas.calendar_feed_url 这一项 —— "
             "不想手改的话，双击「首次设置.bat」（Mac 上是「Mac-2-首次设置.command」）"
             "有个窗口让你粘。",
    "canvas": {
        "_说明": "墨大关闭了学生自助生成 token 的权限，所以改用「日历订阅链接」。"
                 "必填的只有 calendar_feed_url 这一项。",
        "calendar_feed_url": "",
        "_calendar_feed_url说明": "怎么拿：Canvas 左边深色竖条 → Calendar（日历）→ "
                                  "右侧栏拉到最下面 → 点 Calendar Feed → 复制弹出的那串网址"
                                  "（以 .ics 结尾），粘到引号中间。",
        "_calendar_feed_url安全": "这串网址等于一把钥匙：谁拿到都能看到你的课程日历，"
                                  "但看不到成绩、也改不了任何东西。不要发群里、不要提交到 GitHub。"
                                  "想换一把：回到 Calendar Feed 那里点 Reset，旧网址立刻失效。",
        "base_url": "https://canvas.lms.unimelb.edu.au",
        "token": "",
        "_token说明": "留空即可。墨大不让学生生成 API token，所以用不上。"
                      "如果哪天学校开放了，填在这里会自动启用（并优先于日历订阅）。"
                      "它等于你的登录凭证，不要发到群里、不要提交到 GitHub；"
                      "随时可以在同一个页面点 Delete 撤销，撤销后这个程序立刻失效。",
        "timeout": 20,
        "_timeout说明": "单个请求的超时秒数。",
        "max_retries": 3,
        "_max_retries说明": "网络错误重试几次（认证错误不重试，重试也没用）。",
        "token_days": 30,
        "_token_days说明": "Canvas 通常给学生 token 设 30 天有效期。挂件会在快到期时提前提醒你换新的。"
                           "如果你生成时看到有效期不是 30 天，改成实际天数。",
        "use_planner": True,
        "_use_planner说明": "True = 用 Canvas 的 Planner 接口，一次拿到所有课程的作业/测验/事件，"
                            "而且日期已经按你的 section 个性化过。如果这个接口在墨大被关了，"
                            "改成 False 会退回逐课程拉取的方式（慢一点但更稳）。",
    },
    "display": {
        "_说明": "挂件显示相关的设置。改完要重启挂件"
                 "（右键 → 退出，再双击启动挂件的那个文件）。",
        "top_n": 5,
        "_top_n说明": "默认显示最近几个任务。右键菜单里可以临时切换成「显示全部」。",
        "show_submitted": False,
        "_show_submitted说明": "是否显示已经交过的任务。",
        "past_days": 14,
        "_past_days说明": "往回看几天。设成 14 就会把最近两周已经过期但没交的任务也列出来提醒你。",
        "future_days": 180,
        "_future_days说明": "往未来看几天。180 天足够覆盖一整个学期。",
        "stale_hours": 36,
        "_stale_hours说明": "数据超过这么多小时没更新，挂件顶部就变红警告。",
        "notify_hours": 48,
        "_notify_hours说明": "剩余时间少于这么多小时的未交任务，会弹桌面通知。0 = 关掉通知。",
        "emphasize_hours": 72,
        "_emphasize_hours说明": "剩余时间少于这么多小时的任务，挂件上整行字会放大一号。"
                                "默认 72 小时 = 三天。设成 0 关掉这个效果。",
        "hide_types": ["calendar_event", "planner_note"],
        "_hide_types说明": "挂件里不显示的类型。Canvas 的日历事件通常是课程表/讲座时间，"
                           "个人笔记是你自己随手记的 —— 这两类不是「作业」，混进来会把挂件刷屏。"
                           "想看到它们就删掉这一项，或者改成 []（空列表 = 全都显示）。",
    },
    "window": {
        "_说明": "挂件外观。位置会在你拖动后自动记到 state\\widget.json，不用手改。",
        "alpha": 0.96,
        "_alpha说明": "透明度，0.5（很透）到 1.0（完全不透）。",
        "topmost": False,
        "_topmost说明": "是否一直显示在其他窗口上面。默认 false = 当普通窗口，"
                        "别的窗口能正常盖住它；想要常驻最前就在挂件右键菜单里打开「窗口置顶」。",
        "font_size": 10,
        "width": 310,
        "theme": "auto",
        "_theme说明": "auto = 跟随系统深浅色 / light / dark",
    },
    "output": {
        "_说明": "每次拉取额外生成哪些文件。",
        "write_html": True,
        "_write_html说明": "生成完整列表网页。挂件只显示前几个，想看整学期全部的时候用它。",
        "write_text": True,
        "_write_text说明": "生成纯文本快照，万一哪天出问题还能用记事本打开看。",
        "keep_log_days": 60,
        "_keep_log_days说明": "fetch_log.txt 保留多少天的记录，超了自动裁掉。",
    },
}


# --------------------------------------------------------------------------
# JSON 读写
# --------------------------------------------------------------------------
def read_json(path: Path, default: Any = None) -> Any:
    """
    读 JSON。读不出来就「先备份再从默认值重建」，绝不直接覆盖 ——
    用户手改 config.json 改坏了，得让他还能找回自己写的东西。
    编码用 utf-8-sig：记事本和 PowerShell 会在文件头加 BOM，直接用 utf-8 读会炸。
    """
    if not path.exists():
        return default
    try:
        with path.open("r", encoding="utf-8-sig") as fh:
            return json.load(fh)
    except (json.JSONDecodeError, UnicodeDecodeError, OSError) as exc:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = path.with_suffix(path.suffix + f".bad-{stamp}")
        try:
            shutil.copy2(path, backup)
            print(f"[警告] {path.name} 读不出来（{exc}），已备份到 {backup.name}，本次用默认值")
        except OSError:
            print(f"[警告] {path.name} 读不出来（{exc}），且备份失败")
        return default


def write_json(path: Path, data: Any) -> None:
    """独立临时文件 + 原子替换，多个写入者不会共用同一个 .tmp。"""
    write_text(path, json.dumps(data, ensure_ascii=False, indent=2))


def write_text(path: Path, text: str, encoding: str = "utf-8") -> None:
    """
    同样是原子写。

    encoding 只在一种情况下要改：**这个文件是给人用记事本打开的**。
    没有 BOM 的 utf-8 文件，新版记事本能猜对，但别的编辑器（还有老工具、
    有些邮件/聊天软件的预览）会按系统 ANSI 解码，中文就变成「鎴㈡鏃ユ湡」。
    存成 utf-8-sig 就没有这个赌局了。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding=encoding, dir=path.parent,
            prefix=path.name + ".", suffix=".tmp", delete=False,
        ) as fh:
            tmp = Path(fh.name)
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        # Windows 的读取器/杀毒扫描可能短暂占用目标文件；有界重试。
        for attempt in range(7):
            try:
                os.replace(tmp, path)
                break
            except PermissionError:
                if attempt == 6:
                    raise
                time.sleep(0.01 * (2 ** attempt))
    finally:
        if tmp is not None:
            tmp.unlink(missing_ok=True)


# --------------------------------------------------------------------------
# 配置
# --------------------------------------------------------------------------
def _merge_defaults(default: Any, user: Any) -> Any:
    """
    深合并：以 default 为骨架，user 覆盖它。
    这样以后我加了新设置项，你原来的 config.json 不会因为缺键而报错。
    """
    if isinstance(default, dict) and isinstance(user, dict):
        out = dict(default)
        for key, value in user.items():
            out[key] = _merge_defaults(default[key], value) if key in default else value
        return out
    return user if user is not None else default


def load_config(quiet: bool = False) -> dict[str, Any]:
    """
    读配置。文件不存在就用默认值创建一份。
    返回的一定是「默认值 + 你的改动」的合并结果，所以调用方可以放心直接用。
    """
    if not CONFIG_FILE.exists():
        write_json(CONFIG_FILE, DEFAULT_CONFIG)
        if not quiet:
            print(f"[提示] 已生成配置文件：{CONFIG_FILE}")
            print("       双击这个程序，弹出的设置窗口会让你粘贴日历订阅链接。")
    user = read_json(CONFIG_FILE, default={}) or {}
    if not isinstance(user, dict):
        raise ValueError("config.json 必须是 JSON 对象，不能是列表或字符串。")
    for section in ("canvas", "display", "window", "output"):
        if section in user and user[section] is not None and not isinstance(user[section], dict):
            raise ValueError(f"config.json 的 {section} 必须是 JSON 对象。")
    return _merge_defaults(DEFAULT_CONFIG, user)


def get_token(cfg: dict[str, Any]) -> str:
    """取出 token 并做基本清洗。空的话抛 ValueError，让调用方决定怎么提示。"""
    token = str(cfg.get("canvas", {}).get("token", "") or "").strip()
    # 常见手滑：从浏览器复制时带上了引号或 Bearer 前缀
    token = token.strip('"').strip("'").strip()
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    if not token:
        raise ValueError(
            "还没填 Canvas token。打开 config.json，把 canvas.token 那一项填上。\n"
            "       token 获取方式：Canvas 右上角头像 → Account → Settings → "
            "Approved Integrations → + New Access Token"
        )
    return token


def get_feed_url(cfg: dict[str, Any]) -> str:
    """
    取出日历订阅链接。空的话抛 ValueError，让调用方决定怎么提示。

    这串网址本身**就是凭证**（Canvas 的日历订阅不需要登录，谁拿到都能读），
    所以打印/写日志前一律要过 redact。
    """
    url = str(cfg.get("canvas", {}).get("calendar_feed_url", "") or "").strip()
    url = url.strip('"').strip("'").strip()
    if not url:
        raise ValueError(
            "还没填日历订阅链接。\n"
            "       怎么拿：Canvas 左边深色竖条 → Calendar → 右侧栏拉到最下面 → "
            "点 Calendar Feed → 复制那串网址\n"
            "       粘到 config.json 的 canvas.calendar_feed_url 里"
        )
    # 常见手滑：从浏览器复制时带上了 webcal:// 前缀，或者少了协议头
    if url.startswith("webcal://"):
        url = "https://" + url[len("webcal://"):]
    if not url.startswith(("http://", "https://")):
        raise ValueError(
            f"日历订阅链接看起来不对，应该以 https:// 开头。现在填的是：{redact(url)}"
        )
    return url


def redact(text: str) -> str:
    """
    把日历订阅链接里的密钥部分抹掉，好让它可以安全地打印。

    订阅链接长这样：
        https://canvas.lms.unimelb.edu.au/feeds/calendars/user_aBcD1234.ics
                                                          └── 这一段是密钥 ──┘
    只留前面，后面一律变成 [已隐藏]。
    """
    if not text:
        return ""
    text = re.sub(r"(/feeds/calendars/)[^/\s]+", r"\1[已隐藏]", str(text), flags=re.I)
    text = re.sub(r"((?:access_token|token|api_key)=)[^&\s\"'<>]+", r"\1[已隐藏]", text, flags=re.I)
    return re.sub(r"(Bearer\s+)[^\s\"'<>]+", r"\1[已隐藏]", text, flags=re.I)


def load_marked() -> dict[str, Any]:
    """读「你手动标过已交」的记录。读不出来就当空的 —— 这文件丢了不影响别的。"""
    data = read_json(MARKED_FILE, default={}) or {}
    return data if isinstance(data, dict) else {}


def set_marked(task_key: str, done: bool, title: str = "") -> dict[str, Any]:
    """
    标记/取消标记一个任务。挂件右键菜单调这个。

    只记「你标过的」，没标过的不用记 —— 这样即使这个文件被删了，
    也只是所有任务回到「未标记」状态，不会把数据搞乱。
    """
    data = load_marked()
    if done:
        data[task_key] = {
            "done": True,
            "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "title": title[:120],
        }
    else:
        data.pop(task_key, None)
    write_json(MARKED_FILE, data)
    return data


def apply_marked(tasks: list[dict[str, Any]]) -> int:
    """
    把标记应用到任务列表上。返回被标记为已交的条数。

    只动 submitted 字段，并且打上 manual 标记 —— 万一以后 token 方案恢复、
    能拿到真实提交状态了，一眼就能看出哪些是你手标的、哪些是 Canvas 说的。
    """
    marked = load_marked()
    if not marked:
        return 0
    count = 0
    for task in tasks:
        entry = marked.get(task.get("key"))
        if not entry:
            continue
        task["submitted"] = True
        task["manual_submitted"] = True
        count += 1
    return count


def save_widget_state(**changes: Any) -> None:
    """挂件窗口状态（位置、置顶、展开）。读改写，缺键用默认值。"""
    state = read_json(WIDGET_STATE_FILE, default={}) or {}
    state.update(changes)
    write_json(WIDGET_STATE_FILE, state)


def load_widget_state() -> dict[str, Any]:
    return read_json(WIDGET_STATE_FILE, default={}) or {}


def append_log(lines: list[str], keep_days: int = 60) -> None:
    """
    往 fetch_log.txt 追加记录，并裁掉太老的。
    这个文件存在的意义：任务计划用 pythonw 跑，没有控制台，
    出问题时它是唯一能事后翻的地方。
    """
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    entry = "\n".join(f"[{stamp}] {redact(line)}" for line in lines)

    old = ""
    if LOG_FILE.exists():
        try:
            old = LOG_FILE.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            old = ""

    # 用日期行做切割，比按行数切更符合「保留 N 天」的语义
    cutoff = datetime.now().timestamp() - keep_days * 86400
    kept: list[str] = []
    for block in (old + "\n" + entry).split("\n["):
        block = block if block.startswith("[") else "[" + block
        if not block.strip():
            continue
        try:
            when = datetime.strptime(block[1:20], "%Y-%m-%d %H:%M:%S").timestamp()
        except ValueError:
            kept.append(block)          # 格式不认识的别丢，留着
            continue
        if when >= cutoff:
            kept.append(block)

    write_text(LOG_FILE, "\n".join(kept).strip() + "\n")


# --------------------------------------------------------------------------
# 入口脚本统一要做的两件事
# --------------------------------------------------------------------------
def child_command(what: str, *args: str) -> tuple[list[str], str]:
    """
    怎么把「另一个脚本」拉起来。返回 (命令行, 工作目录)。

    **整个项目里只有这一个地方知道打包和没打包的区别。**

    没打包：用同一个解释器跑 xxx.py
            · Windows → 优先 pythonw.exe（不弹黑框）
            · macOS   → 就是 venv 里的 python3（Mac 上没有 pythonw 这东西）
    打包后：没有 .py 文件了，改成再启动一次这个 exe，靠子命令区分（见 app.py）

    以前这段逻辑散在 widget.py / setup.py 里，各自写死了 "pythonw.exe" +
    "xxx.py" —— 打包之后那两个路径都不存在了，挂件的「立即刷新」会静默失效
    （Popen 抛异常被 except 吞掉），你会以为是网络慢。
    """
    if getattr(sys, "frozen", False):
        return [sys.executable, what, *args], str(ROOT)

    exe = Path(sys.executable)
    if sys.platform == "darwin":
        # 不找 pythonw：macOS 上没有这个文件，硬找会白跑一趟。
        # 挂件在程序坞里多出来的那个 Python 图标由 widget.suppress_dock_icon() 处理。
        #
        # **必须转成绝对路径**。macOS 上这个返回值会被写进 launchd 的 plist，
        # 而 launchd 启动任务时的工作目录是 `/` —— 里面存个 ".venv/bin/python"
        # 这种相对路径的话，任务永远起不来，而且报错藏在 state/launchd/*.err 里，
        # 极难查。用 absolute() 而不是 resolve()：venv 里的 python 是个软链接，
        # resolve() 会跟到系统那个原始 Python 上，site-packages 就丢了，
        # 装好的 certifi 也就白装了。
        return [str(exe.absolute()), str(ROOT / f"{what}.py"), *args], str(ROOT)

    pythonw = exe.with_name("pythonw.exe")
    if not pythonw.exists():
        pythonw = exe                       # 万一没有 pythonw，退回 python
    return [str(pythonw), str(ROOT / f"{what}.py"), *args], str(ROOT)


def setup_console() -> None:
    """
    Windows 控制台默认是 GBK，打印中文/特殊字符会抛 UnicodeEncodeError。
    必须在任何 print 之前重设。photo-cleaner 里也是这么干的。
    """
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def ensure_dirs() -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
