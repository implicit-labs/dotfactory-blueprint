---
module: factory delivery
symptom: "planned checks must be Python files under tests/ or .factory/"
root_cause: "root-only test prefixes conflict with nested repository ownership"
solved_date: 2026-09-13
tags: [planning, verification, monorepo]
---

# Nested planning checks rejected

a local planning run committed `factory/tests/test_doctor.py`; the host rejected that declared
check before parsing it. Accept canonical nested `tests/` components, retaining
containment, tracked-file, syntax, changed-file, and approval checks.

Read-only revalidation then exposed an independent missing parenthesis in the
generated test. Fixing path policy is not proof that the entire plan is valid.
Keep the original failed receipt immutable and validate each later gate before
claiming PlanReview. No proposed checks are executed during this validation.

Regression coverage belongs in `factory/tests/test_verified_delivery.py`;
[ADR-0026](../../decisions/0026-accept-nested-verification-tests.md) records policy.
