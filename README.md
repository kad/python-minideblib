# minideblib

Python Mini Debian Library by Alexander D. Kanevskiy <kad@bifh.org>

`minideblib` is a small, dependency-free Python library and command line tool (`minideb`) for
reading Debian archive metadata and `.deb` / `.dsc` files. It browses APT repositories over
HTTP(S) or `file://`, reads the package indices, and inspects packages without `dpkg`, `apt`
or root. It is not a package manager: it installs nothing, resolves no dependencies and does
not verify the OpenPGP signature of a repository (see [Trust](#trust)).

## Features

- Release-driven repository browsing: nothing depends on directory listings. Suites are
  discovered by probing well-known names, and suite names containing `/` work.
- `deb822` `.sources` files and one-line `sources.list` entries.
- `Acquire-By-Hash` and flat repositories; a `Valid-Until` in the past is reported as a
  warning.
- `Packages`, `Sources` and `Contents` indices, plus DEP-11 AppStream metadata and icon
  tarballs, checked against the hashes in `Release`.
- Compression: gzip, bzip2, xz and lzma from the standard library; zstd natively on
  Python 3.14 and later (`compression.zstd`), otherwise with the optional `zstd` extra.
  Decompressed output is capped (`max_size`, 1 GiB by default), and the xz/lzma decoder
  memory is capped at `max_size` clamped to 128 MiB .. 1 GiB.
- Pure Python `.deb` reader and safe extractor, `ar`, control files, changelogs,
  `.dsc` files and Debian version comparison. The `minideb deb` commands are a subset of
  `dpkg-deb` (`-I`, `-e`, `-c`, `-x`, `-f`).
- Typed (`py.typed`) and tested offline against generated `file://` repositories.

## Requirements

Python 3.11 or later. No runtime dependencies. Optional extras:

| extra   | installs    | enables                                                        |
|---------|-------------|----------------------------------------------------------------|
| `zstd`  | `zstandard` | `.zst` indices and `.deb` members (not needed on Python 3.14+) |
| `dep11` | `pyyaml`    | `minideb appstream` (`minideb icons` works without it)         |

## Install

From a checkout:

```
uv tool install .                             # installs the minideb command
uvx --from . minideb --help                   # run without installing
uv tool install '.[zstd,dep11]'               # with the extras
make pyz && python3 dist/minideb.pyz --help   # one self-contained file
```

Debian and Ubuntu packages are built from `debian/` (`make deb`) and an RPM spec is provided
(`make rpm`); see [Packaging](#packaging).

## Command line tour

The sample output below was captured from real runs against the Debian and Ubuntu archives
on 2026-11-01 and trimmed (`...`). Archive contents change over time. Scope large downloads
with `-c` (component) and `-a` (architecture); both are global options and may be repeated.

```
$ minideb --url http://deb.debian.org/debian suites
stable
stable-updates
stable-backports
testing

$ minideb --url http://deb.debian.org/debian components stable
main
contrib
non-free-firmware
non-free

$ minideb --url http://deb.debian.org/debian list stable -c contrib -a amd64
1oom 1.11.2-1 amd64 contrib
alex4 1.1-10+b2 amd64 contrib
alien-arena 7.71.7+dfsg-1 amd64 contrib
...

$ minideb --url http://deb.debian.org/debian show stable 1oom -c contrib -a amd64
Package: 1oom
Version: 1.11.2-1
Installed-Size: 2008
Maintainer: Debian Games Team <pkg-games-devel@lists.alioth.debian.org>
Architecture: amd64
...

$ minideb --url http://deb.debian.org/debian versions 1oom -c contrib -a amd64
stable 1oom 1.11.2-1 amd64 contrib
testing 1oom 1.11.8-1 amd64 contrib

$ minideb --url http://deb.debian.org/debian search stable -c contrib -a amd64 '1oo*'
1oom 1.11.2-1 amd64 contrib

$ minideb --url http://deb.debian.org/debian files stable -c contrib -a amd64 etc/GeoIP.conf
etc/GeoIP.conf  contrib/net/geoipupdate

$ minideb --url http://archive.ubuntu.com/ubuntu list noble -c multiverse -a amd64
1oom 1.8.0-1build2 amd64 multiverse
7zip-rar 23.01-4 amd64 multiverse
abs-guide 10-4 all multiverse
...
```

`Contents` indices are big (Ubuntu has one archive-wide file of about 50 MB), so always scope
`files` with `-c` and `-a`. A Contents file is parsed into memory as a whole, using about 3.8
times its uncompressed size; Ubuntu's archive-wide file needs several GB of RAM, so prefer a
small component where you can.

Commands have short aliases: `su` (suites), `comps` (components), `arch` (archs), `ls` (list),
`sh` (show), `ver` (versions), `find` (search), `contents` (files), `as` (appstream) and
`src` (source). `minideb --help` lists everything with more examples.

### Source packages and `.dsc` files

```
$ minideb --url http://deb.debian.org/debian source stable hello
Source: hello
Version: 2.10-5
Format: 3.0 (quilt)
Binary: hello
Architecture: any
Maintainer: Santiago Vila <sanvila@debian.org>
...

$ minideb dsc files hello_2.10-5.dsc
hello_2.10.orig.tar.gz 725946 md5:6cd0ffea3884a4e79330338dcc2987d6 sha1:f7bebf6f... sha256:31e06613...
...

$ minideb dsc verify hello_2.10-5.dsc        # the listed files are next to the .dsc
verified 3 files
```

`source` downloads the `.dsc` named by the `Sources` index and checks its hash. `dsc verify`
exits with status 2 on a missing file, a wrong size or a wrong hash.

### Inspecting `.deb` files

```
$ minideb deb info hello_2.10-5_amd64.deb
Package: hello
Version: 2.10-5
Architecture: amd64
Maintainer: Santiago Vila <sanvila@debian.org>
...

$ minideb deb ls hello_2.10-5_amd64.deb
drwxr-xr-x root/root 0 2025-04-16 09:50 ./
drwxr-xr-x root/root 0 2025-04-16 09:50 ./usr/
drwxr-xr-x root/root 0 2025-04-16 09:50 ./usr/bin/
-rwxr-xr-x root/root 35544 2025-04-16 09:50 ./usr/bin/hello
...

$ minideb deb field hello_2.10-5_amd64.deb Package Version
Package: hello
Version: 2.10-5

$ minideb deb extract hello_2.10-5_amd64.deb out
```

`deb extract` refuses paths and links that would leave the target directory. That includes
symlinks with an absolute target, which real packages often ship (for example
`usr/lib/ssl/certs -> /etc/ssl/certs`): by default such a package is refused and nothing is
extracted. `--skip-absolute-symlinks` extracts everything else and leaves those links out,
with one warning per skipped link; other unsafe members are still refused.
`deb ctrl` and `deb extract` (and `DebPackage.extract()`) refuse a target directory that is
itself a symlink, and `deb ctrl` never writes through a symlink (or a hard link) it finds below
the target; existing regular files are replaced. Only the last component of the target is
checked: symlinks in the directories leading to it are followed and missing ones are created,
because that path is chosen by the caller, not by the package. A local user who can write to
a parent directory could swap the target after the check, so extract into a private directory.

### AppStream

```
$ minideb --url http://deb.debian.org/debian appstream stable -c contrib -a amd64 --search font
fonts-ibm-plex  fonts-ibm-plex  IBM Plex  extensive typeface family designed by IBM

$ minideb --url http://deb.debian.org/debian icons stable -c contrib
contrib/dep11/icons-48x48.tar.gz  48x48  60139
contrib/dep11/icons-48x48.tar  48x48  96768
contrib/dep11/icons-64x64.tar.gz  64x64  131889
contrib/dep11/icons-64x64.tar  64x64  176640
```

`appstream` needs the `dep11` extra (PyYAML); `icons` only reads `Release` and works without
it.

### JSON, logging and probing

- `-j` / `--json` writes one JSON document to stdout instead of text, for scripting.
- `-v` logs the steps to stderr; `-vv` adds every request and detail with elapsed times;
  `-q` hides warnings. Logs never go to stdout.
- `suites` finds suites by probing a list of well-known names. `--probe
  {all,debian,ubuntu,none}` picks the candidates and `--suite NAME` adds one:

```
$ minideb --url http://deb.debian.org/debian -v suites --probe debian
minideb: probing 4 candidate suites on http://deb.debian.org/debian
minideb: found suite stable
minideb: found suite stable-updates
minideb: found suite stable-backports
minideb: found suite testing
minideb: probed 4 suites: 4 found, 0 absent, 0 errors
```

- Without `--url`, `minideb` reads `/etc/apt/sources.list` and `sources.list.d`
  (or `--sources PATH`).
- Exit status: 0 success, 1 nothing found, 2 usage, network or data error. "Nothing found"
  (1, nothing on stdout, also with `--json`) is: `show`, `files`, `versions`, `source` and
  `suites` with an empty result, `appstream --id`/`--package` matching nothing, and
  `deb field` with a missing field (without `--json` the fields found are still printed).
  `list` and `search` exit with 0 on an empty result.
- The own scope options of `list` (`-c`, `-a`), `files` (`-a`), `appstream` (`-c`, `-a`) and
  `icons` (`-c`) are repeatable and replace the global `-c`/`-a` for that command.

## Library

```python
from minideblib.deb import DebPackage
from minideblib.dsc import Dsc
from minideblib.repo.client import RepoClient
from minideblib.version import Version

client = RepoClient.from_url(
    "http://deb.debian.org/debian",
    suites=["stable"],
    components=["contrib"],
    architectures=["amd64"],
)
for rec in client.search("stable", "1oo*"):
    print(rec.name, rec.version, rec.component, rec.arch)  # 1oom 1.11.2-1 contrib amd64

deb = DebPackage.open("hello_2.10-5_amd64.deb")
print(deb.control["Package"], deb.control["Version"])  # hello 2.10-5

print(Version("1.0~rc1") < Version("1.0") < Version("1:0.9"))  # True

dsc = Dsc.load("hello_2.10-5.dsc")
print(dsc.source, dsc.version, [f.name for f in dsc.files])
dsc.verify(".")  # raises DscError on a missing or modified file
```

`Version` parses permissively, as dpkg does: an upstream version that does not start with a
digit (`a1`) is accepted; pass `strict=True` (also to `compare`) to reject it with
`VersionError`.

Every exception the library raises for bad data (malformed control data, indices, Release,
changelogs, sources files, `.deb`/`.dsc` files or versions, failed downloads and hash
checks) derives from `minideblib.MinidebError`; the concrete classes such as `FetchError`,
`HashMismatch`, `DebError`, `DscError`, `ControlError`, `ChangelogError`, `SourcesError`
or `RepoError` keep their other bases, e.g. `ValueError`. Not covered: programmer errors
(wrong argument types, an unknown `probe` set or index kind), `OSError` from local file
access, `KeyError` for a missing `Paragraph` field, and errors of caller-supplied objects.
`DebPackage.extract()` refuses symlinks with an absolute target unless called with
`skip_absolute_symlinks=True`, which leaves them out (logged as warnings) and returns the
skipped `(path, target)` pairs.

The snippet was run as written. Runnable, annotated scripts are in [`examples/`](examples/)
(with sample output in [`examples/README.md`](examples/README.md)); run one with
`uv run python examples/01_browse_repository.py`.

| example                           | shows                                                            |
|-----------------------------------|------------------------------------------------------------------|
| `01_browse_repository.py`         | suites, components, architectures and packages of a repository   |
| `02_search_and_versions.py`       | name and glob search, versions of a package in Debian order      |
| `03_find_file_owner.py`           | which package ships a file (`Contents`)                          |
| `04_sources_files.py`             | deb822 `.sources` and one-line `sources.list` parsing            |
| `05_inspect_deb.py`               | control fields, file list and md5sums of a `.deb`                |
| `06_extract_deb_safely.py`        | extraction with path and size limits                             |
| `07_compare_versions.py`          | `Version` ordering with epochs, tildes and revisions             |
| `08_changelog_and_control.py`     | changelog and control file parsing                               |
| `09_appstream.py`                 | DEP-11 AppStream components                                      |
| `10_custom_fetcher_and_errors.py` | `Fetcher` options, `FetchError`, non-fatal `problems`            |
| `11_source_package_dsc.py`        | fetching and verifying a source package `.dsc`                   |

## Trust

The `Release` / `InRelease` OpenPGP signature is not verified. Only the file hashes listed in
`Release` are checked (indices, and the `.dsc` named in `Sources`), so repository metadata is
only as trustworthy as the transport. Use `https://` mirrors, or verify with `apt` or `gpgv`
where it matters.

- A SHA256 or stronger hash is required, as in `apt`: an index listed in `Release` with only
  `MD5Sum`/`SHA1`, or a `.dsc` listed in `Sources` with only `Checksums-Sha1`/`Files`, is
  rejected (exit status 2). `--no-verify` (`verify=False`) skips all hash checks.
- A redirect from `https://` to `http://` is refused, as is a redirect to any scheme other
  than http(s).
- Link-local addresses (`169.254.0.0/16`, `fe80::/10`), the metadata service addresses
  `fd00:ec2::254` and `100.100.100.200` and the cloud metadata host names `metadata`,
  `metadata.google.internal` and `instance-data` are refused, also as redirect targets and
  when a host name resolves to such an address (`Fetcher(block_link_local=False)` allows
  them); loopback and private addresses stay allowed. `Fetcher(allowed_hosts=[...])` limits
  http(s) requests and redirects to the listed hosts (`*.example.org` matches sub-domains).
  Through a proxy from `http_proxy`/`https_proxy` the proxy resolves host names, so only the
  URL text is checked (literal link-local and metadata URLs are still refused); the proxy is
  trusted configuration.
- Credentials in URLs (`http://user:password@host/`) are rejected; they would not be used
  and could leak to a proxy. `file://` URLs must not contain `..` components (also
  percent-encoded).
- Paths taken from repository metadata (`Release` entries, `Filename`, `Directory`, `.dsc`
  names) and suite names must stay below the repository URL: `..`, a leading `/`, `\` and NUL
  are refused also when percent-encoded, and so are encoded separators (`%2f`, `%5c`, `%00`).

## Development

```
make help                               # list the targets
make sync                               # uv sync --all-extras
make check                              # ruff, ty and pytest (offline)
make pyz                                # dist/minideb.pyz
MINIDEB_NETWORK=1 make test-network     # live tests
```

The live tests default to Debian `stable`, `contrib`, `amd64`; point them elsewhere with
`MINIDEB_TEST_URL`, `MINIDEB_TEST_SUITE`, `MINIDEB_TEST_COMPONENT` and `MINIDEB_TEST_ARCH`
(see `tests/conftest.py`). The offline suite builds `file://` repositories with
`tests/fixtures/repo.py`.

### Release process

`CHANGES` keeps an `Unreleased` section at the top: add release notes there as changes land,
at the latest before step 1 (the helper warns when that section is empty).

1. `make release-prepare TAG=vX.Y.Z` (`TAG=vX.Y.Z-rc1` for a pre-release; add `DRY_RUN=1`
   to print the diff without writing anything) sets the version in `pyproject.toml`,
   `src/minideblib/__init__.py`, `debian/changelog` (a new `UNRELEASED` entry listing the
   commit subjects since the previous `v*` tag), the header of `debian/minideb.1`,
   `python-minideblib.spec` (`Version:`, `Release: 1`, a `%changelog` entry), `CHANGES` (the
   `Unreleased` section becomes `minideblib X.Y.Z (date)` and a new empty one is added) and
   the `minideblib` entry of `uv.lock`. It refuses a dirty tree, an existing tag, a version not
   greater than the current one and files that do not look as expected, and changes either
   every file or none. Pre-release versions are normalised as for `release-check` (PEP 440
   `X.Y.Zrc1`, Debian and RPM `X.Y.Z~rc1`).
2. Review and edit `CHANGES` and `debian/changelog`. The Debian distribution stays
   `UNRELEASED`; set it (for example with `dch -r`) when uploading to Debian.
3. `make release-check TAG=vX.Y.Z` checks that `pyproject.toml`, `src/minideblib/__init__.py`
   and `debian/changelog` agree with the tag. The man page header and the `Version:` of the
   spec are covered by `tests/test_man_version.py`.
4. Commit (`release: vX.Y.Z`) and create the annotated tag `vX.Y.Z`, or let step 1 do it with
   `COMMIT=1 TAG_IT=1` (`SIGN=1` signs the tag). Nothing is pushed.
5. `git push origin master vX.Y.Z`. The workflow `.github/workflows/release.yml` re-checks the
   tag, runs the tests, builds the wheel, sdist and `minideb.pyz`, publishes a GitHub release
   with checksums and, when enabled, uploads to PyPI.

### Packaging

- Debian and Ubuntu: native packaging in `debian/` (`make deb`; needs `dpkg-dev`,
  `debhelper`, `dh-python` and `pybuild-plugin-pyproject`).
- Fedora and openSUSE: `python-minideblib.spec` (`make rpm` builds from a `git archive`
  tarball). It installs the `minideb` command and its man page.

## Licence

GPL-2.0-only, see [`COPYING`](COPYING) and `debian/copyright`. All modules are independent
implementations; see the acknowledgements below.

Bugs and ideas: <https://github.com/kad/python-minideblib/issues>.

## Acknowledgements

Python Mini Debian Library by Alexander D. Kanevskiy <kad@bifh.org>

The early versions of this library were inspired by the work of the people
behind mini-dinstall (<https://tracker.debian.org/pkg/mini-dinstall>) and the
Debian python tools, in particular Colin Walters, Adam Heath, Wichert
Akkerman, Steve Kowalik and Scott James Remnant; many thanks to them. The
current code is an independent implementation.
