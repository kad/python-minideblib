# SPDX-License-Identifier: GPL-2.0-only
# Copyright (C) 2026 Alexander D. Kanevskiy <kad@bifh.org>

"""Repository access and result hygiene shared by the MCP tools.

:class:`RepoManager` turns the ``repository`` argument of a tool call (a
configured name such as ``repo1`` or an ``http``/``https``/``file`` URL)
into a cached :class:`~minideblib.repo.client.RepoClient` that uses one
:class:`~minideblib.repo.fetch.Fetcher` with the server's network policy.
:func:`clean_text` and :func:`clean_value` make repository text safe to
return: it is untrusted data, so control and invisible formatting
characters are removed and long values are cut with a visible marker.
:func:`page` slices a result list by ``offset``/``limit`` and a byte budget.
"""

from __future__ import annotations

import json
import math
import re
import threading
from collections import OrderedDict
from collections.abc import Callable, Generator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TypeVar

from minideblib.mcp.protocol import ToolCall, ToolError
from minideblib.mcp.startup import RepositorySpec, ServerConfig, StartupError, check_url
from minideblib.repo.client import DEFAULT_CACHE_BUDGET, Probe, RepoClient
from minideblib.repo.fetch import Fetcher, HostNotAllowed, redact_url

__all__ = [
    "FIND_FILE_BUDGET",
    "MAX_CLIENTS",
    "Repo",
    "RepoManager",
    "clean_list",
    "clean_text",
    "clean_value",
    "error_text",
    "page",
]

_T = TypeVar("_T")

#: repository clients kept by :class:`RepoManager` (least recently used dropped)
MAX_CLIENTS = 8
#: estimated parsed-index bytes one ``find_file`` call may read (see the tool)
FIND_FILE_BUDGET = 2 * 1024**3

#: longest repository name or URL quoted back in an error message
_QUOTE = 200

# ANSI/VT escape sequences: CSI (``ESC [ ... final``), OSC (``ESC ] ... BEL``
# or ``ESC \``) and two-character escapes; removed as a whole so that no
# printable remainder such as ``[31m`` is left behind.
_ESCAPES = re.compile(
    r"\x1b\[[0-?]*[ -/]*[@-~]"
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?"
    r"|\x9b[0-?]*[ -/]*[@-~]"
    r"|\x1b[@-_]?"
)
# C0/C1 controls except tab and newline; invisible format characters (soft
# hyphen, zero-width and bidirectional controls, BOM, interlinear
# annotations); line/paragraph separators; surrogates; tag characters
# (invisible ASCII look-alikes).
_UNSAFE = re.compile(
    "[\x00-\x08\x0b-\x1f\x7f-\x9f"
    "\u00ad\u061c\u180e\u200b-\u200f\u2028-\u202e\u2060-\u2064\u2066-\u206f"
    "\ufeff\ufff9-\ufffb\ud800-\udfff\U000e0000-\U000e007f]"
)


def clean_text(value: str, limit: int) -> str:
    """*value* without control/invisible characters, cut to *limit* characters.

    Tab and newline are kept; ANSI escape sequences are removed as a whole.
    Longer text is truncated and ends with ``…[truncated N chars]``, *N*
    counting the characters dropped from the cleaned text.
    """
    text = _UNSAFE.sub("", _ESCAPES.sub("", value))
    if len(text) <= limit:
        return text
    return f"{text[:limit]}…[truncated {len(text) - limit} chars]"


def clean_list(values: Sequence[str], limit: int, max_items: int = 200) -> list[str]:
    """:func:`clean_text` of each value; more than *max_items* end with a marker item."""
    out = [clean_text(v, limit) for v in values[:max_items]]
    if len(values) > max_items:
        out.append(f"…[{len(values) - max_items} more]")
    return out


def clean_value(value: object, limit: int, depth: int = 0, max_items: int = 500) -> object:
    """A JSON-safe, cleaned copy of a parsed (e.g. YAML) structure.

    Strings (also mapping keys) go through :func:`clean_text`; numbers,
    booleans and None stay (non-finite floats become strings); other objects
    become their cleaned ``str``. Lists and mappings keep at most
    *max_items* entries (a marker notes the rest); nesting deeper than 32
    levels is cut.
    """
    if isinstance(value, str):
        return clean_text(value, limit)
    if value is None or isinstance(value, bool | int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if depth >= 32:
        return "…[nested too deep]"
    if isinstance(value, Mapping):
        out: dict[str, object] = {}
        for i, (key, item) in enumerate(value.items()):
            if i >= max_items:
                out["…"] = f"[{len(value) - max_items} more]"
                break
            out[clean_text(str(key), 200)] = clean_value(item, limit, depth + 1, max_items)
        return out
    if isinstance(value, list | tuple):
        items = [clean_value(v, limit, depth + 1, max_items) for v in value[:max_items]]
        if len(value) > max_items:
            items.append(f"…[{len(value) - max_items} more]")
        return items
    return clean_text(str(value), limit)


def error_text(exc: BaseException) -> str:
    """A safe one-line message for *exc*: credentials redacted, cleaned, bounded."""
    return clean_text(redact_url(str(exc)).replace("\n", " "), 500)


def _json_size(value: object) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode())


def page(
    items: Sequence[_T],
    offset: int,
    limit: int,
    budget: int,
    render: Callable[[_T], dict[str, object]],
    key: str,
) -> dict[str, object]:
    """``items[offset:offset + limit]`` rendered, within *budget* bytes of JSON.

    The result holds ``total``, ``offset``, ``limit``, ``returned``,
    ``truncated`` (the byte budget ended the page before *limit* items),
    ``next_offset`` (None when nothing follows) and the rendered items under
    *key*. The first item of a page is always included.
    """
    out: list[dict[str, object]] = []
    used = 0
    truncated = False
    for item in items[offset : offset + limit]:
        rendered = render(item)
        size = _json_size(rendered)
        if out and used + size > budget:
            truncated = True
            break
        out.append(rendered)
        used += size
    end = offset + len(out)
    return {
        "total": len(items),
        "offset": offset,
        "limit": limit,
        "returned": len(out),
        "truncated": truncated,
        "next_offset": end if end < len(items) else None,
        key: out,
    }


@dataclass(frozen=True)
class Repo:
    """A resolved ``repository`` argument.

    *label* is what results report: the configured name, or the URL
    (without credentials, which are refused anyway). *spec* is the
    configured repository, None for a URL given in the call.
    """

    label: str
    url: str
    client: RepoClient
    spec: RepositorySpec | None


def _quote(text: str) -> str:
    return json.dumps(clean_text(redact_url(text), _QUOTE), ensure_ascii=False)


class RepoManager:
    """Resolve ``repository`` arguments into cached :class:`RepoClient` objects.

    All clients share one :class:`Fetcher` built from *config*: timeout,
    retries, ``allowed_hosts`` and link-local blocking (unless
    ``allow_link_local``). Index hash checks follow ``config.verify``.
    Clients are kept per repository URL (trailing slashes ignored), at most
    *max_clients* (least recently used dropped). *cache_budget_bytes* bounds
    the estimated memory of the parsed indices of every client together:
    after each call, the caches of the least recently used other clients
    are cleared until the total fits (see :meth:`trim`).
    *find_file_budget_bytes* is the estimated parsed-index budget of one
    ``find_file`` call.

    Clients are unscoped (configured sources entries keep their own
    components and architectures); the tools pass per-call
    ``components``/``architectures`` filters to the query methods, so one
    cached index serves every filter. URL repositories never probe suites
    implicitly (``probe="none"``).
    """

    def __init__(
        self,
        config: ServerConfig,
        *,
        max_clients: int = MAX_CLIENTS,
        cache_budget_bytes: int = DEFAULT_CACHE_BUDGET,
        find_file_budget_bytes: int = FIND_FILE_BUDGET,
    ) -> None:
        self.config = config
        self.max_clients = max_clients
        self.cache_budget_bytes = cache_budget_bytes
        self.find_file_budget_bytes = find_file_budget_bytes
        self.fetcher = Fetcher(
            timeout=config.timeout,
            retries=config.retries,
            allowed_hosts=config.allowed_hosts,
            block_link_local=not config.allow_link_local,
        )
        self._by_name = {spec.name: spec for spec in config.repositories}
        self._by_url = {spec.url.rstrip("/"): spec for spec in config.repositories}
        self._clients: OrderedDict[str, Repo] = OrderedDict()
        self._lock = threading.Lock()

    def __len__(self) -> int:
        with self._lock:
            return len(self._clients)

    @contextmanager
    def scope(self, call: ToolCall) -> Generator[None]:
        """Apply the call's cancel token and deadline to every request inside."""
        with self.fetcher.scope(call.token, call.deadline):
            yield

    def _spec(self, repository: str | None) -> tuple[RepositorySpec | None, str]:
        """The configured spec (or None) and the URL for *repository*."""
        if repository is None:
            specs = self.config.repositories
            if len(specs) == 1:
                return specs[0], specs[0].url
            if not specs:
                raise ToolError(
                    "repository is required: no repositories are configured; pass an "
                    "http(s):// or file:/// URL"
                )
            raise ToolError(f"repository is required; configured: {', '.join(self._by_name)}")
        spec = self._by_name.get(repository) or self._by_url.get(repository.rstrip("/"))
        if spec is not None:
            return spec, spec.url
        if ":" not in repository:
            known = ", ".join(self._by_name) or "none"
            raise ToolError(
                f"unknown repository {_quote(repository)} (configured: {known}); "
                "pass a configured name or an http(s):// or file:/// URL"
            )
        try:
            check_url(repository, "repository")
        except StartupError as exc:
            raise ToolError(error_text(exc)) from None
        try:
            self.fetcher.check_host(repository)
        except HostNotAllowed as exc:
            raise ToolError(error_text(exc)) from None
        return None, repository

    def _new_client(
        self, spec: RepositorySpec | None, url: str, cache_budget_bytes: int
    ) -> RepoClient:
        if spec is not None and spec.entries:
            return RepoClient(
                list(spec.entries),
                self.fetcher,
                self.config.verify,
                cache_budget_bytes=cache_budget_bytes,
            )
        return RepoClient.from_url(
            url,
            probe="none",
            fetcher=self.fetcher,
            verify=self.config.verify,
            cache_budget_bytes=cache_budget_bytes,
        )

    def resolve(self, repository: str | None) -> Repo:
        """The cached :class:`Repo` for *repository* (``ToolError`` if unusable).

        None means the only configured repository. Configured names and
        configured URLs map to the configured repository; other strings
        must be ``http``/``https``/``file`` URLs without credentials whose
        host the policy allows (checked before any request).
        """
        spec, url = self._spec(repository)
        key = url.rstrip("/")
        with self._lock:
            repo = self._clients.get(key)
            if repo is not None:
                self._clients.move_to_end(key)
                return repo
            label = spec.name if spec is not None else clean_text(redact_url(url), 2048)
            repo = Repo(label, url, self._new_client(spec, url, self.cache_budget_bytes), spec)
            self._clients[key] = repo
            while len(self._clients) > self.max_clients:
                _, old = self._clients.popitem(last=False)
                old.client.clear_cache()
            return repo

    def transient(self, repo: Repo, cache_budget_bytes: int = 1) -> RepoClient:
        """A new, uncached client for *repo* (same fetcher and policy).

        With the default budget only the index read last stays parsed (each
        new index evicts the previous one).
        """
        return self._new_client(repo.spec, repo.url, cache_budget_bytes)

    def prober(self, repo: Repo, suites: Sequence[str], probe: Probe) -> RepoClient:
        """A client of *repo*'s URL whose :meth:`RepoClient.suites` probes *probe*."""
        return RepoClient.from_url(
            repo.url,
            suites=suites,
            probe=probe,
            fetcher=self.fetcher,
            verify=self.config.verify,
        )

    def trim(self, keep: Repo | None = None) -> None:
        """Clear least recently used caches until all clients fit the budget.

        *keep* (default: the most recently used client) is never cleared:
        its own ``cache_budget_bytes`` bounds it.
        """
        with self._lock:
            repos = list(self._clients.values())
        if keep is None and repos:
            keep = repos[-1]
        total = sum(r.client.cache_bytes for r in repos)
        for repo in repos:
            if total <= self.cache_budget_bytes:
                return
            if keep is not None and repo is keep:
                continue
            total -= repo.client.cache_bytes
            repo.client.clear_cache()
