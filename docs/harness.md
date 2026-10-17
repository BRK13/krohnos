# The harness

Most agent failures are environment failures, not reasoning failures: the agent did not know which files mattered,
lost a decision from the last session, claimed success without running the checks, retried the same thing after a
partial failure, or had permission it should not have had. Krohnos is built around fixing the environment.

| Principle | How Krohnos does it |
|---|---|
| Task contract | Every task states the outcome, the scope, what must not change, the proof of done and what needs human approval. |
| Map, not manual | Each repo has a short rules file and a docs index; agents pull details when the task needs them. |
| Done requires evidence | No "done" without a receipt the environment can prove: merged, deployed, checked. |
| Verification attacks the result | A second model (Codex) reviews every PR; tests and CI gate the merge. |
| Policy outside the loop | Money, third-party messages, destructive migrations and secrets always escalate to the owner. |
| Recovery by failure class | Waiting, blocked on a human, broken, or dead session: each has its own action and budget. |
| Instructions become infrastructure | A rule that mattered twice becomes a test, a hook or a filter. |
| Every failure upgrades the harness | Incidents are logged with the part of the system that changed. |
| Harnesses decay too | Every safeguard is measured: which failure it prevents and whether it still happens. |
