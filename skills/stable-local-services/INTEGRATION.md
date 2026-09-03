# Integration

| Requirement | Value |
|---|---|
| Portless | `0.15.6` |
| Node | `>=24` |
| Preflight | `dotfactory-portless-preflight` |
| Network | loopback `.localhost` only |

Put Node 24 or newer first on `PATH`. `scripts/install.sh` verifies Node before
it installs the pinned npm package, then copies the preflight into
`DOTFACTORY_BIN_DIR`, defaulting to `$HOME/.local/bin`.

Run `portless trust` and `portless service install` once. These remain explicit
because they change the OS trust store and install a privileged service. The
JSON preflight reports them as remediation until `portless doctor` has zero
failures and zero warnings.

Factory capability configuration supplies a logical service name and an argv
array. The runtime invokes Portless from the execution worktree so Portless
adds its native worktree prefix.
