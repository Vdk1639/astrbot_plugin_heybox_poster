"""统一管理面板：复用账号、来源及状态 API，增加发布与审核。"""

import asyncio

from astrbot.api.web import error_response, json_response, request

from ..xiaoheihe.runtime import PLUGIN_NAME
from ..xiaoheihe.web_api import WebApiController


class Dashboard(WebApiController):
    def register(self, context) -> None:
        super().register(context)
        routes = (
            ("status", self.publishing_status, ["GET"]),
            ("reviews", self.reviews, ["GET"]),
            ("submit", self.submit, ["POST"]),
            ("reviews/<review_id>/approve", self.approve, ["POST"]),
            ("reviews/<review_id>/reject", self.reject, ["POST"]),
        )
        for suffix, handler, methods in routes:
            context.register_web_api(f"/{PLUGIN_NAME}/publishing/{suffix}", handler, methods, "发布与审核")

    @property
    def publisher(self):
        return self.runtime.publisher

    async def publishing_status(self):
        if response := self._unauthorized():
            return response
        try:
            return json_response(self.publisher.status(str(request.query.get("profile_id", ""))))
        except ValueError as exc:
            return error_response(str(exc), status_code=400)

    async def reviews(self):
        if response := self._unauthorized():
            return response
        try:
            return json_response({"items": self.publisher.queue.list(
                kind=str(request.query.get("kind", "")), status=str(request.query.get("status", "")),
                limit=min(500, max(1, int(request.query.get("limit", 100)))),
            )})
        except ValueError as exc:
            return error_response(str(exc), status_code=400)

    async def submit(self):
        if response := self._unauthorized():
            return response
        body = await request.json(default={})
        if not isinstance(body, dict):
            return error_response("请求体必须是对象", status_code=400)
        try:
            result = await self.publisher.submit(
                body.get("kind"), body.get("profile_id", ""), body.get("payload"),
            )
            return json_response(result)
        except (ValueError, TypeError) as exc:
            return error_response(str(exc), status_code=400)
        except Exception:
            return error_response("提交失败，请检查服务状态", status_code=500)

    async def approve(self, review_id):
        if response := self._unauthorized():
            return response
        body = await request.json(default={})
        if not isinstance(body, dict) or body.get("confirm") is not True:
            return error_response("批准发布需要 confirm=true", status_code=400)
        try:
            return json_response(await self.publisher.approve(int(review_id)))
        except (ValueError, TypeError) as exc:
            return error_response(str(exc), status_code=400)
        except Exception:
            return error_response("审核操作失败，请刷新查看结果，勿重复提交", status_code=500)

    async def reject(self, review_id):
        if response := self._unauthorized():
            return response
        try:
            return json_response({"review": await self.publisher.reject(int(review_id))})
        except (ValueError, TypeError) as exc:
            return error_response(str(exc), status_code=400)

    async def feed_approve(self, candidate_id):
        """历史候选缺少账号绑定，先转入统一队列让管理员再次核对。"""
        if response := self._unauthorized():
            return response
        body = await request.json(default={})
        if not isinstance(body, dict) or body.get("confirm") is not True:
            return error_response("转入统一审核需要 confirm=true", status_code=400)
        try:
            candidate_id = int(candidate_id)
            await self.runtime.ensure_started()
            lock = self.runtime._feed_approval_locks.setdefault("legacy-review", asyncio.Lock())
            async with lock:
                candidate = await self.runtime.repository.feed_candidate(candidate_id)
                if not candidate or candidate["status"] != "pending":
                    raise ValueError("历史候选不存在或已处理")
                text = body.get("edited_text") or candidate["generated_text"]
                result = await self.publisher.submit(
                    "comment", candidate["profile_id"],
                    dict(post_id=candidate["post_id"], text=text),
                    source="legacy_candidate", source_id=f"candidate:{candidate_id}", force_review=True,
                    metadata={"event_id": candidate.get("incoming_event_id"), "proactive": True},
                )
                if result.get("review") is None:
                    raise ValueError("请先为历史候选的档案登录账号")
                await self.runtime.repository.review_feed_candidate(candidate_id, "approved", text)
                return json_response({"approved": True, "result": "queued", "review": result["review"]})
        except (ValueError, TypeError) as exc:
            return error_response(str(exc), status_code=400)
