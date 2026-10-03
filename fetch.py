# -*- coding: utf-8 -*-
"""
fetch.py —— 拉数据、归一化、写文件、发通知

这是唯一会联网的脚本。挂件只读它写出来的 out\\deadlines.json，从不自己联网 ——
这样断网、Canvas 挂了、token 过期，挂件都不会白屏或卡死。

用法：
    python fetch.py              正常拉一次
    python fetch.py --quiet      少打印（任务计划里用）
    python fetch.py --verbose    打印每个请求（排查问题时用）

**这个脚本绝不允许静默死掉。**
任务计划用 pythonw.exe 跑它，没有控制台 —— 崩了你是看不见的。所以：
    * 整个 run() 包在 try/except 里
    * 任何异常都写进 out\\fetch_log.txt 和 deadlines.json 的错误块
    * 挂件读到错误块会把顶栏变红，直接告诉你出了什么事
一个看起来正常但其实是旧数据的看板，比没有看板更危险。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import canvas_api
import ics
import netutil
import notify
import store
import timeutil
from canvas_api import CanvasError

# 墨大的 WAF 会挡掉非浏览器的 User-Agent（实测：curl 默认 UA 访问 Canvas 首页会被 403，
# 换成 Chrome 的就 200）。所以拉订阅时必须装成浏览器，否则会莫名其妙地被拒。
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# planner 的 plannable_type → 中文标签
TYPE_LABELS = {
    "assignment": "作业",
    "quiz": "测验",
    "discussion_topic": "讨论",
    "wiki_page": "页面",
    "calendar_event": "事件",
    "planner_note": "笔记",
    "sub_assignment": "作业",
    "assessment_request": "互评",
    "peer_review_sub_assignment": "互评",
}

# 这些类型没有「提交」这个概念。它们的 submissions 字段是 False（布尔），
# 不是「没提交」—— 不能把布尔 False 当成「未提交」，否则挂件会拿日历事件吓唬你。
NO_SUBMISSION_TYPES = {"calendar_event", "planner_note", "wiki_page", "announcement"}


# --------------------------------------------------------------------------
# 课程表
# --------------------------------------------------------------------------
def short_code(course: dict[str, Any]) -> str:
    """
    Canvas 的 course_code 常常是 "COMP10001_2026_SM2" 这种，
    挂件上要的是 "COMP10001"。取第一个下划线之前的部分。
    """
    raw = (course.get("course_code") or "").strip()
    if raw:
        return raw.split("_")[0]
    name = (course.get("name") or "").strip()
    return name.split()[0] if name else "?"


def build_course_map(courses: list[dict[str, Any]]) -> dict[Any, dict[str, Any]]:
    out: dict[Any, dict[str, Any]] = {}
    for course in courses:
        cid = course.get("id")
        if cid is None:
            continue
        out[cid] = {
            "id": cid,
            "code": short_code(course),
            "name": (course.get("name") or "").strip() or f"课程 {cid}",
            "term": ((course.get("term") or {}) or {}).get("name") or "",
        }
    return out


def course_of(course_map: dict[Any, dict[str, Any]], course_id: Any) -> dict[str, Any]:
    found = course_map.get(course_id)
    if found:
        return found
    # 课程列表里没有（比如刚选上还没同步），不要丢数据，给个占位
    return {
        "id": course_id,
        "code": f"课程{course_id}" if course_id is not None else "未知",
        "name": f"未知课程（id={course_id}）",
        "term": "",
    }


# --------------------------------------------------------------------------
# 归一化
# --------------------------------------------------------------------------
def make_key(kind: str, course_id: Any, item_id: Any) -> str:
    return f"{kind}-{course_id}-{item_id}"


def _submission_flags(submissions: Any) -> tuple[bool | None, bool, bool, bool]:
    """
    从 planner 的 submissions 字段里读出 (submitted, late, missing, excused)。

    submissions 有两种形态：
        * 字典  → 真的作业/测验，里面有 submitted 等布尔值
        * False → 这类东西没有提交这回事（日历事件、笔记、讨论区）
                  这时 submitted 必须是 None（状态未知），不能是 False（没交）
    """
    if not isinstance(submissions, dict):
        return None, False, False, False
    excused = bool(submissions.get("excused"))
    submitted = bool(submissions.get("submitted"))
    # 被豁免的等同于「不用交」，别在挂件上红着脸催你
    if excused:
        submitted = True
    return submitted, bool(submissions.get("late")), bool(submissions.get("missing")), excused


def normalize_planner_item(
    item: dict[str, Any], course_map: dict[Any, dict[str, Any]], zone
) -> dict[str, Any] | None:
    """把 planner 的一条 item 变成统一的「任务」。"""
    ptype = (item.get("plannable_type") or "").strip()
    plannable = item.get("plannable")
    if not isinstance(plannable, dict):
        plannable = {}

    course_id = item.get("course_id") or plannable.get("course_id")
    item_id = item.get("plannable_id") or plannable.get("id")
    if item_id is None:
        return None

    # plannable_date 是 planner 里那条事项的日期，对作业来说就是「你的」截止时间，
    # 已经按你的 section / 个人 override 算过，比 assignments 接口的 due_at 更省心。
    due = timeutil.parse_iso(
        item.get("plannable_date")
        or plannable.get("due_at")
        or plannable.get("start_at")
        or plannable.get("todo_date")
    )

    title = (
        plannable.get("name")
        or plannable.get("title")
        or plannable.get("summary")
        or "(无标题)"
    )

    if ptype in NO_SUBMISSION_TYPES:
        submitted, late, missing, excused = None, False, False, False
    else:
        submitted, late, missing, excused = _submission_flags(item.get("submissions"))

    course = course_of(course_map, course_id)
    return {
        "key": make_key(ptype or "item", course_id, item_id),
        "course_id": course_id,
        "course_code": course["code"],
        "course_name": course["name"],
        "title": str(title).strip(),
        "type": ptype or "item",
        "type_label": TYPE_LABELS.get(ptype, ptype or "事项"),
        "due_utc": due.astimezone(timezone.utc).isoformat() if due else None,
        "due_display": timeutil.format_due(due, zone) if due else "",
        "due_full": timeutil.format_full(due, zone) if due else "",
        "points": plannable.get("points_possible"),
        "submitted": submitted,
        "late": late,
        "missing": missing,
        "excused": excused,
        "url": item.get("html_url") or plannable.get("html_url") or "",
        "source": "planner",
    }


def normalize_assignment(
    assignment: dict[str, Any], course_map: dict[Any, dict[str, Any]], course_id: Any, zone
) -> dict[str, Any] | None:
    """把 assignments 接口的一条记录变成统一的「任务」（回退路径用）。"""
    item_id = assignment.get("id")
    if item_id is None:
        return None

    due = timeutil.parse_iso(assignment.get("due_at"))
    submission = assignment.get("submission")
    excused = bool(isinstance(submission, dict) and submission.get("excused"))

    if isinstance(submission, dict):
        # workflow_state 覆盖 submitted_at 为空的边界情况：
        # 有些作业交完是 graded 状态，submitted_at 可能因为重交被清掉
        state = submission.get("workflow_state") or ""
        submitted = bool(submission.get("submitted_at")) or state in (
            "submitted", "graded", "pending_review"
        )
        late = bool(submission.get("late"))
        missing = bool(submission.get("missing"))
    else:
        submitted, late, missing = None, False, False
    if excused:
        submitted = True

    quiz_like = bool(assignment.get("quiz_id"))
    course = course_of(course_map, course_id)

    return {
        "key": make_key("assignment", course_id, item_id),
        "course_id": course_id,
        "course_code": course["code"],
        "course_name": course["name"],
        "title": str(assignment.get("name") or "(无标题)").strip(),
        "type": "quiz" if quiz_like else "assignment",
        "type_label": "测验" if quiz_like else "作业",
        "due_utc": due.astimezone(timezone.utc).isoformat() if due else None,
        "due_display": timeutil.format_due(due, zone) if due else "",
        "due_full": timeutil.format_full(due, zone) if due else "",
        "points": assignment.get("points_possible"),
        "submitted": submitted,
        "late": late,
        "missing": missing,
        "excused": excused,
        "url": assignment.get("html_url") or "",
        "source": "assignments",
    }


# --------------------------------------------------------------------------
# 日历订阅
# --------------------------------------------------------------------------
def fetch_feed(url: str, cfg: dict[str, Any], verbose: bool = False) -> str:
    """
    下载日历订阅的原文。

    这是整个程序里唯一一个**不需要任何认证**的请求 —— 网址本身就是凭证。
    请求头里没有 token、没有 cookie、什么都没有。

    注意最后那道「是不是真日历文件」的检查，它比看上去重要：
    有些服务器会忽略 Accept 头，把登录页的 HTML 塞回来。拿 HTML 去解析
    只会得到 0 个事件，而「0 个事件」在挂件上看起来就是「这周没作业」——
    那是最危险的假象。宁可报错，也不要安静地显示成没事。
    """
    timeout = float(cfg.get("canvas", {}).get("timeout") or 20)
    tries = max(1, int(cfg.get("canvas", {}).get("max_retries") or 3))
    headers = {
        "User-Agent": BROWSER_UA,
        "Accept": "text/calendar, text/plain, */*",
    }

    last: Exception | None = None

    for attempt in range(1, tries + 1):
        try:
            resp = netutil.get(url, headers=headers, timeout=timeout)
        except netutil.Timeout:
            last = CanvasError("network", f"请求超时（{timeout:.0f} 秒），网络可能不通")
        except netutil.TLSFailure as exc:
            # 证书问题重试一百次结果也一样，直接抛出去，并且说清怎么办。
            # 这条在 Mac 上是头号坑：python.org 装的 Python 不认 macOS 钥匙串。
            raise CanvasError(
                "network",
                f"HTTPS 证书验证没过：{exc}\n"
                "       Mac 上多半是这个原因：python.org 装的 Python 不认系统的钥匙串。\n"
                "       重跑一次「Mac-1-安装.command」会把证书包（certifi）装好。",
            ) from exc
        except netutil.HttpFailure as exc:
            last = CanvasError("network", f"网络错误：{exc}")
        else:
            if resp.status_code == 200:
                text = resp.text
                lines = {line.strip().upper() for line in ics.unfold(text)}
                if "BEGIN:VCALENDAR" not in lines or "END:VCALENDAR" not in lines:
                    raise CanvasError(
                        "bad_response",
                        "拿回来的不是日历文件（看着像登录页或错误页）。"
                        "订阅链接多半已经失效了，去 Canvas 的 Calendar Feed 重新复制一份。",
                    )
                return text

            if resp.status_code in (401, 403):
                # 认证类错误不重试 —— 重试一百次结果也一样
                raise CanvasError(
                    "forbidden",
                    f"订阅链接被拒绝了（HTTP {resp.status_code}）。"
                    "多半是链接失效了，去 Canvas 重新复制一份。",
                )
            if resp.status_code == 404:
                raise CanvasError(
                    "not_found",
                    "订阅链接不存在（HTTP 404）。多半是链接被重置过，去 Canvas 重新复制一份。",
                )
            if resp.status_code in (400, 414):
                # 实测（2026-09-23）：链接抄错一个字符、或者只复制了一半，
                # Canvas 回的是 400，不是 404。以前这条会掉进「没想到的状态码」
                # 那个兜底 —— 对用户来说等于什么都没说。
                raise CanvasError(
                    "bad_url",
                    f"Canvas 不认这个链接（HTTP {resp.status_code}）。"
                    "多半是没复制全或者抄错了一个字符 —— "
                    "回去重新复制一遍，从 https:// 一直复制到 .ics 结尾。",
                )
            if resp.status_code >= 500:
                last = CanvasError("server", f"Canvas 服务器出错（HTTP {resp.status_code}）")
            else:
                last = CanvasError("unknown", f"没想到的状态码 HTTP {resp.status_code}")

        if attempt < tries:
            wait = 2 ** (attempt - 1)
            if verbose:
                print(f"    第 {attempt} 次没成（{last}），{wait} 秒后重试")
            time.sleep(wait)

    raise last or CanvasError("unknown", "拉取日历订阅失败")


def collect_from_feed(
    cfg: dict[str, Any], verbose: bool = False
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    """
    走日历订阅这条路。返回结构跟 collect() 完全一样，
    所以下游（写文件、发通知、挂件）一行都不用改。
    """
    url = store.get_feed_url(cfg)       # 没填会抛 ValueError，由 run() 兜住
    if verbose:
        print(f"  订阅地址：{store.redact(url)}")

    text = fetch_feed(url, cfg, verbose=verbose)

    # 把订阅的「结构摘要」写下来备用。万一 Canvas 的格式跟预期不一样，
    # 看这个文件就知道该改哪儿 —— 不用把整份订阅翻出来（那里面是你的课程信息）。
    try:
        store.write_text(store.FEED_DEBUG_FILE, ics.summarize(text))
    except OSError as exc:
        print(f"  [警告] 订阅结构摘要写不出来（不影响使用）：{exc}")

    # 订阅里没有「你的账号信息」，所以时区只能用默认的墨尔本。
    # 这对墨大学生来说就是对的 —— 而且事件自带 TZID 的话会以 TZID 为准。
    zone = timeutil.get_zone(timeutil.FALLBACK_TZ)
    # 传 base_url 是为了把订阅里的「日历跳转链接」换成真正的作业页面链接 ——
    # 订阅给的那个链接点开是日历，不是作业，双击跳过去会一脸茫然。
    base_url = (cfg.get("canvas") or {}).get("base_url") or ""
    raw = ics.to_tasks(ics.parse_events(text), zone, base_url=base_url)

    if verbose:
        print(f"  解析出 {len(raw)} 条")

    return _postprocess(raw, cfg, zone, {
        "student": "",
        "email": "",
        "timezone": timeutil.FALLBACK_TZ,
        "source": "calendar_feed",
        "feed_events": len(raw),
    })


# --------------------------------------------------------------------------
# 两个数据源共用的后半段
# --------------------------------------------------------------------------
def _postprocess(
    raw: list[dict[str, Any]], cfg: dict[str, Any], zone, meta_extra: dict[str, Any]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    """
    去重 → 应用手动标记 → 算 needs_action → 时间窗过滤 → 排序 → 元信息。

    抽出来是因为日历订阅和 Canvas API 两条路的产出必须长得一模一样，
    下游（写文件 / 发通知 / 挂件）才能一行都不用改。
    """
    display_cfg = cfg.get("display", {})
    past_days = int(display_cfg.get("past_days", 14))
    future_days = int(display_cfg.get("future_days", 180))

    # ---- 去重：同一个 key 只留一条，优先保留有提交状态的 ----
    merged: dict[str, dict[str, Any]] = {}
    for task in raw:
        existing = merged.get(task["key"])
        if existing is None:
            merged[task["key"]] = task
            continue
        # 已有的提交状态是未知，新来的知道 → 用新的
        if existing.get("submitted") is None and task.get("submitted") is not None:
            merged[task["key"]] = task

    tasks_all = list(merged.values())

    # ---- 屏蔽「不是作业」的类型（上课时间表）----
    # 必须在这儿过滤，不能只在挂件里过滤：实测墨大的订阅 187 条里
    # 有 166 条是上课时间/讲座，它们会灌满 deadlines.json、网页和文本快照，
    # 真正要看的 21 个作业反而被埋掉。
    hidden_types = set(display_cfg.get("hide_types") or [])
    hidden_count = 0
    hidden_kinds: dict[str, int] = {}       # 只记真正出现过的，好写人话提示
    if hidden_types:
        kept = []
        for task in tasks_all:
            kind = task.get("type") or ""
            if kind in hidden_types:
                hidden_kinds[kind] = hidden_kinds.get(kind, 0) + 1
            else:
                kept.append(task)
        hidden_count = len(tasks_all) - len(kept)
        tasks_all = kept

    # ---- 应用「你在挂件上手动标的已交」----
    # 必须放在算 needs_action **之前** —— 顺序反了的话，
    # 你标了已交它照样催你，那这个功能就白做了。
    manual_count = store.apply_marked(tasks_all)

    # ---- 算 needs_action：唯一一处「要不要催你」的判断 ----
    for task in tasks_all:
        if task.get("excused"):
            task["needs_action"] = False        # 被豁免的不用交，别催
        elif task.get("type") in NO_SUBMISSION_TYPES:
            task["needs_action"] = False        # 日历事件之类没有「交」这回事
        elif task.get("submitted") is True:
            task["needs_action"] = False
        elif task.get("submitted") is False:
            task["needs_action"] = True         # Canvas 明说了没交
        else:
            # 状态未知。日历订阅这条路**全是**这种情况。
            # 拿不到「交了没」的时候，只能当你没交 —— 宁可多提醒一次，
            # 也不要漏掉一个真的没交的作业。真实状态由你手动标记来修正。
            task["needs_action"] = True

    # ---- 时间窗过滤 ----
    now = timeutil.now_utc()
    dated: list[dict[str, Any]] = []
    undated: list[dict[str, Any]] = []
    lo = now - timedelta(days=past_days)
    hi = now + timedelta(days=future_days)

    for task in tasks_all:
        due = timeutil.parse_iso(task.get("due_utc"))
        if due is None:
            undated.append(task)
            continue
        if due < lo or due > hi:
            continue
        dated.append(task)

    # 排序：按截止时间升序 —— 已过期未交的自然排在最前面，那正是最该看的
    dated.sort(key=lambda t: t.get("due_utc") or "")
    undated.sort(key=lambda t: (t.get("course_code") or "", t.get("title") or ""))

    needs = sum(1 for t in dated if t.get("needs_action"))

    # 课程清单从任务里反推（日历订阅没有单独的课程接口）
    course_seen: dict[str, dict[str, Any]] = {}
    for task in tasks_all:
        code = task.get("course_code") or "?"
        if code not in course_seen:
            course_seen[code] = {
                "code": code,
                "name": task.get("course_name") or code,
                "term": "",
            }

    meta: dict[str, Any] = {
        "course_count": len(course_seen),
        "task_count": len(dated),
        "undated_count": len(undated),
        "hidden_count": hidden_count,
        "hidden_types": sorted(hidden_kinds),
        "needs_action_count": needs,
        "manual_submitted_count": manual_count,
        "courses": [course_seen[c] for c in sorted(course_seen)],
    }
    meta.update(meta_extra)
    return dated, undated, meta


# --------------------------------------------------------------------------
# 采集
# --------------------------------------------------------------------------
def collect(
    client: canvas_api.CanvasClient, cfg: dict[str, Any], verbose: bool = False
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    """
    返回 (有截止日期的任务, 没截止日期的任务, 元信息)。
    """
    display_cfg = cfg.get("display", {})
    past_days = int(display_cfg.get("past_days") or 14)
    future_days = int(display_cfg.get("future_days") or 180)

    profile = client.profile()
    tz_name = profile.get("time_zone") or timeutil.FALLBACK_TZ
    zone = timeutil.get_zone(tz_name)

    courses = client.courses()
    course_map = build_course_map(courses)

    now = timeutil.now_utc()
    start = (now - timedelta(days=past_days)).strftime("%Y-%m-%d")
    end = (now + timedelta(days=future_days)).strftime("%Y-%m-%d")

    raw: list[dict[str, Any]] = []
    source = "planner"

    use_planner = bool(cfg.get("canvas", {}).get("use_planner", True))
    planner_error: str | None = None

    if use_planner:
        try:
            items = client.planner_items(start, end)
            if verbose:
                print(f"  Planner 返回 {len(items)} 条")
            for item in items:
                task = normalize_planner_item(item, course_map, zone)
                if task:
                    raw.append(task)
        except CanvasError as exc:
            # Planner 被学校关掉是很可能的事，退回逐课程拉取。
            # 但要把这件事记下来 —— 静默降级会让人以为数据是完整的。
            planner_error = f"{canvas_api.hint_for(exc.kind)}（{exc}）"
            print(f"  [注意] Planner 用不了：{planner_error}")
            print("         改用逐课程拉取的方式（慢一点，但结果一样）")
            use_planner = False

    if not use_planner:
        source = "fallback"
        todo = []
        try:
            todo = client.todo()
            if verbose:
                print(f"  待办返回 {len(todo)} 条")
        except CanvasError as exc:
            print(f"  [注意] 待办也拿不到：{canvas_api.hint_for(exc.kind)}")

        # 待办里的作业先占个位（它们至少说明「有事要做」），
        # 下面逐课程拉取时会用更完整的记录覆盖掉
        seen: set[str] = set()
        for entry in todo:
            holder = entry.get("assignment") or entry.get("quiz") or {}
            if not isinstance(holder, dict):
                continue
            task = normalize_planner_item(
                {
                    "plannable_type": "quiz" if entry.get("quiz") else "assignment",
                    "plannable_id": holder.get("id"),
                    "plannable_date": holder.get("due_at"),
                    "plannable": holder,
                    "html_url": entry.get("html_url"),
                    "course_id": entry.get("course_id") or holder.get("course_id"),
                    "submissions": False,       # 待办里没有提交状态，标成未知
                },
                course_map, zone,
            )
            if task and task["key"] not in seen:
                seen.add(task["key"])
                task["submitted"] = None
                raw.append(task)

        for course in courses:
            cid = course.get("id")
            if cid is None:
                continue
            try:
                assignments = client.course_assignments(cid)
            except CanvasError as exc:
                print(f"  [注意] {course.get('name', cid)} 的作业拉不到："
                      f"{canvas_api.hint_for(exc.kind)}")
                continue
            if verbose:
                print(f"  {course.get('name', cid)}：{len(assignments)} 个作业")
            for assignment in assignments:
                task = normalize_assignment(assignment, course_map, cid, zone)
                if task:
                    raw.append(task)

    return _postprocess(raw, cfg, zone, {
        "student": profile.get("name") or "",
        "email": profile.get("primary_email") or "",
        "timezone": tz_name,
        "source": source,
        "planner_error": planner_error,
    })


# --------------------------------------------------------------------------
# token 年龄（Canvas 给学生 token 通常设 30 天有效期）
# --------------------------------------------------------------------------
def track_token_age(cfg: dict[str, Any]) -> dict[str, Any]:
    """
    记下 token 第一次用是什么时候，好在快到期前提醒你换。
    没有这层的话，一个月后的某天挂件会突然变红，而你会以为是程序坏了。
    """
    limit = float(cfg.get("canvas", {}).get("token_days") or 30)
    state = store.read_json(store.TOKEN_STATE_FILE, default={}) or {}
    today = datetime.now().strftime("%Y-%m-%d")

    if not state.get("first_seen"):
        state["first_seen"] = today
        state["note"] = "第一次成功拉取数据的日期，用来估算 token 什么时候到期"
    state["last_ok"] = today
    store.write_json(store.TOKEN_STATE_FILE, state)

    first = timeutil.parse_stamp(f"{state['first_seen']} 00:00:00")
    used = timeutil.days_since(first) if first else 0.0
    return {
        "token_days_used": round(used, 1),
        "token_days_limit": limit,
        "token_warn": used >= max(1.0, limit - 5),
    }


# --------------------------------------------------------------------------
# 输出
# --------------------------------------------------------------------------
def render_text(payload: dict[str, Any]) -> str:
    """纯文本快照。HTML 万一出问题，记事本还能打开这个。"""
    meta = payload["fetch"]
    lines = [
        "=" * 62,
        "  Canvas 截止日期",
        "=" * 62,
    ]
    if not meta.get("ok"):
        lines += [
            "",
            f"  [!] 本次拉取失败：{meta.get('error') or '原因不明'}",
            f"      {meta.get('hint') or ''}",
            "      下面是上一次成功拉取的数据，可能已经过时。",
        ]
    lines += [
        f"  学生：{meta.get('student') or '(未知)'}",
        f"  更新：{meta.get('time')}（时区 {meta.get('timezone')}）",
        f"  课程 {meta.get('course_count', 0)} 门 · "
        f"任务 {meta.get('task_count', 0)} 个 · "
        f"待处理 {meta.get('needs_action_count', 0)} 个",
    ]
    # 说清楚「有些东西被屏蔽了」—— 不然你会以为作业漏了
    if meta.get("hidden_count"):
        kinds = meta.get("hidden_types") or []
        types = "、".join(ics.label_for(k) for k in kinds) or "日历事件"
        lines.append(f"  （另有 {meta['hidden_count']} 条{types}没列出来，"
                     f"想看到就改 config.json 的 display.hide_types）")
    lines.append("")

    now = timeutil.now_utc()
    groups: list[tuple[str, list[dict[str, Any]]]] = [
        ("已过期且未交", []),
        ("24 小时内", []),
        ("3 天内", []),
        ("7 天内", []),
        ("更远", []),
        ("已提交 / 已豁免", []),
    ]
    for task in payload["tasks"]:
        due = timeutil.parse_iso(task.get("due_utc"))
        if due is None:
            continue
        if not task.get("needs_action"):
            groups[5][1].append(task)
            continue
        bucket = timeutil.urgency(timeutil.remaining_seconds(due, now))
        index = {"past": 0, "urgent": 1, "soon": 2, "week": 3, "later": 4}[bucket]
        groups[index][1].append(task)

    for label, items in groups:
        if not items:
            continue
        lines.append(f"--- {label}（{len(items)}）---")
        for task in items:
            due = timeutil.parse_iso(task.get("due_utc"))
            left = timeutil.short_remaining(due, now) if due else "无截止日期"
            mark = {"True": "[已交]", "False": "[未交]", "None": "[未知]"}[str(task.get("submitted"))]
            lines.append(
                f"  {mark} {task.get('course_code', ''):<10} {task.get('title', '')}"
            )
            lines.append(f"         {task.get('due_full', '')}   剩 {left}")
            if task.get("url"):
                lines.append(f"         {task['url']}")
        lines.append("")

    if payload.get("undated"):
        lines.append(f"--- 没有截止日期（{len(payload['undated'])}）---")
        for task in payload["undated"]:
            lines.append(f"  {task.get('course_code', ''):<10} {task.get('title', '')}")
        lines.append("")

    return "\n".join(lines)


def render_html(payload: dict[str, Any]) -> str:
    """
    把 ui.html 当模板，把数据塞进去。
    用模板文件而不是在 Python 里拼 HTML 字符串：HTML/CSS/JS 有自己的语法，
    塞进 Python 字符串里既难写又难看，改一次样式要小心翼翼。
    """
    # 注意是 ASSETS 不是 ROOT：ui.html 是跟着程序走的只读资源，
    # 打包之后它在解压目录里，而 ROOT 指向的是 exe 旁边的用户数据目录。
    template = (store.ASSETS / "ui.html").read_text(encoding="utf-8")
    data = json.dumps(payload, ensure_ascii=False)
    # </script> 会提前关掉 script 标签，必须打断
    data = data.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    return template.replace("/*__DATA__*/", data)


def write_outputs(
    tasks: list[dict[str, Any]], undated: list[dict[str, Any]],
    meta: dict[str, Any], cfg: dict[str, Any], ok: bool,
    error: str | None = None, error_kind: str | None = None,
) -> dict[str, Any]:
    """
    写盘。注意：**失败时也要写**，只是把错误信息放进 fetch 块。
    这样挂件才知道「数据是旧的，而且是因为 token 过期」。
    """
    meta = dict(meta)
    meta.update({
        "ok": ok,
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "error": error,
        "error_kind": error_kind,
        "hint": canvas_api.hint_for(error_kind) if error_kind else None,
    })

    payload = {"fetch": meta, "tasks": tasks, "undated": undated}
    store.write_json(store.DEADLINES_FILE, payload)

    output_cfg = cfg.get("output", {})
    if output_cfg.get("write_text", True):
        store.write_text(store.TEXT_FILE, render_text(payload))
    if output_cfg.get("write_html", True):
        try:
            store.write_text(store.HTML_FILE, render_html(payload))
        except Exception as exc:                 # 模板坏了也不该影响 json
            print(f"  [警告] 网页生成失败（不影响挂件）：{exc}")

    return payload


def _write_failure(cfg: dict[str, Any], error: str, error_kind: str, say) -> None:
    """
    拉取失败时的统一收尾：**保留上一次的数据**，只把错误写进 fetch 块。

    这是整个工具最容易做错、后果也最严重的地方。直接清空的话，挂件会变成
    「这周没作业」—— 而那正是最危险的假象：你会以为没事，然后错过 deadline。
    宁可显示旧数据 + 一条红色警告，也不要显示一个干净但骗人的空列表。
    """
    previous = store.read_json(store.DEADLINES_FILE, default={}) or {}
    old_tasks = previous.get("tasks") or []
    old_undated = previous.get("undated") or []
    old_meta = previous.get("fetch") or {}

    if old_tasks:
        say(f"  已保留上次拉到的 {len(old_tasks)} 个任务，挂件上会标红提示数据已过时")

    write_outputs(old_tasks, old_undated, old_meta, cfg,
                  ok=False, error=error, error_kind=error_kind)


# --------------------------------------------------------------------------
# 自检（run-check.bat 调的）
# --------------------------------------------------------------------------
def check(cfg: dict[str, Any], verbose: bool = False) -> int:
    """
    只验证「订阅链接能不能用、能解析出什么东西」，**不写任何输出文件**。

    跟正常拉取的区别就在这儿：跑坏了不会把挂件正在用的数据搞脏。
    你应该在第一次配好之后跑这个 —— 它会让你亲眼核对课程和作业对不对得上，
    这一步比看挂件有没有报错重要得多。
    """
    url = store.get_feed_url(cfg)
    print(f"  订阅地址：{store.redact(url)}")
    print()

    print("  下载中 ……")
    text = fetch_feed(url, cfg, verbose=verbose)
    print(f"  下载成功，{len(text.encode('utf-8')) / 1024:.1f} KB")

    try:
        store.write_text(store.FEED_DEBUG_FILE, ics.summarize(text))
    except OSError:
        pass

    zone = timeutil.get_zone(timeutil.FALLBACK_TZ)
    events = ics.parse_events(text)
    base_url = (cfg.get("canvas") or {}).get("base_url") or ""
    tasks = ics.to_tasks(events, zone, base_url=base_url)

    print(f"  解析出 {len(events)} 条事件 → {len(tasks)} 个有截止时间的条目")
    print()

    if not tasks:
        print("  [不对] 一条都没解析出来。")
        print("         把 state\\feed_debug.txt 发给给你这个程序的人 —— 里面记了")
        print("         订阅里实际有哪些字段，照着改解析就行。")
        return 1

    hidden_types = set(cfg.get("display", {}).get("hide_types") or [])

    # ---- 按类型统计 ----
    # 「作业」是真正要看的；「事件」是上课时间表，默认被 hide_types 屏蔽。
    # 如果这里「作业」是 0，那挂件必然是空的 —— 这是最常见的故障。
    by_type: dict[tuple[str, str], int] = {}
    for task in tasks:
        k = (task["type"], task["type_label"])
        by_type[k] = by_type.get(k, 0) + 1

    print("  按类型分：")
    for (kind, label), count in sorted(by_type.items(), key=lambda kv: -kv[1]):
        hidden = "  ← 挂件里默认不显示" if kind in hidden_types else ""
        print(f"      {label:<6} {count:>4} 个{hidden}")
    visible = sum(c for (k, _), c in by_type.items() if k not in hidden_types)
    print(f"      {'—' * 22}")
    print(f"      挂件里会显示 {visible} 个")
    print()

    # ---- 按课程统计：核对课程对不对得上，就靠这一段 ----
    by_course: dict[str, list] = {}
    for task in tasks:
        entry = by_course.setdefault(
            task["course_code"], [task.get("course_name") or "", 0]
        )
        entry[1] += 1
    print(f"  涉及 {len(by_course)} 门课：")
    for code, (name, count) in sorted(by_course.items(), key=lambda kv: -kv[1][1]):
        shown = f"  {name}" if name and name != code else ""
        print(f"      {code:<24} {count:>4} 个{shown}")
    print()

    # ---- 最近几个「作业」：跟 Canvas 网页上对一遍，对得上就说明时区没错 ----
    homework = [t for t in tasks if t["type"] not in hidden_types and t["due_utc"]]
    homework.sort(key=lambda t: t["due_utc"])
    now = timeutil.now_utc()
    upcoming = [t for t in homework if timeutil.parse_iso(t["due_utc"]) >= now]
    soon = (upcoming or homework)[:8]

    if not soon:
        print("  [注意] 一条作业都没认出来 —— 挂件会是空的。")
        print("         把 state\\feed_debug.txt 发给给你这个程序的人。")
        return 1

    print(f"  最近的 {len(soon)} 个作业：（拿去跟 Canvas 网页上显示的日期对一遍）")
    for task in soon:
        due = timeutil.parse_iso(task["due_utc"])
        left = timeutil.short_remaining(due, now) if due else "无期限"
        code = task["course_code"]
        print(f"      {left:>14}  {code:<12} {task['title']}")
        print(f"                      {task['due_full']}")
    print()

    print("  [OK] 订阅可用。")
    print()
    print("  请核对：上面这些课程和作业，跟你 Canvas 上看到的一致吗？")
    print("  一致就双击 run-fetch.bat 正式拉一次。")
    print("  不一致（比如少了课、时间差几个小时）就把这段话发给给你这个程序的人。")
    return 0


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------
def run(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="从 Canvas 拉取所有截止日期")
    parser.add_argument("--quiet", action="store_true", help="少打印（任务计划用）")
    parser.add_argument("--verbose", action="store_true", help="打印每个请求的细节")
    parser.add_argument("--check", action="store_true",
                        help="只验证订阅能不能用，不写任何文件")
    args = parser.parse_args(argv)

    store.setup_console()
    store.ensure_dirs()
    cfg = store.load_config(quiet=args.quiet)

    def say(message: str = "") -> None:
        if not args.quiet:
            print(message)

    # ---- 自检模式：只验证，不写文件 ----
    if args.check:
        print("=" * 62)
        print("  检查日历订阅")
        print("=" * 62)
        print()
        try:
            return check(cfg, verbose=args.verbose)
        except (CanvasError, ValueError) as exc:
            kind = getattr(exc, "kind", "config")
            print()
            print(f"  [没通过] {canvas_api.hint_for(kind)}")
            print(f"  原因：{store.redact(str(exc))}")
            print()
            print("  把上面的信息发给给你这个程序的人。")
            return 1

    say("=" * 62)
    say("  拉取 Canvas 截止日期")
    say("=" * 62)

    # ---- 挑数据源 ----
    # 优先用 API token（数据最全，带 Canvas 官方的提交状态）；
    # 没有 token 就退回日历订阅。
    # 墨大当前不让学生生成 token，所以实际跑的是日历订阅那条路。
    canvas_cfg = cfg.get("canvas", {})
    has_token = bool(str(canvas_cfg.get("token") or "").strip())
    feed_url = str(canvas_cfg.get("calendar_feed_url") or "").strip()

    if not has_token and not feed_url:
        # 两个都没有 —— 这是最常见的「还没配置」情况，给明确指引而不是堆栈
        say("\n[没配置好] 还没填日历订阅链接。")
        say("  怎么拿：Canvas 左边深色竖条 → Calendar（日历）")
        say("          → 右侧栏拉到最下面 → 点 Calendar Feed → 复制那串网址")
        say("  粘到 config.json 的 canvas.calendar_feed_url 里，再跑一次。")
        _write_failure(cfg, "还没填日历订阅链接", "config", say)
        store.append_log(["配置问题：还没填日历订阅链接"],
                         int(cfg.get("output", {}).get("keep_log_days") or 60))
        return 2

    try:
        if has_token:
            client = canvas_api.build_client(cfg, verbose=args.verbose)
            say(f"\n  连接 {client.base_url} ……（数据源：Canvas API）")
            tasks, undated, meta = collect(client, cfg, verbose=args.verbose)
        else:
            say("\n  读取日历订阅 ……")
            say(f"  地址：{store.redact(store.get_feed_url(cfg))}")
            tasks, undated, meta = collect_from_feed(cfg, verbose=args.verbose)
    except (CanvasError, ValueError) as exc:
        kind = getattr(exc, "kind", "config")
        say(f"\n[失败] {canvas_api.hint_for(kind)}")
        # 报错信息里可能带着订阅链接（那等于一把钥匙），一律先抹掉再打印
        say(f"  原因：{store.redact(str(exc))}")

        _write_failure(cfg, store.redact(str(exc)), kind, say)
        store.append_log([f"拉取失败（{kind}）：{store.redact(str(exc))}"],
                         int(cfg.get("output", {}).get("keep_log_days") or 60))
        return 1

    # token 年龄只在真的用 token 时才有意义
    if has_token:
        age = track_token_age(cfg)
        meta.update(age)
    else:
        age = {}

    if meta.get("planner_error"):
        say(f"\n  [注意] Planner 不可用，已回退到逐课程拉取：{meta['planner_error']}")

    write_outputs(tasks, undated, meta, cfg, ok=True)

    # ---- 通知 ----
    notify_hours = float(cfg.get("display", {}).get("notify_hours") or 0)
    notify_lines = notify.notify_urgent(tasks, notify_hours)

    # ---- 控制台摘要 ----
    now = timeutil.now_utc()
    needs = [t for t in tasks if t.get("needs_action")]
    say()
    if meta.get("student"):
        say(f"  学生：{meta['student']}")
    else:
        # 日历订阅里没有账号信息，不用假装有
        say("  数据源：日历订阅（不含账号信息，也拿不到「交没交」的状态）")
    say(f"  时区：{meta.get('timezone')}")
    say(f"  课程 {meta['course_count']} 门 · 任务 {meta['task_count']} 个 · "
        f"待处理 {meta['needs_action_count']} 个")
    if meta.get("manual_submitted_count"):
        say(f"  其中 {meta['manual_submitted_count']} 个是你手动标记为已交的")

    if needs:
        say()
        say("  最紧的几个：")
        for task in needs[:5]:
            due = timeutil.parse_iso(task.get("due_utc"))
            left = timeutil.short_remaining(due, now) if due else "无期限"
            say(f"    {left:>12}  {task['course_code']:<10} {task['title']}")
            say(f"                  {task.get('due_full', '')}")
    else:
        say()
        say("  最近没有待处理的截止任务。")

    if age.get("token_warn"):
        say()
        say(f"  [!] 这个 token 已经用了 {age['token_days_used']:.0f} 天，"
            f"Canvas 通常 {age['token_days_limit']:.0f} 天就让它过期。")
        say("      现在去 Canvas 换一串新的，比某天早上突然发现数据不更新了要省事。")

    say()
    say(f"  已写入：{store.DEADLINES_FILE}")
    if cfg.get("output", {}).get("write_html", True):
        say(f"          {store.HTML_FILE}")
    if cfg.get("output", {}).get("write_text", True):
        say(f"          {store.TEXT_FILE}")
    say()
    say("  挂件会自动读到新数据（每 3 秒看一次文件）。")

    summary = (
        f"成功：{meta['task_count']} 个任务（待处理 {meta['needs_action_count']}），"
        f"数据源 {meta['source']}"
    )
    if age.get("token_days_used") is not None:
        summary += f"，token 已用 {age['token_days_used']:.0f} 天"
    if meta.get("manual_submitted_count"):
        summary += f"，手动标记已交 {meta['manual_submitted_count']} 个"

    store.append_log(
        [summary] + [f"  {line}" for line in notify_lines],
        int(cfg.get("output", {}).get("keep_log_days") or 60),
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    """
    最外层兜底。pythonw 跑的时候没有控制台，
    未捕获的异常会让进程直接消失，你完全不会知道。
    """
    try:
        return run(argv)
    except SystemExit:
        raise
    except BaseException as exc:                      # noqa: BLE001
        detail = store.redact(f"{type(exc).__name__}: {exc}")
        try:
            store.setup_console()
            store.ensure_dirs()
            print(f"[崩溃] {detail}")
            traceback.print_exc()
            cfg = store.load_config(quiet=True)
            # 崩溃时也要保住旧数据 —— 这是最后一道防线，
            # 不能让一个未预料的异常把「这周有什么作业」变成一片空白
            _write_failure(cfg, detail, "crash", print)
            store.append_log(
                ["崩溃：" + detail] + traceback.format_exc().splitlines()[-6:],
                int(cfg.get("output", {}).get("keep_log_days") or 60),
            )
        except Exception:
            pass                                      # 连日志都写不进去就只能放弃了
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
