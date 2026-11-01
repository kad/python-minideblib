# SPDX-License-Identifier: GPL-2.0-only
# Copyright (C) 2026 Alexander D. Kanevskiy <kad@bifh.org>

"""Python Mini Debian Library: APT repositories, ``.deb``/``.dsc`` files and versions.

The modules are imported individually (``minideblib.repo.client``,
``minideblib.deb``, ...); this package only exports :class:`MinidebError`,
the base class of all library exceptions, and ``__version__``.
"""

from minideblib.errors import MinidebError

__all__ = ["MinidebError", "__version__"]

__version__ = "0.7.0"
