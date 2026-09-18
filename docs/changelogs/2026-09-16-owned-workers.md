# Owned worker execution

- Add optional stage placement, exact Git handoffs, worker-local native authentication, and centrally accepted results.
- Add a manual Render worker image and setup; retain SSH Mac verification and support a colocated coordinator on the same persistent disk.
- Live provider, subscription, cancellation and restart canaries remain required.
- Include the full runtime, instance initialization and explicit persistent cloud startup; suspended compute cannot discover new work.
- Validate local worker attempt roots, including symlink targets, alongside the durable coordinator paths.

## Verified delivery repair

Keep worker Git receipts in the handoff ledger, separate from file-only delivery
evidence. Regression coverage exercises planning, implementation and verification
contracts across separate worker directories, followed by restart replay.

- Add explicitly declared local/cloud placement labels to worker summaries; preserve historical labels and keep transport independent. Agent Sessions show one stable placement activity per attempt.
