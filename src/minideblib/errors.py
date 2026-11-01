# SPDX-License-Identifier: GPL-2.0-only
# Copyright (C) 2026 Alexander D. Kanevskiy <kad@bifh.org>

"""The common base class of the library's exceptions."""

from __future__ import annotations

__all__ = ["MinidebError"]


class MinidebError(Exception):
    """Base class of every exception minideblib raises for bad data.

    Malformed or unusable input (control data, indices, Release, changelogs,
    sources files, ``.deb``/``.dsc`` files, versions, compressed data,
    DEP-11 YAML), failed downloads and hash checks all raise a subclass.
    Catch it to handle any library error at once. The concrete classes keep
    their other, more specific bases (``ValueError``, ``ImportError``,
    ``KeyError``), so existing ``except`` clauses still work.

    Not covered, and raised as the built-in exceptions:

    * programmer errors: wrong argument types (``TypeError``), an unknown
      ``probe`` set of ``RepoClient.from_url`` or index ``kind`` of
      ``select_indices`` (``ValueError``), assigning to a ``Version``
      attribute (``AttributeError``);
    * ``OSError`` from reading or writing local files (``DebPackage.open``,
      ``Dsc.load``, ``control.parse_file``, ``sources.load``, extraction);
    * ``KeyError`` for a missing field of a ``Paragraph`` (mapping protocol);
    * whatever caller-supplied objects raise, such as a text stream given to
      ``parse_paragraphs`` or a custom ``Fetcher``.
    """
