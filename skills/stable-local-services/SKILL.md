---
name: stable-local-services
description: Install and preflight the pinned Portless host capability used by dotfactory.
---

# Stable local services

Use Portless 0.15.6 with Node 24 or newer to expose development services on
stable loopback-only `.localhost` URLs.

## Install

Put Node 24 or newer first on `PATH`, then run:

```bash
skills/stable-local-services/scripts/install.sh
```

The installer checks Node before changing the global npm installation. First
use requires two explicit host actions:

```bash
portless trust
portless service install
dotfactory-portless-preflight
```

Trust and service installation stay operator-owned because they change the OS
trust store and install a privileged background service.

## Preflight

```bash
dotfactory-portless-preflight
```

The preflight is non-interactive and returns JSON checks plus remediation. It
requires Portless doctor to report zero failures and zero warnings. A nonzero
exit means the factory must request attention; it must not fall back to a raw
port.

Run the guarded live fixture only on a configured host:

```bash
DOTFACTORY_PORTLESS_LIVE=1 skills/stable-local-services/test.sh
```

## Guardrails

- Do not use `--force`.
- Do not enable LAN, tunnels, wildcard routes, custom TLDs, or custom trust.
- Let Portless derive the linked-worktree prefix.
- Stop only a child whose in-memory ownership handle still matches.
