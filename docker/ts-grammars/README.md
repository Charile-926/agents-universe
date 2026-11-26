# Vendored tree-sitter grammars

Pre-built Linux x86_64 grammar libraries baked into the Docker image
(`COPY docker/ts-grammars /opt/ts-grammars`, read at runtime through
`TREE_SITTER_GRAMMAR_CACHE`) so the repo knowledge graph can parse code in a
container that has no network access. The language pack itself downloads
grammars lazily on first use, which would silently fail offline.

## Provenance

| Item | Value |
| --- | --- |
| Upstream project | [tree-sitter-language-pack](https://github.com/xberg-io/tree-sitter-language-pack) (MIT; every bundled grammar is MIT / Apache-2.0 / BSD / ISC — no copyleft) |
| Pinned version | `1.14.3` — must match `packages/agent-core/pyproject.toml` (`tree-sitter-language-pack==1.14.3`, ABI-coupled to `tree-sitter>=0.26,<0.27`) |
| Asset | `parsers-linux-x86_64.tar.zst` from the `v1.14.3` GitHub release |
| Integrity | Tarball sha256 checked against the release manifest (`parsers.json`); per-file sha256 of what is committed is in `SHA256SUMS` |
| Refresher | `python scripts/fetch_ts_grammars.py` (see below) |

Only the grammars registered in `packages/agent-core/src/agent_core/knowledge/graph/languages.py`
are kept: python, typescript, tsx, javascript, vue, java, csharp. `jsx` is
parsed with the javascript grammar (the pack ships none).

## Verifying / refreshing

```bash
# Verify the committed binaries
cd docker/ts-grammars && sha256sum -c SHA256SUMS

# Re-download (official GitHub release first, public mirrors as fallback)
python scripts/fetch_ts_grammars.py
```

When the pinned pack version changes, re-run the fetch script in the same
commit and review the resulting `SHA256SUMS` diff — the binaries are
intentionally committed (see the `check-added-large-files` exclusion in
`.pre-commit-config.yaml`), so a provenance or hash change must be visible.
