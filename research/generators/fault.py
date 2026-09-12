"""Tool-neutral input for the next controlled fault-generator implementation."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any


@dataclass(frozen=True)
class FaultSpec:
    """A fault description, not an executable shell command."""

    fault_type: str
    duration: timedelta
    parameters: dict[str, Any] = field(default_factory=dict)
