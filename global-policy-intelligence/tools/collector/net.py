# -*- coding: utf-8 -*-
"""collector · 网络层

职责：提供一个「永远能拿到东西或明确报错」的 HTTP 客户端。

设计要点（踩过的坑，务必保留）：
  1. 本机环境的 NO_PROXY 里含 `[::1]` 这种写法，httpx 解析时会抛
     `InvalidURL: Invalid port: ':1]'`，导致**所有**请求失败。
     这里在导入时就把它清掉，让 HTTP(S)_PROXY 正常生效。
  2. 单例连接池 + 统一超时 + 统一 UA；每次请求都记录 attempt，供证据台账审计。
  3. 默认 verify=True。只有显式配置 allow_insecure_tls 的信源才关闭校验
     （部分政府站点证书链不完整），绝不作为全局默认。
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

# ---------------------------------------------------------------- proxy 修复
# 必须在 import httpx 之前清理，否则 httpx 会在首次请求时抛 InvalidURL。
for _bad in ("NO_PROXY", "no_proxy"):
    _val = os.environ.get(_bad, "")
    if "[::" in _val or "::1]" in _val:
        os.environ[_bad] = ",".join(
            part for part in _val.split(",") if not part.strip().startswith("[")
        )

import httpx  # noqa: E402

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# 该 UA 用于标识自动化采集，便于站点管理员识别与联系；
# 政策情报场景建议在配置里声明真实联系方式（见 sources.yaml meta.contact_ua）。
DEFAULT_CONTACT_UA = "policy-intel-collector/1.0 (+contact: set-your-email-in-sources.yaml)"


# ---------------------------------------------------------------- 代理分流
DEFAULT_BYPASS_SUFFIXES = (
    "gov.cn",
    "npc.gov.cn",
    "org.cn",
    "gov.hk",
    "gov.sg",
    "mas.gov.sg",
    "legislation.gov.uk",
    "europa.eu",
    "iaisweb.org",
    "artemis.bm",
)


def prepare_proxy_env(extra_bypass: tuple[str, ...] = ()) -> dict[str, str]:
    """修好 NO_PROXY，并把重要的官方站点加入直连名单。

    实测结论（很重要）：
      · 本机代理在部分境内政府站上会握手失败（SSL: UNEXPECTED_EOF_WHILE_READING），
        而**直连完全正常**；对境外站（Federal Register）则代理与直连都可用。
      · 所以正确策略不是「关掉代理」，也不是「全部走代理」，而是**按域名分流**。
    这里把官方信源域名写进 NO_PROXY，让境外走代理、境内走直连。
    """
    current = os.environ.get("NO_PROXY") or os.environ.get("no_proxy") or ""
    parts = [p.strip() for p in current.split(",") if p.strip() and not p.strip().startswith("[")]
    for suffix in tuple(DEFAULT_BYPASS_SUFFIXES) + tuple(extra_bypass):
        if suffix and suffix not in parts:
            parts.append(suffix)
    joined = ",".join(parts)
    os.environ["NO_PROXY"] = joined
    os.environ["no_proxy"] = joined
    return {"no_proxy": joined}


def proxy_detected() -> str:
    return (
        os.environ.get("HTTPS_PROXY")
        or os.environ.get("https_proxy")
        or os.environ.get("HTTP_PROXY")
        or os.environ.get("http_proxy")
        or ""
    )


def probe_transport(url: str, timeout: float = 12.0) -> str:
    """探测某个 URL 该走代理还是直连。返回 'proxy' / 'direct' / 'none'。

    用途：给「代理与直连谁更可靠」提供**证据**，而不是靠猜。结果写进运行日志。
    """
    headers = {"User-Agent": DEFAULT_UA}
    proxy = proxy_detected()

    def _try(use_proxy: bool) -> bool:
        try:
            with httpx.Client(
                proxy=proxy if use_proxy else None,
                timeout=timeout,
                follow_redirects=True,
                headers=headers,
            ) as client:
                resp = client.get(url)
                return resp.status_code < 500
        except Exception:  # noqa: BLE001
            return False

    if proxy and _try(True):
        return "proxy"
    if _try(False):
        return "direct"
    return "none"


@dataclass
class Attempt:
    """一次抓取尝试的完整记录——用于证明「确实努力过」而不是一遇失败就判无动态。"""

    url: str
    tier: str                 # api / feed / html / render / pdf / mirror
    status: int | None = None
    ok: bool = False
    chars: int = 0
    error: str | None = None
    elapsed_ms: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "tier": self.tier,
            "status": self.status,
            "ok": self.ok,
            "chars": self.chars,
            "error": self.error,
            "elapsed_ms": self.elapsed_ms,
        }


@dataclass
class NetConfig:
    timeout: float = 25.0
    retries: int = 2
    sleep_between: float = 0.8          # 同一 host 的礼貌间隔（秒）
    user_agent: str = DEFAULT_UA
    verify_tls: bool = True
    max_bytes: int = 8 * 1024 * 1024    # 单次响应上限，防止误抓大文件
    use_proxy: bool = True              # 是否允许使用环境代理
    direct_fallback: bool = True        # 代理失败时自动改直连（实测必需）

    _last_host_hit: dict[str, float] = field(default_factory=dict)


class Fetcher:
    """带礼貌限速、重试、代理分流与尝试记录的 HTTP 客户端。"""

    def __init__(self, config: NetConfig | None = None) -> None:
        self.config = config or NetConfig()
        self.transport_notes: list[str] = []
        self._doc_cache: dict[str, tuple[str, str]] = {}
        self._client = self._build_client(use_proxy=self.config.use_proxy)
        self._direct_client: httpx.Client | None = (
            self._build_client(use_proxy=False) if self.config.direct_fallback else None
        )
        self.attempts: list[Attempt] = []

    def _build_client(self, *, use_proxy: bool) -> httpx.Client:
        proxy = proxy_detected() if use_proxy else None
        return httpx.Client(
            proxy=proxy,
            timeout=httpx.Timeout(self.config.timeout),
            follow_redirects=True,
            verify=self.config.verify_tls,
            headers={
                "User-Agent": self.config.user_agent,
                "Accept": "text/html,application/xhtml+xml,application/xml,application/json;q=0.9,*/*;q=0.8",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            },
        )

    # ------------------------------------------------------------------ 限速
    def _polite_wait(self, url: str) -> None:
        host = urlsplit(url).netloc
        last = self.config._last_host_hit.get(host)
        if last is not None:
            gap = self.config.sleep_between - (time.monotonic() - last)
            if gap > 0:
                time.sleep(gap)
        self.config._last_host_hit[host] = time.monotonic()

    # ------------------------------------------------------------------ 请求
    def request(
        self,
        url: str,
        *,
        tier: str = "html",
        method: str = "GET",
        json_body: Any = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response | None:
        """发起请求。失败不抛异常，返回 None 并把原因写进 attempts。

        顺序：先按配置走代理（境外站），失败且允许回退时改直连（境内政府站实测必需）。
        """
        clients: list[tuple[str, httpx.Client]] = [("proxy" if self.config.use_proxy else "direct", self._client)]
        if self._direct_client is not None and self.config.use_proxy:
            clients.append(("direct", self._direct_client))

        last_error: str | None = None
        for route, client in clients:
            for attempt_no in range(self.config.retries + 1):
                self._polite_wait(url)
                started = time.monotonic()
                try:
                    resp = client.request(
                        method, url, json=json_body, headers=headers, timeout=self.config.timeout
                    )
                    elapsed = int((time.monotonic() - started) * 1000)
                    body_len = len(resp.content)
                    ok = resp.status_code == 200 and body_len > 0
                    self.attempts.append(
                        Attempt(
                            url=url,
                            tier=f"{tier}:{route}",
                            status=resp.status_code,
                            ok=ok,
                            chars=body_len,
                            elapsed_ms=elapsed,
                            error=None if ok else f"HTTP {resp.status_code}, {body_len} bytes",
                        )
                    )
                    if resp.status_code == 200:
                        if body_len > self.config.max_bytes:
                            self.attempts[-1].error = "response too large, truncated"
                        if route == "direct" and self.config.use_proxy:
                            self.transport_notes.append(f"代理失败后直连成功: {url}")
                        return resp
                    if resp.status_code in (403, 404, 410):
                        return resp  # 确定性失败，重试无意义
                    last_error = f"HTTP {resp.status_code}"
                except Exception as exc:  # noqa: BLE001
                    elapsed = int((time.monotonic() - started) * 1000)
                    last_error = f"{type(exc).__name__}: {exc}"
                    self.attempts.append(
                        Attempt(url=url, tier=f"{tier}:{route}", ok=False,
                                error=last_error[:300], elapsed_ms=elapsed)
                    )
                if attempt_no < self.config.retries:
                    time.sleep(1.2 * (attempt_no + 1))  # 退避
            if last_error and "SSL" in last_error and route == "proxy":
                self.transport_notes.append(f"代理 TLS 握手失败，转直连重试: {url}")
        return None

    def get_text(self, url: str, *, tier: str = "html", encoding: str | None = None) -> str | None:
        resp = self.request(url, tier=tier)
        if resp is None or resp.status_code != 200:
            return None
        return decode_body(resp, encoding)

    def get_document(
        self, url: str, *, tier: str = "html", encoding: str | None = None
    ) -> tuple[str, str] | None:
        """取回一个文档，并**靠内容本身**判定它是 HTML 还是 PDF。

        为什么必须看 magic bytes：详情页链接经常直接指向 PDF（政府站尤其常见），
        若只按 URL 后缀判断，就会把 PDF 二进制当"正文"存下来并标成 full——
        这是最隐蔽的一类污染（实测踩到：UK legislation 的详情页返回 PDF）。
        返回 (kind, content)，kind 为 'pdf' 或 'html'。
        """
        if url in self._doc_cache:
            return self._doc_cache[url]
        resp = self.request(url, tier=tier)
        if resp is None or resp.status_code != 200:
            return None
        raw = resp.content
        ctype = resp.headers.get("content-type", "").lower()
        # 注意：这里全是 bytes。写成 `"%PDF-" in raw[:1024]`（str 找 bytes）会抛
        # TypeError，并被上层 try 吞掉 → 所有静态 HTML 源静默变成 blocked。
        # 这类"静默全线降级"是本项目最危险的 bug 类型，必须有端到端回归测试。
        is_pdf = (
            raw[:5] == b"%PDF-"
            or b"%PDF-" in raw[:1024]
            or "application/pdf" in ctype
        )
        if is_pdf:
            result = ("pdf", raw.decode("latin-1", errors="replace"))
        else:
            result = ("html", decode_body(resp, encoding))
        # 短缓存：避免 html 级与 pdf 级对同一 URL 重复请求（同一 URL 在一次采集中内容不会变）
        if len(self._doc_cache) < 500:
            self._doc_cache[url] = result
        return result

    def get_bytes(self, url: str, *, tier: str = "pdf") -> bytes | None:
        resp = self.request(url, tier=tier)
        if resp is None or resp.status_code != 200:
            return None
        return resp.content

    def get_json(self, url: str, *, tier: str = "api") -> Any | None:
        resp = self.request(url, tier=tier)
        if resp is None or resp.status_code != 200:
            return None
        try:
            return resp.json()
        except Exception as exc:  # noqa: BLE001
            self.attempts.append(
                Attempt(url=url, tier=tier, status=resp.status_code, error=f"JSON 解析失败: {exc}")
            )
            return None

    def close(self) -> None:
        self._client.close()
        if self._direct_client is not None:
            self._direct_client.close()

    def __enter__(self) -> "Fetcher":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


# ------------------------------------------------------------------ 编码处理
_CJK_HINTS = ("charset=gb2312", "charset=gbk", "charset=gb18030")


def decode_body(resp: httpx.Response, encoding: str | None = None) -> str:
    """政府站点大量使用 GB2312/GBK，httpx 的默认推断不可靠，这里按序兜底。"""
    raw = resp.content
    candidates: list[str] = []
    if encoding:
        candidates.append(encoding)
    ctype = resp.headers.get("content-type", "").lower()
    for hint in _CJK_HINTS:
        if hint in ctype:
            candidates.append(hint.split("=")[1])
    candidates += ["utf-8", "gb18030", "gbk", "big5", "latin-1"]
    for enc in candidates:
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="replace")


def host_of(url: str) -> str:
    return urlsplit(url).netloc.lower()
