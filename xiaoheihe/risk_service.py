"""Fixed local signals; never use community content as authority."""
from __future__ import annotations

import hashlib
import re
import unicodedata
from typing import Any

RULE_VERSION = '1'


def content_facts(content: str) -> tuple[str, bool]:
    normalized = ' '.join(unicodedata.normalize('NFKC', content[:4000]).casefold().split())
    return (hashlib.sha256(normalized.encode()).hexdigest() if normalized else '', len(content) <= 4000)


def text_hits(content: str) -> list[str]:
    if not content or len(content) > 4000:
        return []
    text = ' '.join(unicodedata.normalize('NFKC', content).casefold().split())
    # Ambiguous quotations and negations are observation only in this first version.
    if any(word in text for word in ('不要', '不能', '不应', '请勿', '引用', '例如', '示例', '讨论', '解释',
                                      'do not', "don't", 'never ', 'example', 'quote', 'discuss')) or any(c in text for c in ('"', '“', '”', chr(96), '>')):
        return []
    hits = []
    if re.search(r'(忽略|覆盖|无视).{0,24}(之前|先前|以上|原有|所有|系统|开发者).{0,12}(指令|规则|提示)', text) or re.search(r'(ignore|override|disregard).{0,30}(previous|prior|system|developer|all).{0,20}(instructions?|rules?|prompts?)', text) or re.search(r'(我是|以我的).{0,8}(系统|开发者).{0,8}(权限|身份|指令)', text):
        hits.append('inject_override')
    if re.search(r'(给我|告诉我|提供|泄露|输出|发给我|显示).{0,35}(你的|机器人|主人|管理员).{0,20}(密钥|凭据|密码|私密|隐私|非公开|cookie|token|api.key)', text) or re.search(r'(give|show|send|reveal|print|provide).{0,30}(your|bot.s?|owner.s?|admin.s?).{0,25}(secrets?|passwords?|credentials?|private|api.?keys?|tokens?|cookies?)', text):
        hits.append('privacy_exfiltration')
    return hits


def risk_snapshot(profile_id: str, uid: str, row, config: dict[str, Any] | None = None, *, owner_uid: str = '') -> dict[str, Any]:
    config = config or {}
    result = dict(row) if row else {'profile_id': profile_id, 'sender_uid': uid, 'score': 0, 'manual_blocked': False, 'note': '', 'last_hit_at': None}
    score = int(result['score'])
    owner_uid = owner_uid or config.get('owner_uids', {}).get(profile_id, '')
    exempt = bool(uid and owner_uid and uid == owner_uid and config.get('owner_exempt', True))
    threshold = config.get('auto_filter_score', 60)
    result.update(has_record=row is not None, manual_blocked=bool(result['manual_blocked']),
                  risk_score=score, risk_level='blocked' if score >= threshold else 'watch' if score >= config.get('watch_score', 20) else 'normal',
                  auto_filtered=bool(config.get('enabled', True) and config.get('auto_filter_enabled', False) and score >= threshold and not exempt),
                  owner_exempt=exempt, enabled=config.get('enabled', True), auto_filter_enabled=config.get('auto_filter_enabled', False))
    result['reason'] = 'risk_manual_blocked' if result['manual_blocked'] else 'risk_auto_filtered' if result['auto_filtered'] else ''
    return result
