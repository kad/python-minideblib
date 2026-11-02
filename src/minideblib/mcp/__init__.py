# SPDX-License-Identifier: GPL-2.0-only
# Copyright (C) 2026 Alexander D. Kanevskiy <kad@bifh.org>

"""A stdlib-only Model Context Protocol (MCP) server for ``minideb mcp``.

:mod:`minideblib.mcp.protocol` holds the stdio transport, JSON-RPC handling
and tool dispatch (:class:`McpServer`, :class:`Tool`, :class:`ToolCall`,
:class:`ToolError`, :func:`validate`); :mod:`minideblib.mcp.safeio` the
confined write helpers and :mod:`minideblib.mcp.startup` the startup checks
and :class:`~minideblib.mcp.startup.ServerConfig`; :mod:`minideblib.mcp.tools`
builds the tools (:func:`~minideblib.mcp.tools.build_tools`), with repository
access and text cleaning in :mod:`minideblib.mcp.repos` and the read-only
repository tools in :mod:`minideblib.mcp.tools_repo`.
"""

from __future__ import annotations

from minideblib.mcp.protocol import (
    SUPPORTED_VERSIONS,
    McpServer,
    Tool,
    ToolCall,
    ToolError,
    validate,
)

__all__ = ["SUPPORTED_VERSIONS", "McpServer", "Tool", "ToolCall", "ToolError", "validate"]
