# SPDX-License-Identifier: GPL-2.0-only
# Copyright (C) 2026 Alexander D. Kanevskiy <kad@bifh.org>

"""Schema and argument helpers shared by the MCP tool modules.

Builders for the JSON schema fragments of tool arguments (:func:`string`,
:func:`strings`, :func:`schema`, :data:`LIMIT`, :data:`OFFSET`) and
accessors for validated arguments (:func:`opt_str`, :func:`req_str`,
:func:`int_arg`), plus the paging and text length limits the tools share.
"""

from __future__ import annotations

from collections.abc import Sequence

from minideblib.mcp.protocol import ToolError

__all__ = [
    "DEFAULT_LIMIT",
    "FIELD_LIMIT",
    "LIMIT",
    "MAX_LIMIT",
    "OFFSET",
    "SHORT_LIMIT",
    "int_arg",
    "opt_str",
    "req_str",
    "schema",
    "string",
    "strings",
]

DEFAULT_LIMIT = 50
MAX_LIMIT = 500
#: longest field value returned (characters)
FIELD_LIMIT = 4000
#: longest short text (names, short descriptions, list items)
SHORT_LIMIT = 300


def string(description: str, max_length: int = 256) -> dict[str, object]:
    """Schema of a non-empty string argument."""
    return {"type": "string", "minLength": 1, "maxLength": max_length, "description": description}


def strings(description: str) -> dict[str, object]:
    """Schema of a list-of-strings argument."""
    return {
        "type": "array",
        "items": {"type": "string", "minLength": 1, "maxLength": 128},
        "description": description,
    }


LIMIT: dict[str, object] = {
    "type": "integer",
    "minimum": 1,
    "maximum": MAX_LIMIT,
    "default": DEFAULT_LIMIT,
    "description": f"Page size (default {DEFAULT_LIMIT}, at most {MAX_LIMIT}).",
}
OFFSET: dict[str, object] = {
    "type": "integer",
    "minimum": 0,
    "default": 0,
    "description": "Items to skip (use next_offset of the previous page).",
}


def schema(required: Sequence[str], **props: dict[str, object]) -> dict[str, object]:
    """Schema of the arguments object of a tool."""
    return {
        "type": "object",
        "properties": props,
        "required": list(required),
        "additionalProperties": False,
    }


def opt_str(args: dict[str, object], name: str) -> str | None:
    """The string argument *name*, or None when absent."""
    value = args.get(name)
    return value if isinstance(value, str) else None


def req_str(args: dict[str, object], name: str) -> str:
    """The string argument *name* (:class:`ToolError` when absent)."""
    value = args.get(name)
    if not isinstance(value, str):
        raise ToolError(f'"{name}": required')
    return value


def int_arg(args: dict[str, object], name: str, default: int) -> int:
    """The integer argument *name*, *default* when absent or not an integer."""
    value = args.get(name, default)
    return value if isinstance(value, int) and not isinstance(value, bool) else default
