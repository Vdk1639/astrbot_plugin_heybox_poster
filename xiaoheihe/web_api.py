from __future__ import annotations

import json
import math
import re
import time
from collections.abc import Callable
from copy import deepcopy
from datetime import UTC, datetime
from typing import Any

from astrbot.api.web import error_response, json_response, request, stream_response

from . import __version__
from .config_service import DEFAULT_CONFIG, ConfigValidationError
from .models import EventState
from .runtime import PLUGIN_NAME, RuntimeServices
from .security import SecurityError, redact_data, redact_text, validate_profile_id
from .topic_service import (
    fetch_topic_catalog,
    fetch_topic_feed,
    normalize_topic_id,
    normalize_topic_ids,
)


class WebApiController:
    def __init__(
        self,
        runtime: RuntimeServices,
        provider_supplier: Callable[[], list[dict[str, str]]] | None = None,
    ) -> None:
        self.runtime = runtime
        self.provider_supplier = provider_supplier or (lambda: [])

    def register(self, context) -> None:
        routes = (
            ("status", self.status, ["GET"], "小黑盒状态总览"),
            (
                "status/provider-cooldown/clear",
                self.clear_provider_cooldown,
                ["POST"],
                "手动结束辅助模型冷却",
            ),
            ("auth/qr", self.auth_qr, ["POST"], "获取小黑盒登录二维码"),
            ("auth/status", self.auth_status, ["GET"], "读取登录状态"),
            ("auth/check", self.auth_check, ["POST"], "检查扫码或凭证"),
            ("auth/logout", self.auth_logout, ["POST"], "安全登出"),
            ("config", self.config_get, ["GET"], "读取插件配置"),
            ("config/schema", self.config_schema, ["GET"], "读取配置界面定义"),
            ("config/save", self.config_save, ["POST"], "保存插件配置"),
            ("config/defaults", self.config_defaults, ["GET"], "读取默认配置"),
            ("events", self.events, ["GET"], "查询事件记录"),
            ("risk/users", self.risk_users, ["GET"], "查询风险档案"),
            ("risk/users/detail", self.risk_user_detail, ["GET"], "读取风险详情与审计"),
            ("risk/users/block", self.risk_user_block, ["POST"], "人工封禁"),
            ("risk/users/unblock", self.risk_user_unblock, ["POST"], "解除人工封禁"),
            ("risk/users/adjust", self.risk_user_adjust, ["POST"], "调整风险分"),
            ("feed/candidates", self.feed_candidates, ["GET"], "查询审核候选"),
            (
                "feed/topics/probe",
                self.feed_topics_probe,
                ["GET"],
                "只读探测小黑盒真实分区",
            ),
            (
                "feed/candidates/<candidate_id>/approve",
                self.feed_approve,
                ["POST"],
                "批准主动回复候选",
            ),
            (
                "feed/candidates/<candidate_id>/reject",
                self.feed_reject,
                ["POST"],
                "拒绝主动回复候选",
            ),
            (
                "feed/candidates/reject-expired",
                self.feed_reject_expired,
                ["POST"],
                "批量拒绝过期候选",
            ),
            ("logs", self.logs, ["GET"], "查询脱敏结构化日志"),
            ("logs/stream", self.logs_stream, ["GET"], "订阅脱敏日志 SSE"),
            ("storage", self.storage, ["GET"], "读取存储状态"),
            ("storage/cleanup-preview", self.cleanup_preview, ["GET"], "预览清理"),
            ("storage/cleanup", self.cleanup_run, ["POST"], "执行安全清理"),
            ("diagnostics", self.diagnostics, ["GET"], "导出脱敏诊断"),
        )
        for suffix, handler, methods, description in routes:
            context.register_web_api(
                f"/{PLUGIN_NAME}/{suffix}",
                handler,
                methods,
                description,
            )

    def _unauthorized(self):
        if not request.username:
            return error_response("需要已认证的 AstrBot Dashboard 会话", status_code=401)
        return None

    async def status(self):
        if response := self._unauthorized():
            return response
        try:
            return json_response(await self.runtime.status())
        except Exception as exc:
            return error_response(f"读取状态失败: {_safe_error(exc)}", status_code=500)

    async def clear_provider_cooldown(self):
        if response := self._unauthorized():
            return response
        payload = await request.json(default={})
        if not isinstance(payload, dict):
            return error_response("冷却操作请求体必须是对象", status_code=400)
        try:
            profile_id = validate_profile_id(str(payload.get("profile_id", "default")))
            purpose = str(payload.get("purpose", "")).strip()
            provider_id = str(payload.get("provider_id", "")).strip()
            if purpose not in {"context", "image"}:
                raise ValueError("purpose 必须是 context 或 image")
            if not provider_id or len(provider_id) > 256:
                raise ValueError("provider_id 无效")
            cleared = self.runtime.clear_auxiliary_provider_cooldown(
                profile_id,
                purpose,
                provider_id,
            )
            return json_response({"cleared": cleared})
        except (SecurityError, ValueError) as exc:
            return error_response(str(exc), status_code=400)

    async def auth_qr(self):
        if response := self._unauthorized():
            return response
        payload = await request.json(default={})
        if not isinstance(payload, dict):
            return error_response("登录请求体必须是对象", status_code=400)
        try:
            profile_id = validate_profile_id(str(payload.get("profile_id", "default")))
            self.runtime.config.profile(profile_id)
            await self.runtime.ensure_started()
            result = await self.runtime.auth.request_qr(profile_id)
            await self.runtime.notify_profile_changed(
                profile_id,
                preserve_anonymous=True,
            )
            return json_response(result)
        except (SecurityError, ConfigValidationError, ValueError) as exc:
            return error_response(str(exc), status_code=400)
        except Exception as exc:
            return error_response(f"获取二维码失败: {_safe_error(exc)}", status_code=502)

    async def auth_status(self):
        if response := self._unauthorized():
            return response
        try:
            profile_id = validate_profile_id(str(request.query.get("profile_id", "default")))
            await self.runtime.ensure_started()
            return json_response(await self.runtime.auth.status(profile_id))
        except (SecurityError, ValueError) as exc:
            return error_response(str(exc), status_code=400)

    async def auth_check(self):
        if response := self._unauthorized():
            return response
        payload = await request.json(default={})
        if not isinstance(payload, dict):
            return error_response("登录请求体必须是对象", status_code=400)
        try:
            profile_id = validate_profile_id(str(payload.get("profile_id", "default")))
            await self.runtime.ensure_started()
            result = await self.runtime.auth.check(profile_id)
            if result.get("state") == "success":
                await self.runtime.notify_profile_changed(profile_id)
            return json_response(result)
        except (SecurityError, ValueError) as exc:
            return error_response(str(exc), status_code=400)
        except Exception as exc:
            return error_response(f"登录检查失败: {_safe_error(exc)}", status_code=502)

    async def auth_logout(self):
        if response := self._unauthorized():
            return response
        payload = await request.json(default={})
        if not isinstance(payload, dict):
            return error_response("登出请求体必须是对象", status_code=400)
        if payload.get("confirm") is not True:
            return error_response("登出需要 confirm=true", status_code=400)
        try:
            profile_id = validate_profile_id(str(payload.get("profile_id", "default")))
            await self.runtime.ensure_started()
            result = await self.runtime.auth.logout(profile_id)
            await self.runtime.notify_profile_changed(profile_id)
            return json_response(result)
        except (SecurityError, ValueError) as exc:
            return error_response(str(exc), status_code=400)

    async def config_get(self):
        if response := self._unauthorized():
            return response
        return json_response(self.runtime.config.snapshot())

    async def config_defaults(self):
        if response := self._unauthorized():
            return response
        return json_response(self.runtime.config.defaults())

    async def config_schema(self):
        if response := self._unauthorized():
            return response
        try:
            schema = deepcopy(self.runtime.config.ui_schema())
            providers = self._provider_options()
            provider_items = schema.get("providers", {}).get("items", {})
            for field_name in ("llm_provider_id", "image_provider_id", "context_provider_id"):
                field = provider_items.get(field_name)
                if isinstance(field, dict):
                    field["options"] = providers
            return json_response(schema)
        except ConfigValidationError as exc:
            return error_response(str(exc), status_code=500)

    async def config_save(self):
        if response := self._unauthorized():
            return response
        payload = await request.json(default={})
        if not isinstance(payload, dict):
            return error_response("配置请求体必须是对象", status_code=400)
        unknown = set(payload) - set(DEFAULT_CONFIG)
        if unknown:
            return error_response(
                f"包含不允许的配置分组: {', '.join(sorted(unknown))}",
                status_code=400,
            )
        try:
            changed = await self.runtime.config.save(payload)
            return json_response({"saved": True, "changed": sorted(changed)})
        except (ConfigValidationError, SecurityError, ValueError) as exc:
            return error_response(f"配置校验失败: {exc}", status_code=400)
        except Exception as exc:
            return error_response(f"配置保存失败: {_safe_error(exc)}", status_code=500)

    async def events(self):
        if response := self._unauthorized():
            return response
        try:
            group_by = request.query.get("group_by", "")
            if group_by not in ("", "uid"):
                raise ValueError
            profile_id = await self._query_profile(request.query.get("profile_id", ""))
            event_id = request.query.get("event_id", "")
            if event_id != "":
                if not isinstance(event_id, str) or not re.fullmatch(r"[0-9]{1,19}", event_id):
                    raise ValueError
                event_id = _positive_int(event_id, "event_id")
                if event_id > 2**63 - 1:
                    raise ValueError
            else:
                event_id = None
            start_time = _optional_float(request.query.get("start_time"))
            end_time = _optional_float(request.query.get("end_time"))
            if start_time is not None and end_time is not None and start_time > end_time:
                raise ValueError
            status = str(request.query.get("status", ""))
            if status and status not in {state.value for state in EventState}:
                raise ValueError
            page, page_size = _query_page()
            filters = {}
            if group_by or profile_id or event_id is not None:
                filters = {"group_by": group_by, "profile_id": profile_id, "event_id": event_id}
            if group_by == "uid":
                filters["risk_config"] = self._risk_config()
        except (TypeError, ValueError):
            return error_response("事件查询参数无效", status_code=400)
        except Exception:
            return error_response("事件查询失败", status_code=500)
        try:
            result = await self.runtime.repository.list_events(
                status=status,
                uid=str(request.query.get("uid", "")),
                post_id=str(request.query.get("post_id", "")),
                keyword=str(request.query.get("keyword", "")),
                start_time=start_time,
                end_time=end_time,
                page=page,
                page_size=page_size,
                **filters,
            )
            return json_response(redact_data(result))
        except Exception:
            return error_response("事件查询失败", status_code=500)

    async def _query_profile(self, value: Any, *, required: bool = False) -> str:
        if value == "" and not required:
            return ""
        if not isinstance(value, str) or not value or value != value.strip():
            raise ValueError
        profile_id = validate_profile_id(value)
        try:
            self.runtime.config.profile(profile_id)
        except ConfigValidationError:
            if not await self.runtime.repository.has_profile_events(profile_id):
                raise ValueError from None
        return profile_id

    def _risk_config(self) -> dict[str, Any]:
        config = self.runtime.config.snapshot()
        return {
            **config["risk"],
            "owner_uids": {
                profile["profile_id"]: profile.get("owner_uid", "")
                for profile in config["profiles"]
            },
        }

    async def risk_users(self):
        if response := self._unauthorized():
            return response
        try:
            profile_id = await self._query_profile(request.query.get("profile_id", ""))
            uid = request.query.get("uid", "")
            if uid != "":
                uid = _risk_uid(uid)
            level = request.query.get("level", "")
            if level not in ("", "normal", "watch", "blocked"):
                raise ValueError
            manual_blocked = request.query.get("manual_blocked", "")
            if manual_blocked not in ("", "true", "false"):
                raise ValueError
            manual_blocked = None if manual_blocked == "" else manual_blocked == "true"
            page, page_size = _query_page()
        except (TypeError, ValueError):
            return error_response("风险查询参数无效", status_code=400)
        except Exception:
            return error_response("风险查询失败", status_code=500)
        try:
            result = await self.runtime.repository.list_risk_users(
                profile_id=profile_id, uid=uid, level=level, manual_blocked=manual_blocked,
                page=page, page_size=page_size,
                risk_config=self._risk_config(),
            )
            return json_response(redact_data(result))
        except Exception:
            return error_response("风险查询失败", status_code=500)

    async def risk_user_detail(self):
        if response := self._unauthorized():
            return response
        try:
            profile_id = await self._query_profile(
                request.query.get("profile_id", ""), required=True,
            )
            uid = _risk_uid(request.query.get("uid", ""))
            page, page_size = _query_page()
        except (TypeError, ValueError):
            return error_response("风险详情参数无效", status_code=400)
        except Exception:
            return error_response("风险详情读取失败", status_code=500)
        try:
            result = await self.runtime.repository.risk_user_detail(
                profile_id, uid, page=page, page_size=page_size,
                risk_config=self._risk_config(),
            )
            return json_response(redact_data(result))
        except Exception:
            return error_response("风险详情读取失败", status_code=500)

    async def risk_user_block(self):
        return await self._manage_risk_user("block")

    async def risk_user_unblock(self):
        return await self._manage_risk_user("unblock")

    async def risk_user_adjust(self):
        return await self._manage_risk_user("adjust")

    async def _manage_risk_user(self, action: str):
        if response := self._unauthorized():
            return response
        try:
            payload = await request.json(default={})
            if not isinstance(payload, dict) or payload.get("confirm") is not True:
                raise ValueError
            profile_id = payload.get("profile_id", "")
            if not isinstance(profile_id, str) or not profile_id or profile_id != profile_id.strip():
                raise ValueError
            profile_id = validate_profile_id(profile_id)
            self.runtime.config.profile(profile_id)
            uid = _risk_uid(payload.get("uid", ""))
            note = payload.get("note")
            if not isinstance(note, str) or len(note) > 200:
                raise ValueError
            reset_score = payload.get("reset_score", False)
            if type(reset_score) is not bool or (action != "unblock" and reset_score):
                raise ValueError
            delta = payload.get("delta", 0)
            if type(delta) is not int or not -1000 <= delta <= 1000:
                raise ValueError
            if action == "adjust" and "delta" not in payload:
                raise ValueError
        except (TypeError, ValueError):
            return error_response("风险操作参数无效", status_code=400)
        except Exception:
            return error_response("风险操作失败，请刷新查看结果，勿重复提交", status_code=500)
        try:
            result = await self.runtime.repository.manage_risk_user(
                profile_id, uid, action=action, note=redact_text(note.strip())[:200],
                operator=request.username, delta=delta, reset_score=reset_score,
                risk_config=self._risk_config(),
            )
            return json_response(redact_data(result))
        except Exception:
            return error_response("风险操作失败，请刷新查看结果，勿重复提交", status_code=500)

    async def feed_candidates(self):
        if response := self._unauthorized():
            return response
        try:
            items = await self.runtime.repository.list_feed_candidates(
                status=str(request.query.get("status", "pending")),
                limit=int(request.query.get("limit", 100, type=int)),
            )
            return json_response({"items": items})
        except ValueError as exc:
            return error_response(str(exc), status_code=400)

    async def feed_topics_probe(self):
        """Discover Xiaoheihe's real topic catalog and verify one GET-only topic feed."""

        if response := self._unauthorized():
            return response
        try:
            profile_id = validate_profile_id(str(request.query.get("profile_id", "default")))
            self.runtime.config.profile(profile_id)
            requested_topic = str(request.query.get("topic_id", "")).strip()
            if requested_topic:
                requested_topic = normalize_topic_id(requested_topic)
            await self.runtime.ensure_started()
            client = await self.runtime.get_client(profile_id)
            topics = await fetch_topic_catalog(client)
            configured_ids = normalize_topic_ids(
                self.runtime.config.snapshot()["proactive_feed"].get("topic_ids", [])
            )

            topic_by_id = {item["id"]: item for item in topics}
            candidate_ids: list[str] = []
            candidate_topics = [
                requested_topic,
                *configured_ids,
                *(item["id"] for item in topics[:5]),
            ]
            for topic_id in candidate_topics:
                if topic_id and topic_id in topic_by_id and topic_id not in candidate_ids:
                    candidate_ids.append(topic_id)

            verified_topic_id = ""
            sample_posts: list[dict[str, Any]] = []
            feed_error = ""
            for topic_id in candidate_ids[:5]:
                try:
                    page = await fetch_topic_feed(client, topic_id, limit=3)
                except Exception as exc:
                    feed_error = _safe_error(exc)
                    continue
                verified_topic_id = topic_id
                sample_posts = [
                    {
                        "post_id": str(
                            post.get("link_id", post.get("post_id", post.get("id", "")))
                        ),
                        "title": str(post.get("title", ""))[:200],
                        "created_at": post.get("created_at", 0),
                    }
                    for post in page.items[:3]
                ]
                feed_error = ""
                break

            return json_response(
                {
                    "read_only": True,
                    "profile_id": profile_id,
                    "topic_count": len(topics),
                    "topics": topics,
                    "configured_topic_ids": configured_ids,
                    "feed_verified": bool(verified_topic_id),
                    "verified_topic_id": verified_topic_id,
                    "sample_posts": sample_posts,
                    "feed_error": feed_error,
                }
            )
        except (SecurityError, ConfigValidationError, ValueError) as exc:
            return error_response(str(exc), status_code=400)
        except Exception as exc:
            return error_response(f"真实分区只读探测失败: {_safe_error(exc)}", status_code=502)

    async def feed_approve(self, candidate_id: str):
        if response := self._unauthorized():
            return response
        payload = await request.json(default={})
        if not isinstance(payload, dict):
            return error_response("审核请求体必须是对象", status_code=400)
        try:
            numeric_id = _positive_int(candidate_id, "candidate_id")
            candidate = await self.runtime.repository.feed_candidate(numeric_id)
            if not candidate:
                return error_response("候选不存在", status_code=404)
            profile_id = validate_profile_id(str(candidate["profile_id"]))
            client = await self.runtime.get_client(profile_id)
            feed = self.runtime.feed_service(profile_id, client, self._unused_synthetic_dispatch)
            result = await feed.approve(
                numeric_id,
                str(payload.get("edited_text", "")).strip() or None,
            )
            return json_response({"approved": True, "result": result})
        except (SecurityError, ValueError) as exc:
            return error_response(str(exc), status_code=400)
        except Exception as exc:
            return error_response(f"批准候选失败: {_safe_error(exc)}", status_code=502)

    async def feed_reject(self, candidate_id: str):
        if response := self._unauthorized():
            return response
        try:
            numeric_id = _positive_int(candidate_id, "candidate_id")
            changed = await self.runtime.repository.reject_feed_candidate(numeric_id)
            if not changed:
                return error_response("候选不存在或已处理", status_code=409)
            return json_response({"rejected": True})
        except ValueError as exc:
            return error_response(str(exc), status_code=400)

    async def feed_reject_expired(self):
        if response := self._unauthorized():
            return response
        payload = await request.json(default={})
        if not isinstance(payload, dict):
            return error_response("批量审核请求体必须是对象", status_code=400)
        try:
            hours = int(payload.get("older_than_hours", 72))
        except (TypeError, ValueError):
            return error_response("older_than_hours 必须是整数", status_code=400)
        if not 1 <= hours <= 24 * 365:
            return error_response("older_than_hours 超出范围", status_code=400)
        count = 0
        candidates = await self.runtime.repository.list_feed_candidates(status="pending", limit=500)
        cutoff = time.time() - hours * 3600
        for candidate in candidates:
            if float(candidate["created_at"]) < cutoff:
                changed = await self.runtime.repository.review_feed_candidate(
                    int(candidate["id"]), "expired"
                )
                count += int(changed)
        return json_response({"rejected": count})

    async def logs(self):
        if response := self._unauthorized():
            return response
        items = self.runtime.logging.list(
            level=str(request.query.get("level", "")),
            keyword=str(request.query.get("keyword", "")),
            limit=int(request.query.get("limit", 200, type=int)),
        )
        return json_response({"items": items})

    async def logs_stream(self):
        if response := self._unauthorized():
            return response

        async def stream():
            async for event in self.runtime.tasks.sse_events(max_queue=100):
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"

        return stream_response(stream())

    async def storage(self):
        if response := self._unauthorized():
            return response
        await self.runtime.ensure_started()
        return json_response(
            {
                "database_size": self.runtime.repository.database_size(),
                "log_size": self.runtime.logging.total_size(),
                "counts": await self.runtime.repository.table_counts(),
                "last_cleanup_at": self.runtime.last_cleanup_at,
            }
        )

    async def cleanup_preview(self):
        if response := self._unauthorized():
            return response
        await self.runtime.ensure_started()
        preview = await self.runtime.repository.cleanup_preview(
            self.runtime.config.snapshot()["retention"]
        )
        return json_response({"preview": preview})

    async def cleanup_run(self):
        if response := self._unauthorized():
            return response
        payload = await request.json(default={})
        if not isinstance(payload, dict):
            return error_response("清理请求体必须是对象", status_code=400)
        if payload.get("confirm") is not True:
            return error_response("必须明确传入 confirm=true", status_code=400)
        await self.runtime.ensure_started()
        retention = self.runtime.config.snapshot()["retention"]
        removed = await self.runtime.repository.cleanup(retention)
        pruned = await self.runtime.repository.prune_event_bodies(retention)
        pressure = await self.runtime.repository.enforce_soft_limit(retention)
        self.runtime.last_cleanup_at = datetime.now(UTC).isoformat()
        return json_response(
            {
                "removed": removed,
                "pruned_bodies": pruned,
                "storage_pressure": pressure,
            }
        )

    async def diagnostics(self):
        if response := self._unauthorized():
            return response
        await self.runtime.ensure_started()
        payload = {
            "generated_at": datetime.now(UTC).isoformat(),
            "plugin": {"name": PLUGIN_NAME, "version": f"v{__version__}"},
            "status": await self.runtime.status(),
            "storage": await self.runtime.repository.diagnostic_snapshot(),
            "logs": self.runtime.logging.list(limit=100),
            "config": redact_data(self.runtime.config.snapshot()),
        }
        return json_response(payload)

    async def _unused_synthetic_dispatch(self, notification, metadata: dict[str, Any]) -> None:
        raise RuntimeError(f"审核发送不应创建合成事件: {notification.post_id} {metadata!r}")

    def _provider_options(self) -> list[dict[str, str]]:
        options = [{"value": "", "label": "跟随当前配置"}]
        try:
            configured = self.provider_supplier()
        except Exception:
            configured = []
        seen = {""}
        for item in configured:
            if not isinstance(item, dict):
                continue
            value = str(item.get("value", "")).strip()
            if not value or value in seen:
                continue
            seen.add(value)
            label = str(item.get("label", value)).strip() or value
            options.append({"value": value, "label": label})
        return options


def _positive_int(value: str, name: str) -> int:
    candidate = int(value)
    if candidate <= 0:
        raise ValueError(f"{name} 必须是正整数")
    return candidate


def _optional_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    candidate = float(value)
    if isinstance(value, bool) or not math.isfinite(candidate):
        raise ValueError
    return candidate


def _query_page() -> tuple[int, int]:
    return (
        max(1, int(request.query.get("page", 1, type=int))),
        max(1, min(100, int(request.query.get("page_size", 30, type=int)))),
    )


def _risk_uid(value: Any) -> str:
    if type(value) is int:
        value = str(value)
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{1,32}", value) or int(value) <= 0:
        raise ValueError
    return value


def _safe_error(error: BaseException) -> str:
    return redact_text(str(error))[:1000]
