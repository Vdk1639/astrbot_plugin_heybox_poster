"""小黑盒 Web API 的最小异步客户端（发帖向）。

零第三方依赖：只用标准库 ``urllib``，阻塞调用丢进线程池，因此：

* 插件不需要额外 pip 包，AstrBot 里开箱即用；
* 不会和 AstrBot 自带的 httpx / aiohttp 版本打架。

统一处理公共参数、``hkey`` 签名、写接口 ``_rnd``、表单编码与业务错误。
"""

from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from .sign import signed_params, workshop_rnd

API_BASE = "https://api.xiaoheihe.cn"

#: 网页端全站公共参数
BASE_PARAMS = {
    "app": "heybox",
    "os_type": "web",
    "x_app": "heybox_website",
    "x_client_type": "web",
    "x_os_type": "Windows",
    "x_client_version": "",
    "client_type": "web",
    "web_version": "3.0",
    "version": "999.0.4",
}

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
)

#: 内容形态：27 = 动态（图文），11 = 长文/文章
LINK_TAG_DYNAMIC = 27
LINK_TAG_ARTICLE = 11

_ERROR_STATUS = {"failed", "login", "relogin", "expired", "frontend_failed"}


class XhhError(Exception):
    """业务失败 / 网络失败。"""

    def __init__(self, message: str, *, status: str | None = None,
                 path: str | None = None, payload: dict | None = None):
        super().__init__(message)
        self.status = status
        self.path = path
        self.payload = payload


class CaptchaRequired(XhhError):
    """命中风控验证码：``x_xhh_tokenid`` 缺失或过期。"""


class RateLimited(XhhError):
    """触发发帖限流（错误码 ``10006``），调用方应提示稍后再试，不自动重试。"""


def build_post_form(
    title: str,
    content: str,
    *,
    mode: str = "dynamic",
    topic_ids: list[str] | tuple[str, ...] = (),
    draft: bool = False,
    desc: str = "",
    view_limit: int = 1,
) -> dict[str, str]:
    """构造 ``POST /bbs/app/api/link/post`` 的表单。

    :param mode: ``dynamic``（动态，纯文本）或 ``article``（长文，支持 HTML）。
    :param topic_ids: 社区/话题 ID，**必填**，服务端缺了会报“请添加分区”。
    :param view_limit: 可见范围，``1`` 公开、``2`` 粉丝可见、``3`` 仅自己可见。
    """
    title = (title or "").strip()
    content = (content or "").strip()
    if not title:
        raise ValueError("标题不能为空")
    if not content:
        raise ValueError("正文不能为空")
    if len(title) > 100:
        raise ValueError(f"标题过长（{len(title)} 字，上限 100）")
    if not topic_ids:
        raise ValueError("缺少话题/社区 ID（topic_ids）")
    if int(view_limit) not in (1, 2, 3):
        raise ValueError("可见范围只能是 1 公开、2 粉丝可见、3 仅自己可见")

    is_article = mode == "article"
    blocks = [{"type": "html" if is_article else "text", "text": content}]

    form: dict[str, str] = {
        "title": title,
        "desc": (desc or title)[:100],
        "post_type": "1",
        "text": json.dumps(blocks, ensure_ascii=False),
        "link_tag": str(LINK_TAG_ARTICLE if is_article else LINK_TAG_DYNAMIC),
        "topic_ids": ",".join(str(t) for t in topic_ids),
        "hashtags": "[]",
        "original": "1",
        "view_limit": str(int(view_limit)),
    }
    if is_article:
        # 长文必需的四件套，缺了会回“错误的帖子类型”
        form["words_count"] = str(len(content))
        form["declaration"] = "1"
        form["extra_declaration"] = "-1"
    if draft:
        form["draft"] = "1"
    return form


class XhhClient:
    """一个账号一个实例；发帖是写操作，不自动重试。"""

    def __init__(self, cookie: str, heybox_id: str | int = "",
                 timeout: float = 20.0, base: str = API_BASE):
        self.cookie = (cookie or "").strip()
        self.heybox_id = str(heybox_id or "").strip()
        self.timeout = float(timeout)
        self.base = base.rstrip("/")

    # ------------------------------------------------------------------
    @property
    def logged_in(self) -> bool:
        return "user_pkey=" in self.cookie

    async def __aenter__(self) -> "XhhClient":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()

    async def close(self) -> None:
        """无连接池，保留接口方便插件 terminate 调用。"""
        return None

    # ------------------------------------------------------------------
    def _build_url(self, path: str, params: dict | None, write: bool) -> str:
        signed = signed_params(path)
        query: dict[str, Any] = dict(BASE_PARAMS)
        query.update({k: v for k, v in (params or {}).items() if v is not None})
        if self.heybox_id:
            query["heybox_id"] = self.heybox_id
        query.update(signed)
        if write:
            query["_rnd"] = workshop_rnd(signed)
        return f"{self.base}{path}?{urllib.parse.urlencode(query, encoding='utf-8')}"

    def _request_sync(self, method: str, url: str, form: dict | None) -> dict:
        headers = {
            "User-Agent": UA,
            "Referer": "https://www.xiaoheihe.cn/",
            "Origin": "https://www.xiaoheihe.cn",
            "Accept": "application/json, text/plain, */*",
        }
        if self.cookie:
            headers["Cookie"] = self.cookie

        body = None
        if (method or "GET").upper() == "POST":
            body = urllib.parse.urlencode(form or {}, encoding="utf-8").encode("utf-8")
            headers["Content-Type"] = "application/x-www-form-urlencoded;charset=UTF-8"

        req = urllib.request.Request(url, data=body, headers=headers, method=method.upper())
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                code, raw = resp.status, resp.read()
        except urllib.error.HTTPError as exc:
            code, raw = exc.code, exc.read()
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise XhhError(f"请求失败：{type(exc).__name__}: {exc}") from exc

        text = raw.decode("utf-8", "replace")
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise XhhError(f"响应不是 JSON（HTTP {code}）：{text[:200]}") from exc
        if code >= 500:
            raise XhhError(f"服务端错误 HTTP {code}", payload=payload)
        return payload

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        form: dict | None = None,
        write: bool = False,
    ) -> dict:
        url = self._build_url(path, params, write)
        try:
            return await asyncio.to_thread(self._request_sync, method, url, form)
        except XhhError as exc:
            if exc.path is None:
                exc.path = path
            raise

    # ------------------------------------------------------------------
    def _unwrap(self, payload: dict, path: str) -> dict:
        status = payload.get("status")
        if status is None:
            return payload
        if status == "show_captcha":
            raise CaptchaRequired(
                "触发风控验证码：cookie 里的 x_xhh_tokenid 缺失或已失效",
                status=status, path=path,
            )
        msg = str(payload.get("msg") or "")
        code = payload.get("code") or payload.get("error_code")
        if "10006" in msg or code in (10006, "10006"):
            raise RateLimited(
                "发帖频率过快，请稍后再试",
                status=status, path=path, payload=payload,
            )
        if status in _ERROR_STATUS:
            hint = "（登录态失效，请重新提供 cookie）" if status in {"login", "relogin", "expired"} else ""
            raise XhhError((msg or f"status={status}") + hint,
                           status=status, path=path, payload=payload)
        if status != "ok":
            raise XhhError(payload.get("msg") or f"status={status}",
                           status=status, path=path, payload=payload)
        result = payload.get("result")
        return result if isinstance(result, dict) else {}

    # ------------------------------------------------------------------
    async def account_status(self) -> dict:
        """返回 ``{logged_in, nickname, user_id, ...}``。"""
        path = "/account/restore_login"
        try:
            payload = await self.request("GET", path)
        except XhhError:
            return {"logged_in": False}
        if payload.get("status") != "ok":
            return {"logged_in": False, "status": payload.get("status")}
        result = payload.get("result") or {}
        profile = result.get("profile") or {}
        return {
            "logged_in": True,
            "nickname": profile.get("nickname") or "",
            "user_id": str(profile.get("heybox_id") or result.get("heybox_id") or ""),
        }

    async def search_topics(self, keyword: str, limit: int = 10) -> list[dict]:
        """按名称搜社区/话题，返回 ``[{topic_id, name}]``。"""
        path = "/bbs/app/api/post_editor/topic_selection/search"
        payload = await self.request(
            "GET", path,
            params={"q": keyword, "limit": limit, "only_hashtag": 0},
        )
        result = self._unwrap(payload, path)
        topics: list[dict] = []
        # 线上返回的是 search_result，兼容旧字段名。
        items = (
            result.get("search_result")
            or result.get("topics")
            or result.get("list")
            or []
        )
        for item in items:
            if not isinstance(item, dict):
                continue
            tid = item.get("topic_id") or item.get("id")
            name = item.get("name") or item.get("topic_name") or ""
            if tid:
                topics.append({"topic_id": str(tid), "name": str(name)})
        return topics

    async def publish(
        self,
        title: str,
        content: str,
        *,
        mode: str = "dynamic",
        topic_ids: list[str] | tuple[str, ...] = (),
        draft: bool = False,
        desc: str = "",
        view_limit: int = 1,
    ) -> dict:
        """发帖 / 存草稿，返回 ``{link_id, is_draft, raw}``。"""
        form = build_post_form(
            title,
            content,
            mode=mode,
            topic_ids=topic_ids,
            draft=draft,
            desc=desc,
            view_limit=view_limit,
        )
        path = "/bbs/app/api/link/post"
        payload = await self.request("POST", path, form=form, write=True)
        result = self._unwrap(payload, path)
        link_id = str(result.get("link_id") or result.get("linkid") or "")
        if not link_id and not draft:
            # 线上发帖成功后 result 可能是空对象，回查最新帖子兜底。
            link_id = await self._find_recent_link_id(title)
        return {"link_id": link_id, "is_draft": draft, "raw": result}

    async def _find_recent_link_id(self, title: str, limit: int = 5) -> str:
        """按标题回查自己最近的帖子，用于服务端不回 link_id 的情况。"""
        target = (title or "").strip()
        if not target or not self.heybox_id:
            return ""
        path = "/bbs/app/profile/user/link/list_v2"
        try:
            payload = await self.request(
                "GET",
                path,
                params={"userid": self.heybox_id, "offset": 0, "limit": limit},
            )
            result = self._unwrap(payload, path)
        except Exception:
            return ""
        for item in result.get("post_links") or []:
            if not isinstance(item, dict):
                continue
            if str(item.get("title") or "").strip() != target:
                continue
            link_id = item.get("linkid") or item.get("link_id")
            if link_id:
                return str(link_id)
        return ""

    async def comment(self, link_id: int | str, text: str, *,
                      reply_id: int | str = "-1", root_id: int | str = "-1") -> dict:
        """发评论 / 回复（顶层评论 reply_id 与 root_id 都传 ``-1``）。"""
        path = "/bbs/app/comment/create"
        form = {
            "is_cy": "0",
            "link_id": str(link_id),
            "reply_id": str(reply_id),
            "root_id": str(root_id),
            "text": text,
        }
        payload = await self.request("POST", path, form=form, write=True)
        return self._unwrap(payload, path)
