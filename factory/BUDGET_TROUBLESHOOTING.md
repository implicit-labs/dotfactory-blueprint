# Budget troubleshooting

Budget limits gate the next stage or dispatch; they are not a hard cap on an in-flight call.
Counts are provider-reported tokens, not dollars, subscription
credits, or remaining account allowance.

- Project usage covers the project's lifetime in the same ledger, including
  completed and canceled executions. It survives coordinator restart.
- Execution usage covers every stage and retry in one execution.
- Missing final usage is unknown, never zero. Do not delete the ledger to reset
  usage.

Replace `/ABSOLUTE/CHECKOUT`, `/ABSOLUTE/INSTANCE/factory.json`, and
`PROJECT_KEY` below. Keep credentials outside the configuration and commands.

## `limit_reached`

1. Inspect the latest budget receipt, blocked scope, `used`, and `limit`:
   ```bash
   PYTHONPATH=/ABSOLUTE/CHECKOUT/factory/src python3 -m dotfactory status --config /ABSOLUTE/INSTANCE/factory.json --project PROJECT_KEY
   ```
2. Request a graceful drain so the active stage can finish without another
   dispatch:
   ```bash
   PYTHONPATH=/ABSOLUTE/CHECKOUT/factory/src python3 -m dotfactory operator drain --config /ABSOLUTE/INSTANCE/factory.json --project PROJECT_KEY
   ```
3. After reviewing accumulated usage, deliberately raise the reached limit or remove that limit.
   Removing it means accepting loss of that protection.
4. Save a reviewable copy, edit the config, and inspect the exact change:
   ```bash
   cp /ABSOLUTE/INSTANCE/factory.json /ABSOLUTE/INSTANCE/factory.json.before-budget-edit
   ${EDITOR:-vi} /ABSOLUTE/INSTANCE/factory.json
   diff -u /ABSOLUTE/INSTANCE/factory.json.before-budget-edit /ABSOLUTE/INSTANCE/factory.json
   ```
5. Restart using one applicable launch method from [Continuous work](CONTINUOUS_WORK.md#start-inspect-drain-restart).

## `usage_unavailable`

1. Run the same `status` command and identify the blocked scope and unknown-run
   count. Investigate affected executions using their trace evidence; the
   aggregate budget receipt does not identify individual runs.
2. Run the same `operator drain` command before changing configuration.
3. Investigate whether you can recover final usage/result evidence. There is
   no automatic accounting-repair command. Missing final usage is never zero;
   raising the numeric limit alone does not bypass `usage_unavailable`.
4. If accounting cannot be recovered, removing the relevant limit is an
   explicit decision to run without that protection. Never imply an account
   balance from partial token data.
5. Use the copy, editor, and `diff -u` commands above to review any config
   change, then restart with one method below.

## Restart

Use the same ledger and choose the launch method that owns this instance.

Direct coordinator:

```bash
PYTHONPATH=/ABSOLUTE/CHECKOUT/factory/src python3 -m dotfactory work --config /ABSOLUTE/INSTANCE/factory.json --project PROJECT_KEY
```

Linux user service, after the graceful drain has exited:

```bash
systemctl --user start dotfactory
systemctl --user status dotfactory
```

Re-run `status` after restart. A historical receipt is not proof that the
coordinator is live; for a service, also inspect `systemctl --user status`.
