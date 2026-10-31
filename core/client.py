"""小黑盒业务 API：账号、话题、帖子、评论与发送结果对账。"""

from __future__ import annotations

import json
import time
from typing import Any

# 保留原模块的公开导入路径，避免调用方随内部拆分迁移。
from .post import (
    LINK_TAG_ARTICLE,
    LINK_TAG_DYNAMIC,
    account_key,
    build_post_form,
    content_fingerprint,
)
from .transport import (
    API_BASE,
    BASE_PARAMS,
    UA,
    CaptchaRequired,
    NetworkError,
    RateLimited,
    XhhError,
    XhhTransport,
)


class XhhClient(XhhTransport):
    """一个账号一个实例；发帖登记和对账共用同一个 outbox。"""

    def __init__(self, cookie: str, heybox_id: str | int = "",
                 timeout: float = 20.0, base: str = API_BASE,
                 outbox: Any = None):
        super().__init__(cookie, heybox_id=heybox_id, timeout=timeout, base=base)
        self.outbox = outbox
        self._account_key = account_key(self.cookie, self.heybox_id)

    async def account_status(self) -> dict:
        """返回 ``{logged_in, nickname, user_id, ...}``。"""
        path = "/account/restore_login"
        try:
            result = self._unwrap(await self.request("GET", path), path)
        except XhhError as exc:
            if exc.status in {"login", "relogin", "expired"}:
                return {"logged_in": False}
            raise NetworkError("登录检查暂时不可用，请稍后重试", path=path) from None
        profile = result.get("profile") or result.get("account_detail") or result
        if not isinstance(profile, dict):
            raise NetworkError("登录检查响应缺少账号信息", path=path)
        uid = profile.get("heybox_id") or profile.get("heyboxid") or profile.get("userid") or result.get("heybox_id")
        if not uid:
            raise NetworkError("登录检查响应缺少账号 ID", path=path)
        return {
            "logged_in": bool(uid),
            "nickname": profile.get("nickname") or "",
            "user_id": str(uid or ""),
        }

    async def search_topics(self, keyword: str, limit: int = 5) -> list[dict]:
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
