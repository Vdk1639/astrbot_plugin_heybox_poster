"""Independent Python implementation of the Xiaoheihe AstrBot adapter."""

from .models import (
    Credentials,
    EventState,
    LoginState,
    Notification,
    NotificationType,
    RoutingTarget,
)

__all__ = [
    "Credentials",
    "EventState",
    "LoginState",
    "Notification",
    "NotificationType",
    "RoutingTarget",
]

from pathlib import Path

__version__ = Path(__file__).resolve().parents[1].joinpath("VERSION").read_text(encoding="ascii").strip()
