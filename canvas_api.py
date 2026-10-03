# -*- coding: utf-8 -*-
"""
canvas_api.py —— Canvas LMS 的只读客户端

为什么不用 canvasapi 那个现成的库：它不支持 Planner 接口
（GitHub 上 ucfopen/canvasapi#260 还挂着没实现），而 Planner 恰好是最适合这个
工具的数据源 —— 一次请求拿到所有课程的作业/测验/讨论/事件，日期已经按你的
section 个性化过，还带提交状态。为了它去引一个覆盖不全的依赖不划算，
用标准库自己写这一百来行更清楚（HTTP 那层在 netutil.py，零依赖）。

用法（自检）：
    python canvas_api.py --check

设计要点：
    * 只读。这个文件里没有任何 POST/PUT/DELETE，不可能改到你的 Canvas。
    * 错误分类。401 / 403 / 429 / 网络问题 是四件不同的事，处理方式也不同，
      所以要分开报，让挂件能显示「token 过期了」而不是笼统的「出错了」。
    * token 不进日志。所有要落盘或打印的文本都先过 redact()。
"""

from __future__ import annotations

import re
import sys
import time
from typing import Any, Iterator

# 让 `python canvas_api.py --check` 也能直接跑
if __package__ in (None, ""):
    sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))

import netutil  # noqa: E402
import store  # noqa: E402
import timeutil  # noqa: E402


# --------------------------------------------------------------------------
# 错误
# --------------------------------------------------------------------------
class CanvasError(Exception):
    """
    kind 是给程序看的（决定要不要重试、挂件显示什么），
    str(e) 是给你看的中文说明。
    """

    def __init__(self, kind: str, message: str, status: int | None = None):
        super().__init__(message)
        self.kind = kind
        self.status = status


# 挂件状态栏直接显示这些文字，所以写得越具体越好 ——
# 「出错了」帮不了你，「token 过期了，去重新生成」能。
ERROR_HINTS: dict[str, str] = {
    "config": "还没配置日历订阅链接",
    "auth": "token 已失效，去 Canvas 重新生成一串",
    "forbidden": "链接被拒绝了 —— 多半是订阅链接失效，去 Canvas 重新复制一份",
    "not_found": "链接不存在 —— 订阅多半被重置过，去 Canvas 重新复制一份",
    "bad_url": "这个链接 Canvas 不认 —— 多半是没复制全，回去重新复制一遍",
    "rate_limit": "请求太频繁被限流了，等一会儿再试",
    "network": "连不上 Canvas，检查一下网络",
    "server": "Canvas 服务器出错了，等会儿再试",
    "bad_response": "拿回来的不是日历文件 —— 订阅链接可能失效了",
    "unknown": "未知错误",
}


def hint_for(kind: str) -> str:
    return ERROR_HINTS.get(kind, ERROR_HINTS["unknown"])


# --------------------------------------------------------------------------
# token 脱敏
# --------------------------------------------------------------------------
def redact(text: str, token: str | None = None) -> str:
    """
    把 token 从要打印/落盘的文本里抹掉。
    HTTP 库的异常消息有时会把完整的请求 URL 带出来，而 token 理论上只走
    header —— 但万一哪天有人改成 query 参数传，这层兜底能防止它进日志。
    """
    if not text:
        return text
    if token and len(token) > 8:
        text = text.replace(token, "***")
    # 顺手把形如 ?access_token=xxx 的也抹掉
    return re.sub(r"(access_token=)[^&\s]+", r"\1***", text)


# --------------------------------------------------------------------------
# 分页
# --------------------------------------------------------------------------
# Link: <https://...&page=2>; rel="next", <https://...&page=1>; rel="current"
_LINK_RE = re.compile(r'<([^>]+)>\s*;\s*rel="([^"]+)"')


def next_link(header: str | None) -> str | None:
    """
    Canvas 默认每页只给 10 条。不跟 Link 头的话你会以为一学期只有 10 个作业 ——
    这是这类工具最容易出的静默错误。URL 当成不透明字符串用，不要自己拼 page 参数。
    """
    if not header:
        return None
    for url, rel in _LINK_RE.findall(header):
        if rel == "next":
            return url
    return None


# --------------------------------------------------------------------------
# 客户端
# --------------------------------------------------------------------------
class CanvasClient:
    def __init__(
        self,
        base_url: str,
        token: str,
        timeout: float = 20.0,
        max_retries: int = 3,
        verbose: bool = False,
    ):
        self.base_url = base_url.rstrip("/")
        self.api = f"{self.base_url}/api/v1"
        self.token = token
        self.timeout = timeout
        self.max_retries = max(1, int(max_retries))
        self.verbose = verbose

        # 每个请求都带上的头。原来挂在 requests.Session 上，
        # 换标准库之后就是一个普通 dict，交给 netutil.get()。
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            # 带上 UA 是礼貌，也方便学校管理员在日志里认出这是个人工具而不是爬虫
            "User-Agent": "canvas-deadlines/1.0 (personal student tool)",
        }

        # 限流相关：Canvas 的 X-Request-Cost 是每次请求对你配额的开销。
        # 正常单个学生用量远远够，记下来只是为了出问题时能诊断。
        self.last_cost: float | None = None
        self.last_remaining: float | None = None

    # ---------------------------------------------------------------- 底层
    def _log(self, message: str) -> None:
        if self.verbose:
            print(redact(message, self.token))

    def _request(self, url: str, params: dict[str, Any] | None = None) -> netutil.Response:
        """
        发一个 GET，处理重试和错误分类。
        哪些重试、哪些不重试：
            * 网络超时 / 连接失败  → 重试（多半是 WiFi 抽风）
            * 429 限流             → 重试（退避等一会儿）
            * 5xx                  → 重试（Canvas 自己抽风）
            * 401 / 403 / 404      → 不重试（重试一百次结果一样，只是浪费你时间）
            * 证书错               → 不重试（也一样）
        """
        if netutil.url_origin(url) != netutil.url_origin(self.base_url):
            raise CanvasError("bad_response", "分页链接指向其他来源，已停止请求以保护 token。")
        delay = 1.0
        last_error: Exception | None = None

        for attempt in range(1, self.max_retries + 1):
            try:
                resp = netutil.get(
                    url, headers=self.headers, params=params, timeout=self.timeout
                )
            except netutil.Timeout as exc:
                last_error = CanvasError("network", f"请求超时（{self.timeout} 秒）：{exc}")
            except netutil.TLSFailure as exc:
                # 证书出错重试没意义，多半是系统证书包没配对
                raise CanvasError("network", f"HTTPS 证书验证没过：{exc}") from exc
            except netutil.HttpFailure as exc:
                last_error = CanvasError("network", f"连接失败：{exc}")
            except Exception as exc:                              # noqa: BLE001
                last_error = CanvasError("unknown", f"请求出错：{exc}")
            else:
                self._read_headers(resp)

                if resp.status_code == 200:
                    return resp
                if resp.status_code == 401:
                    raise CanvasError("auth", "Canvas 说这个 token 无效（401）", 401)
                if resp.status_code == 403:
                    raise CanvasError("forbidden", "这个 token 没有访问权限（403）", 403)
                if resp.status_code == 404:
                    raise CanvasError("not_found", "接口不存在（404）", 404)
                if resp.status_code == 429:
                    last_error = CanvasError("rate_limit", "被 Canvas 限流（429）", 429)
                    retry_after = resp.headers.get("Retry-After")
                    if retry_after:
                        try:
                            delay = max(delay, float(retry_after))
                        except ValueError:
                            pass
                elif resp.status_code >= 500:
                    last_error = CanvasError(
                        "server", f"Canvas 服务器错误（{resp.status_code}）", resp.status_code
                    )
                else:
                    raise CanvasError(
                        "unknown", f"意料之外的响应码 {resp.status_code}", resp.status_code
                    )

            if attempt < self.max_retries:
                self._log(f"  第 {attempt} 次失败（{last_error}），{delay:.0f} 秒后重试")
                time.sleep(delay)
                delay *= 2

        raise last_error or CanvasError("unknown", "请求失败，原因不明")

    def _read_headers(self, resp: netutil.Response) -> None:
        for header, attr in (("X-Request-Cost", "last_cost"),
                             ("X-Rate-Limit-Remaining", "last_remaining")):
            raw = resp.headers.get(header)
            if raw is None:
                continue
            try:
                setattr(self, attr, float(raw))
            except ValueError:
                pass

    def _decode(self, resp: netutil.Response) -> Any:
        try:
            return resp.json()
        except ValueError as exc:
            # 典型场景：被学校的登录门户劫持，返回了一个 HTML 登录页而不是 JSON
            body = (resp.text or "")[:120].replace("\n", " ")
            raise CanvasError(
                "bad_response",
                f"返回的不是 JSON（可能被登录页劫持了）：{redact(body, self.token)}",
            ) from exc

    # ------------------------------------------------------------ 分页封装
    def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        """单个请求，不分页。用于 profile 这种只返回一个对象的接口。"""
        url = path if path.startswith("http") else f"{self.api}/{path.lstrip('/')}"
        return self._decode(self._request(url, params))

    def get_all(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        max_pages: int = 60,
    ) -> list[Any]:
        """
        跟完 Link 头，把所有页拼起来。
        max_pages 是防呆：万一 Canvas 给了个循环的 next 链接，不至于死循环。
        60 页 × 100 条 = 6000 条，一个学生的数据不可能超过。
        """
        query = dict(params or {})
        query.setdefault("per_page", 100)

        url = path if path.startswith("http") else f"{self.api}/{path.lstrip('/')}"
        out: list[Any] = []
        pages = 0

        while url and pages < max_pages:
            resp = self._request(url, query if pages == 0 else None)
            chunk = self._decode(resp)
            if isinstance(chunk, list):
                out.extend(chunk)
            elif chunk is not None:
                out.append(chunk)

            pages += 1
            url = next_link(resp.headers.get("Link"))
            if url:
                self._log(f"  还有下一页（已取 {len(out)} 条），继续")

        if url:
            raise CanvasError("bad_response", f"达到 {max_pages} 页上限，数据不完整，请检查分页。")

        return out

    # ---------------------------------------------------------------- 接口
    def profile(self) -> dict[str, Any]:
        """自检用。返回 name / time_zone / primary_email 等。"""
        return self.get("users/self/profile") or {}

    def courses(self) -> list[dict[str, Any]]:
        """
        当前在修且可用的课程。include[]=term 才会带上学期名（不带就是 null）。
        只要 active + student + available，避免把旁听的、已结课的都拉进来。
        """
        return self.get_all("courses", {
            "enrollment_state": "active",
            "enrollment_type": "student",
            "state[]": "available",
            "include[]": ["term", "total_scores"],
            "exclude_blueprint_courses": "true",
        })

    def planner_items(self, start: str, end: str) -> list[dict[str, Any]]:
        """
        主数据源。start/end 是 yyyy-mm-dd。

        返回的每一项里，我们要的是：
            plannable_type  assignment / quiz / discussion_topic / calendar_event / ...
            plannable_date  该事项在 planner 里的日期 —— 对作业来说就是「你的」截止时间，
                            已经按你的 section 个性化过（这点比 assignments 接口省事）
            submissions     含 submitted / missing / late / excused。注意：
                            讨论区、日历事件、笔记这几类这个字段是 False（布尔），
                            不是「没提交」，而是「这类东西没有提交这回事」
        """
        return self.get_all("planner/items", {
            "start_date": start,
            "end_date": end,
            "per_page": 100,
        })

    def todo(self) -> list[dict[str, Any]]:
        """回退路径之一：待办事项。只有「还需要你动手」的东西。"""
        return self.get_all("users/self/todo", {
            "include[]": ["ungraded_quizzes"],
        })

    def course_assignments(self, course_id: int | str) -> list[dict[str, Any]]:
        """
        回退路径之二：逐课程拉作业。最稳，但请求数随课程数增长。

        bucket 的合法值是 past / overdue / undated / ungraded / unsubmitted /
        upcoming / future —— 注意没有 current 这个值（很容易记错）。
        不使用 bucket 过滤，否则会漏掉已过期和无截止日期的作业。
        时间范围统一交给 fetch._postprocess 处理。
        """
        return self.get_all(f"courses/{course_id}/assignments", {
            "order_by": "due_at",
            "include[]": ["submission", "all_dates"],
            "per_page": 100,
        })

    def check(self) -> dict[str, Any]:
        """
        自检：token 有效吗？能看到几门课？
        这是个单独方法，因为它是你配好之后第一个该跑的东西 ——
        与其等挂件显示「出错了」，不如直接告诉你「token 有效，看到 4 门课」。
        """
        profile = self.profile()
        courses = self.courses()
        return {
            "name": profile.get("name") or "(拿不到名字)",
            "email": profile.get("primary_email") or "",
            "timezone": profile.get("time_zone") or timeutil.FALLBACK_TZ,
            "courses": [
                {
                    "id": c.get("id"),
                    "name": c.get("name") or c.get("course_code") or "(无名)",
                    "code": c.get("course_code") or "",
                    "term": (c.get("term") or {}).get("name") or "",
                }
                for c in courses
            ],
        }


def build_client(cfg: dict[str, Any], verbose: bool = False) -> CanvasClient:
    """从配置里造一个客户端。token 缺失会抛 CanvasError('config')。"""
    canvas = cfg.get("canvas", {})
    try:
        token = store.get_token(cfg)
    except ValueError as exc:
        raise CanvasError("config", str(exc)) from exc

    return CanvasClient(
        base_url=canvas.get("base_url") or "https://canvas.lms.unimelb.edu.au",
        token=token,
        timeout=float(canvas.get("timeout") or 20),
        max_retries=int(canvas.get("max_retries") or 3),
        verbose=verbose,
    )


# --------------------------------------------------------------------------
# 命令行自检
# --------------------------------------------------------------------------
def main() -> int:
    store.setup_console()
    cfg = store.load_config()

    print("=" * 58)
    print("  Canvas token 自检")
    print("=" * 58)
    print(f"  服务器：{cfg['canvas'].get('base_url')}")

    try:
        client = build_client(cfg, verbose=True)
    except CanvasError as exc:
        print(f"\n[没配置好] {hint_for(exc.kind)}")
        print(f"  {exc}")
        return 2

    masked = client.token[:4] + "…" + client.token[-4:] if len(client.token) > 10 else "***"
    print(f"  token ：{masked}（长度 {len(client.token)}）")
    print()
    print("  正在连接……")

    try:
        info = client.check()
    except CanvasError as exc:
        print(f"\n[失败] {hint_for(exc.kind)}")
        print(f"  原因：{exc}")
        if exc.kind == "auth":
            print()
            print("  怎么处理：")
            print("    1. 登录 Canvas → 右上角头像 → Account → Settings")
            print("    2. 往下拉找到 Approved Integrations")
            print("    3. 点 + New Access Token 生成一串新的")
            print("    4. 复制，粘进 config.json 的 canvas.token")
        return 1

    print(f"\n  ✓ token 有效")
    print(f"  学生：{info['name']}  {info['email']}")
    print(f"  时区：{info['timezone']}")
    print(f"  在修课程 {len(info['courses'])} 门：")
    for course in info["courses"]:
        term = f"  ({course['term']})" if course["term"] else ""
        print(f"    - {course['name']}{term}")

    if not info["courses"]:
        print("\n  [注意] 一门课都没看到。可能是选的学期还没开始，或者选课还没生效。")

    print()
    print("  下一步：双击 run-fetch.bat 拉一次真实数据")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
