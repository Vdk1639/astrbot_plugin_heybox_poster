"""小黑盒 Web API 的最小异步客户端（发帖向）。

零第三方依赖：只用标准库 ``urllib``，阻塞调用丢进线程池，因此：

* 插件不需要额外 pip 包，AstrBot 里开箱即用；
* 不会和 AstrBot 自带的 httpx / aiohttp 版本打架。

统一处理公共参数、``hkey`` 签名、写接口 ``_rnd``、表单编码与业务错误。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from http.client import HTTPException
from http.cookies import SimpleCookie
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


class NetworkError(XhhError):
    """网络或响应异常，写操作结果未知，需要核实。"""


def content_fingerprint(account: str, form: dict[str, str]) -> str:
    """按账号和实际提交表单去重，区分草稿、话题及可见范围。"""
    material = json.dumps([account, form], sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


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
                 timeout: float = 20.0, base: str = API_BASE,
                 outbox: Any = None):
        self.cookie = (cookie or "").strip()
        self.heybox_id = str(heybox_id or "").strip()
        self.timeout = float(timeout)
        self.base = base.rstrip("/")
        self.outbox = outbox
        pkey = SimpleCookie(self.cookie).get("user_pkey")
        # shortcut: 未配置账号 ID 时依赖登录凭据，需跨重新登录去重时配置 heybox_id。
        self._account_key = (
            "id:" + self.heybox_id if self.heybox_id else
            "session:" + hashlib.sha256((pkey.value if pkey else self.cookie).encode("utf-8")).hexdigest()
        )

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
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    code, raw = resp.status, resp.read()
            except urllib.error.HTTPError as exc:
                code, raw = exc.code, exc.read()
        except (urllib.error.URLError, TimeoutError, OSError, HTTPException) as exc:
            raise NetworkError(f"请求失败：{type(exc).__name__}: {exc}") from exc

        text = raw.decode("utf-8", "replace")
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise NetworkError(f"响应不是 JSON（HTTP {code}），无法确认结果") from exc
        if not isinstance(payload, dict):
            raise NetworkError(f"响应结构异常（HTTP {code}），无法确认结果")
        if code >= 500:
            raise NetworkError(f"服务端错误 HTTP {code}，无法确认结果", payload=payload)
        if code >= 400:
            self._unwrap(payload, urllib.parse.urlsplit(url).path)
            raise XhhError(f"请求被拒绝 HTTP {code}", payload=payload)
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
        if not isinstance(payload, dict) or not isinstance(payload.get("status"), str):
            raise NetworkError("响应缺少有效业务状态，无法确认结果", path=path)
        status = payload.get("status")
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
            raise NetworkError(f"未知业务状态：{status}，无法确认结果",
                               status=status, path=path, payload=payload)
        result = payload.get("result", {})
        if not isinstance(result, dict):
            raise NetworkError("响应 result 结构异常，无法确认结果", path=path)
        return result

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
        if limit > 0:
            topics = topics[:limit]
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
        fingerprint = content_fingerprint(self._account_key, form)
        posted_at = time.time()
        baseline_ids: set[str] = set()
        if not draft:
            try:
                for item in await self._my_recent_links(20):
                    lid = item.get("linkid") or item.get("link_id")
                    if lid:
                        baseline_ids.add(str(lid))
            except Exception:
                baseline_ids = set()
        if self.outbox is not None:
            dedup = self.outbox.mark_pending(
                fingerprint,
                {
                    "account": self._account_key,
                    "form": form,
                    "title": title,
                    "content": content,
                    "baseline_ids": sorted(baseline_ids),
                    "posted_at": posted_at,
                },
                legacy_fingerprint=hashlib.sha1(
                    f"{title.strip()}\n{content.strip()}".encode("utf-8")
                ).hexdigest(),
            )
            if dedup.get("blocked"):
                raise XhhError("相同请求仍在发送或结果待对账，请先核实，勿重复发送。")
            if dedup["duplicate"]:
                return {
                    "link_id": dedup["link_id"],
                    "is_draft": draft,
                    "raw": {},
                    "fingerprint": fingerprint,
                    "duplicate": True,
                }
        path = "/bbs/app/api/link/post"
        try:
            payload = await self.request("POST", path, form=form, write=True)
            result = self._unwrap(payload, path)
        except BaseException as exc:
            if self.outbox is not None:
                # 仅明确业务拒绝可重试；取消或未知异常都保留待对账。
                if isinstance(exc, XhhError) and not isinstance(exc, NetworkError):
                    self.outbox.mark_failed(fingerprint)
                else:
                    self.outbox.mark_unknown(fingerprint)
            raise
        link_id = str(result.get("link_id") or result.get("linkid") or "")
        if not link_id and not draft:
            # 先保存未知状态，避免回查途中取消任务后留下不可恢复的记录。
            if self.outbox is not None:
                self.outbox.mark_unknown(fingerprint)
            link_id = await self._reconcile_link_id(title, baseline_ids, posted_at)
        if self.outbox is not None:
            if link_id:
                self.outbox.mark_sent(fingerprint, link_id)
            else:
                self.outbox.mark_unknown(fingerprint)
        return {
            "link_id": link_id,
            "is_draft": draft,
            "raw": result,
            "fingerprint": fingerprint,
            "duplicate": False,
        }

    async def reconcile_outbox(self) -> int:
        """把未知或中断的发送与「我的帖子列表」对账，命中即认领。"""
        if self.outbox is None:
            return 0
        claimed = 0
        for fingerprint, payload_json in self.outbox.unknown_rows():
            try:
                meta = json.loads(payload_json)
            except Exception:
                continue
            if meta.get("account") not in (None, self._account_key):
                continue
            if (meta.get("form") or {}).get("draft") == "1":
                continue
            link_id = await self._reconcile_link_id(
                str(meta.get("title") or ""),
                set(meta.get("baseline_ids") or []),
                float(meta.get("posted_at") or 0),
            )
            if link_id:
                self.outbox.mark_sent(fingerprint, link_id)
                claimed += 1
        return claimed

    async def delete_link(self, link_id: int | str) -> dict:
        """删除帖子：``POST /bbs/app/link/delete``，表单 ``link_id=<id>``。"""
        path = "/bbs/app/link/delete"
        form = {"link_id": str(link_id)}
        payload = await self.request("POST", path, form=form, write=True)
        result = self._unwrap(payload, path)
        if self.outbox is not None:
            self.outbox.remove_by_link_id(str(link_id))
        return result

    async def _my_recent_links(self, limit: int = 20) -> list[dict]:
        """当前账号最近的帖子条目，供发帖后对账使用。"""
        if not self.heybox_id:
            return []
        path = "/bbs/app/profile/user/link/list_v2"
        payload = await self.request(
            "GET",
            path,
            params={"userid": self.heybox_id, "offset": 0, "limit": limit},
        )
        result = self._unwrap(payload, path)
        items = result.get("post_links") or []
        return [item for item in items if isinstance(item, dict)]

    async def _reconcile_link_id(
        self,
        title: str,
        baseline_ids: set[str],
        posted_at: float,
        limit: int = 20,
    ) -> str:
        """在基线之外的新条目里回查 link_id，并按时间窗口过滤。"""
        target = (title or "").strip()
        if not target or not self.heybox_id:
            return ""
        try:
            items = await self._my_recent_links(limit)
        except Exception:
            return ""
        for item in items:
            lid = item.get("linkid") or item.get("link_id")
            if not lid:
                continue
            lid = str(lid)
            if lid in baseline_ids:
                continue
            if str(item.get("title") or "").strip() != target:
                continue
            ts = item.get("create_time") or item.get("created_at") or item.get("time")
            if ts is not None:
                try:
                    t = float(ts)
                    if t > 10_000_000_000:  # 毫秒时间戳
                        t /= 1000
                    if t < posted_at - 120:
                        continue
                except (TypeError, ValueError):
                    pass
            return lid
        return ""

    async def list_my_posts(self, limit: int = 20) -> list[dict]:
        """只读查询我的帖子列表，返回 ``[{link_id, title}]``。"""
        items = await self._my_recent_links(limit)
        posts: list[dict] = []
        for item in items:
            lid = item.get("linkid") or item.get("link_id")
            if not lid:
                continue
            posts.append({
                "link_id": str(lid),
                "title": str(item.get("title") or "").strip(),
            })
        return posts

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
