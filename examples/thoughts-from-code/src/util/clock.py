"""Time helpers."""

from datetime import datetime


def now() -> str:
    return datetime.now().isoformat()
