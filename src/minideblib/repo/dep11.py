# SPDX-License-Identifier: GPL-2.0-only
# Copyright (C) 2026 Alexander D. Kanevskiy <kad@bifh.org>

"""DEP-11 (AppStream collection metadata) support.

Archives ship ``dists/<suite>/<component>/dep11/Components-<arch>.yml.*``:
a multi-document YAML stream with a header document (``File: DEP-11``,
``Version``, ``Origin``, ``MediaBaseUrl``, ...) followed by one document per
software component (``Type``, ``ID``, ``Package``, localized ``Name`` and
``Summary``, ``Categories``, ``Icon``, ``Provides``, ...). See
https://dep-team.pages.debian.net/deps/dep11/.

Parsing needs PyYAML, the optional extra ``dep11``
(``pip install 'minideblib[dep11]'``); it is imported lazily and only ever
used with a safe loader. :func:`summarize` works without it. YAML
timestamps and ``!!binary`` values are kept as plain strings and ``!!set``
values become sorted lists, so documents stay JSON-serialisable.

Hostile input is bounded: before the YAML loader sees the text, a linear
pass over the parser's event stream rejects nesting deeper than
:data:`MAX_NESTING` (composing such input would crash the C loader) and any
alias (aliases allow exponential "billion laughs" expansion when the data is
later turned into text or JSON). This costs about half a load again. Summary strings are capped at
:data:`MAX_TEXT` characters.

Limitations: the whole decompressed file is held in memory, as bytes and
again as decoded text, while it is parsed. YAML 1.1 resolution applies, so
unquoted ``yes``/``no``/``on``/``off`` become booleans, also as mapping keys.
"""

from __future__ import annotations

import functools
import logging
from dataclasses import dataclass, field
from typing import Any

from minideblib.compress import DEFAULT_MAX_SIZE, decompress
from minideblib.errors import MinidebError

__all__ = [
    "Dep11Error",
    "Dep11Header",
    "Dep11Summary",
    "MissingYamlError",
    "load_components",
    "parse_components",
    "require_yaml",
    "summarize",
]

log = logging.getLogger(__name__)

Dep11Header = dict[str, object]

#: Deepest flow plus block nesting accepted (DEP-11 needs well under 10).
MAX_NESTING = 100
#: Longest string kept in a :class:`Dep11Summary` field.
MAX_TEXT = 4096

_TIMESTAMP = "tag:yaml.org,2002:timestamp"
_BINARY = "tag:yaml.org,2002:binary"
_SET = "tag:yaml.org,2002:set"
_INT = "tag:yaml.org,2002:int"
#: Longer integer scalars stay strings (``int()`` refuses > 4300 digits and
#: such values are no meaningful numbers in DEP-11 anyway).
_MAX_INT_TEXT = 100


class MissingYamlError(MinidebError, ImportError):
    """PyYAML (the ``dep11`` extra) is not installed."""


class Dep11Error(MinidebError, ValueError):
    """Malformed DEP-11 YAML."""


def require_yaml() -> Any:
    """Return the :mod:`yaml` module or raise :class:`MissingYamlError`."""
    try:
        import yaml
    except ImportError as e:
        raise MissingYamlError(
            "DEP-11/AppStream support requires PyYAML; "
            "install with: pip install 'minideblib[dep11]'"
        ) from e
    return yaml


def _too_deep() -> Dep11Error:
    return Dep11Error(f"YAML nesting deeper than {MAX_NESTING}")


def _prescan(yaml: Any, text: str, base: type[Any]) -> None:
    """Check the event stream before anything is composed.

    The libyaml/PyYAML event parsers are non-recursive state machines; only
    composing nodes recurses (and crashes the C loader on deep input). This
    pass bounds collection nesting to :data:`MAX_NESTING` and rejects any
    alias, exactly and in linear time, stopping at the first violation.
    """
    seq_start, map_start = yaml.SequenceStartEvent, yaml.MappingStartEvent
    seq_end, map_end = yaml.SequenceEndEvent, yaml.MappingEndEvent
    alias, doc_start = yaml.AliasEvent, yaml.DocumentStartEvent
    depth = 0
    index = -1
    try:
        for event in yaml.parse(text, Loader=base):
            kind = type(event)
            if kind is seq_start or kind is map_start:
                depth += 1
                if depth > MAX_NESTING:
                    raise _too_deep()
            elif kind is seq_end or kind is map_end:
                depth -= 1
            elif kind is alias:
                raise Dep11Error("YAML aliases are not supported in DEP-11 data")
            elif kind is doc_start:
                index += 1
    except Dep11Error as e:
        raise Dep11Error(f"DEP-11 document {max(index, 0)}: {e}") from None
    except yaml.YAMLError as e:
        raise Dep11Error(f"malformed DEP-11 YAML in document {max(index, 0)}: {e}") from e


def _construct_set(loader: Any, node: Any) -> list[Any]:
    return sorted(loader.construct_mapping(node), key=lambda k: (str(type(k)), str(k)))


def _base_loader(yaml: Any) -> type[Any]:
    return getattr(yaml, "CSafeLoader", None) or yaml.SafeLoader


@functools.cache
def _loader(yaml: Any) -> type[Any]:
    """Safe loader (C-accelerated when available) with JSON-friendly values."""
    base = _base_loader(yaml)

    class Loader(base):
        pass

    Loader.yaml_implicit_resolvers = {
        first: [(tag, rx) for tag, rx in resolvers if tag != _TIMESTAMP]
        for first, resolvers in base.yaml_implicit_resolvers.items()
    }
    for tag in (_TIMESTAMP, _BINARY):
        Loader.add_constructor(tag, lambda loader, node: loader.construct_scalar(node))
    Loader.add_constructor(_SET, _construct_set)

    def construct_int(loader: Any, node: Any) -> Any:
        if len(node.value) > _MAX_INT_TEXT:
            return loader.construct_scalar(node)
        return base.construct_yaml_int(loader, node)

    Loader.add_constructor(_INT, construct_int)
    return Loader


def _is_header(doc: dict) -> bool:
    return str(doc.get("File", "")) == "DEP-11"


def parse_components(data: bytes, *, limit: int | None = None) -> tuple[dict, list[dict]]:
    """Parse decompressed ``Components-<arch>.yml`` bytes into ``(header, components)``.

    Documents are streamed with :func:`yaml.load_all`. The first document is
    the header when it has ``File: DEP-11``; otherwise there is no header
    (``{}``) and it is a component. Empty documents are skipped, other
    non-mapping documents are skipped with a warning; unknown keys are kept.
    At most *limit* component documents are returned (``None``: all).
    Empty input gives ``({}, [])``.

    Raises :class:`MissingYamlError` without PyYAML and :class:`Dep11Error`
    for malformed YAML (naming the 0-based document index), text that is not
    UTF-8, nesting deeper than :data:`MAX_NESTING` and anchors/aliases.
    """
    yaml = require_yaml()
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as e:
        raise Dep11Error(f"DEP-11 data is not valid UTF-8: {e}") from e
    header: dict = {}
    components: list[dict] = []
    if limit is not None and limit <= 0:
        return header, components
    _prescan(yaml, text, _base_loader(yaml))
    index = 0
    docs = yaml.load_all(text, Loader=_loader(yaml))
    try:
        while True:
            try:
                doc = next(docs)
            except StopIteration:
                break
            except yaml.YAMLError as e:
                raise Dep11Error(f"malformed DEP-11 YAML in document {index}: {e}") from e
            except RecursionError as e:
                raise Dep11Error(f"DEP-11 document {index}: {_too_deep()}") from e
            except ValueError as e:  # a constructor rejected a scalar value
                raise Dep11Error(f"bad value in DEP-11 document {index}: {e}") from e
            current, index = index, index + 1
            if doc is None:
                continue
            if not isinstance(doc, dict):
                log.warning("skipping non-mapping DEP-11 document %d", current)
                continue
            if current == 0 and _is_header(doc):
                header = doc
                continue
            components.append(doc)
            if limit is not None and len(components) >= limit:
                break
    finally:
        docs.close()
    return header, components


def load_components(
    data: bytes,
    name: str = "Components.yml",
    *,
    max_size: int = DEFAULT_MAX_SIZE,
    limit: int | None = None,
) -> tuple[dict, list[dict]]:
    """Like :func:`parse_components` for possibly compressed *data*.

    *name* hints the codec; decompression stops with
    :class:`~minideblib.compress.DecompressionLimitError` beyond *max_size*.
    """
    return parse_components(decompress(data, name, max_size=max_size), limit=limit)


@dataclass
class Dep11Summary:
    """The commonly shown fields of one component, localized."""

    id: str
    type: str
    package: str
    name: str
    summary: str
    categories: list[str] = field(default_factory=list)


def _text(value: object) -> str:
    """Scalar as a string of at most :data:`MAX_TEXT` characters; ``""`` otherwise."""
    if isinstance(value, str):
        return value[:MAX_TEXT]
    if isinstance(value, int | float):  # bool included
        return str(value)[:MAX_TEXT]
    return ""


def _localized(value: object, locale: str) -> str:
    """Pick *locale*, its language part, ``C``, then any value from a localized dict."""
    if not isinstance(value, dict):
        return _text(value)
    if not value:
        return ""
    lang = locale.split(".", 1)[0].split("@", 1)[0]
    for key in dict.fromkeys((locale, lang, lang.split("_", 1)[0], "C")):
        if key in value:
            return _text(value[key])
    return _text(next(iter(value.values())))


def summarize(component: dict[str, Any], locale: str = "C") -> Dep11Summary:
    """Summary of *component* with ``Name``/``Summary`` chosen for *locale*.

    Lookup order for localized values: *locale* (e.g. ``de_DE.UTF-8``), the
    locale without encoding/modifier (``de_DE``), the language (``de``),
    ``C``, then any value. Missing keys give empty strings/lists.
    """
    cats = component.get("Categories")
    return Dep11Summary(
        id=_text(component.get("ID")),
        type=_text(component.get("Type")),
        package=_text(component.get("Package")),
        name=_localized(component.get("Name"), locale),
        summary=_localized(component.get("Summary"), locale),
        categories=[_text(c) for c in cats] if isinstance(cats, list) else [],
    )
