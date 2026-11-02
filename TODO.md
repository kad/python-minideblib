# TODO

- GPG verification of Release/InRelease signatures, as a fresh design (the removed `gpg` module is not a base): keyring option and a `debian_keyrings` helper to find the distribution keyrings.
- DEP-11 `dep11-index` support (alternative metadata layout).
- HTTP transport for MCP (streamable HTTP, with authentication) next to the stdio server of `minideb mcp`.
- MCP resources: expose repository indices and package metadata as resources, besides the tools.
