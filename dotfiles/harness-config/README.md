# Harness configuration

| Harness | Portable source | Install target |
|---|---|---|
| OMP | `omp/config.yml.template` + `omp/profiles.tsv` | `~/.omp/agent/config.yml`, `~/.omp/profiles/<name>/agent/config.yml` |
| Claude Code | `claude-code/settings.json` | `~/.claude/settings.json` |
| Codex | `codex/config.toml` | `~/.codex/config.toml` |

These files own portable harness behavior. Credentials, OAuth state, MCP tokens,
session history, per-project trust, and machine-specific hooks stay outside the
repository.

## OMP parity

OMP must expose the Claude Code-like capabilities made explicit in
`omp/config.yml.template`:

- web search
- web fetch with automatic provider selection
- computer use
- explicit approval policy
- asynchronous work and background commands
- the required MCP surface in `omp/mcp-required.tsv`

The default and `omp-api` profiles share one template. Only approval policy
differs: `write` for the default profile, `yolo` for `omp-api`.

## Ownership

- Shell identity routing stays canonical in `../.zshrc.template`.
- OMP usage and authentication guidance stays canonical in `../USE_OMP.md`.
- Harness-native settings live here.
- Do not copy settings between harness folders. Express equivalent behavior in
  each harness's native config.

Run `test.sh` to validate all three configs offline.
