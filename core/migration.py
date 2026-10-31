"""将旧版发帖配置一次性迁入多账号存储，失败时保留原配置。"""

from copy import deepcopy
from http.cookies import SimpleCookie
from pathlib import Path

from ..xiaoheihe.config_service import DEFAULT_CONFIG
from ..xiaoheihe.models import Credentials
from ..xiaoheihe.parsers import validate_credentials
from ..xiaoheihe.security import atomic_write_json


LEGACY_CREDENTIAL_KEYS = ("cookie", "user_pkey", "x_xhh_tokenid", "heybox_id")


def migrate_settings(config, data_dir: Path) -> None:
    marker = data_dir / "settings_migrated.json"
    if marker.exists():
        return
    previous = deepcopy(dict(config))
    publishing = deepcopy(config.get("publishing") or {})
    for key, default in DEFAULT_CONFIG["publishing"].items():
        if key in config and (key not in publishing or publishing[key] == default):
            publishing[key] = deepcopy(config[key])
    if isinstance(publishing.get("outbox_purge_days"), int):
        publishing["outbox_purge_days"] = max(0, publishing["outbox_purge_days"])
    save = getattr(config, "save_config", None)
    if not callable(save):
        return
    config["publishing"] = publishing
    try:
        save()
        atomic_write_json(marker, {"version": 1})
    except BaseException:
        config.clear()
        config.update(previous)
        try:
            save()
        except Exception:
            pass
        raise


def migrate_credentials(config, store, data_dir: Path) -> str:
    marker = data_dir / "integration_migrated.json"
    if marker.exists():
        return ""
    legacy = {key: config.get(key, "") for key in LEGACY_CREDENTIAL_KEYS}
    imported = False
    if any(legacy.values()):
        cookie = str(legacy["cookie"] or "").strip()
        if not cookie:
            cookie = f"user_pkey={legacy['user_pkey']}; x_xhh_tokenid={legacy['x_xhh_tokenid']}"
        try:
            parsed = SimpleCookie(cookie)
            cookies = {key: item.value for key, item in parsed.items()}
        except Exception:
            return "旧版 Cookie 无法解析；原配置已保留，请在统一账号面板登录"
        uid = str(legacy["heybox_id"] or "").strip()
        existing = store.load("default")
        if existing is None and (not uid.isascii() or not uid.isdigit()
                                 or not cookies.get("user_pkey") or not cookies.get("x_xhh_tokenid")):
            return "旧版凭据或账号 ID 不完整；原配置已保留，请在 default 档案扫码后重启以完成迁移"
        if existing is None:
            credentials = Credentials(profile_id="default", uid=uid, nickname="", cookies=cookies)
            try:
                validate_credentials(credentials, profile_id="default")
            except ValueError:
                return "旧版凭据的账号身份或格式无效；原配置已保留，请在统一账号面板登录"
        atomic_write_json(data_dir / "legacy_login_backup.json", legacy)
        if existing is None:
            store.save(credentials)
            imported = True
    previous = deepcopy(dict(config))
    try:
        for key in LEGACY_CREDENTIAL_KEYS:
            config[key] = ""
        save = getattr(config, "save_config", None)
        if callable(save):
            save()
        elif any(legacy.values()):
            raise ValueError("宿主不支持保存迁移后的配置")
        # 持久标记阻止登出后旧配置复活，标记中不含凭据。
        atomic_write_json(marker, {"version": 1})
    except BaseException:
        config.clear()
        config.update(previous)
        if imported:
            store.delete("default")
        if callable(getattr(config, "save_config", None)):
            config.save_config()
        raise
    return ""
