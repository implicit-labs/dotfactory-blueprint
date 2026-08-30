# Integration

| Requirement | Value |
|---|---|
| Portless | `0.15.6` |
| Node | `>=24` |
| Preflight | `dotfactory-portless-preflight` |
| Network | loopback `.localhost` only |

`scripts/install.sh` installs the pinned npm package and copies the preflight
into `DOTFACTORY_BIN_DIR`, defaulting to `$HOME/.local/bin`.

Factory capability configuration supplies a logical service name and an argv
array. The runtime invokes Portless from the execution worktree so Portless
adds its native worktree prefix.
