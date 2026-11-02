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
- `minideb mcp`: a local MCP server that lets AI agents query repositories and download
  verified packages (see [Using minideb with AI agents](#using-minideb-with-ai-agents)).
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

`Fetcher.download(url, fileobj, max_bytes=..., expected_size=..., expected_hashes=...)`
streams a file to disk in chunks and checks its size and hashes on the way (raising
`HashMismatch`; the caller discards the output then); it retries only before the first
byte is written. `RepoClient.plan_binary(suite, name)` and `plan_source(suite, name)` turn a
package into `DownloadPlan`s of `FileSpec(name, url, size, hashes)` from the indices alone
(one plan per architecture, one for an `Architecture: all` package, which every
architecture filter includes; the `.dsc` first), and
`group_packages(records)` groups records per version with their architectures and
`Multi-Arch` value. `search()` takes optional `components=`, `architectures=` and
`kind="source"`. `client.problems` keeps the newest 1000 messages
(`problems_mark()`/`problems_since(mark)` give those of one call), and parsed indices are
kept within an estimated `cache_budget_bytes` (default 512 MiB, least recently used dropped
first).

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
| `12_mcp_client.py`                | a stdlib client talking to `minideb mcp`                         |

## Using minideb with AI agents

`minideb mcp` is a local [Model Context Protocol](https://modelcontextprotocol.io/) server
for AI agents. The agent's host program starts it as a child process and talks to it over
stdin and stdout (JSON-RPC 2.0, one message per line); there is no network listener. It
adds no parsing of its own: every tool is a thin layer over the library, so an agent can
point at a repository, ask which versions of the packages matching a name exist there,
read their full metadata, download sources or binaries with verified hashes, and inspect
local `.deb` and `.dsc` files. It needs only the standard library and runs on POSIX
systems (Linux, macOS; macOS is not exercised in CI): it relies on directory file
descriptors and `O_NOFOLLOW`, and refuses to start elsewhere.

Start it with `minideb mcp`, `python3 -m minideblib mcp`, `uvx --from minideblib minideb mcp`
or `python3 minideb.pyz mcp`. Most MCP hosts take a configuration in this generic shape
(the file name and place depend on the host):

```json
{
  "mcpServers": {
    "minideb": {
      "command": "minideb",
      "args": ["mcp", "--download-dir", "/home/user/debs", "--allow-host", "deb.debian.org"],
      "env": {
        "https_proxy": "http://proxy.example.org:3128",
        "no_proxy": "localhost,127.0.0.1,.example.org"
      }
    }
  }
}
```

Without an installed `minideb` use `"command": "uvx"` with `"args": ["--from", "minideblib",
"minideb", "mcp", ...]`. Give the server repositories with `--url` (repeatable) or
`--sources PATH`; they are named `repo1`, `repo2`, ... in the order given and listed by
`repository_overview`. Unlike the other commands, `mcp` never reads `/etc/apt` on its own:
with no repository configured, the agent names a repository URL (`https://...` or
`file:///...`) in each call. A client written in Python is in
[`examples/12_mcp_client.py`](examples/12_mcp_client.py).

### Tools

- `repository_overview`: server version, configured repositories, proxy settings
  (credentials redacted), the download directories and whether they are writable; for a
  repository, its suites, components, architectures and what each suite offers.
- `search_packages`: packages of a suite whose name or short description contains a text,
  or whose name matches a glob (`*`, `?`), with all versions, paged.
- `package_versions`: all versions of one package across suites, newest first by Debian
  ordering.
- `package_info`: full metadata of a binary package, with per-architecture variants.
- `source_info`: the same for a source package, optionally with the hash-checked `.dsc`.
- `find_file`: which packages ship a path, from the `Contents` indices of one component.
- `appstream_components`: DEP-11 AppStream components (needs PyYAML on the server).
- `download_package`: download a `.deb`, or a `.dsc` with every file it lists, into the
  download directory (absent with `--no-download`).
- `inspect_deb`: control fields, file list and one text member of a local `.deb`.
- `inspect_dsc`: parsed fields and files of a local `.dsc`, optionally verified against the
  files next to it.

Results are JSON (also as `structuredContent` for protocol `2025-06-18` and later). Tool
errors come back as results with `isError` so the model can react. The server speaks the
protocol versions `2025-11-25`, `2025-06-18`, `2025-03-26` and `2024-11-05`, and rejects
JSON-RPC batches.

### Security model

- Not root: the server refuses to run as root. If the real, effective or saved user id is 0 it exits
  with status 2 (rootless containers that map to uid 0 are refused as well). There is no
  override.
- It writes in two places only: the base download directory (`--download-dir`, default the
  directory it was started in) and a private session directory under the temporary
  directory (mode 0700, created when first used, **not** removed at exit; its path is in
  the results). It never overwrites a file and never follows a symlink while writing. An
  explicit `--download-dir` must exist and be writable at start; the default directory is
  checked when a download is requested, because many hosts start servers in an unwritable
  directory. Leftover `.minideb-` temporary files of crashed runs older than an hour are
  removed from those directories.
- Every downloaded file is checked against a SHA256-or-stronger hash taken from the
  repository metadata, and a failed check leaves no file behind (subdirectories created
  for a download that fails completely are removed again when empty). `--no-verify` only skips
  the index checks, never the check of downloads (the server prints a warning at start).
  The `Release` signature is not verified, so the metadata is unauthenticated: use
  `https://` mirrors.
- Everything a repository says (descriptions, changelogs, file names) is untrusted data.
  The server never acts on it, strips control characters, truncates long fields, and tells
  the model not to follow instructions found there. File names come only from validated
  base names, so a hostile `Filename` cannot leave the download directory.
- `file://` mirrors are only read, never written. Paths taken from the metadata stay below
  the given base (no `..`), but symlinks inside a mirror are followed, wherever they point.
  `inspect_deb` and `inspect_dsc` read only local regular files that the user can
  open and return only the parsed structure and, for `read_file`, the text of one member;
  they cannot list directories (whether a file exists can still be probed).
- Network use follows the library: `http`, `https` and `file` only, no credentials in URLs,
  no redirect from https to http. `--allow-host HOST` (repeatable, `*.example.org`
  allowed) restricts repositories to those hosts and `file://`; link-local addresses and
  cloud metadata names are refused unless `--allow-link-local` is given. Proxies come from
  `http_proxy`, `https_proxy` and `no_proxy`; an `https_proxy` of the form `http://...`
  sends proxy credentials unencrypted, and `no_proxy` has no CIDR support.
- `--no-download` makes a read-only server without `download_package`.
- stdout carries only protocol messages; logs go to stderr with control characters
  escaped and URL credentials removed.

### Limits and defaults

| setting | default |
|---|---|
| tool result (`--max-response-bytes`) | 200000 bytes, at least 4096; results hold their data twice for newer protocol versions, so about a third is usable |
| download total per call (`--max-download-bytes`) | 1 GiB |
| time per call (`--call-timeout`) / per download (`--download-timeout`) | 120 s / 900 s, at most 7 days |
| search and file lists | 50 entries per page, at most 500 (`limit`, `offset`) |
| local `.deb` / `.dsc` | 512 MiB / 8 MiB; at most 256 MiB unpacked and 200000 entries; `read_file` 64 KiB by default, 1 MiB at most |
| input line | 4 MiB; an over-long line gets an error and the session goes on |
| repository index | 256 MiB downloaded (refused before the download when `Release` lists it larger), 512 MiB decompressed |
| `find_file` | Contents indices within an estimated 1 GiB parsed per call (and 80 % of the call time); later architectures are left out with `partial` |
| cache | 8 repositories, parsed indices within 512 MiB |

`--timeout` and `--retries` set the network timeout and retries as for the other commands.
At EOF on stdin, SIGTERM or SIGINT the server cancels running calls (they get no response),
waits up to 5 seconds and exits with status 0; a second signal exits at once. This holds
once the server is running: a signal that arrives while it is still starting gets the
default action. The exit status is 2 when it cannot start.

### Architectures

Architecture is optional everywhere. `search_packages`, `package_versions`, `package_info`
and `source_info` take an `architectures` list (default: all the suite offers) and group
results: one entry per name, version and component with an `architectures` list, so an
`Architecture: all` package listed in every per-architecture index appears once. Fields
that differ per architecture (size, file name, hashes, dependencies) come in `variants`.
`find_file` needs a `component` and without `architectures` searches one architecture at
a time within a byte and time budget, marking the result `partial` when it stops early.
`download_package` without `architectures` downloads the only candidate and fails with
the list of the available architectures when there are several.

### Troubleshooting

- Nothing but protocol messages may reach stdout. If the host reports a parse error, check
  that a wrapper script or a shell profile does not write to stdout.
- To see what the server does, add `-vv` to the arguments (`"args": ["mcp", "-vv"]`); the
  log goes to stderr, which most hosts show in their MCP log.
- A host may start the server in an unwritable current directory such as `/`: pass
  `--download-dir`. Behind a proxy, set the proxy variables in `env`; a missing variable is
  the usual cause of timeouts, and `repository_overview` shows the effective settings.
- Exit status 2 at start: running as root, an unusable `--download-dir`, a bad option or an
  unsupported platform; the reason is on stderr.

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
