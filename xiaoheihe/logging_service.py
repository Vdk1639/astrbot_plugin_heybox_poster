from __future__ import annotations

from collections import deque
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from astrbot.api import logger

from .security import redact_log_data, redact_log_text
from .task_manager import TaskManager

_LEVELS = {
    "DEBUG": 10, "INFO": 20, "WARNING": 30, "WARN": 30,
    "ERROR": 40, "CRITICAL": 50, "FATAL": 50,
}
_MAX_FILE_BYTES = 5 * 1024 * 1024


class LoggingService:
    def __init__(
        self,
        data_dir: Path,
        task_manager: TaskManager,
        *,
        level: str = "INFO",
        max_memory_entries: int = 2000,
        total_limit_mb: int = 100,
    ) -> None:
        self._entries: deque[dict[str, Any]] = deque(
            maxlen=max(100, min(max_memory_entries, 10000))
        )
        self._tasks = task_manager
        log_dir = data_dir / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        self._log_path = log_dir / "xiaoheihe.log"
        self.reconfigure(
            level=level,
            max_memory_entries=max_memory_entries,
            total_limit_mb=total_limit_mb,
        )

    @property
    def log_path(self) -> Path:
        return self._log_path

    def emit(
        self,
        level: str,
        message: str,
        *,
        profile_id: str = "",
        details: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        normalized_level = level.upper()
        safe_message = redact_log_text(message).replace("\r", "\\r").replace("\n", "\\n")
        entry = {
            "time": datetime.now(UTC).isoformat(),
            "level": normalized_level,
            "profile_id": profile_id,
            "message": safe_message,
            "details": redact_log_data(details or {}),
        }
        self._entries.append(entry)
        text = (
            f"[小黑盒][profile={profile_id or '-'}] "
            f"{safe_message} {entry['details'] or ''}"
        )
        log_method = getattr(logger, normalized_level.lower(), logger.info)
        log_method(text)
        if _LEVELS.get(normalized_level, 20) >= self._min_level:
            try:
                self._write_file(f"{entry['time']} {normalized_level} {text}\n")
            except OSError as exc:
                logger.warning(f"[小黑盒] 本地日志写入失败：{type(exc).__name__}")
        self._tasks.publish_sse({"type": "log", "entry": entry})
        return entry

    def _write_file(self, text: str) -> None:
        if self._log_path.exists():
            size = self._log_path.stat().st_size
            if size and size + len(text.encode("utf-8")) >= _MAX_FILE_BYTES:
                for index in range(self._backup_count, 0, -1):
                    source = self._log_path.with_name(
                        f"xiaoheihe.log.{index - 1}" if index > 1 else "xiaoheihe.log"
                    )
                    if source.exists():
                        source.replace(self._log_path.with_name(f"xiaoheihe.log.{index}"))
        with self._log_path.open("a", encoding="utf-8", newline="") as stream:
            stream.write(text)

    def list(self, *, level: str = "", keyword: str = "", limit: int = 200) -> list[dict[str, Any]]:
        level_filter = level.upper()
        needle = keyword.casefold()
        result: list[dict[str, Any]] = []
        for entry in reversed(self._entries):
            if level_filter and entry["level"] != level_filter:
                continue
            if needle and needle not in str(entry).casefold():
                continue
            result.append(entry)
            if len(result) >= max(1, min(limit, 1000)):
                break
        return result

    def total_size(self) -> int:
        return sum(
            path.stat().st_size
            for path in self._log_path.parent.glob("xiaoheihe.log*")
            if path.is_file()
        )

    def reconfigure(
        self,
        *,
        level: str,
        max_memory_entries: int,
        total_limit_mb: int,
    ) -> None:
        self._min_level = _LEVELS.get(level.upper(), 20)
        max_entries = max(100, min(max_memory_entries, 10000))
        if self._entries.maxlen != max_entries:
            self._entries = deque(list(self._entries)[-max_entries:], maxlen=max_entries)
        self._backup_count = max(1, min(39, int(total_limit_mb) // 5 - 1))

    def close(self) -> None:
        """文件在每次写入后关闭，无需释放常驻句柄。"""
