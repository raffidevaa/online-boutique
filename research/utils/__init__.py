"""Shared configuration and append-only artifact helpers."""

from .artifacts import append_event, create_run, write_json
from .config import ResearchConfig

__all__ = ["ResearchConfig", "append_event", "create_run", "write_json"]
