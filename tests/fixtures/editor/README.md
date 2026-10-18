# Editor profile fixtures

## Real, anonymized captures

- antigravity_trust_snapshot.txt: full Antigravity IDE accessibility tree, from plan/raw-captures/ide_snapshot_full.txt.
- antigravity_trust_interactive.txt: interactive-only tree, from plan/raw-captures/ide_snapshot_i.txt. It contains window controls but does not expose the trust dialog or editor content.
- antigravity_trust_text.txt: flattened text, from plan/raw-captures/ide_text.txt.

These captures show project-alpha with src/parser.py open. The full tree exposes a workspace-trust dialog and a Python-extension recommendation. The agent panel says it is currently loading; this is not evidence of a running task.

Private workspace roots were replaced with C:\work\project-alpha (or c:/work/project-alpha for forward-slash paths), retaining suffixes such as src/parser.py. Application-resource paths were replaced with c:/work/tools/antigravity/workbench.html. The temporary parent-directory label was replaced with work. Roles, indentation, refs, IDs, control ordering, and the interactive capture's limited structure are preserved. No private username or original user path is retained.

## Synthetic fixtures

- vscode_problems_snapshot.txt: based on the anonymized full tree, with a VS Code title, trusted workspace, Problems panel containing "2 errors, 1 warning" and parser diagnostics, and terminal output "FAILED tests/test_parser.py::test_unicode". Trust/onboarding/extension prompts are removed.
- vscode_clean_text.txt: a small VS Code text view following the same title/Explorer/panel conventions, with "No Problems" and terminal output "3 passed".

Synthetic fixtures are parser cases, not evidence that a live VS Code Problems/terminal capture was validated.
