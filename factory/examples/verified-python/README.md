# Verified Python fixture

Copy this directory into a disposable Git repository and commit the seed.
Add `/.worktrees/` to `.gitignore` if the pool is inside that checkout.

Issue description:

> Implement greet(name) in greeting.py. Trim surrounding whitespace and return
> Hello, <name>! . Reject empty or whitespace-only names with ValueError.

No verification script is supplied. Planning must create the acceptance criteria,
Python checks, and manual procedures from the issue. Autoplanning continues
automatically after host validation; manual Planning stops at PlanReview for
human approval of the exact proposed commit.

Suggested implementation review revision:

> Add the module docstring "Greeting utilities." without changing behavior.

The approved behavior checks remain frozen. Check the requested docstring during
human review; changing acceptance checks requires a new planning approval.
See `docs/VERIFIED-DELIVERY.md` in the factory repository.
