"""帖子表单与账号范围的去重标识。"""

from __future__ import annotations

import hashlib
import json
from http.cookies import SimpleCookie

LINK_TAG_DYNAMIC = 27
LINK_TAG_ARTICLE = 11


def content_fingerprint(account: str, form: dict[str, str]) -> str:
    """按账号和实际提交表单去重，区分草稿、话题及可见范围。"""
    material = json.dumps([account, form], sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def account_key(cookie: str, heybox_id: str) -> str:
    """同一账号的发帖记录与持久限流共用标识。"""
    if heybox_id:
        return "id:" + heybox_id
    pkey = SimpleCookie(cookie).get("user_pkey")
    # shortcut: 未配置账号 ID 时依赖登录凭据，需跨重新登录去重时配置 heybox_id。
    return "session:" + hashlib.sha256((pkey.value if pkey else cookie).encode("utf-8")).hexdigest()


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
