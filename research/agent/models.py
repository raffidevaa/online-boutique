"""Session and problem contracts shared by the AIOps-compatible agent runtime."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
import uuid


@dataclass(frozen=True)
class SessionItem:
    role: str
    content: str

    def to_dict(self) -> dict[str, str]:
        return {"role": self.role, "content": self.content}


@dataclass
class AgentSession:
    problem_id: str
    agent_name: str = "agent"
    session_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    history: list[SessionItem] = field(default_factory=list)
    solution: Any = None
    results: dict[str, Any] = field(default_factory=dict)
    submitted: bool = False
    termination_reason: str | None = None
    invalid_action_count: int = 0
    start_time: datetime | None = None
    end_time: datetime | None = None

    def add(self, role: str, content: Any) -> None:
        if content is None:
            return
        if not isinstance(content, str):
            content = str(content)
        self.history.append(SessionItem(role, content))

    def start(self) -> None:
        self.start_time = datetime.now(UTC)

    def end(self) -> None:
        self.end_time = datetime.now(UTC)

    @property
    def duration_seconds(self) -> float:
        if self.start_time is None or self.end_time is None:
            return 0.0
        return (self.end_time - self.start_time).total_seconds()

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "session_id": self.session_id,
            "problem_id": self.problem_id,
            "agent_name": self.agent_name,
            "start_time": self.start_time.isoformat() if self.start_time else None,
            "end_time": self.end_time.isoformat() if self.end_time else None,
            "trace": [item.to_dict() for item in self.history],
            "solution": self.solution,
            "submitted": self.submitted,
            "termination_reason": self.termination_reason,
            "invalid_action_count": self.invalid_action_count,
            "results": self.results,
            "duration_seconds": self.duration_seconds,
        }


@dataclass(frozen=True)
class ProblemContext:
    problem_id: str
    description: str
    instructions: str
    actions: dict[str, str]
