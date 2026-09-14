Implement the issue and the latest applicable review feedback from the immutable
handoff. Read the approved .factory/plan.md and .factory/verification.json.
Read the approved checks. Never modify the plan, verification definition, verifier, or declared test/helper files.
Make the actual source change. Write .factory/delivery.json containing a nonempty
"summary" string and a "limitations" list of strings. State what was not proven.
Commit all deliverables on the prepared branch, including plan and delivery.json.
Return preferred_label "complete", an outcome summarizing what happened, and local file evidence (relative URIs) for the changed
source and report. The factory independently verifies later; your success claim
is not proof that checks passed. Do not contact Linear, publish, or merge.

If an approved check needs changing, stop and explain why; do not weaken it.
