# -*- coding: utf-8 -*-
"""
netutil.py —— 用标准库发 HTTP 请求（顶替 requests）

**为什么要换掉 requests**：
    这个工具要在 Mac 上跑，而那台 Mac 上什么都没有。让用户先 pip 装个包，
    是整个流程里最容易卡住的一步 —— 网络、代理、权限、pip 源，什么都可能出问题。
    而整个项目其实只用到 requests 的两件事：

        1. 发一个带自定义 header 的 GET
        2. 区分「超时 / 连不上 / 证书不对 / 服务器返回 4xx-5xx」

    这两件事 urllib.request 都能做，标准库自带，零依赖。
    换完之后 Windows 和 Mac 跑的是同一份代码，不用再分叉。

和 requests 的几个差别，下面都处理过了：
    * **重定向**：urllib 默认就跟（跟 requests 一样），不用管。
    * **压缩**：urllib 不会自己解压。这里显式要 gzip 并自己解 ——
      有些 CDN 不管你要不要都压，不处理的话拿回来是一堆乱码。
    * **4xx / 5xx**：urllib 会抛 HTTPError，requests 不会。
      这里把它**转回普通响应**返回，因为调用方本来就是靠 status_code 分支的
      （fetch.py 要区分 401/403/404/400/5xx 各自给不同提示）。
    * **证书**：python.org 装的 Python 不认 macOS 钥匙串，会报
      CERTIFICATE_VERIFY_FAILED。这里优先用 certifi 的证书包
      （Mac 安装脚本会装进 venv），装了就不需要用户手动跑
      「Install Certificates.command」。
    * **异常**：urllib 把网络问题全包在 URLError 里，真正的原因在 .reason 里
      （还可能再嵌一层）。这里拆成 Timeout / TLSFailure / NetworkFailure 三类，
      好让调用方能做到「超时就重试、证书错就不重试」——
      这套分类是原来 requests 那版就有的，不能丢。
"""

from __future__ import annotations

import gzip
import http.client
import io
import json as _json
import re
import socket
import ssl
import urllib.error
import urllib.parse
import urllib.request
import zlib
from typing import Any

DEFAULT_TIMEOUT = 20.0
DEFAULT_UA = "canvas-deadlines/1.0 (personal student tool)"

_CTX: ssl.SSLContext | None = None


# --------------------------------------------------------------------------
# 异常
# --------------------------------------------------------------------------
class HttpFailure(Exception):
    """网络层错误的基类。调用方要一把捞的时候用它。"""


class Timeout(HttpFailure):
    """超时。多半是 WiFi 抽风或服务器慢，值得重试。"""


class TLSFailure(HttpFailure):
    """HTTPS 握手 / 证书验证失败。**重试没用**，得修证书。"""


class NetworkFailure(HttpFailure):
    """连不上（DNS 失败、拒绝连接、断网）。值得重试。"""


# --------------------------------------------------------------------------
# 响应
# --------------------------------------------------------------------------
class Headers:
    """
    header 的名字大小写不敏感（HTTP 本来就不敏感，但 dict 敏感）。
    Canvas 回的是 Link / Retry-After / X-Request-Cost，写法必须对得上。
    """

    def __init__(self, raw: Any = None):
        self._raw = raw if raw is not None else {}

    def get(self, name: str, default: Any = None) -> Any:
        try:
            value = self._raw.get(name)
        except Exception:
            value = None
        if value is not None:
            return value
        # 有些实现（比如自己造的 dict）不认大小写，退回去自己找一遍
        try:
            lowered = name.lower()
            for key, val in self._raw.items():
                if str(key).lower() == lowered:
                    return val
        except Exception:
            pass
        return default

    def __contains__(self, name: str) -> bool:
        return self.get(name) is not None

    def __repr__(self) -> str:                                  # pragma: no cover
        try:
            return f"Headers({dict(self._raw)!r})"
        except Exception:
            return "Headers(?)"


class Response:
    """
    跟 requests.Response 长得像的那几个属性，够这个项目用。
    content 是 bytes，text 是解码后的字符串（惰性算，省得每次都解）。
    """

    def __init__(self, status_code: int, headers: Any, content: bytes, url: str = ""):
        self.status_code = status_code
        self.headers = Headers(headers)
        self.content = content or b""
        self.url = url
        self._text: str | None = None

    @property
    def text(self) -> str:
        if self._text is None:
            self._text = _decode(self.content, self.headers.get("Content-Type"))
        return self._text

    def json(self) -> Any:
        """解析不了会抛 ValueError（json.JSONDecodeError 是它的子类）。"""
        return _json.loads(self.text)

    def __repr__(self) -> str:                                  # pragma: no cover
        return f"<Response {self.status_code} {len(self.content)}B>"


def _decode(raw: bytes, content_type: Any) -> str:
    charset = "utf-8"
    if isinstance(content_type, str):
        found = re.search(r"charset=\s*([\w\-]+)", content_type, re.I)
        if found:
            charset = found.group(1)
    try:
        text = raw.decode(charset, errors="replace")
    except LookupError:                     # 服务器报了个不认识的字符集名
        text = raw.decode("utf-8", errors="replace")
    # 开头的 BOM 一律去掉：ics / json 里带 BOM 会让解析器认不出第一个字段，
    # 而这个文件可能是从 Windows 那边传过来的
    return text.lstrip("﻿")


def _decompress(raw: bytes, encoding: Any) -> bytes:
    name = (encoding or "").lower() if isinstance(encoding, str) else ""
    if "gzip" in name:
        try:
            return gzip.decompress(raw)
        except Exception:
            try:
                return gzip.GzipFile(fileobj=io.BytesIO(raw)).read()
            except Exception:
                return raw
    if "deflate" in name:
        try:
            return zlib.decompress(raw)
        except Exception:
            try:
                return zlib.decompress(raw, -zlib.MAX_WBITS)   # 裸 deflate
            except Exception:
                return raw
    return raw


def _read(resp: Any) -> tuple[bytes, Any]:
    raw = b""
    headers = None
    try:
        headers = resp.headers
    except Exception:
        pass
    # 读到一半断网必须报错，不能把失败伪装成 HTTP 200 + 空正文。
    raw = resp.read()
    return _decompress(raw, Headers(headers).get("Content-Encoding")), headers


# --------------------------------------------------------------------------
# 证书
# --------------------------------------------------------------------------
def ssl_context() -> ssl.SSLContext:
    """
    造一个 SSL 上下文。优先用 certifi 的证书包。

    certifi 是可选的 —— 没装就退回系统默认（Windows 上完全够用，
    Mac 上如果没装 certifi 会报证书错，安装脚本负责把它装上）。
    """
    global _CTX
    if _CTX is not None:
        return _CTX
    try:
        import certifi                                        # type: ignore
        _CTX = ssl.create_default_context(cafile=certifi.where())
    except Exception:
        _CTX = ssl.create_default_context()
    return _CTX


def has_certifi() -> bool:
    """给安装脚本/自检用：证书包到底装上没有。"""
    try:
        import certifi                                        # type: ignore  # noqa: F401
        return True
    except Exception:
        return False


# --------------------------------------------------------------------------
# 请求
# --------------------------------------------------------------------------
def _classify(reason: Any, timeout: float) -> HttpFailure:
    """
    URLError.reason 里可能套着好几层东西，挨个认一遍。
    认不出来就当断网处理 —— 至少比抛出个「unknown」强。
    """
    text = str(reason)
    if isinstance(reason, (socket.timeout, TimeoutError)):
        return Timeout(f"请求超时（{timeout:.0f} 秒）")
    if isinstance(reason, ssl.SSLCertVerificationError):
        return TLSFailure(f"证书验证没通过：{reason}")
    if isinstance(reason, ssl.SSLError):
        return TLSFailure(f"HTTPS 握手失败：{reason}")
    if isinstance(reason, socket.gaierror):
        return NetworkFailure(f"域名解析不了（DNS）：{reason}")
    if isinstance(reason, ConnectionRefusedError):
        return NetworkFailure(f"连接被拒绝：{reason}")
    if "timed out" in text.lower():
        return Timeout(f"请求超时（{timeout:.0f} 秒）")
    if "certificate" in text.lower() or "ssl" in text.lower():
        return TLSFailure(f"HTTPS 证书有问题：{reason}")
    return NetworkFailure(f"连不上：{reason}")


def build_url(url: str, params: dict[str, Any] | None) -> str:
    """
    把查询参数拼进 URL。

    doseq=True 是必须的：Canvas 的接口要 `include[]=term&include[]=total_scores`
    这种「一个键多个值」的写法（canvas_api 里传的就是 list）。
    值为 None 的键丢掉 —— requests 也是这么干的，而 urllib 会把它写成字符串 "None"。
    """
    if not params:
        return url
    clean = {k: v for k, v in params.items() if v is not None}
    if not clean:
        return url
    query = urllib.parse.urlencode(clean, doseq=True)
    if not query:
        return url
    parts = urllib.parse.urlsplit(url)
    return urllib.parse.urlunsplit(parts._replace(query="&".join(filter(None, (parts.query, query)))))


def url_origin(url: str) -> tuple[str, str | None, int | None]:
    parts = urllib.parse.urlsplit(url)
    return parts.scheme.lower(), parts.hostname, parts.port or (443 if parts.scheme.lower() == "https" else 80)


class SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """认证请求只允许同源 HTTPS 重定向，避免把 token 发给其他主机。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        authenticated = any(k.lower() == "authorization" for k, _ in req.header_items())
        if authenticated and url_origin(req.full_url) != url_origin(newurl):
            raise NetworkFailure("拒绝向其他来源重定向认证请求。")
        if urllib.parse.urlsplit(req.full_url).scheme == "https" and urllib.parse.urlsplit(newurl).scheme != "https":
            raise TLSFailure("拒绝从 HTTPS 降级到不安全的连接。")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def get(
    url: str,
    headers: dict[str, str] | None = None,
    params: dict[str, Any] | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> Response:
    """
    发一个 GET。返回 Response —— **4xx / 5xx 也是正常返回**，
    由调用方看 status_code 决定怎么办（见模块开头的说明）。

    真正抛出去的只有三种：Timeout / TLSFailure / NetworkFailure。
    """
    target = build_url(url, params)

    merged = {
        "User-Agent": DEFAULT_UA,
        # 自己要 gzip 就自己解（_decompress）；不写这一行的话有些 CDN 也会压，
        # 那时候拿回来的就是乱码，而且不报错 —— 这种静默错误最难查
        "Accept-Encoding": "gzip, deflate",
    }
    if headers:
        merged.update(headers)

    request = urllib.request.Request(target, headers=merged, method="GET")
    timeout = float(timeout)

    try:
        opener = urllib.request.build_opener(
            urllib.request.HTTPSHandler(context=ssl_context()), SafeRedirectHandler(),
        )
        with opener.open(request, timeout=timeout) as resp:
            body, resp_headers = _read(resp)
            final = target
            try:
                final = resp.geturl() or target
            except Exception:
                pass
            return Response(getattr(resp, "status", 200), resp_headers, body, final)

    except urllib.error.HTTPError as exc:
        # 服务器有回应，只是状态码不是 2xx。转成普通响应。
        body, resp_headers = _read(exc)
        return Response(exc.code, resp_headers, body, target)

    except urllib.error.URLError as exc:
        raise _classify(getattr(exc, "reason", exc), timeout) from exc

    except (socket.timeout, TimeoutError) as exc:
        raise Timeout(f"请求超时（{timeout:.0f} 秒）") from exc

    except ssl.SSLError as exc:
        raise TLSFailure(f"HTTPS 证书有问题：{exc}") from exc

    except FileNotFoundError as exc:
        raise RuntimeError("程序资源文件缺失。请完全退出程序，重新解压最新版后启动。") from exc

    except OSError as exc:
        raise NetworkFailure(f"连不上：{exc}") from exc

    except http.client.HTTPException as exc:
        raise NetworkFailure(f"响应下载中断：{exc}") from exc


def probe(url: str, timeout: float = 10.0) -> tuple[bool, str]:
    """
    粗略探一下这个地址通不通 —— 给自检/排查用，不做任何解析。
    返回 (通了吗, 人话说明)。任何异常都吞掉，只回一句说明。
    """
    try:
        resp = get(url, timeout=timeout)
    except HttpFailure as exc:
        return False, f"{type(exc).__name__}：{exc}"
    except Exception as exc:                                  # noqa: BLE001
        return False, f"{type(exc).__name__}：{exc}"
    return True, f"HTTP {resp.status_code}，{len(resp.content)} 字节"


if __name__ == "__main__":                                    # pragma: no cover
    import store
    store.setup_console()
    print("=" * 58)
    print("  netutil 自检")
    print("=" * 58)
    print(f"  certifi：{'装了' if has_certifi() else '没装（Mac 上会报证书错）'}")
    ok, note = probe("https://canvas.lms.unimelb.edu.au")
    print(f"  Canvas 首页：{'通' if ok else '不通'} —— {note}")
