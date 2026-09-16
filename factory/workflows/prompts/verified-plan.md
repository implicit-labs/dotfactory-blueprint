Plan the issue and its verification before implementation.
Commit .factory/plan.md with requirements, design, risks, and acceptance coverage.
Create .factory/verification.json with schema_version 1 and criteria: each has a
unique id, requirement, and kind. Automated criteria list files (Python checks
under a tests/ directory at any repository depth, such as factory/tests/, or
root .factory/); manual criteria state a concrete procedure. Use canonical
repository-relative paths: no traversal, URI/percent encoding, or symlinks.
Write .factory/verify.py, taking the exported source directory as argv[1], to run
those acceptance checks and relevant regression tests. Declare every test/helper
it relies on in the criteria files lists. Do not implement production code.
Example criterion: {"id":"blank-name","requirement":"Reject blank names",
"kind":"automated","files":[".factory/verify.py","tests/test_greeting.py"]}.
Checks can fail on the current implementation. The host validates their structure
without executing proposed checks. Before handing off, parse each proposed Python
file with ast.parse (do not import or execute it) and repair syntax errors.
Autoplanning continues automatically after host validation; its accepted handoff
freezes the exact plan commit and declared files. Manual Planning stops at
PlanReview for human approval of that exact commit. Implementation is a separate
runner stage; do not implement within this planning attempt.
Return preferred_label "complete", a descriptive outcome, and committed local
file evidence. Do not contact Linear, publish, or merge.
