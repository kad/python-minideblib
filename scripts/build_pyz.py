#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
# Copyright (C) 2026 Alexander D. Kanevskiy <kad@bifh.org>

"""Build a self-contained, reproducible zipapp of the minideb CLI (stdlib zipfile only).

The archive contains the pure-Python ``minideblib`` package and a ``__main__.py`` that
calls ``minideblib.cli:main``. The output is deterministic: sorted entries, fixed
timestamps (``SOURCE_DATE_EPOCH`` if set, else 2020-01-01 UTC) and normalised permissions.

Optional features need extra packages. ``--include-extras`` vendors pure-Python PyYAML
(DEP-11 / AppStream support) when it is importable in the build environment. C extensions
and ``zstandard`` are never bundled, so reading ``.zst`` indexes needs ``zstandard``
installed separately for the interpreter that runs the pyz. With ``--include-extras`` the PyYAML
licence text is copied into the archive as ``yaml/LICENSE``.

Byte-identical output across builds assumes the same zlib build; the archive does not
depend on the host time zone, file modes, timestamps, directory iteration order or the
Python version (the archive is written with ``zipfile``, not ``zipapp.create_archive``,
whose member order differs between 3.11 and 3.12+).
"""

import argparse
import importlib.metadata
import importlib.util
import os
import stat
import time
import zipfile
from collections.abc import Sequence
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_EPOCH = 1577836800  # 2020-01-01T00:00:00Z
MIN_EPOCH = 315532800  # 1980-01-01T00:00:00Z, the zip format minimum
MAX_EPOCH = 4354819198  # 2107-12-31T23:59:58Z, the zip (DOS date) maximum
SHEBANG = "/usr/bin/env python3"
MAIN = (
    "import sys\n"
    "from minideblib.cli import main\n"
    "\n"
    'if __name__ == "__main__":\n'
    "    sys.exit(main())\n"
)
COMPRESS_LEVEL = 9
SKIP_SUFFIXES = (".pyc", ".pyo", ".so", ".pyd", ".dylib")


def collect_tree(src: Path, prefix: str, files: dict[str, bytes]) -> None:
    """Add pure-Python files of *src* to *files* under *prefix*, skipping caches and binaries."""
    for path in src.rglob("*"):
        rel = path.relative_to(src)
        if "__pycache__" in rel.parts or path.suffix in SKIP_SUFFIXES or not path.is_file():
            continue
        files[f"{prefix}/{rel.as_posix()}"] = path.read_bytes()


def write_archive(output: Path, files: dict[str, bytes], epoch: int) -> None:
    """Write a zipapp with sorted entries and fixed metadata, independent of the Python version."""
    date_time = time.gmtime(epoch)[:6]
    dirs = {d.as_posix() + "/" for name in files for d in PurePosixPath(name).parents if d.name}
    entries = sorted([*files, *dirs])
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("wb") as fp:
        fp.write(f"#!{SHEBANG}\n".encode())
        with zipfile.ZipFile(fp, "w") as zf:
            for name in entries:
                info = zipfile.ZipInfo(name, date_time)
                info.create_system = 3  # Unix, whatever the build host is
                info.extract_version = 20
                info.create_version = 20
                if name.endswith("/"):
                    info.compress_type = zipfile.ZIP_STORED
                    info.external_attr = (stat.S_IFDIR | 0o755) << 16 | 0x10
                    zf.writestr(info, b"")
                else:
                    info.compress_type = zipfile.ZIP_DEFLATED
                    info.external_attr = (stat.S_IFREG | 0o644) << 16
                    zf.writestr(info, files[name], compresslevel=COMPRESS_LEVEL)
    output.chmod(0o755)


def find_yaml() -> Path | None:
    spec = importlib.util.find_spec("yaml")
    if spec is None or spec.origin is None:
        return None
    return Path(spec.origin).parent


def yaml_license() -> str | None:
    """Return the PyYAML licence text from its installed metadata, if available."""
    try:
        dist = importlib.metadata.distribution("PyYAML")
    except importlib.metadata.PackageNotFoundError:
        return None
    for f in dist.files or ():
        if f.name.upper().startswith(("LICENSE", "COPYING")):
            return Path(str(dist.locate_file(f))).read_text(encoding="utf-8")
    return None


def source_epoch() -> int:
    raw = os.environ.get("SOURCE_DATE_EPOCH")
    if raw is None:
        return DEFAULT_EPOCH
    try:
        epoch = int(raw)
    except ValueError:
        epoch = -1
    if not MIN_EPOCH <= epoch <= MAX_EPOCH:
        raise SystemExit(
            f"error: SOURCE_DATE_EPOCH={raw!r} must be an integer from {MIN_EPOCH} to"
            f" {MAX_EPOCH} (1980-01-01 to 2107-12-31, the zip timestamp range)"
        )
    return epoch


def build(output: Path, *, include_extras: bool = False, root: Path = ROOT) -> Path:
    epoch = source_epoch()
    yaml_dir = yaml_text = None
    if include_extras:
        yaml_dir = find_yaml()
        if yaml_dir is None:
            raise SystemExit("error: --include-extras needs PyYAML installed to vendor it")
        yaml_text = yaml_license()
        if yaml_text is None:
            raise SystemExit("error: cannot find the PyYAML licence text to bundle")
    files: dict[str, bytes] = {}
    collect_tree(root / "src" / "minideblib", "minideblib", files)
    files["COPYING"] = (root / "COPYING").read_bytes()  # GPL text
    files["__main__.py"] = MAIN.encode()
    if yaml_dir is not None and yaml_text is not None:
        collect_tree(yaml_dir, "yaml", files)
        files["yaml/LICENSE"] = yaml_text.encode()
    write_archive(output, files, epoch)
    return output


def main(argv: Sequence[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Build the minideb zipapp (dist/minideb.pyz).")
    p.add_argument("-o", "--output", type=Path, default=ROOT / "dist" / "minideb.pyz")
    p.add_argument(
        "--include-extras",
        action="store_true",
        help="vendor pure-Python PyYAML from the build environment (for DEP-11 commands)",
    )
    args = p.parse_args(argv)
    out = build(args.output, include_extras=args.include_extras)
    print(f"built {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
