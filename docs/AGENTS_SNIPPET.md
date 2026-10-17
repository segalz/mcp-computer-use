## On-screen checks (mcp-computer-use)
To see or check what is on this computer's screen (e.g. "is Claude finished?", "what does Codex show?"),
use the `computer-use` MCP tool `look_at_screen` instead of running agent-cu or screenshots yourself.
- Write `question` in English, even if I write to you in Hebrew or another language.
- One short, focused question per call (under 300 chars). Pass `app` when known ("Claude", "ChatGPT").
- For bigger checks go step by step: first "Is Claude working or finished?", then "What is Claude's last message about?", then any detail.
- Optional: pass `client="codex"` (your name) and a `session_id` you choose for the task (e.g. "check-claude"), and reuse both on every call of that task: answers get shorter and more accurate and `changed_since_last` shows what changed. A new session_id replaces the old one. Without them each call is independent (full re-read, no "what changed").
- Use `details` (project, session, branch, next_step, failures, files) from the result directly.
- It is read-only: never ask it to click or type.
- On `error`, follow `error.suggestion` (OLLAMA_UNREACHABLE → call `health()`; MODEL_TIMEOUT → retry once; QUESTION_TOO_LONG → split). If a `hint` is returned, apply it to the next call.
