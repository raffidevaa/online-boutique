"""Safe parser for the single-action markdown protocol used by AIOpsLab."""

from __future__ import annotations

import ast
import json
import re
from typing import Any


class ActionParseError(ValueError):
    """Raised when an agent response is not exactly one permitted action call."""


class ResponseParser:
    _BLOCK = re.compile(
        r"```(?P<language>python|json)?\s*\n(?P<body>.*?)\n```",
        re.DOTALL | re.IGNORECASE,
    )

    def parse(self, response: str) -> tuple[str, list[Any], dict[str, Any]]:
        blocks = self._BLOCK.findall(response)
        if len(blocks) != 1:
            raise ActionParseError("Response must contain exactly one markdown action block")
        language, body = blocks[0]
        if language.lower() == "json":
            return self._parse_json_action(body)
        try:
            tree = ast.parse(body.strip(), mode="eval")
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

    @staticmethod
    def _parse_json_action(body: str) -> tuple[str, list[Any], dict[str, Any]]:
        try:
            action = json.loads(body)
        except json.JSONDecodeError as error:
            raise ActionParseError("JSON action is not valid JSON") from error
        if not isinstance(action, dict) or not isinstance(action.get("action"), str):
            raise ActionParseError('JSON action must be an object with a string "action" field')
        name = action["action"]
        kwargs = {key: value for key, value in action.items() if key != "action"}
        return name, [], kwargs
