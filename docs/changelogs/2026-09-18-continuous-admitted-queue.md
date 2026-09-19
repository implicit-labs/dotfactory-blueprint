# Continuous admitted queue

- Require explicit configuration and an admission label for continuous discovery.
- Exclude test/demo labels, unfinished blockers, invalid intent and previously executed issues; paginate, prioritize and recheck immediately before adoption.
- Service existing work before admission; expose durable queue and budget decisions through the existing status interface.
- Gate preparation on optional execution/project lifetime provider-token limits. Missing required usage blocks dispatch; final usage is not a hard spending cap.
- Document a Linux user service without installing or activating it automatically.
- Normalize tracker observation identity independently of query selection sets.
- Include concrete admission examples and budget troubleshooting; explain aggregate unknown counts and the absence of automatic accounting repair.

See [ADR-0036](../decisions/0036-admit-continuous-work-explicitly.md) and the [operator guide](../../factory/CONTINUOUS_WORK.md).
