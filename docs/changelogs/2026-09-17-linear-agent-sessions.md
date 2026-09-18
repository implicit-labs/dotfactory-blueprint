# Optional Linear Agent Sessions

- Add opt-in native Agent Sessions with sparse workflow, attention, error and
  terminal activities while keeping the local ledger authoritative.
- Persist session and activity identities before network writes, reconcile
  unknown outcomes by identity and fall back to the owned summary comment when
  the preview API or app actor is unavailable.
- Add a credential-separated canary, receipt-listener deployment contract and
  secret-safe Render status, suspend and resume commands.
- Preserve activity order across retries and restarts, and expose projection
  status through the observation API.

Offline recovery, fallback, migration, configuration, deployment and control
tests cover the reusable contract. Native provider behavior, hosted receipt
durability and current hosting costs remain separate live verification gates.
