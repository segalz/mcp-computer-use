# Codex Desktop Test Fixtures

Test fixtures representing the Codex desktop application (Windows process/app name `ChatGPT`).

## Origin & Classification

### Real (Anonymized) Captures
- **`home_snapshot.txt`**: Derived from a real capture (`plan/raw-captures/codex_snapshot_1.txt`) taken via `agent-cu snapshot -i -c --human`.
- **`home_text.txt`**: Derived from a real capture (`plan/raw-captures/codex_text_1.txt`) taken via `agent-cu text`.

#### Anonymization & Trimming
- Real repository and project identifiers in the sidebar were replaced with placeholders (`project-alpha`, `project-beta`, `project-gamma`).
- Chat titles (in English and Hebrew) were replaced with neutral developer tasks (e.g., `Refactor parser`, `Fix login test`, `בדיקת בנייה`).
- Paths, user directories, and personal metadata were scrubbed.
- The snapshot structure (indentation, node types, element IDs like `radix-_r_*`, roles) was preserved, while the sidebar lists were trimmed to remain under 200 lines while retaining navigation, projects, recents, and the main composer area.

### Synthetic Thread-View Captures
The thread-view fixtures simulate conversations inside a project workspace (`project-alpha`, session `Refactor parser`) and maintain the exact accessibility hierarchy (`window "ChatGPT"` -> web area -> sidebar navigation -> main thread -> window control buttons):

- **`working_snapshot.txt` / `working_text.txt`**:
  - State: Agent active.
  - Characteristics: `Thinking…` status, partial assistant output, `button "Stop"` present, no completion message.
- **`finished_snapshot.txt` / `finished_text.txt`**:
  - State: Agent completed.
  - Characteristics: No stop button, final message summarising file changes ("Changed 3 files …"), explicit "Next step: run the integration tests", and `button "Copy"` / feedback buttons.
- **`waiting_snapshot.txt` / `waiting_text.txt`**:
  - State: Agent waiting for user approval.
  - Characteristics: Prompt (`Allow command?`), command preview (`npm test`), and explicit action buttons (`button "Approve"`, `button "Deny"`).
- **`failure_snapshot.txt` / `failure_text.txt`**:
  - State: Agent completed with failure.
  - Characteristics: Test failure counts (`2 failed, 10 passed`), error log line (`Error: build failed while compiling ...`), and recommended next step.
