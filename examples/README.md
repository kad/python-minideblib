# minideblib examples

Small, runnable scripts showing the public API of `minideblib`. Each one is 30 to 60
lines, fully annotated and has a `main(argv) -> int` entry point.

All network examples (01, 02, 03, 09, 10, 11, 12) take `URL SUITE [COMPONENT]` arguments
(example 03 takes `URL SUITE [PATH]`) and default to Debian `http://deb.debian.org/debian`
(example 12: `https://deb.debian.org/debian`), suite `stable`, component `contrib`,
architecture `amd64`: a small index that downloads
quickly (use `main` for the full one). Ubuntu works as well, for example
`http://archive.ubuntu.com/ubuntu noble multiverse`. Examples that need a `.deb`
(05, 06) accept a path and otherwise build a tiny sample package with the helper in
`examples/_sample.py`.

The sample output below was captured from real runs against the live Debian archive
on 2026-11-01 (trimmed; archive contents change over time). Examples 04 to 08 are offline. Example 12 starts `minideb mcp` as a child process.

## 01 browse a repository

Suites, components, architectures and the first packages of a repository; `--sources FILE`
uses the entries of a `sources.list` or deb822 `.sources` file instead.

```
uv run python examples/01_browse_repository.py [URL SUITE [COMPONENT]] [--sources FILE]
```

`suites()` probes a short list of well-known suite names (no directory listing is needed) and
raises `FetchError` when the repository is unreachable; the script reports that as an error.

```
suites:        stable, stable-updates, stable-backports, testing
components:    main, contrib, non-free-firmware, non-free
architectures: all, amd64, arm64, armel, armhf, i386, ppc64el, riscv64, s390x
  1oom 1.11.2-1 [contrib/amd64] Master of Orion engine
  alex4 1.1-10+b2 [contrib/amd64] Alex the Allegator 4 - a retro platform game
```

## 02 search and versions

Substring and glob search, and all versions of a package across suites in Debian version
order. `--also SUITE` (repeatable) adds suites, for example `--also oldstable`; `--name`
picks the package. The client is scoped with `RepoClient.from_url(url, suites=...,
components=[COMPONENT], architectures=["amd64"])`, so only one `Packages` index per suite is
downloaded, and `versions(name, suites=...)` skips suite probing.

```
uv run python examples/02_search_and_versions.py [URL SUITE [COMPONENT]] [--name NAME] [--also SUITE]
```

```
search '1oom': 1 hits, e.g. ['1oom']
search '1oo*': 1 hits, e.g. ['1oom']
1oom:
  oldstable        1.0-2  contrib/amd64
  stable           1.11.2-1  contrib/amd64
ordered: 1.0-2 < 1.11.2-1
```

## 03 find the package that owns a file

Looks a path (or base name) up in the Contents indices and prints `no package found for PATH`
to stderr (exit status 1) when nothing matches. Contents files are the largest indices of an
archive: Debian has one per component (`main/Contents-amd64.gz` is about 12 MB compressed,
`contrib` well under 1 MB), Ubuntu a single archive-wide `Contents-amd64.gz` of about 50 MB.
Without `-c` every component is searched; restrict it with `-c` (repeatable), which passes
`components=[...]` to `RepoClient.find_files`.

```
uv run python examples/03_find_file_owner.py [URL SUITE [PATH]] [-c COMPONENT]
```

```
$ uv run python examples/03_find_file_owner.py -c contrib
etc/GeoIP.conf is shipped by contrib/net/geoipupdate
```

## 04 sources files

Loads a deb822 `.sources` file and a one-line `sources.list` (written to a temporary
directory, or the files you pass) and lists the entries.

```
uv run python examples/04_sources_files.py [FILE ...]
```

```
deb822 debian.sources: 4 entries
  deb      http://deb.debian.org/debian stable main/contrib arch=any
  deb-src  http://deb.debian.org/debian stable main/contrib arch=any
one-line ubuntu.list: 2 entries
  deb      http://archive.ubuntu.com/ubuntu noble multiverse arch=amd64
```

## 05 inspect a .deb

A `dpkg-deb -I` / `-c` equivalent: control fields, file list and md5sums.

```
uv run python examples/05_inspect_deb.py [PATH.deb]
```

```
format 2.0, control members: ['control']
Package: minideb-sample
Version: 1.0-1
f 0644       21 usr/bin/minideb-sample
```

## 06 extract a .deb safely

Extracts with a size limit and shows how a `../` member and an over-limit extraction are
rejected with `DebError` before anything is written.

```
uv run python examples/06_extract_deb_safely.py [PATH.deb]
```

```
extracted minideb-sample to a temporary directory:
   usr/bin/minideb-sample
rejected (unsafe path): unsafe member path: '../escape.txt'
rejected (size limit): extracted size exceeds 1 bytes
escape.txt written outside: False
```

## 07 compare and sort versions

Epochs, tildes and revisions with `Version` and `compare`.

```
uv run python examples/07_compare_versions.py [VERSION ...]
```

```
sorted: 1.0~rc1 < 1.0 < 1.0+b1 < 1:0.9
newest: 1:0.9
  epoch=1 upstream='0.9' revision=''
1.0 == 0:1.0 ? True
```

## 08 changelog and control

Parses a changelog (bugs, `LP:` keywords) and a control paragraph, using `get_list` to
unfold the wrapped `Binary` field.

```
uv run python examples/08_changelog_and_control.py [CHANGELOG [CONTROL]]
```

```
sample-pkg 1.0-2 2026-11-01 by Jane Doe <jane@example.com>
  bugs closed: ['123456', '654321']  LP: ['42']
  Binary (folded list): ['sample-pkg', 'sample-pkg-doc', 'sample-pkg-dev']
```

## 09 AppStream

Lists DEP-11 components. Needs the `dep11` extra (PyYAML); without it the script prints the
`MissingYamlError` message with the install hint and exits with status 2.

```
uv run python examples/09_appstream.py [URL SUITE [COMPONENT]]
```

```
47 AppStream components in stable/contrib
  com.corsixth.corsixth (desktop-application) package=corsix-th: CorsixTH - Open source clone of Theme Hospital
  fonts-ibm-plex (font) package=fonts-ibm-plex: IBM Plex - extensive typeface family designed by IBM
```

## 10 custom fetcher and error handling

`Fetcher(timeout, retries, user_agent)`, `FetchError` (with `.status`), `HashMismatch`
and the non-fatal `client.problems` list. It requests a missing file on purpose.

```
uv run python examples/10_custom_fetcher_and_errors.py [URL SUITE [COMPONENT]]
```

```
303 packages in stable/contrib
expected FetchError: status=404 transient=False
problems: none
```

## 11 source package (.dsc)

Fetches the `.dsc` of the first source package with `RepoClient.fetch_dsc` (checked against
the `Sources` index) and summarises it; `--verify` downloads the listed files and checks them
with `Dsc.verify`.

```
uv run python examples/11_source_package_dsc.py [URL SUITE [COMPONENT]] [--verify]
```

```
1oom 1.11.2-1 (3.0 (quilt)) maintained by Debian Games Team <pkg-games-devel@lists.alioth.debian.org>
  binaries: 1oom; native: False
  file 1oom_1.11.2.orig.tar.gz (561880 bytes) ['md5', 'sha1', 'sha256']
verified 2 files
```

## 12 MCP client

A stdlib-only client for the MCP server (`minideb mcp`, see "Using minideb with AI agents" in
the main README). It starts the server with `--no-download` through `sys.executable`,
sends `initialize`, the `notifications/initialized` notification and `tools/list`, then
calls `repository_overview` and `search_packages` for the repository; `--pattern` is a
substring or glob of package names. Messages are one
JSON object per line; a tool result is JSON text in `content[0].text`. The output below is
shortened (the overview also reports the proxy settings of your environment).

```
uv run python examples/12_mcp_client.py [URL SUITE [COMPONENT]] [--pattern TEXT]
```

```
tools: repository_overview, search_packages, package_versions, package_info, source_info, ...
repository_overview: {"repository":"https://deb.debian.org/debian","server":{"name":"minideb","version":"0.7.0"},...
search_packages: {"repository":"https://deb.debian.org/debian","suite":"stable","kind":"binary","total":44,...
```

## Running the tests

```
uv run pytest tests/test_examples.py          # offline, against file:// fixtures
MINIDEB_NETWORK=1 uv run pytest tests/test_examples.py   # also runs 01, 02 and 12 live
```
