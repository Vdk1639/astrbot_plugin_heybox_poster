from __future__ import annotations

import ast
from pathlib import Path
from unittest.mock import Mock

import pytest

from xiaoheihe import logging_service as logging_module
from xiaoheihe.logging_service import LoggingService
from xiaoheihe.task_manager import TaskManager


async def test_logging_redacts_and_service_closes(tmp_path) -> None:
    tasks = TaskManager()
    service = LoggingService(tmp_path, tasks, max_memory_entries=100)
    entry = service.emit(
        "INFO",
        (
            "Authorization: Bearer secret-value; "
            "https://cdn.example.test/image.png?signature=private"
        ),
        details={
            "cookie": "private",
            "error": "fetch https://cdn.example.test/a.png?expires=1&sig=private failed",
        },
    )
    assert "secret-value" not in entry["message"]
    assert "signature=private" not in entry["message"]
    assert entry["details"]["cookie"] == "[REDACTED]"
    assert "sig=private" not in entry["details"]["error"]
    assert logging_module.redact_log_data({"session_mappings": 2}) == {"session_mappings": 2}
    assert service.list(limit=1)[0] == entry
    content = service.log_path.read_text(encoding="utf-8")
    assert "secret-value" not in content
    assert "signature=private" not in content
    assert "sig=private" not in content
    assert "[REDACTED]" in content
    assert service.total_size() == service.log_path.stat().st_size > 0
    service.reconfigure(level="ERROR", max_memory_entries=250, total_limit_mb=50)
    assert service._entries.maxlen == 250
    assert service._backup_count == 9
    service.emit("INFO", "filtered from file")
    assert service.log_path.read_text(encoding="utf-8") == content
    assert service.list(limit=1)[0]["message"] == "filtered from file"
    service.emit("ERROR", "written after reconfigure")
    assert "written after reconfigure" in service.log_path.read_text(encoding="utf-8")
    service.close()
    service.close()
    await tasks.close()


async def test_logging_rotates_utf8_files_and_limits_backups(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(logging_module, "_MAX_FILE_BYTES", 160)
    tasks = TaskManager()
    service = LoggingService(tmp_path, tasks, total_limit_mb=15)
    try:
        for index in range(4):
            service.emit("INFO", f"第{index}条日志" + "中" * 20)
        paths = sorted(service.log_path.parent.glob("xiaoheihe.log*"))
        assert [path.name for path in paths] == [
            "xiaoheihe.log", "xiaoheihe.log.1", "xiaoheihe.log.2",
        ]
        for path, index in zip(paths, (3, 2, 1)):
            content = path.read_text(encoding="utf-8")
            assert f"第{index}条日志" in content
            assert "第0条日志" not in content
        assert service.total_size() == sum(path.stat().st_size for path in paths)
    finally:
        service.close()
        await tasks.close()


@pytest.mark.parametrize("level", ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL", "unknown"])
async def test_logging_uses_only_astrbot_public_logger(tmp_path, monkeypatch, level) -> None:
    public_logger = Mock(spec=["debug", "info", "warning", "error", "critical"])
    monkeypatch.setattr(logging_module, "logger", public_logger)
    tasks = TaskManager()
    monkeypatch.setattr(tasks, "publish_sse", Mock())
    service = LoggingService(tmp_path, tasks)
    try:
        entry = service.emit(
            level, "status refreshed\r\nAuthorization: Bearer secret-value;",
            profile_id="default", details={"cookie": "private"},
        )
        method = level.lower() if level != "unknown" else "info"
        assert len(public_logger.mock_calls) == 1
        getattr(public_logger, method).assert_called_once_with(
            f"[小黑盒][profile=default] {entry['message']} {entry['details']}"
        )
        text = getattr(public_logger, method).call_args.args[0]
        assert "secret-value" not in text
        assert "private" not in text
        assert "\n" not in text and "\r" not in text
        tasks.publish_sse.assert_called_once_with({"type": "log", "entry": entry})
    finally:
        service.close()
        await tasks.close()


def test_logging_service_imports_only_public_logger() -> None:
    tree = ast.parse(Path(logging_module.__file__).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            assert all(alias.name.split(".")[0] != "logging" for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert not (node.module or "").startswith("logging")
    assert any(
        isinstance(node, ast.ImportFrom) and node.module == "astrbot.api"
        and any(alias.name == "logger" for alias in node.names)
        for node in ast.walk(tree)
    )


async def test_file_write_failure_preserves_public_log_and_sse(tmp_path, monkeypatch) -> None:
    public_logger = Mock(spec=["info", "warning"])
    monkeypatch.setattr(logging_module, "logger", public_logger)
    tasks = TaskManager()
    monkeypatch.setattr(tasks, "publish_sse", Mock())
    service = LoggingService(tmp_path, tasks)
    monkeypatch.setattr(service, "_write_file", Mock(side_effect=PermissionError("secret path")))
    try:
        entry = service.emit("INFO", "still visible")
        public_logger.info.assert_called_once_with("[小黑盒][profile=-] still visible ")
        public_logger.warning.assert_called_once_with("[小黑盒] 本地日志写入失败：PermissionError")
        assert service.list(limit=1) == [entry]
        tasks.publish_sse.assert_called_once_with({"type": "log", "entry": entry})
    finally:
        service.close()
        await tasks.close()
