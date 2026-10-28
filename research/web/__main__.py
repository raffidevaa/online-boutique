"""Run the host-accessible RCA dashboard."""

from __future__ import annotations

import argparse
import uvicorn

from research.utils import ResearchConfig

from .app import create_app


def main() -> int:
    config = ResearchConfig.from_environment()
    parser = argparse.ArgumentParser(description="Online Boutique RCA dashboard")
    parser.add_argument("--host", default=config.gui_host)
    parser.add_argument("--port", type=int, default=config.gui_port)
    arguments = parser.parse_args()
    uvicorn.run(create_app(config), host=arguments.host, port=arguments.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
