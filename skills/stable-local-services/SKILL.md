---
name: stable-local-services
description: Install and preflight the pinned Portless host capability used by dotfactory.
---

# Stable local services

Use Portless 0.15.6 with Node 24 or newer to expose development services on
stable loopback-only `.localhost` URLs.

## Install

```bash
skills/stable-local-services/scripts/install.sh
```

## Preflight

```bash
dotfactory-portless-preflight
```

The preflight is non-interactive and returns JSON. A nonzero exit means the
factory must request attention; it must not fall back to a raw port.

Run the guarded live fixture only on a configured host:

```bash
DOTFACTORY_PORTLESS_LIVE=1 skills/stable-local-services/test.sh
```

## Guardrails

- Do not use `--force`.
- Do not enable LAN, tunnels, wildcard routes, custom TLDs, or custom trust.
- Let Portless derive the linked-worktree prefix.
- Stop only a child whose in-memory ownership handle still matches.
