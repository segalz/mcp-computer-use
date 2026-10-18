## On-screen checks and actions (mcp-computer-use)
To see what is on screen or interact with app windows (e.g. "is Claude finished?", "click 7 in Calculator"), use the `computer-use` MCP tools (`look_at_screen`, `click`, `type_text`) instead of running agent-cu directly.

### Screen reading (`look_at_screen`)
- Write `question` in English, even if the user speaks another language.
- One short, focused question per call (under 300 chars). Pass `app` when known ("Claude", "ChatGPT", "מחשבון").
- For bigger checks go step by step: first "Is Claude working or finished?", then "What is Claude's last message about?", then details.
- Optional: pass `client="codex"` and a `session_id` (e.g. "task-1") to get context across calls and `changed_since_last`.
- Use `details` (project, session, branch, next_step, failures, files) directly from the result.
- Read first with `look_at_screen` if unsure about the current screen or target labels.
- Request `include_image=true` only when the text answer is not enough.

### App actions (`click`, `type_text`)
- **Click by visible label**: Pass the visible UI label (1–200 chars), never an `@eN` ref; element refs change on every snapshot.
- **Batches**: Prefer one click per call when exploring. Use `steps` for a known sequence of up to 10 labels (e.g. `["7", "+", "5", "="]`); execution stops at the first failure.
- **Text entry (`type_text`)**: Sets the value of an editable field directly without submitting (no keystrokes sent to other windows; Enter is never pressed).
- **Safety**: A default blocklist guards dangerous actions (Delete, Send, Pay, מחק, שלח, etc.). If an action returns `DANGEROUS_ACTION_NEEDS_CONFIRMATION`, **never set `confirm_dangerous=true` without explicit human approval in chat**.
- **CLI fallback**: Do not use `agent-cu` directly to click or type unless the tool fails.

### General
- To launch an app, run `powershell -ExecutionPolicy Bypass -File C:\Develop\mcp-computer-use\scripts\cu-open.ps1 <exe|path|uri>` instead of `agent-cu open`, then use the returned `app` window title (e.g. `מחשבון`).
- Before a task, call `ping()` to verify the bridge. When done, call `release_model()` to free RAM.
- Follow `error.suggestion` on errors. Apply any returned `hint` on your next call.
