from __future__ import annotations

import json
import time
from dataclasses import replace
from unittest.mock import AsyncMock

import httpx
import pytest

from xiaoheihe.api_client import CredentialInvalidError, XiaoheiheApiClient
from xiaoheihe.auth import AuthService, CredentialStore
from xiaoheihe.models import Credentials, LoginState, QRLoginSession
from xiaoheihe.task_manager import TaskManager


def mock_credentials(profile_id="default") -> Credentials:
    return Credentials(
        profile_id=profile_id,
        uid="10001",
        nickname="MockUser",
        cookies={"mock_sid": "redacted-fixture-value"},
        logged_in_at="2026-07-28T00:00:00+00:00",
    )


def test_credential_atomic_save_load_delete(tmp_path) -> None:
    store = CredentialStore(tmp_path)
    store.save(mock_credentials())
    path = store.path_for("default")
    assert path.parent.name == "credentials"
    assert store.load("default").uid == "10001"
    store.delete("default")
    assert not path.exists()


class FakeLoginClient:
    def __init__(self, state=LoginState.SUCCESS) -> None:
        self.state = state

    async def request_qr(self):
        return QRLoginSession(
            profile_id="default",
            request_id="qr-1",
            qr_content="https://example.com/qr?mock=1",
            expires_at=time.time() + 180,
        )

    async def check_qr(self, session):
        return self.state, "state", mock_credentials() if self.state is LoginState.SUCCESS else None

    async def check_credentials(self):
        return {"uid": "10001"}


async def test_auth_qr_success_persists_credentials(tmp_path, repository) -> None:
    store = CredentialStore(tmp_path)
    client = FakeLoginClient()

    async def factory(profile_id, anonymous=False):
        return client

    auth = AuthService(store, repository, factory)
    await repository.update_account_state(
        "default",
        consecutive_401=3,
        consecutive_403=2,
        consecutive_429=1,
        circuit_open_until=time.time() + 600,
    )
    qr = await auth.request_qr("default")
    assert qr["state"] == "waiting_scan"
    assert qr["qr_image"].startswith("data:image/png;base64,")
    result = await auth.check("default")
    assert result["state"] == "success"
    assert "qr_image" not in result
    assert "default" not in auth._sessions
    assert store.load("default").nickname == "MockUser"
    state = await repository.account_state("default")
    assert state["consecutive_401"] == 0
    assert state["circuit_open_until"] == 0


@pytest.mark.parametrize("previous_state", [LoginState.CREDENTIAL_INVALID, LoginState.FAILED])
async def test_manual_reauth_clears_circuit_and_preserves_previous_credentials(
    tmp_path,
    repository,
    previous_state,
) -> None:
    store = CredentialStore(tmp_path)
    store.save(mock_credentials())
    client = FakeLoginClient(LoginState.WAITING_SCAN)

    async def factory(profile_id, anonymous=False):
        return client

    auth = AuthService(store, repository, factory)
    await repository.update_account_state(
        "default",
        status=previous_state.value,
        last_error="relogin",
        consecutive_401=2,
        circuit_open_until=time.time() + 300,
    )

    result = await auth.request_qr("default")

    assert result["state"] == LoginState.WAITING_SCAN.value
    assert store.load("default") == mock_credentials()
    state = await repository.account_state("default")
    assert state["status"] == LoginState.WAITING_SCAN.value
    assert state["last_error"] == ""
    assert state["consecutive_401"] == 0
    assert state["circuit_open_until"] == 0


async def test_auth_qr_expired(tmp_path, repository) -> None:
    store = CredentialStore(tmp_path)

    async def factory(profile_id, anonymous=False):
        return FakeLoginClient()

    auth = AuthService(store, repository, factory)
    auth._sessions["default"] = QRLoginSession(
        profile_id="default",
        request_id="expired",
        qr_content="https://example.com/qr",
        expires_at=time.time() - 1,
    )
    result = await auth.check("default")
    assert result["state"] == "expired"


async def test_logout_removes_only_selected_profile(tmp_path, repository) -> None:
    store = CredentialStore(tmp_path)
    store.save(mock_credentials("default"))
    store.save(mock_credentials("other"))

    async def factory(profile_id, anonymous=False):
        return FakeLoginClient()

    auth = AuthService(store, repository, factory)
    await auth.logout("default")
    assert store.load("default") is None
    assert store.load("other") is not None


async def test_qr_background_poll_is_owned_and_cancelled(tmp_path, repository) -> None:
    store = CredentialStore(tmp_path)
    tasks = TaskManager()
    client = FakeLoginClient(LoginState.WAITING_SCAN)

    async def factory(profile_id, anonymous=False):
        return client

    auth = AuthService(store, repository, factory, task_manager=tasks)
    await auth.request_qr("default")
    assert "xhh-login-default" in tasks.task_names()
    await auth.logout("default")
    assert "xhh-login-default" not in tasks.task_names()
    await tasks.close()


async def test_saved_credentials_are_checked_and_invalidated(tmp_path, repository) -> None:
    store = CredentialStore(tmp_path)
    store.save(mock_credentials())
    client = FakeLoginClient()

    async def factory(profile_id, anonymous=False):
        return client

    auth = AuthService(store, repository, factory)
    assert (await auth.check("default"))["state"] == LoginState.SUCCESS.value

    async def invalid_check():
        raise CredentialInvalidError("expired", category="credential_invalid")

    client.check_credentials = invalid_check
    result = await auth.check("default")
    assert result["state"] == LoginState.CREDENTIAL_INVALID.value


async def test_idle_check_explains_that_no_qr_or_credentials_exist(
    tmp_path,
    repository,
) -> None:
    async def factory(profile_id, anonymous=False):
        return FakeLoginClient()

    auth = AuthService(CredentialStore(tmp_path), repository, factory)
    result = await auth.check("default")
    assert result["state"] == LoginState.IDLE.value
    assert "尚未生成二维码" in result["message"]


async def test_background_login_poll_notifies_runtime(tmp_path, repository, monkeypatch) -> None:
    store = CredentialStore(tmp_path)
    client = FakeLoginClient()
    notified = []

    async def factory(profile_id, anonymous=False):
        return client

    async def on_login(profile_id):
        notified.append(profile_id)

    async def no_sleep(seconds):
        return None

    monkeypatch.setattr("xiaoheihe.auth.asyncio.sleep", no_sleep)
    auth = AuthService(store, repository, factory, on_login=on_login)
    await auth.request_qr("default")
    status = await auth.status("default")
    assert status["qr_image"].startswith("data:image/png;base64,")
    await auth._poll_login("default")
    assert notified == ["default"]


async def test_background_login_poll_recovers_from_transient_check_error(
    tmp_path,
    repository,
    monkeypatch,
) -> None:
    store = CredentialStore(tmp_path)
    client = FakeLoginClient()
    attempts = 0
    notified = []

    async def factory(profile_id, anonymous=False):
        return client

    async def on_login(profile_id):
        notified.append(profile_id)

    async def no_sleep(seconds):
        return None

    auth = AuthService(store, repository, factory, on_login=on_login)
    auth._sessions["default"] = await client.request_qr()

    async def flaky_check(profile_id):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("temporary QR state failure")
        return {"state": LoginState.SUCCESS.value}

    monkeypatch.setattr("xiaoheihe.auth.asyncio.sleep", no_sleep)
    monkeypatch.setattr(auth, "check", flaky_check)

    await auth._poll_login("default")

    assert attempts == 2
    assert notified == ["default"]


def test_replacing_credentials_keeps_previous_backup(tmp_path) -> None:
    store = CredentialStore(tmp_path)
    previous = mock_credentials()
    store.save(previous)
    replacement = replace(previous, uid="20002", cookies={"pkey": "new-session"})
    store.save_replacing(replacement)
    backup = store.path_for("default").with_suffix(".json.bak")
    assert store.load("default") == replacement
    assert json.loads(backup.read_text(encoding="utf-8"))["uid"] == previous.uid
    store.delete("default")
    assert not store.exists("default")
    assert not backup.exists()


@pytest.mark.parametrize("write_before_failure", [False, True])
def test_replacing_credentials_rolls_back_on_save_failure(tmp_path, monkeypatch, write_before_failure):
    store = CredentialStore(tmp_path)
    previous = mock_credentials()
    store.save(previous)
    save = store.save

    def fail(credentials):
        if write_before_failure:
            save(credentials)
        raise OSError("simulated write failure")

    monkeypatch.setattr(store, "save", fail)
    with pytest.raises(OSError, match="simulated"):
        store.save_replacing(replace(previous, uid="20002"))
    assert store.load("default") == previous


def test_failed_first_login_does_not_leave_credentials(tmp_path, monkeypatch):
    store = CredentialStore(tmp_path)
    save = store.save

    def fail(credentials):
        save(credentials)
        raise OSError("simulated write failure")

    monkeypatch.setattr(store, "save", fail)
    with pytest.raises(OSError):
        store.save_replacing(mock_credentials())
    assert not store.exists("default")


def test_backup_failure_never_replaces_previous_credentials(tmp_path, monkeypatch):
    store = CredentialStore(tmp_path)
    previous = mock_credentials()
    store.save(previous)
    original = store.path_for("default").read_bytes()

    def fail(*args):
        raise OSError("backup failed")

    monkeypatch.setattr("xiaoheihe.auth.atomic_write_json", fail)
    with pytest.raises(OSError, match="backup failed"):
        store.save_replacing(replace(previous, uid="20002"))
    assert store.path_for("default").read_bytes() == original


@pytest.mark.parametrize("failure_point", ["reset", "request"])
async def test_qr_request_failure_preserves_credentials(tmp_path, repository, failure_point):
    store = CredentialStore(tmp_path)
    previous = mock_credentials()
    store.save(previous)
    client = FakeLoginClient()

    async def fail():
        raise RuntimeError("private-session-secret")

    async def factory(profile_id, anonymous=False):
        client.request_qr = fail
        return client

    async def reset(profile_id):
        if failure_point == "reset":
            await fail()

    auth = AuthService(store, repository, factory, reset_anonymous=reset)
    auth._sessions["default"] = await client.request_qr()
    with pytest.raises(RuntimeError):
        await auth.request_qr("default")
    assert store.load("default") == previous
    assert "default" not in auth._sessions
    state = await repository.account_state("default")
    assert state["status"] == LoginState.FAILED.value
    assert "private-session-secret" not in state["last_error"]


@pytest.mark.parametrize("updates", [
    {"uid": ""}, {"uid": "0"}, {"uid": "-1"}, {"uid": "１"}, {"profile_id": "other"},
    {"cookies": {}}, {"cookies": {"heybox_id": "20002", "pkey": "fixture"}},
    {"cookies": {"pkey": "unsafe\r\nvalue"}},
    {"cookies": {"device_id": "device", "x_xhh_tokenid": "client-identity"}},
])
async def test_invalid_qr_credentials_never_replace_saved_account(tmp_path, repository, updates):
    store = CredentialStore(tmp_path)
    previous = mock_credentials()
    store.save(previous)
    client = FakeLoginClient()

    async def factory(profile_id, anonymous=False):
        return client

    async def check_qr(session):
        return LoginState.SUCCESS, "", replace(previous, **updates)

    client.check_qr = check_qr
    auth = AuthService(store, repository, factory)
    auth._sessions["default"] = await client.request_qr()
    result = await auth.check("default")
    assert result["state"] == LoginState.FAILED.value
    assert store.load("default") == previous
    assert store.load("other") is None
    assert (await auth.check("default"))["state"] == LoginState.FAILED.value


@pytest.mark.parametrize("state,has_credentials", [(LoginState.SUCCESS, False), (LoginState.WAITING_SCAN, True)])
async def test_only_confirmed_success_with_credentials_can_save(tmp_path, repository, state, has_credentials):
    store = CredentialStore(tmp_path)
    store.save(mock_credentials())
    client = FakeLoginClient()

    async def factory(profile_id, anonymous=False):
        return client

    async def check_qr(session):
        return state, "", replace(mock_credentials(), uid="20002") if has_credentials else None

    client.check_qr = check_qr
    auth = AuthService(store, repository, factory)
    auth._sessions["default"] = await client.request_qr()
    assert (await auth.check("default"))["state"] == LoginState.FAILED.value
    assert store.load("default") == mock_credentials()


async def test_auth_save_failure_does_not_report_success(tmp_path, repository, monkeypatch):
    store = CredentialStore(tmp_path)
    previous = mock_credentials()
    store.save(previous)
    client = FakeLoginClient()

    async def factory(profile_id, anonymous=False):
        return client

    def fail(credentials):
        raise OSError("private-save-detail")

    monkeypatch.setattr(store, "save", fail)
    auth = AuthService(store, repository, factory)
    auth._sessions["default"] = await client.request_qr()
    result = await auth.check("default")
    assert result["state"] == LoginState.FAILED.value
    assert "private-save-detail" not in str(result)
    assert store.load("default") == previous
    assert (await repository.account_state("default"))["status"] == LoginState.FAILED.value


async def test_logout_removes_only_the_selected_profile_backup(tmp_path, repository):
    store = CredentialStore(tmp_path)
    for profile_id in ("default", "other"):
        store.save(mock_credentials(profile_id))
        store.save_replacing(replace(mock_credentials(profile_id), uid="20002"))
    auth = AuthService(store, repository, None)
    await auth.logout("other")
    assert store.exists("default")
    await auth.logout("default")
    assert not list(store.directory.glob("*.bak"))


async def test_fresh_qr_resets_only_anonymous_cookie_jar(tmp_path, repository):
    store = CredentialStore(tmp_path)
    store.save(mock_credentials())
    observed = []
    clients = []
    transports = []
    cached = {}
    authenticated = object()

    def handler(request):
        observed.append((request.url.path, request.headers.get("cookie", "")))
        if request.url.path == "/account/get_qrcode_url/":
            return httpx.Response(200, json={"result": {"qr_url": "https://api.xiaoheihe.cn/account/qr_login/?qr=fixture"}},
                                  headers={"set-cookie": "qr_session=fresh; Path=/"})
        return httpx.Response(200, json={"result": {"error": "wait"}})

    async def factory(profile_id, anonymous=False):
        if not anonymous:
            return authenticated
        if profile_id not in cached:
            transport = httpx.AsyncClient(base_url="https://api.xiaoheihe.cn", transport=httpx.MockTransport(handler))
            transports.append(transport)
            client = XiaoheiheApiClient(profile_id, client=transport, min_request_interval_seconds=0)
            clients.append(client)
            cached[profile_id] = client
        return cached[profile_id]

    async def reset(profile_id):
        client = cached.pop(profile_id, None)
        if client is not None:
            await client.close()

    old = await factory("default", anonymous=True)
    transports[-1].cookies.set("pkey", "previous-session")
    auth = AuthService(store, repository, factory, reset_anonymous=reset)
    try:
        await auth.request_qr("default")
        assert old.closed
        await auth.check("default")
        await auth.request_qr("default")
        assert observed[0][1] == ""
        assert "qr_session=fresh" in observed[1][1]
        assert observed[2][1] == ""
        assert await factory("default") is authenticated
        assert store.load("default") == mock_credentials()
    finally:
        for client in clients:
            await client.close()
        for transport in transports:
            await transport.aclose()


async def test_runtime_wires_anonymous_reset_without_closing_logged_in_client(tmp_path, fake_config):
    from xiaoheihe.runtime import RuntimeServices

    runtime = RuntimeServices(fake_config, tmp_path)
    anonymous = AsyncMock()
    authenticated = AsyncMock()
    runtime._clients[("default", True)] = anonymous
    runtime._clients[("default", False)] = authenticated
    try:
        await runtime.auth._reset_anonymous("default")
        anonymous.close.assert_awaited_once()
        authenticated.close.assert_not_awaited()
        assert ("default", True) not in runtime._clients
        assert runtime._clients[("default", False)] is authenticated
    finally:
        await runtime.close()
