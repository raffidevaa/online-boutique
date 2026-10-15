"""Safe parser for the single-action markdown protocol used by AIOpsLab."""

from __future__ import annotations

import ast
import re
from typing import Any


class ActionParseError(ValueError):
    """Raised when an agent response is not exactly one permitted action call."""


class ResponseParser:
    _BLOCK = re.compile(r"```(?:python)?\s*\n(.*?)\n```", re.DOTALL | re.IGNORECASE)

    def parse(self, response: str) -> tuple[str, list[Any], dict[str, Any]]:
        blocks = self._BLOCK.findall(response)
        if len(blocks) != 1:
            raise ActionParseError("Response must contain exactly one markdown action block")
        try:
            tree = ast.parse(blocks[0].strip(), mode="eval")
        except SyntaxError as error:
            raise ActionParseError("Action is not valid Python call syntax") from error
        if not isinstance(tree.body, ast.Call) or not isinstance(tree.body.func, ast.Name):
            raise ActionParseError("Action must be a direct function call")
        if any(keyword.arg is None for keyword in tree.body.keywords):
            raise ActionParseError("Starred keyword arguments are not allowed")
        try:
            args = [ast.literal_eval(argument) for argument in tree.body.args]
            kwargs = {
                keyword.arg: ast.literal_eval(keyword.value)
                for keyword in tree.body.keywords
            }
        except (ValueError, TypeError) as error:
            raise ActionParseError("Action arguments must be literals") from error
        return tree.body.func.id, args, kwargs
