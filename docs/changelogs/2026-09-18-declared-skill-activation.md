# Declared skill activation

Resolve workflow-declared skill packages before launch, bind their content hashes
to preparation, and present them through Codex, Claude Code, and OMP adapters.
Missing or changed packages fail loudly. Durable receipts bind the presented
protocol payload; they do not prove the agent followed the skill.

Captured subprocess tests cover presentation and failure paths without provider
credentials. Live provider behavior requires separate authenticated validation.
