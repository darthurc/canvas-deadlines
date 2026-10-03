# -*- coding: utf-8 -*-
"""
timeutil.py —— 时间解析、时区换算、倒计时格式化

单独一个模块的原因：时区是这类工具最容易搞错、而且搞错了后果最严重的地方
（差一小时就是晚交）。所有跟时间有关的转换只写在这一处，别处不许自己算。

两个必须记住的事实：
    1. Canvas 返回的时间戳有两种写法 —— 有的带 Z（UTC），有的带数字偏移
       （比如 2013-08-28T23:59:00-06:00，这是按课程时区给的）。
       两种都要能解析，不能假设只有一种。
    2. 墨尔本有夏令时。10 月第一个周日切 AEDT(+11)，4 月第一个周日切回 AEST(+10)。
       zoneinfo 会自动处理，所以绝对不要自己写 +10 / +11。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# 兜底时区。正常情况下会用 Canvas 上你账号设置里的时区（见 fetch.py），
# 只有拿不到的时候才用这个。
FALLBACK_TZ = "Australia/Melbourne"

WEEKDAY_CN = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


def get_zone(name: str | None) -> ZoneInfo:
    """
    拿到时区对象。名字无效就退回墨尔本，不要让整个程序因为一个时区名挂掉。
    系统缺 tzdata 时 ZoneInfoNotFoundError 也可能抛 —— 本机已确认有 tzdata。
    """
    for candidate in (name, FALLBACK_TZ):
        if not candidate:
            continue
        try:
            return ZoneInfo(candidate)
        except (ZoneInfoNotFoundError, ValueError, KeyError):
            continue
    raise ValueError("缺少时区数据库，无法可靠显示截止时间。请安装 requirements.txt 中的 tzdata。")


def parse_iso(value: str | None) -> datetime | None:
    """
    解析 Canvas 的时间戳。解析不了就返回 None ——
    调用方必须能接受 None（Canvas 的 due_at 本来就经常是 null）。
    """
    if not value or not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    # Python 3.11+ 的 fromisoformat 已经能认 "Z"，但显式替换更保险，
    # 而且这段代码万一被拿到 3.10 上跑也不会炸。
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        # 不带时区的当 UTC 处理。Canvas 理论上不会给这种，但别猜成本地时间。
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def to_zone(dt: datetime, zone: ZoneInfo) -> datetime:
    return dt.astimezone(zone)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def tz_label(dt: datetime) -> str:
    """AEST / AEDT 这种缩写。显示截止时间时必须带上，不然很容易看错一小时。"""
    return dt.tzname() or ""


def format_due(dt: datetime, zone: ZoneInfo) -> str:
    """给挂件用：10-25 周日 23:59 AEDT"""
    local = to_zone(dt, zone)
    return (
        f"{local.strftime('%m-%d')} {WEEKDAY_CN[local.weekday()]} "
        f"{local.strftime('%H:%M')} {tz_label(local)}"
    )


def format_full(dt: datetime, zone: ZoneInfo) -> str:
    """给网页/文本用：2026-10-25 周日 23:59 AEDT"""
    local = to_zone(dt, zone)
    return (
        f"{local.strftime('%Y-%m-%d')} {WEEKDAY_CN[local.weekday()]} "
        f"{local.strftime('%H:%M')} {tz_label(local)}"
    )


def remaining_seconds(dt: datetime, reference: datetime | None = None) -> float:
    """还有多少秒。已经过期就是负数。"""
    ref = reference or now_utc()
    return (dt - ref).total_seconds()


def countdown(dt: datetime, reference: datetime | None = None) -> str:
    """
    倒计时文字，每秒都会变的那种：1天 04:12:38 / 04:12:38 / 已过期 2天3小时
    天数部分不做零填充（1天 和 12天 都自然），时分秒固定两位，
    配合等宽字体就不会左右抖动。
    """
    secs = remaining_seconds(dt, reference)

    if secs < 0:
        return "已过期 " + _rough(-secs)

    total = int(secs)
    days, rest = divmod(total, 86400)
    hours, rest = divmod(rest, 3600)
    minutes, seconds = divmod(rest, 60)
    clock = f"{hours:02d}:{minutes:02d}:{seconds:02d}"
    return f"{days}天 {clock}" if days else clock


def _rough(secs: float) -> str:
    """粗粒度时长，给「已过期」和通知正文用：3天2小时 / 5小时 / 12分钟"""
    total = int(secs)
    days, rest = divmod(total, 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    if days:
        return f"{days}天{hours}小时" if hours else f"{days}天"
    if hours:
        return f"{hours}小时{minutes}分" if minutes else f"{hours}小时"
    return f"{minutes}分钟"


def short_remaining(dt: datetime, reference: datetime | None = None) -> str:
    """短的，给网页表格和通知标题用：1天4小时 / 47分钟 / 已过期2天"""
    secs = remaining_seconds(dt, reference)
    return ("已过期" + _rough(-secs)) if secs < 0 else _rough(secs)


def urgency(seconds_left: float) -> str:
    """
    紧急程度分档。挂件的颜色、排序、通知都看这个，集中定义避免三处不一致。
    返回 urgent / soon / week / later / past
    """
    if seconds_left < 0:
        return "past"
    if seconds_left < 24 * 3600:
        return "urgent"
    if seconds_left < 72 * 3600:
        return "soon"
    if seconds_left < 7 * 86400:
        return "week"
    return "later"


def progress_fraction(seconds_left: float, window_hours: float = 168.0) -> float:
    """
    进度条的填充比例：越紧急填得越满。
    window_hours 默认 7 天 —— 也就是「还有一周」时条是空的，「马上就要交」时是满的。
    超过一周返回 0，已经过期返回 1。
    """
    if seconds_left <= 0:
        return 1.0
    window = window_hours * 3600
    if seconds_left >= window:
        return 0.0
    return 1.0 - (seconds_left / window)


def human_age(seconds: float) -> str:
    """「多久以前」的说法，给状态栏用：刚刚 / 12分钟前 / 3小时前 / 2天前"""
    if seconds < 60:
        return "刚刚"
    total = int(seconds)
    if total < 3600:
        return f"{total // 60}分钟前"
    if total < 86400:
        return f"{total // 3600}小时前"
    return f"{total // 86400}天前"


def parse_stamp(text: str | None) -> datetime | None:
    """
    解析我们自己存进 deadlines.json 的本地时间戳（"%Y-%m-%d %H:%M:%S"，不带时区）。
    这种戳只用于「上次更新于」的显示，按本地时间理解就够了。
    """
    if not text:
        return None
    try:
        return datetime.strptime(text, "%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return None


def local_now() -> datetime:
    return datetime.now()


def days_since(dt: datetime) -> float:
    return (datetime.now() - dt).total_seconds() / 86400.0


def add_days(dt: datetime, days: int) -> datetime:
    return dt + timedelta(days=days)
