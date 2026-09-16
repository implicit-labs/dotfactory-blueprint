Inspect the committed change against .factory/plan.md, the issue requirements,
and the latest applicable human feedback. Report discrepancies with preferred_label "failed" and an explanatory outcome.
Keep scope fixed. Ensure .factory/delivery.json describes actual limitations.
Do not modify .factory/verify.py. Commit any report changes and leave a clean tree.
Return preferred_label "complete", an outcome summarizing what happened, and kind verification and local file evidence including
.factory/delivery.json. The factory runs the approved planning revision's verification script
against an isolated export of committed HEAD. Only its passing receipt allows
Review. Do not contact Linear, publish, or merge; the operator exports the packet.

If an approved check needs changing, stop and explain why; do not weaken it.
