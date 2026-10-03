# -*- coding: utf-8 -*-
"""
ics.py —— 解析 Canvas 的日历订阅（iCalendar / .ics）

为什么需要这个模块：
    墨大关闭了学生自助生成 API token 的权限（Settings 页那个按钮是灰的），
    所以改用「日历订阅链接」这条路。订阅返回的是标准 iCalendar 格式的纯文本，
    不是 JSON，得自己解析。

这个模块只做一件事：把 .ics 文本变成 fetch.py 认得的「任务」字典。
不联网、不写文件 —— 网络请求在 fetch.py 里做，这样万一格式对不上，
可以直接拿一段 .ics 文本喂进来单独调试。

iCalendar 格式的三个坑（RFC 5545）：
    1. 折行 —— 超过 75 字节的行会被拆开，续行以「一个空格或 Tab」开头，
       必须先把它们接回去，否则 SUMMARY 会被腰斩
    2. 转义 —— 值里的逗号、分号、换行分别写成 \\, \\; \\n
    3. 时间是多种形态 —— UTC（结尾 Z）、带时区名（TZID 参数）、浮动时间、
       纯日期（全天事件），四种都要处理，且都不能猜错时区

墨大订阅的真实长相（2026-09-23 拿真订阅逐条核对过，别改错了）：

    UID:event-assignment-662952                     ← 真作业
    UID:event-assignment-override-354102            ← 真作业（学校给某个班次单独设的截止时间）
    UID:event-calendar-event-5595493                ← 上课时间表，不是作业
    SUMMARY:Assignment 3 [MAST10006_2026_SM2]       ← 课名是人话，课程代码在方括号里
    SUMMARY:Physics 1 (PHYC10003_2026_SM2) [...]    ← 时间表标题里反而带人话课名，拿来建对照表
    URL;VALUE=URI:.../calendar?include_contexts=course_241268&...#assignment_662952

三个容易踩的坑：

    1. **课号不在 URL 路径里**，在查询串的 `include_contexts=course_NNN` 里；
       条目 id 也不在路径里，在 URL 末尾的 `#assignment_NNN` 片段里。
    2. **override 那条的 UID 里是 override 自己的 id**（354102），
       而 URL 片段里才是真作业 id（664796）。拿 UID 里的 id 去拼网址会 404。
    3. **课程代码后面紧跟下划线**（`MAST10006_2026_SM2`），
       所以不能用 `\\b` 结尾 —— `6` 和 `_` 都是词字符，中间没有词边界，匹配不上。
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

import timeutil

# Canvas 的 URL 里带着课程 id 和条目 id，这是最可靠的来源
_URL_RE = re.compile(r"/courses/(\d+)/([a-z_]+)/(\d+)")

# 墨大订阅：课号在查询串里，条目 id 在 URL 末尾的 #片段 里
_CTX_RE = re.compile(r"include_contexts=course_(\d+)")
_FRAG_RE = re.compile(r"#([a-z_]+)_(\d+)")

# UID 的形状：event-<类型>-<id>，类型的连字符换成下划线后跟 URL 片段一致
_UID_RE = re.compile(r"^event-([a-z][a-z-]*)-(\d+)$")

# 课程代码的样子：COMP10001 / MAST10006 / ECON10004 / FNCE30007
# 注意结尾**不能**用 \b：代码后面紧跟 _2026_SM2，下划线是词字符，中间没有边界
_CODE_RE = re.compile(r"([A-Z]{2,6}\d{4,5})")
# 标题末尾的 [MAST10006_2026_SM2] —— 这是课程代码最干净的来源
_BRACKET_RE = re.compile(r"\[([^\]]*)\]\s*$")
# 时间表标题里的人话课名：Physics 1 (PHYC10003_2026_SM2)
_NAME_RE = re.compile(r"^(.+?)\s*\(([A-Z]{2,6}\d{4,5})[^)]*\)")

# UID / URL 片段里的类型 → 我们内部的类型
# （连字符写法来自 UID，下划线写法来自 URL 片段，这里统一成下划线）
_KIND_TO_TYPE = {
    "assignment": ("assignment", "作业"),
    "assignment_override": ("assignment", "作业"),
    "quiz": ("quiz", "测验"),
    "discussion_topic": ("discussion_topic", "讨论"),
    "wiki_page": ("wiki_page", "页面"),
    "calendar_event": ("calendar_event", "事件"),
}

# 旧版 Canvas（以及 API 那条路）用 URL 路径表示类型，留着备用
_PATH_TO_TYPE = {
    "assignments": ("assignment", "作业"),
    "quizzes": ("quiz", "测验"),
    "discussion_topics": ("discussion_topic", "讨论"),
    "wiki_pages": ("wiki_page", "页面"),
    "calendar_events": ("calendar_event", "事件"),
    "appointments": ("calendar_event", "预约"),
}

# 这些类型「没有提交这回事」，挂件默认不显示（跟 config 里的 hide_types 对应）
NO_SUBMISSION_TYPES = {"calendar_event", "planner_note", "wiki_page", "announcement"}

# 内部类型名 → 中文标签。
# config.json 的 hide_types 里写的是左边这些内部名，但给人看的提示要用中文，
# 不然会冒出「另有 166 条 calendar_event 没列出来」这种没人看得懂的话。
TYPE_LABELS = {
    "assignment": "作业",
    "quiz": "测验",
    "discussion_topic": "讨论",
    "wiki_page": "页面",
    "calendar_event": "上课时间/日历事件",
    "planner_note": "待办笔记",
    "announcement": "公告",
}


def label_for(kind: str) -> str:
    """内部类型名换成中文；认不出来就原样返回，不编一个。"""
    return TYPE_LABELS.get(kind, kind)


# --------------------------------------------------------------------------
# 第一层：把文本拆成属性
# --------------------------------------------------------------------------
def unfold(text: str) -> list[str]:
    """
    接回被折行的长行。

    RFC 5545 规定超过 75 字节的行要拆开，续行以「一个空格或 Tab」开头。
    不接回去的话，长标题、长 URL 会被腰斩成两半。
    """
    # 统一换行；开头的 BOM 也要去掉（有些服务器会带）
    text = text.lstrip("﻿").replace("\r\n", "\n").replace("\r", "\n")

    lines: list[str] = []
    for raw in text.split("\n"):
        if raw[:1] in (" ", "\t") and lines:
            lines[-1] += raw[1:]        # 续行：接回上一行，去掉那个前导空格
        else:
            lines.append(raw)
    return lines


def split_property(line: str) -> tuple[str, dict[str, str], str]:
    """
    把一行拆成 (属性名, 参数表, 值)。

        DTSTART;TZID=Australia/Melbourne:20261025T235900
        └─名──┘ └──────参数──────┘ └──────值──────┘

    注意：值里可能有冒号（比如 URL），所以只按**第一个**冒号切。
    """
    if ":" not in line:
        return line.strip().upper(), {}, ""

    head, value = line.split(":", 1)
    parts = head.split(";")
    name = parts[0].strip().upper()

    params: dict[str, str] = {}
    for item in parts[1:]:
        if "=" in item:
            key, val = item.split("=", 1)
            params[key.strip().upper()] = val.strip().strip('"')
        elif item.strip():
            params[item.strip().upper()] = ""

    return name, params, value


def unescape(value: str) -> str:
    """还原 iCalendar 的转义。顺序很重要：先处理 \\\\ 会把后面的转义吃掉。"""
    out: list[str] = []
    i = 0
    while i < len(value):
        ch = value[i]
        if ch == "\\" and i + 1 < len(value):
            nxt = value[i + 1]
            if nxt in ("n", "N"):
                out.append("\n")
            elif nxt in ("\\", ",", ";", ":"):
                out.append(nxt)
            else:
                out.append(nxt)
            i += 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)


# --------------------------------------------------------------------------
# 第二层：时间
# --------------------------------------------------------------------------
def parse_dt(value: str, params: dict[str, str], default_zone) -> datetime | None:
    """
    解析 DTSTART / DTEND。四种形态都要认，且时区不能猜错：

        VALUE=DATE:20261025          全天事件（只有日期）
        20261025T135900Z             UTC（结尾 Z）
        TZID=...:20261025T235900     指定时区
        20261025T235900              浮动时间（没说是哪个时区，按你的时区算）
    """
    raw = (value or "").strip()
    if not raw:
        return None

    # ---- 全天事件：只有 8 位日期 ----
    if len(raw) == 8 and raw.isdigit():
        try:
            day = datetime.strptime(raw, "%Y%m%d")
        except ValueError:
            return None
        # 全天事件按「当天结束」算 —— 否则凌晨 00:01 就显示成已过期了
        return day.replace(hour=23, minute=59, second=59, tzinfo=default_zone)

    # ---- 结尾 Z = UTC ----
    if raw.endswith("Z"):
        try:
            return datetime.strptime(raw[:-1], "%Y%m%dT%H%M%S").replace(tzinfo=timezone.utc)
        except ValueError:
            return None

    # ---- 其余都当本地时间串解析，时区从 TZID 拿，拿不到就用默认 ----
    body = raw
    if body.endswith("Z"):
        body = body[:-1]
    zone = default_zone
    tzid = params.get("TZID")
    if tzid:
        zone = timeutil.get_zone(tzid)      # 认不出来会退回 FALLBACK_TZ，不抛异常
    for fmt in ("%Y%m%dT%H%M%S", "%Y%m%dT%H%M"):
        try:
            return datetime.strptime(body, fmt).replace(tzinfo=zone)
        except ValueError:
            continue
    return None


# --------------------------------------------------------------------------
# 第三层：把文本变成事件列表
# --------------------------------------------------------------------------
def parse_events(text: str) -> list[dict[str, Any]]:
    """
    切出所有 VEVENT，每个变成 {属性名: [(参数表, 原始值), ...]}。

    用「属性名 → 列表」而不是字典，因为同一个属性可能合法地出现多次
    （比如多个 CATEGORIES），用字典会悄悄丢掉后面的。
    """
    events: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None

    for line in unfold(text):
        stripped = line.strip()
        if not stripped:
            continue

        name, params, value = split_property(stripped)

        if name == "BEGIN" and value.strip().upper() == "VEVENT":
            current = {}
            continue
        if name == "END" and value.strip().upper() == "VEVENT":
            if current is not None:
                events.append(current)
            current = None
            continue
        if current is None:
            continue        # VEVENT 外面的东西（VCALENDAR 头、VTIMEZONE 等）不管

        current.setdefault(name, []).append((params, value))

    return events


def _first(event: dict[str, Any], name: str) -> tuple[dict[str, str], str]:
    """取某个属性的第一个值。取不到返回 ('', '')。"""
    got = event.get(name.upper())
    if not got:
        return {}, ""
    params, value = got[0]
    return params, value


def _text(event: dict[str, Any], name: str) -> str:
    """取一个文本属性的值（已还原转义）。"""
    _, value = _first(event, name)
    return unescape(value).strip()


# --------------------------------------------------------------------------
# 第四层：事件 → 任务
# --------------------------------------------------------------------------
def _kind_of(url: str, uid: str) -> str:
    """
    从 URL 片段或 UID 里读出条目类型，读不到返回空串。

    两边都写上是因为它们各有所长：URL 片段里的 id 更可信（override 那条
    只有片段里才是真作业 id），但 UID 一定存在、片段不一定有。
    """
    if url:
        found = _FRAG_RE.search(url)
        if found:
            return found.group(1).lower()
    if uid:
        found = _UID_RE.match(uid)
        if found:
            return found.group(1).lower().replace("-", "_")
    return ""


def object_id(url: str) -> int | None:
    """
    拿出条目 id（URL 末尾 #assignment_662952 里的那个数字）。

    必须用 URL 片段而不是 UID：override 那条的 UID 里是 override 自己的 id，
    拿它拼出来的网址是 404。
    """
    if not url:
        return None
    found = _FRAG_RE.search(url)
    if found:
        try:
            return int(found.group(2))
        except ValueError:
            return None
    return None


def classify(url: str, uid: str, summary: str) -> tuple[str, str]:
    """
    判断这是作业、测验还是日历事件。

    优先看 URL 片段和 UID（这是 Canvas 自己写的类型，最可靠），
    实在读不出来才靠标题猜，猜不出就当成日历事件 ——
    宁可默认不显示，也不要拿上课时间冒充作业。
    """
    kind = _kind_of(url, uid)
    if kind in _KIND_TO_TYPE:
        return _KIND_TO_TYPE[kind]

    for source in (url, uid):
        if source:
            match = _URL_RE.search(source)
            if match and match.group(2) in _PATH_TO_TYPE:
                return _PATH_TO_TYPE[match.group(2)]

    low = (summary or "").lower()
    if "quiz" in low or "测验" in low:
        return "quiz", "测验"
    return "calendar_event", "事件"


def clean_title(summary: str) -> str:
    """
    去掉标题末尾的 [MAST10006_2026_SM2]。

        输入：Assignment 1 (2026) (157 students) [COMP10002_2026_SM2]
        输出：Assignment 1 (2026) (157 students)

    课程信息我们已经单独存了，挂在标题里既占地方又不好看。
    """
    text = (summary or "").strip()
    stripped = _BRACKET_RE.sub("", text).strip()
    # 全是方括号的话（比如整个标题就是 [XXX]）就别剥了，免得标题变空
    return stripped or text


def build_course_names(events: list[dict[str, Any]]) -> dict[str, str]:
    """
    从上课时间表的标题里攒一张「课程代码 → 人话课名」的对照表。

    订阅里没有课程列表接口，但时间表的标题反而写着课名：
        Physics 1 (PHYC10003_2026_SM2) [PHYC10003_2026_SM2]
        └── 课名 ──┘ └──── 课程代码 ────┘

    只从 calendar_event 里取，不从作业标题里取 —— 作业标题长得像
    "Assignment 1 (2026) (157 students)"，用它取名会得到一堆垃圾。
    """
    names: dict[str, str] = {}
    for event in events:
        kind = _kind_of(_text(event, "URL"), _text(event, "UID"))
        if kind != "calendar_event":
            continue
        found = _NAME_RE.match(_text(event, "SUMMARY"))
        if not found:
            continue
        name, code = found.group(1).strip(), found.group(2)
        # 同一个代码出现多次时用最短的那个 —— 时间表标题常带
        # "Wednesday 12pm" 这种后缀，最短的通常就是干净的课名
        if code not in names or len(name) < len(names[code]):
            names[code] = name
    return names


def course_info(
    url: str, uid: str, summary: str, description: str, names: dict[str, str] | None = None
) -> tuple[Any, str, str]:
    """
    尽量挖出 (course_id, course_code, course_name)。

    订阅里**没有课程列表**，所以课名只能靠推断 —— 这是这条路的代价之一。
    推断不出来就老实退回课程代码，不要瞎编一个课名。
    """
    course_id: Any = None
    for source in (url, uid, description):
        if not source:
            continue
        found = _CTX_RE.search(source)
        if found:
            course_id = int(found.group(1))
            break
    if course_id is None:
        for source in (url, uid, description):
            if not source:
                continue
            found = _URL_RE.search(source)
            if found:
                course_id = int(found.group(1))
                break

    # 课程代码：优先信标题末尾方括号里的那个（Canvas 自己填的，最准）
    code = ""
    bracket = _BRACKET_RE.search((summary or "").strip())
    if bracket:
        found = _CODE_RE.search(bracket.group(1))
        if found:
            code = found.group(1)
    if not code:
        for haystack in (summary, description):
            if not haystack:
                continue
            found = _CODE_RE.search(haystack)
            if found:
                code = found.group(1)
                break
    if not code:
        code = f"课程{course_id}" if course_id is not None else "未知课程"

    name = (names or {}).get(code) or code
    return course_id, code, name


def to_tasks(
    events: list[dict[str, Any]], zone, base_url: str = ""
) -> list[dict[str, Any]]:
    """
    把解析出来的事件变成 fetch.py 认得的任务字典。

    跟 planner / assignments 那条路的关键差别：
        submitted 一律是 None（状态未知）—— 日历订阅根本不告诉你交没交。
        绝不能写成 False，那会让挂件红着脸催你交一个早就交了的作业。
        真实的「已交」由你在挂件上右键手动标记（见 store.marked.json）。

    base_url 传 Canvas 的根地址（比如 https://canvas.lms.unimelb.edu.au），
    传了才能把订阅里那个日历跳转链接换成真正的作业页面链接。
    """
    names = build_course_names(events)
    tasks: list[dict[str, Any]] = []
    index: dict[str, int] = {}          # key → 在 tasks 里的位置

    for event in events:
        if _text(event, "STATUS").upper() == "CANCELLED":
            continue
        dtstart_params, dtstart_raw = _first(event, "DTSTART")
        due = parse_dt(dtstart_raw, dtstart_params, zone)

        # 有些日历把结束时间放在 DTEND，没有 DTSTART 的条目没法排期，跳过
        if due is None:
            dtend_params, dtend_raw = _first(event, "DTEND")
            due = parse_dt(dtend_raw, dtend_params, zone)
        if due is None:
            continue

        summary = _text(event, "SUMMARY")
        description = _text(event, "DESCRIPTION")
        url = _text(event, "URL")
        uid = _text(event, "UID")

        # URL 常常藏在描述里（Canvas 有时不填 URL 字段）
        if not url and description:
            found = re.search(r"https?://\S+", description)
            if found:
                url = found.group(0)

        kind, label = classify(url, uid, summary)
        course_id, course_code, course_name = course_info(
            url, uid, summary, description, names
        )
        obj_id = object_id(url)
        is_override = "override" in _kind_of(url, uid)

        # ---- 主键 ----
        # 作业用「课程+作业 id」而不是 UID：同一次作业可能同时存在
        # event-assignment-NNN 和 event-assignment-override-MMM 两条，
        # 用 UID 当主键会让它在挂件里出现两遍。其余沿用 UID（天生唯一且稳定）。
        if kind == "assignment" and obj_id is not None:
            key = f"feed-assignment-{course_id}-{obj_id}"
        elif uid:
            key = f"feed-{uid}"
        else:
            key = f"feed-{kind}-{course_id}-{due.isoformat()}"

        # ---- 真正的作业页面链接 ----
        # 订阅里给的是日历跳转链接（点开是日历，不是作业），换成作业页面。
        # 注意用 obj_id（URL 片段里的）而不是 UID 里的 —— override 那条不一样。
        open_url = url
        if base_url and kind == "assignment" and course_id and obj_id:
            open_url = f"{base_url.rstrip('/')}/courses/{course_id}/assignments/{obj_id}"

        title = clean_title(summary) or "(无标题)"

        task = {
            "key": key,
            "course_id": course_id,
            "course_code": course_code,
            "course_name": course_name,
            "title": title,
            "type": kind,
            "type_label": label,
            "due_utc": due.astimezone(timezone.utc).isoformat(),
            "due_display": timeutil.format_due(due, zone),
            "due_full": timeutil.format_full(due, zone),
            "points": None,
            "submitted": None,               # 关键：未知，不是「没交」
            "late": False,
            "missing": False,
            "excused": False,
            "url": open_url,
            "source": "feed",
        }

        # 同一个作业两条时，留 override 那条 —— 那是属于你的截止时间
        if key in index:
            if is_override:
                tasks[index[key]] = task
            continue
        index[key] = len(tasks)
        tasks.append(task)

    return tasks


def summarize(text: str, max_events: int = 2) -> str:
    """
    给排查用：列出所有出现过的属性名，外加前几个事件的原始内容。

    存在的意义：万一 Canvas 的订阅格式跟我预期的不一样，
    看这个文件就知道该改哪儿 —— 不用把整个订阅内容翻出来。
    """
    events = parse_events(text)
    names: dict[str, int] = {}
    for event in events:
        for name, values in event.items():
            names[name] = names.get(name, 0) + len(values)

    lines = [
        f"字节数：{len(text.encode('utf-8'))}",
        f"事件数：{len(events)}",
        "",
        "出现过的属性（属性名 → 出现次数）：",
    ]
    for name in sorted(names):
        lines.append(f"    {name:<16} {names[name]}")

    # 类型分布 —— 排查「挂件怎么空了」时第一个要看的东西。
    # 全是 calendar_event 就说明分类没认出来（而 calendar_event 默认被隐藏）。
    lines.append("")
    lines.append("条目类型分布（UID / URL 片段判定）：")
    kinds: dict[str, int] = {}
    for event in events:
        kind = _kind_of(_text(event, "URL"), _text(event, "UID")) or "(认不出)"
        kinds[kind] = kinds.get(kind, 0) + 1
    for kind in sorted(kinds, key=lambda k: -kinds[k]):
        lines.append(f"    {kind:<24} {kinds[kind]}")

    lines.append("")
    lines.append("课程代码分布：")
    codes: dict[str, int] = {}
    for event in events:
        _, code, _ = course_info(
            _text(event, "URL"), _text(event, "UID"),
            _text(event, "SUMMARY"), _text(event, "DESCRIPTION"),
        )
        codes[code] = codes.get(code, 0) + 1
    for code in sorted(codes, key=lambda c: -codes[c]):
        lines.append(f"    {code:<24} {codes[code]}")

    lines.append("")
    lines.append("人话课名对照表（从上课时间表的标题里攒的）：")
    built = build_course_names(events)
    if built:
        for code in sorted(built):
            lines.append(f"    {code:<24} {built[code]}")
    else:
        lines.append("    （一个都没攒到）")

    lines.append("")
    lines.append(f"前 {min(max_events, len(events))} 个事件的原始内容：")
    lines.append("")

    count = 0
    for line in unfold(text):
        stripped = line.strip()
        if stripped.upper().startswith("BEGIN:VEVENT"):
            count += 1
            if count > max_events:
                break
            lines.append("  ── 事件 ──")
            continue
        if count and count <= max_events and stripped:
            lines.append("    " + stripped)

    return "\n".join(lines)
