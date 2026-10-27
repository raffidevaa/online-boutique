"""Compatibility entry point for ``python -m research.agent.evaluate``."""

from .evaluator import main


if __name__ == "__main__":
    raise SystemExit(main())
