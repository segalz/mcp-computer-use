# mcp-computer-use

**English** · [עברית](#עברית)

A small MCP server that lets coding agents (Codex, Claude Code, Cline, Antigravity…) ask
*"what does that window show?"* and get back a **two-line JSON answer** instead of thousands of tokens of
raw window text, as well as perform safe, app-scoped UI actions (`click`, `type_text`).

> Intended to be published publicly on GitHub (MIT). **Not published yet.**

## Why (token savings)
When an agent runs `agent-cu` itself, the whole accessibility tree / window text lands in its context and is
re-sent on every following step. In our test one "is Claude done?" check cost ~206k tokens (~47k context per
step, ~3% of a ChatGPT Plus 5-hour quota).

This server does the reading **locally**:
1. `agent-cu text -a <App>` reads the window (no model).
2. **Code** extracts only the status and the last message using specialized per-app extractor profiles
   (Claude Code, Codex desktop, editor, browser, or generic fallback). ~1.5 s.
3. A **local Ollama model** (default `gpt-oss:20b`) summarizes that short text. Measured 4–8 s, correct.
4. The agent receives ~50 tokens of JSON.

`qwen3:4b` was tested and kept emitting reasoning, so `gpt-oss:20b` is the default (configurable).

## Tools
| Tool | Description |
|---|---|
| `look_at_screen(question, app=None, client=None, session_id=None, keep_warm=False)` | Reads one window and answers. If `app` is omitted, the app named in the question (also Hebrew aliases like "קלוד") or the frontmost app is used. Read-only. `client` / `session_id` are optional (see *Sessions*). CLI: `--ask QUESTION [--app APP]`. |
| `click(app, target=None, steps=None, client=None, session_id=None, confirm_dangerous=False)` | Clicks a visible label, or a sequence of 1–10 labels via `steps`. Stopped on first failure. CLI: `--click APP TARGET`, `--click-steps APP STEP...`. |
| `type_text(app, target, text, client=None, session_id=None, confirm_dangerous=False)` | Sets the value of an editable field in the app without submitting. Replaces content; Enter is never pressed; no keystrokes sent to other windows. CLI: `--type APP TARGET TEXT`. |
| `ping()` | Heartbeat: server up, local Ollama reachable (auto-started if needed), model replies `pong`. No screen reading. Returns `{ok, server, ollama, model_reply_ok, model, seconds, cold, error?}`. The first ping can take 30–50 s when the model is cold. CLI: `--ping`. |
| `release_model()` | Unloads the model now and returns `freed_mb`. Call it at the end of a task. CLI: `--release`. |
| `health()` | Checks backend health: Ollama reachable, model pulled, model loaded, memory, free RAM, current keep_alive policy, agent-cu installed. CLI: `--health`. |

### When to read (`look_at_screen`) vs click/type
- **Read first**: Use `look_at_screen` to inspect window state, verify results, or when unsure what UI elements/labels are visible. It is completely read-only and side-effect free.
- **Visible labels, never refs**: Pass the visible UI label (1 to 200 characters), never an `@eN` reference. Accessibility element refs change on every snapshot; `@eN` targets are rejected with `INVALID_TARGET`.
- **Single action vs batches**: Prefer one click per call when exploring or verifying step by step. Use `steps` for a known sequence of 1 to 10 visible labels (e.g. calculator buttons `["7", "+", "5", "="]`). Every step gets a fresh snapshot and runs through the safety gate; execution stops at the first failure.
- **Direct text entry (`type_text`)**: Sets the value of an editable field (roles: `textfield`, `textarea`, `edit`, `combobox`, `textbox`, `document`, `searchfield`) directly in the target app. It replaces the existing content without submitting — Enter is never pressed, and no keystrokes are sent to other windows. Text length is limited by `max_type_chars` (default 2000).

### Label matching hierarchy & model resolution
The server resolves UI targets using a strict ranking hierarchy:
1. **Exact match**: Case-sensitive exact string equality with the UI element name (`tier 1`, score 1.0, `how: "exact"`).
2. **Normalized match**: Casefold, Unicode NFKC normalization, Hebrew niqqud and cantillation stripped (U+0591–U+05C7), maqaf (`־` U+05BE → space), Hebrew geresh (`׳` U+05F3) and gershayim (`״` U+05F4) converted to ASCII `'` and `"`, only the bidi/zero-width marks U+200B–U+200F, U+061C, U+202A–U+202E, U+2066–U+2069, U+FEFF removed (the safety blocklist check separately strips all category Cf format characters), and only a trailing `…`, `...` or `:` stripped (`tier 2`, score 0.95, `how: "normalized"`).
3. **Synonyms and IDs**: Digit and math operator synonyms in English and Hebrew (e.g. `"7"` ↔ `"seven"` ↔ `"שבע"`, `"+"` ↔ `"plus"` ↔ `"ועוד"`, `"-"` ↔ `"−"` (U+2212) ↔ `"minus"` ↔ `"פחות"`, `"*"` ↔ `"×"` ↔ `"multiply"` ↔ `"כפול"` ↔ `"הכפל ב"`, `"/"` ↔ `"÷"` ↔ `"divide"` ↔ `"חלקי"` ↔ `"חלק ב"`, `"="` ↔ `"equals"` ↔ `"שווה"`, `"."` ↔ `"decimal separator"` ↔ `"תו הפרדה עשרוני"`), or matching element accessibility `id` (`tier 3`, score 0.9). Note that `"-"` also matches `"−"` (U+2212). Role phrases (e.g. `button 7`, `כפתור 7`, `field Search`, `שדה חיפוש`) are split to disambiguate.
4. **Weak matches**: Live value matches, token contains, or fuzzy sequence similarity >= 0.80 (`tier 4-5`). Weak matches never auto-click by themselves; they return candidates, and when `resolve_with_model` is on the local model may choose one of them (≤5), which is then clicked after the safety check; otherwise up to 5 candidates are returned with `TARGET_AMBIGUOUS`.

### Safety rules & blocklist
To prevent unintended destructive actions, an action blocklist is always enforced before clicking or typing:
- **Default blocklist (always enforced)**:
  - **English dangerous actions**: `Delete`, `Remove`, `Send`, `Allow`, `Approve`, `Accept`, `Pay`, `Buy`, `Purchase`, `Submit`, `Confirm`, `Publish`, `Merge`, `Uninstall`, `Format`.
  - **Hebrew multi-word**: `הסר התקנה`.
  - **Hebrew base actions**: `מחק`, `הסר`, `שלח`, `אשר`, `אפשר`, `שלם`, `קנה`, `פרסם`.
  - **Hebrew plural imperatives**: `מחקו`, `הסירו`, `שלחו`, `אשרו`, `אפשרו`, `שלמו`, `קנו`, `פרסמו`.
  - **Hebrew inflected forms / verbal nouns / infinitives**: `מחיקה`, `למחוק`, `הסרה`, `להסיר`, `שליחה`, `לשלוח`, `אישור`, `לאשר`, `לאפשר`, `תשלום`, `לשלם`, `קנייה`, `לקנות`, `פרסום`, `לפרסם`.
- **Config & environment additions**: `[safety] blocklist` / `blocklist_extra` in TOML, or environment variables `MCP_CU_SAFETY_BLOCKLIST` / `MCP_CU_SAFETY_BLOCKLIST_EXTRA` only **ADD** words; default blocklist words can never be removed.
- **Evaluation**: The target label is checked first (before taking a snapshot), followed by the resolved element name.
- **Confirmation workflow**: If a blocked word matches and `confirm_dangerous=false` (the default), the action is blocked and returns `DANGEROUS_ACTION_NEEDS_CONFIRMATION` with `would_click` and `matched_rule`.
- **CRITICAL**: **The calling agent MUST ask the human user and get explicit approval before calling again with `confirm_dangerous=true`**.
- **Non-blocked defaults**: Common navigation actions like `"Close"` / `"Exit"` (and Hebrew `"סגור"` / `"יציאה"`) are **not blocked by default** (add them to the blocklist in TOML or env if wanted).

### Action audit log (`.sessions/actions.log`)
Every click and type action is recorded in an append-only JSON Lines file at `<project>/.sessions/actions.log` (or `MCP_CU_SESSIONS_DIR`):
- **Retention**: Records older than 24 hours (`action_log_ttl_hours`, default 24.0) are purged atomically during appends.
- **Concurrency**: Thread-safe and process-safe with file-level locking (`actions.log.lock`).
- **Whitelisted fields only**: `time`, `client`, `session_id`, `app`, `action`, `target`, `ref`, `name`, `role`, `result`, `confirmed`, `error_code`, `text_len`, `hint`.
- **Privacy & Scrubbing**: **Typed text is NEVER logged** — only its character length (`text_len`). Furthermore, automatic scrubbing inspects all logged string fields and replaces any value containing the typed text or fragments of 6+ characters with `<redacted>`.

### Error codes
| Error Code | Description |
|---|---|
| `TARGET_NOT_FOUND` | No matching UI target found in the window (returns up to 5 near candidate suggestions). |
| `TARGET_AMBIGUOUS` | Multiple candidates matched (returns up to 5 `candidates` to disambiguate). |
| `TARGET_NOT_EDITABLE` | The target was found but its role is not an editable field (`type_text`). |
| `INVALID_TARGET` | Target is empty, longer than 200 chars, or an `@eN` ref; passing both or neither of `target` and `steps`; invalid `steps` count (not 1–10) or invalid step label; or a non-string `text` in `type_text`. |
| `TEXT_TOO_LONG` | Typed text exceeds `max_type_chars` (default 2000 characters). |
| `DANGEROUS_ACTION_NEEDS_CONFIRMATION` | Target or element matched the safety blocklist. Requires explicit human user approval before setting `confirm_dangerous=true`. |
| `APP_NOT_FOUND` | App window not found, empty app name, or app name contains shell metacharacters. |
| `AGENT_CU_NOT_INSTALLED` | `agent-cu` binary not found on PATH. |
| `AGENT_CU_FAILED` | Underlying UI automation command failed. |
| `OLLAMA_UNREACHABLE` | Ollama service could not be reached or auto-started. |
| `MODEL_NOT_FOUND` | Configured model is not pulled in Ollama. |
| `MODEL_TIMEOUT` | Model inference timed out. |
| `MODEL_FAILED` | Model inference failed. |
| `QUESTION_TOO_LONG` | Question exceeds `max_question_chars` (default 300). |
| `EMPTY_QUESTION` | Question string is empty. |

### CLI options & security notice
The CLI supports running actions directly:
```powershell
# Click a single label
mcp-computer-use --click "מחשבון" "7"

# Run a sequence of steps (1 to 10 labels)
mcp-computer-use --click-steps "מחשבון" "7" "+" "5" "="

# Set text in an editable field
mcp-computer-use --type "מחשבון" "שדה הזנה" "123"

# Confirm a dangerous action after explicit human approval
mcp-computer-use --click "MyApp" "Delete" --confirm-dangerous
```
> [!WARNING]
> **Command-Line & Text Entry Security Notice**: Both CLI `--type` and the MCP `type_text` tool pass the text to `agent-cu` as an argument (`agent-cu type -s REF -a APP -- TEXT`), making the text visible to local processes while it runs (briefly, e.g. via Task Manager or process inspection). The CLI additionally exposes the text in the calling shell's command line and history. Do not type secrets or passwords with it.

### Calculator examples (MCP & CLI)
Packaged and UWP apps run inside `ApplicationFrameHost` where the app name is the localized window title (e.g. `Calculator` on English Windows or `מחשבון` on Hebrew Windows; launch with `cu-open.ps1 calc.exe`).

**MCP Tool Calls (JSON)**:
```json
// Single click
{
  "app": "מחשבון",
  "target": "7"
}

// Known sequence via steps (max 10)
{
  "app": "מחשבון",
  "steps": ["7", "+", "5", "="]
}

// Type into an editable field
{
  "app": "מחשבון",
  "target": "שדה הזנה",
  "text": "42"
}
```

**CLI Execution**:
```powershell
mcp-computer-use --click "מחשבון" "7"
mcp-computer-use --click-steps "מחשבון" "7" "+" "5" "="
```

### Per-app profiles
When `look_at_screen` reads a window, it selects a specialized extractor profile based on the application name (`app_name`) and window title (`window_title`). Profiles are evaluated in strict first-match-wins order:

$$\text{claude\_code} \longrightarrow \text{codex\_desktop} \longrightarrow \text{editor} \longrightarrow \text{browser} \longrightarrow \text{generic}$$

1. **`claude_code`**: Matched when `"claude"` is in the lower-cased app name.
2. **`codex_desktop`**: Matched when the app name is `chatgpt`, `codex`, `codex desktop`, or `openai.codex` (Windows app `ChatGPT`), or when the window title contains `codex`.
3. **`editor`**: Matched for VS Code-family editors by app name (`code`, `code.exe`, `code - oss`, `code-oss`, `code - insiders`, `cursor`, `antigravity ide`, `windsurf`, `vscodium`), or by window title patterns (` - Visual Studio Code`, ` - Cursor`, ` - Antigravity IDE - `, ` - Windsurf`, ` - VSCodium`, ` - Code - Insiders`).
4. **`browser`**: Matched for supported browsers by app name (`chrome`, `google chrome`, `msedge`, `edge`, `microsoft edge`, `brave`, `firefox`, `mozilla firefox`) or by window title browser suffixes (` - Google Chrome`, ` - Microsoft Edge`, `microsoft edge`, ` - Mozilla Firefox`, ` - Brave`).
5. **`generic`**: Default fallback when no specialized profile matches.

#### Output per profile (top-level status & details)
Every `look_at_screen` call returns a top-level `status` (`working`, `finished`, `waiting`, `error`, or `unknown`) alongside an optional `details` dictionary populated by code (from UI Automation text and accessibility snapshots, without a model; disable extra snapshot inspection with `MCP_CU_SNAPSHOT_DETAILS=0`):

- **Claude Code (`claude_code`)**:
  - **Top-level `status`**: `working`, `finished`, or `unknown`.
  - **`details` keys**: `project`, `session` (chat title), `branch`, `folder`, `model`, `files`, `background_tasks`, `context_used`, `waiting_for_user`, `waiting_prompt` (from approval buttons), `failures`, `next_step` (actionable suggestions in English and Hebrew), and `asks_user`.
- **Codex desktop (`codex_desktop`, Windows app `ChatGPT`)**:
  - **Top-level `status`**: `working`, `finished`, `waiting`, or `unknown` — determined **strictly from UI controls only** (Stop button, Copy + Good/Bad response buttons, approval dialogs; prose text inside assistant turns is never used as a status signal).
  - **`details` keys**:
    - `project`: Project or workspace name (from "What should we work on in...", "Change project: ...", or thread header folder/project controls).
    - `session`: Chat / thread title (from heading or thread header).
    - `mode`: Current mode (from "Switch mode, current mode: ...").
    - `projects`: Sidebar project list (up to 8, corroborated by project action controls).
    - `recent_chats`: Sidebar recent chat list (up to 5, corroborated by pin/archive controls).
    - `needs_attention`: Boolean `true` when the "View activity, needs attention" button is visible.
    - `next_step`: Extracted from "Next steps:" or "השלב הבא:".
    - `failures`: Error and failure lines detected via `find_failures`.
    - `waiting_prompt` (and `waiting_for_user=true`): Extracted prompt when waiting for user approval (e.g. "Allow command?", "Approve command?"), corroborated by paired approve/deny buttons.
- **Editor (`editor`, VS Code / Cursor / Antigravity IDE / Windsurf / VSCodium)**:
  - **Top-level `status`**: `waiting` if a prompt is pending, `error` if failures are detected, otherwise `unknown`.
  - **`details` keys**:
    - `project`: Workspace or project folder name (from window title or Explorer tree section).
    - `file`: Currently active file (from window title or single active editor tab).
    - `waiting_prompt` (and `waiting_for_user=true`): Real modal dialog prompts (workspace trust "Do you trust the authors...", extension installation, save confirmations), verified by accompanying dialog action buttons.
    - `failures`: Diagnostic errors (`Error: ...`, `Exception:`) and error counts from the Problems panel or last terminal run (older output superseded by new shell prompts or subsequent test summaries is automatically discarded).
    - `next_step`: Actionable next steps found in evidence lines.
- **Browser (`browser`, Chrome / Edge / Brave / Firefox)**:
  - **Top-level `status`**: `error` when a network error page is detected (e.g. DNS probe, connection refused, no internet); otherwise `unknown`.
  - **`details` keys**:
    - `page_title` (and `title`): Cleaned page title (browser suffixes and Edge profile segments stripped).
    - `url`: Current page URL from address bar or top text lines.
    - `tab`: Active tab label (memory usage tags stripped).
    - `headings`: Up to 5 key page headings extracted from the web content area (`RootWebArea`) or text.
    - `failures`: Failures and errors within web document content.
    - `next_step`: Actionable next steps found within document content.
    - Filters out browser chrome (toolbars, tab strip, address bar, favourites bar) so only web content is extracted.
- **Generic (`generic`)**:
  - **Top-level `status`**: Always `unknown`.
  - **`details` keys**: `failures` (detected from the last 30 lines of extracted tail text, if any).

#### The conservative rule
All extractors strictly adhere to a conservative principle: return `unknown` status and retain unclassified content rather than guessing or inferring state from ambiguous prose or unstructured text.

#### Limitations
- **Browser content visibility**: Browsers may expose no page content to UI Automation while the Windows session is locked or the browser window is minimised (accessibility tree only exposes top-level chrome or an empty document area).
- **Codex desktop thread-state rules**: Thread-state heuristics for Codex desktop were built and validated from synthetic samples and may need tuning against future desktop app updates.

### Sessions (optional context between calls)
`client` (e.g. `codex`) and `session_id` (chosen by the client, e.g. `fix-ui`) are **optional**; both are
sanitized to `[A-Za-z0-9_-]`, max 64 chars.
- **Without them** every call is independent: the full screen is re-read, "what changed" is not possible and
  no file is written. Repeated calls without a session get a short `hint` suggesting one.
- **With them** the server keeps one small JSON file per pair, `<project>/.sessions/{client}__{session_id}.json`
  (app, project, session, last_message, next_step, status, last 5 questions with short answers, updated_at).
  Each call reads the file, reads the screen, compares, answers only the question, returns `changed_since_last`
  and updates the file. The same question on an unchanged screen is answered from the file (`cached: true`, no model call).
- **A new `session_id` replaces the old one** for that client (the old file is deleted), so each client has one active file.
  A `session_id` without `client` uses client `default`.
- Files not updated for 24 h are deleted on every call (`MCP_CU_SESSION_TTL_HOURS`). Folder: `MCP_CU_SESSIONS_DIR`.
  No in-memory state is kept.

### Ollama auto-start
If `OLLAMA_URL` points at this machine (`127.0.0.1` / `localhost`) and Ollama does not answer, the server starts
`ollama serve` itself: detached, hidden window, with `OLLAMA_HOST=127.0.0.1:<port from OLLAMA_URL>` set **only for
the child process** (your global `OLLAMA_HOST`, e.g. a Mac, is never touched), then waits up to 10 s for `/api/tags`.
- Checked on every call **without** a session and on the **first** call of a session (new session file); not on
  every call inside a session. If a call inside a session fails with `OLLAMA_UNREACHABLE`, it starts Ollama once and retries once.
- A port check plus a lock file (`.sessions/ollama-autostart.lock`) prevent double starts.
- `ollama` is found on PATH or at `%LOCALAPPDATA%\Programs\Ollama\ollama.exe`.
- `MCP_CU_AUTOSTART_OLLAMA=0` disables it; `MCP_CU_AUTOSTART_TIMEOUT` (default 10). `health()` reports `ollama_autostarted`.

### Memory release (smart keep_alive)
- Single call, no active session: the model stays loaded **3 min** (`MCP_CU_KEEP_ALIVE_IDLE`).
- Active session (any `.sessions` file updated in the last 10 min) or `keep_warm=true`: **10 min**
  (`MCP_CU_KEEP_ALIVE_SESSION`), so the model is released 10 min after the last call.
- Memory pressure: free RAM is checked before and after each call; below **2048 MB** (`MCP_CU_MIN_FREE_MB`, 0 = off)
  the model is unloaded right after answering, even mid-session. A background thread in the server
  (`MCP_CU_MEMORY_WATCH=1`, every `MCP_CU_MEMORY_WATCH_INTERVAL`=30 s) does the same while the model is loaded.
- Callers should call **`release_model()` at the end of a task**. Each answer has a `memory` field
  (`keep_alive`, `reason`, `was_loaded`, `free_ram_mb`, `unloaded_low_memory`). CLI: `mcp-computer-use --release`.

Success:
```json
{"windows_seen": ["Claude"], "status": "finished",
 "summary": "Claude finished: 916 tests pass, commit be8b48e pushed, installer built.",
 "confidence": 0.95,
 "details": {"project": "MyApp-Desktop", "session": "Multimodal work in progress", "branch": "fix/pre-api",
             "next_step": "After installing, reopen QUAL.", "failures": ["Finished a background command (1 failed)"]},
 "changed_since_last": ["status: working -> finished", "new text in the last message"],
 "session": {"client": "codex", "session_id": "bench"},
 "model_used": "gpt-oss:20b", "duration_seconds": 6.1,
 "memory_mb": 11264.0, "tokens_per_second": 12.1}
```
Failure (the caller can still act on `extracted`):
```json
{"error": {"code": "MODEL_TIMEOUT", "message": "gpt-oss:20b did not answer within 18s",
           "retryable": true, "suggestion": "Retry once (the model may have been cold-loading) or use the raw `extracted` text."},
 "windows_seen": ["Claude"],
 "extracted": {"status": "finished", "last_message": "…", "extractor": "claude", "truncated": false},
 "model_used": "gpt-oss:20b", "timeout_seconds": 18.0, "duration_seconds": 19.6}
```

**Smart timeout:** `timeout_base + timeout_per_1k_chars × chars/1000`, clamped to `[timeout_min, timeout_max]`,
plus `cold_load_extra` when `/api/ps` shows the model is not loaded.

## Setup (Windows, PowerShell)
Requirements: Python 3.10+, Node + `npm i -g agent-cu`, Ollama.
```powershell
cd C:\Develop\mcp-computer-use
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
pytest -q
```
### Ollama
The default URL is `http://127.0.0.1:11435` (a local instance on a non-default port, because a global
`OLLAMA_HOST` may point to another machine).
```powershell
$env:OLLAMA_HOST = "127.0.0.1:11435"; ollama serve      # in its own window
$env:OLLAMA_HOST = "127.0.0.1:11435"; ollama pull gpt-oss:20b
mcp-computer-use --health                                 # check everything
mcp-computer-use --ask "Is Claude finished?" --app Claude # one-off test
```

## Configuration
Priority: defaults < TOML file < env vars. File: `mcp-computer-use.toml` in the working dir,
`%USERPROFILE%\.config\mcp-computer-use\config.toml`, or `MCP_CU_CONFIG=<path>` (see `mcp-computer-use.example.toml`).

| Env | Default | Description |
|---|---|---|
| `MCP_CU_OLLAMA_URL` / `OLLAMA_URL` | `http://127.0.0.1:11435` | Ollama server URL |
| `MCP_CU_MODEL` / `MODEL` | `gpt-oss:20b` | Model used for summarization / candidate picking |
| `MCP_CU_MAX_CHARS` | `2000` | Max chars extracted from window for model |
| `MCP_CU_MAX_QUESTION_CHARS` | `300` | Longer questions rejected with `QUESTION_TOO_LONG` |
| `MCP_CU_TIMEOUT_BASE` / `_PER_1K_CHARS` / `_MIN` / `_MAX` | `10` / `4` / `8` / `60` | Smart timeout bounds and scaling |
| `MCP_CU_COLD_LOAD_EXTRA` | `45` | Extra seconds added when model is cold |
| `MCP_CU_AGENT_CU_CMD` / `MCP_CU_AGENT_CU_TIMEOUT` | `agent-cu` / `20` | Command and timeout for UI automation |
| `MCP_CU_HEALTH_HTTP` / `_HOST` / `_PORT` | `false` / `127.0.0.1` / `8765` | Optional HTTP health check server |
| `MCP_CU_SYSTEM_PROMPT_FILE` | bundled `prompts/system_prompt.md` | Override default system prompt |
| `MCP_CU_SNAPSHOT_DETAILS` | `true` | Read accessibility tree for Claude details |
| `MCP_CU_SESSIONS_DIR` | `<project>/.sessions` | Sessions and action log folder |
| `MCP_CU_SESSION_TTL_HOURS` | `24` | Hours before inactive session files are purged |
| `MCP_CU_KEEP_ALIVE_IDLE` / `MCP_CU_KEEP_ALIVE_SESSION` | `3m` / `10m` | Model retention duration in memory |
| `MCP_CU_SESSION_ACTIVE_MINUTES` | `10` | Session recency window for keeping model alive |
| `MCP_CU_MIN_FREE_MB` | `2048` | Minimum free RAM in MB before unloading model |
| `MCP_CU_MEMORY_WATCH` / `MCP_CU_MEMORY_WATCH_INTERVAL` | `true` / `30` | Background watcher to unload model under RAM pressure |
| `MCP_CU_AUTOSTART_OLLAMA` / `MCP_CU_AUTOSTART_TIMEOUT` | `true` / `10` | Auto-start local Ollama if unreachable |
| `MCP_CU_SAFETY_BLOCKLIST` | `""` | Additional comma-separated blocklist words |
| `MCP_CU_SAFETY_BLOCKLIST_EXTRA` | `""` | Extra additional blocklist words |
| `MCP_CU_ACTION_LOG_TTL_HOURS` | `24` | Hours before old action audit records are purged |
| `MCP_CU_RESOLVE_WITH_MODEL` | `true` | Use local model to resolve ambiguous candidates (≤5) |
| `MCP_CU_MAX_TYPE_CHARS` | `2000` | Max characters allowed for `type_text` |

Optional HTTP health: `MCP_CU_HEALTH_HTTP=1` (or `--health-http`) → `GET http://127.0.0.1:8765/health`
(200 when ok, 503 otherwise).

## Register the server
**Codex** – `%USERPROFILE%\.codex\config.toml`:
```toml
[mcp_servers.computer-use]
command = 'C:\Develop\mcp-computer-use\.venv\Scripts\mcp-computer-use.exe'
args = []
env = { MCP_CU_OLLAMA_URL = "http://127.0.0.1:11435", MCP_CU_MODEL = "gpt-oss:20b" }
startup_timeout_sec = 20
tool_timeout_sec = 120
```
**Claude Code**:
```powershell
claude mcp add computer-use --scope user -e MCP_CU_OLLAMA_URL=http://127.0.0.1:11435 -- C:\Develop\mcp-computer-use\.venv\Scripts\mcp-computer-use.exe
```
Then add the AGENTS.md / CLAUDE.md snippet below so agents use it correctly.

## How agents should call this server
The server sends these rules to clients on initialize (MCP `instructions`) and in the tool descriptions:
- **English only** in `question` (translate the user's request), **one short focused question per call**
  (max 300 chars, `MCP_CU_MAX_QUESTION_CHARS`), pass `app` when known, **step by step** for bigger checks,
  and follow `error.suggestion`.
- **Read first**: Use `look_at_screen` if unsure of window state or target labels.
- **Click by visible label**: Pass visible UI labels (1–200 chars), never `@eN` refs.
- **Use `steps` for known sequences**: Up to 10 labels (e.g. calculator keys).
- **Safety**: Never set `confirm_dangerous=true` without explicit human approval in chat.
- **Do not use `agent-cu` directly** to click or type unless the MCP tool fails.
- Optionally pass `client` + `session_id` and reuse them for the whole task to get context and `changed_since_last`.
- Call `release_model()` when the task is done.
- Longer questions return `QUESTION_TOO_LONG`. Multi-part questions (3+ `?` or a numbered/bulleted list) and
  mostly non-Latin questions are still answered, with a non-fatal `hint`.

| Good | Bad |
|---|---|
| `Is Claude working or finished?` | `מה קלוד עושה?` (works, but returns `hint: Ask in English for best results.`) |
| `What is Claude's last message about?` | `Is Claude done? Did tests pass? What commit? Did it build?` |
| `click(app="מחשבון", target="7")` | `click(app="מחשבון", target="@e12")` (`@eN` refs change every snapshot) |
| `type_text(app="מחשבון", target="שדה הזנה", text="123")` | Sending keystrokes with Enter without human confirmation |

### Snippet for a global AGENTS.md / CLAUDE.md
Paste into `%USERPROFILE%\.codex\AGENTS.md` and/or `%USERPROFILE%\.claude\CLAUDE.md` (also in `docs/AGENTS_SNIPPET.md`):
```markdown
## On-screen checks and actions (mcp-computer-use)
To see what is on screen or interact with app windows (e.g. "is Claude finished?", "click 7 in Calculator"), use the `computer-use` MCP tools (`look_at_screen`, `click`, `type_text`) instead of running agent-cu directly.

### Screen reading (`look_at_screen`)
- Write `question` in English, even if the user speaks another language.
- One short, focused question per call (under 300 chars). Pass `app` when known ("Claude", "ChatGPT", "מחשבון").
- For bigger checks go step by step: first "Is Claude working or finished?", then "What is Claude's last message about?", then details.
- Optional: pass `client="codex"` and a `session_id` (e.g. "task-1") to get context across calls and `changed_since_last`.
- Use `details` (project, session, branch, next_step, failures, files) directly from the result.
- Read first with `look_at_screen` if unsure about the current screen or target labels.

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
```

## Launching apps: `scripts/cu-open.ps1`
`agent-cu open calc.exe --wait` starts Calculator but then times out: it waits for an app literally named
`calc.exe`, while UWP/packaged apps run inside `ApplicationFrameHost` with a localized title (e.g. `מחשבון`).
`cu-open.ps1` launches anything `Start-Process` can (exe name, full path, `shell:` / `ms-settings:` /
`calculator:` URIs), polls the open windows every 500 ms until a new window appears (or an existing
single-instance window comes to the front), checks that `agent-cu snapshot -a <app>` works, and prints one JSON line:
```powershell
powershell -ExecutionPolicy Bypass -File C:\Develop\mcp-computer-use\scripts\cu-open.ps1 calc.exe
# {"ok":true,"app":"מחשבון","title":"מחשבון","process":"ApplicationFrameHost","pid":16708,"seconds":1.8,"hint":"UWP app hosted by ApplicationFrameHost: use -a 'מחשבון' (the window title).","verified":true}
agent-cu snapshot -a 'מחשבון' -i -c --human
```
Options: `-Args '<arguments>'` (with `-File`, pass several arguments as one quoted string), `-TimeoutSec 15`,
`-NoVerify` (skip the snapshot check). On timeout it prints `ok:false` with `error`, `new_windows_seen` and
`frontmost`, and exits 1. Use the returned `app` with every `agent-cu ... -a`. It never closes or moves other windows.
Output is UTF-8 (Hebrew titles are kept).

## Usage examples
- `look_at_screen("Is Claude finished? What did it report?", "Claude")`
- `click("מחשבון", steps=["7", "+", "5", "="])`
- `type_text("מחשבון", "שדה הזנה", "100")`

## Development
`src/mcp_computer_use/`: `server.py` (MCP + CLI), `core.py` (logic), `agent_cu.py`, `matching.py`, `safety.py`,
`extractors/` (`registry.py`, `codex_desktop.py`, `browser.py`, `editor.py`, `claude_code.py`, `generic.py`),
`ollama_client.py`, `timeouts.py`, `config.py`, `errors.py`,
`guidance.py` (instructions + question validation), `prompts/system_prompt.md`. Tests mock subprocess and Ollama: `pytest -q`. Works with `mcp` 1.x (`FastMCP`)
and 2.x (`MCPServer`).

### Adding a profile
To add a new per-app extractor profile:
1. **Register the profile**: Register via `register_profile(name, matcher, extractor, snapshot_details)` in `src/mcp_computer_use/extractors/registry.py` (and secondary window-title matching in `select_profile` in `src/mcp_computer_use/extractors/__init__.py`).
2. **Add anonymized fixtures**: Place sanitized accessibility snapshot and text captures under `tests/fixtures/<profile>/` (scrub personal data, usernames, tokens, and local paths).
3. **Fixture tests**: Add at least 3 fixture tests (≥ 3 fixture tests) covering status detection, details extraction, and fallback/edge cases (e.g. `tests/test_extractor_<profile>.py`).

## License
MIT

---

<div dir="rtl">

# עברית

שרת MCP קטן לקריאת מסך ולביצוע פעולות בטוחות וממוקדות-חלון (`click`, `type_text`), שמאפשר לסוכני קוד (Codex, ‏Claude Code, ‏Cline, ‏Antigravity) לשאול "מה מופיע בחלון הזה?" ולקבל **תשובת JSON של שתי שורות** במקום אלפי טוקנים של טקסט גולמי מהחלון, וכן לבצע לחיצות והזנות טקסט באופן מאובטח ומפוקח.

> הפרויקט מיועד לפרסום פומבי ב-GitHub (רישיון MIT). **עדיין לא פורסם.**

## למה זה חוסך טוקנים
כשהסוכן מריץ `agent-cu` בעצמו, כל הטקסט של החלון נכנס להקשר שלו ונשלח שוב בכל צעד. בבדיקה שלנו, שאלה אחת
כמו "קלוד סיים?" עלתה כ-206 אלף טוקנים, בערך 3% ממכסת 5 השעות של Plus.

השרת הזה עושה את הקריאה **מקומית**:
1. ‏`agent-cu text -a <App>` קורא את החלון, בלי מודל.
2. **קוד** שולף רק את המצב ואת ההודעה האחרונה באמצעות פרופילים ייעודיים לפי תוכנה (Claude Code, ‏Codex desktop, עורכי קוד, דפדפנים, או ברירת מחדל כללית). כשנייה וחצי.
3. **מודל מקומי ב-Ollama** (ברירת מחדל `gpt-oss:20b`) מסכם את הטקסט הקצר. נמדד: 4 עד 8 שניות, עם תשובה נכונה.
4. הסוכן מקבל כ-50 טוקנים של JSON.

נבדק גם `qwen3:4b`, אבל הוא ממשיך "לחשוב בקול", ולכן ברירת המחדל היא `gpt-oss:20b`. אפשר לשנות אותה.

## הכלים
| כלי | תיאור |
|---|---|
| `look_at_screen(question, app=None, client=None, session_id=None, keep_warm=False)` | קורא חלון אחד ועונה. לקריאה בלבד. ללא `app`, נבחרת התוכנה מהשאלה או החלון שבחזית. ‏`client` ו-`session_id` הם אופציונליים (ראו "סשנים"). שורת פקודה: `--ask QUESTION [--app APP]`. |
| `click(app, target=None, steps=None, client=None, session_id=None, confirm_dangerous=False)` | לוחץ על תווית גלויה, או על רצף של 1 עד 10 תוויות דרך `steps`. נעצר בכישלון ראשון. שורת פקודה: `--click APP TARGET`, `--click-steps APP STEP...`. |
| `type_text(app, target, text, client=None, session_id=None, confirm_dangerous=False)` | מזין ערך ישירות לשדה עריכה ללא שליחה (Enter אינו נלחץ). מחליף תוכן קיים; לא נשלחות הקשות לחלונות אחרים. שורת פקודה: `--type APP TARGET TEXT`. |
| `ping()` | בדיקת דופק: מוודא שהשרת עונה, ש-Ollama המקומי זמין (ומפעיל אותו אם צריך), ושהמודל עונה `pong`. לא קורא מסך. מחזיר `{ok, server, ollama, model_reply_ok, model, seconds, cold, error?}`. הפינג הראשון יכול לקחת 30–50 שניות כשהמודל עוד לא טעון (cold). שורת פקודה: `--ping`. |
| `release_model()` | פורק את המודל מהזיכרון מיד ומחזיר כמה זיכרון שוחרר (`freed_mb`). שורת פקודה: `--release`. |
| `health()` | בודק אם Ollama זמין, אם המודל הורד ונטען, זיכרון, RAM פנוי, מדיניות keep_alive ואם agent-cu מותקן. שורת פקודה: `--health`. |

### מתי לקרוא (`look_at_screen`) ומתי ללחוץ או להקליד
- **קריאה תחילה**: משתמשים ב-`look_at_screen` כדי לבחון את מצב המסך, לוודא תוצאות, או כשלא בטוחים אילו פקדים או תוויות קיימים בחלון. הכלי מיועד לקריאה בלבד וללא תופעות לוואי.
- **תווית גלויה, לעולם לא מזהי רפרנס**: מעבירים את התווית הגלויה כפי שהיא מופיעה בממשק (1 עד 200 תווים), ולעולם לא מזהה רפרנס כדוגמת `@eN`. מזהי הרפרנס של עץ הנגישות משתנים בכל snapshot; שימוש ביעד בתבנית `@eN` נדחה עם שגיאת `INVALID_TARGET`.
- **פעולה בודדת מול רצף שלבים**: מומלץ לבצע לחיצה אחת בכל קריאה בעת בדיקה או חקירה של הממשק. עבור רצף ידוע מראש (עד 10 תוויות, כגון מקשי מחשבון `["7", "+", "5", "="]`), משתמשים בפרמטר `steps`. כל שלב מבצע snapshot רענן ועובר שער בטיחות; הרצף נעצר בכישלון הראשון.
- **הזנת טקסט ישירה (`type_text`)**: מזין ערך ישירות לשדה עריכה (תפקידי `textfield`, `textarea`, `edit`, `combobox`, `textbox`, `document`, `searchfield`) בחלון המבוקש. הפעולה מחליפה את תוכן השדה ללא הגשה (Enter לעולם אינו נלחץ, ולא נשלחות הקשות לחלונות אחרים). אורך הטקסט מוגבל על ידי `max_type_chars` (ברירת מחדל: 2,000 תווים).

### מדרג התאמת תוויות ובחירת מועמדים
השרת מאתר פקדים בממשק לפי מדרג עדיפויות קפדני:
1. **התאמה מדויקת (Exact)**: שוויון מחרוזות מדויק כולל אותיות גדולות/קטנות (`tier 1`, ציון 1.0, `how: "exact"`).
2. **התאמה מנורמלת (Normalized)**: התעלמות מגודל אותיות, נרמול NFKC ביוניקוד, הסרת ניקוד וטעמים בעברית (U+0591–U+05C7), המרת מקף עברי (`־` U+05BE) לרווח, המרת גרש (`׳` U+05F3) וגרשיים (`״` U+05F4) לתווי ASCII תקניים `'` ו-`"` (מומרים ולא נמחקים), הסרת סימני כיווניות ורוחב-אפס בלבד: U+200B–U+200F, U+061C, U+202A–U+202E, U+2066–U+2069, U+FEFF (בדיקת רשימת החסימה בבטיחות מסירה בנפרד את כל תווי ה-Cf), והסרת סיומת `…`, `...` או `:` בלבד בסוף התווית (`tier 2`, ציון 0.95, `how: "normalized"`).
3. **מילים נרדפות ומזהים (Synonyms & IDs)**: מילים נרדפות לספרות ולפעולות חשבון בעברית ובאנגלית (למשל: `"7"` ↔ `"seven"` ↔ `"שבע"`, `"+"` ↔ `"plus"` ↔ `"ועוד"`, `"-"` ↔ `"−"` (U+2212) ↔ `"minus"` ↔ `"פחות"`, `"*"` ↔ `"×"` ↔ `"multiply"` ↔ `"כפול"` ↔ `"הכפל ב"`, `"/"` ↔ `"÷"` ↔ `"divide"` ↔ `"חלקי"` ↔ `"חלק ב"`, `"="` ↔ `"equals"` ↔ `"שווה"`, `"."` ↔ `"decimal separator"` ↔ `"תו הפרדה עשרוני"`), או התאמה למזהה הנגישות `id` של הפקד (`tier 3`, ציון 0.9). שימו לב ש-`-` תואם גם לסימן המינוס `"−"` (U+2212). ביטויי תפקיד (כגון `button 7`, `כפתור 7`, `field Search`, `שדה חיפוש`) מופרדים כדי למנוע עמימות.
4. **התאמות חלשות (Weak matches)**: התאמת ערך נוכחי (live value), הכלה של מילים (contains), או דמיון עמום (fuzzy similarity) של 0.80 ומעלה (`tier 4-5`). התאמות חלשות לעולם אינן לוחצות מעצמן באופן אוטומטי; הן מחזירות מועמדים, וכאשר `resolve_with_model` מופעל המודל המקומי רשאי לבחור אחד מהם (עד 5 מועמדים, `match: "model"`), והוא נלחץ רק לאחר בדיקת הבטיחות; אחרת מוחזרים עד 5 מועמדים עם השגיאה `TARGET_AMBIGUOUS`.

### כלל בטיחות ורשימת חסימה (Safety Rule & Blocklist)
כדי למנוע פעולות הרסניות בשוגג, מופעלת תמיד רשימת חסימה לפני כל לחיצה או הקלדה:
- **רשימת חסימה ברירת מחדל (נאכפת תמיד ולא ניתנת לביטול)**:
  - **באנגלית**: `Delete`, `Remove`, `Send`, `Allow`, `Approve`, `Accept`, `Pay`, `Buy`, `Purchase`, `Submit`, `Confirm`, `Publish`, `Merge`, `Uninstall`, `Format`.
  - **בעברית ביטויים**: `הסר התקנה`.
  - **בעברית פעולות בסיס**: `מחק`, `הסר`, `שלח`, `אשר`, `אפשר`, `שלם`, `קנה`, `פרסם`.
  - **בעברית ציווי רבים**: `מחקו`, `הסירו`, `שלחו`, `אשרו`, `אפשרו`, `שלמו`, `קנו`, `פרסמו`.
  - **בעברית נטיות ושמות פעולה**: `מחיקה`, `למחוק`, `הסרה`, `להסיר`, `שליחה`, `לשלוח`, `אישור`, `לאשר`, `לאפשר`, `תשלום`, `לשלם`, `קנייה`, `לקנות`, `פרסום`, `לפרסם`.
- **הגדרות ומשתני סביבה**: הגדרות `[safety] blocklist` / `blocklist_extra` בקובץ TOML או משתני הסביבה `MCP_CU_SAFETY_BLOCKLIST` / `MCP_CU_SAFETY_BLOCKLIST_EXTRA` יכולים **רק להוסיף** מילים לרשימה. מילות ברירת המחדל נאכפות תמיד.
- **בדיקה מקדימה**: שם היעד נבדק תחילה (עוד לפני צילום ה-snapshot), ולאחר מכן נבדק שוב שם הפקד שאותר.
- **תהליך אישור**: אם נמצאה התאמה למילה חסומה והפרמטר `confirm_dangerous=false` (ברירת המחדל), הפעולה נחסמת ומוחזרת השגיאה `DANGEROUS_ACTION_NEEDS_CONFIRMATION` יחד עם `would_click` ו-`matched_rule`.
- **הנחיה קריטית**: **הסוכן הפונה חייב לשאול את המשתמש האנושי ולקבל אישור מפורש בצ'אט לפני קריאה חוזרת עם `confirm_dangerous=true`**.
- **פעולות שאינן חסומות כברירת מחדל**: פעולות ניווט נפוצות כגון `"Close"` / `"Exit"` (ובעברית `"סגור"` / `"יציאה"`) **אינן חסומות כברירת מחדל** (ניתן להוסיפן לרשימת החסימה בהגדרות לפי הצורך).

### יומן פעולות לביקורת (`.sessions/actions.log`)
כל פעולת לחיצה והקלדה מתועדת בקובץ JSON Lines בנתיב `<project>/.sessions/actions.log` (או לפי `MCP_CU_SESSIONS_DIR`):
- **תוקף רשומות (TTL)**: רשומות ישנות מ-24 שעות (`action_log_ttl_hours`, ברירת מחדל 24.0) נמחקות אוטומטית ובאופן אטומי בעת כל כתיבה.
- **סנכרון תהליכים**: נעילה בטוחה בין תהליכים וחוטים באמצעות קובץ נעילה ייעודי (`actions.log.lock`).
- **שדות מורשים בלבד**: `time`, `client`, `session_id`, `app`, `action`, `target`, `ref`, `name`, `role`, `result`, `confirmed`, `error_code`, `text_len`, `hint`.
- **פרטיות וטיהור מידע**: **הטקסט המוקלד לעולם אינו נרשם ביומן** — נרשם רק אורכו במספר תווים (`text_len`). בנוסף, מנגנון טיהור אוטומטי סורק את כל שדות המחרוזת ומחליף ב-`<redacted>` כל שדה המכיל את הטקסט המוקלד או מקטעים ממנו באורך 6 תווים ומעלה.

### קודי שגיאה
| קוד שגיאה | תיאור |
|---|---|
| `TARGET_NOT_FOUND` | לא נמצא פקד ממשק מתאים בחלון (מוחזרים עד 5 מועמדים קרובים כהצעה). |
| `TARGET_AMBIGUOUS` | נמצאו מספר פקדים מתאימים (מוחזרים עד 5 `candidates` להבהרה). |
| `TARGET_NOT_EDITABLE` | הפקד אותר אך סוגו אינו שדה עריכה (`type_text`). |
| `INVALID_TARGET` | היעד ריק, ארוך מ-200 תווים, או מזהה רפרנס `@eN`; העברת שניהם יחד או אף אחד מהם מבין `target` ו-`steps`; מספר שלבים לא תקין ב-`steps` (לא 1–10) או תווית שלב לא תקינה; או ערך `text` שאינו מחרוזת ב-`type_text`. |
| `TEXT_TOO_LONG` | הטקסט חורג ממגבלת `max_type_chars` (ברירת מחדל: 2,000 תווים). |
| `DANGEROUS_ACTION_NEEDS_CONFIRMATION` | היעד או שם הפקד (target or element) תואם למילה ברשימת החסימה. נדרש אישור אנושי מפורש לפני קריאה עם `confirm_dangerous=true`. |
| `APP_NOT_FOUND` | חלון התוכנה לא נמצא, שם תוכנה ריק, או שם המכיל תווים מיוחדים של מעטפת הפקודות. |
| `AGENT_CU_NOT_INSTALLED` | הכלי `agent-cu` אינו מותקן ב-PATH. |
| `AGENT_CU_FAILED` | פקודת הנגישות נכשלה. |
| `OLLAMA_UNREACHABLE` | שירות Ollama אינו נגיש ולא הצליח להפעיל את עצמו. |
| `MODEL_NOT_FOUND` | המודל המבוקש טרם הורד ב-Ollama. |
| `MODEL_TIMEOUT` | פניית המודל חרגה ממגבלת הזמן. |
| `MODEL_FAILED` | פעולת המודל נכשלה. |
| `QUESTION_TOO_LONG` | השאלה ארוכה מ-`max_question_chars` (ברירת מחדל: 300 תווים / default 300). |
| `EMPTY_QUESTION` | מחרוזת השאלה ריקה. |

### אפשרויות שורת הפקודה (CLI) והערת אבטחה
ניתן להפעיל פעולות ישירות משורת הפקודה:
```powershell
# לחיצה על תווית בודדת
mcp-computer-use --click "מחשבון" "7"

# רצף שלבים (1 עד 10 תוויות)
mcp-computer-use --click-steps "מחשבון" "7" "+" "5" "="

# הזנת טקסט לשדה עריכה
mcp-computer-use --type "מחשבון" "שדה הזנה" "123"

# אישור פעולה מסוכנת רק לאחר קבלת אישור אנושי מפורש
mcp-computer-use --click "MyApp" "מחק" --confirm-dangerous
```
> [!WARNING]
> **הערת אבטחה להזנת טקסט ושורת הפקודה**: גם ה-CLI (באפשרות `--type`) וגם כלי ה-MCP (`type_text`) מעבירים את הטקסט ל-`agent-cu` כארגומנט (`agent-cu type -s REF -a APP -- TEXT`), כך שהטקסט גלוי לתהליכים מקומיים אחרים במערכת בזמן ריצתו (לזמן קצר, למשל במנהל המשימות או ברשימת התהליכים). בנוסף, שימוש ב-CLI חושף את הטקסט בשורת הפקודה ובהיסטוריית הפקודות של המעטפת (shell). אין להקליד סיסמאות או מידע סודי/רגיש באמצעותו.

### דוגמאות למחשבון (MCP ו-CLI)
בתוכנות UWP ואפליקציות מודרניות של Windows (הרצות בתוך `ApplicationFrameHost`), שם התוכנה הוא כותרת החלון המתורגמת (למשל `Calculator` במערכת באנגלית, או `מחשבון` במערכת בעברית; פתיחה עם `cu-open.ps1 calc.exe`).

**קריאות כלי MCP (JSON)**:
```json
// לחיצה בודדת
{
  "app": "מחשבון",
  "target": "7"
}

// רצף שלבים ידוע מראש (עד 10)
{
  "app": "מחשבון",
  "steps": ["7", "+", "5", "="]
}

// הזנת ערך לשדה עריכה
{
  "app": "מחשבון",
  "target": "שדה הזנה",
  "text": "42"
}
```

**הפעלה משורת הפקודה (CLI)**:
```powershell
mcp-computer-use --click "מחשבון" "7"
mcp-computer-use --click-steps "מחשבון" "7" "+" "5" "="
```

### פרופילים ייעודיים לפי תוכנה (Per-app profiles)
בעת קריאת חלון באמצעות `look_at_screen`, השרת מתאים ובוחר פרופיל חילוץ ייעודי לפי שם התוכנה (`app_name`) וכותרת החלון (`window_title`). בדיקת הפרופילים מתבצעת בסדר עדיפויות קפדני (ההתאמה הראשונה שמתקיימת נבחרת):

$$\text{claude\_code} \longrightarrow \text{codex\_desktop} \longrightarrow \text{editor} \longrightarrow \text{browser} \longrightarrow \text{generic}$$

1. **`claude_code`**: נבחר כאשר מחרוזת `"claude"` מופיעה בשם התוכנה (באותיות קטנות).
2. **`codex_desktop`**: נבחר כאשר שם התוכנה הוא `chatgpt`, `codex`, `codex desktop` או `openai.codex` (אפליקציית Windows של `ChatGPT`), או כאשר כותרת החלון כוללת `codex`.
3. **`editor`**: נבחר עבור עורכי קוד ממשפחת VS Code לפי שם התוכנה (`code`, `code.exe`, `code - oss`, `code-oss`, `code - insiders`, `cursor`, `antigravity ide`, `windsurf`, `vscodium`), או לפי סיומת ותבנית כותרת החלון (` - Visual Studio Code`, ` - Cursor`, ` - Antigravity IDE - `, ` - Windsurf`, ` - VSCodium`, ` - Code - Insiders`).
4. **`browser`**: נבחר עבור דפדפנים נתמכים לפי שם התוכנה (Chrome: `chrome`, `google chrome`; Edge: `msedge`, `edge`, `microsoft edge`; Brave: `brave`; Firefox: `firefox`, `mozilla firefox`), או לפי סיומת כותרת החלון (` - Google Chrome`, ` - Microsoft Edge`, `microsoft edge`, ` - Mozilla Firefox`, ` - Brave`).
5. **`generic`**: ברירת המחדל כאשר אף פרופיל ייעודי אינו מתאים.

#### פלט לפי פרופיל (סטטוס עליון ושדות details)
כל קריאה ל-`look_at_screen` מחזירה סטטוס עליון (`status`: ‏`working`, `finished`, `waiting`, `error` או `unknown`) יחד עם אובייקט `details` אופציונלי שממולא על ידי קוד (מתוך טקסט הנגישות ועץ ה-snapshot, ללא מודל; לביטול צילום snapshot נוסף: `MCP_CU_SNAPSHOT_DETAILS=0`):

- **Claude Code (`claude_code`)**:
  - **סטטוס עליון (`status`)**: ‏`working`, `finished` או `unknown`.
  - **מפתחות `details`**: ‏`project`, שם הסשן (`session`), ענף (`branch`), תיקייה (`folder`), מודל (`model`), קבצים (`files`), משימות רקע (`background_tasks`), ניצול הקשר (`context_used`), כפתורי אישור ממתינים (`waiting_for_user`), שאלת אישור (`waiting_prompt`), כשלים (`failures`), הצעות לצעד הבא באנגלית ובעברית (`next_step`), ושאלות שהסוכן מפנה למשתמש (`asks_user`).
- **Codex desktop (`codex_desktop`, תוכנת Windows בשם `ChatGPT`)**:
  - **סטטוס עליון (`status`)**: ‏`working`, `finished`, `waiting` או `unknown` — נקבע **אך ורק מתוך פקדי ממשק (UI controls)** (כפתור Stop, כפתור העתקה Copy יחד עם משוב Good/Bad response, ותיבות אישור; טקסט חופשי בתוך תור העוזר לעולם אינו נחשב כאות סטטוס).
  - **מפתחות `details`**:
    - `project`: שם הפרויקט או סביבת העבודה (מתוך "What should we work on in...", "Change project: ...", או מפקדי התיקייה/הפרויקט בכותרת השרשור).
    - `session`: כותרת השיחה / השרשור (מכותרת ה-heading או כותרת השרשור).
    - `mode`: מצב עבודה נוכחי (מתוך "Switch mode, current mode: ...").
    - `projects`: רשימת פרויקטים בסרגל הצד (עד 8, מאומתים מול פקדי פעולות פרויקט).
    - `recent_chats`: רשימת שיחות אחרונות בסרגל הצד (עד 5, מאומתות מול פקדי נעיצה וארכוב).
    - `needs_attention`: ערך בוליאני `true` כאשר מופיע כפתור "View activity, needs attention".
    - `next_step`: נשלף מתוך "Next steps:" או "השלב הבא:".
    - `failures`: שגיאות וכשלים שזוהו באמצעות `find_failures`.
    - `waiting_prompt` (ו-`waiting_for_user=true`): שאלת האישור כשהתוכנה ממתינה לאישור משתמש (כגון "Allow command?", "Approve command?"), מאומתת מול צמד כפתורי אישור/דחייה (approve/deny).
- **Editor (`editor`, עורכי קוד VS Code / Cursor / Antigravity IDE / Windsurf / VSCodium)**:
  - **סטטוס עליון (`status`)**: ‏`waiting` אם ממתינה שאלת אישור, `error` אם זוהו כשלים, אחרת `unknown`.
  - **מפתחות `details`**:
    - `project`: שם התיקייה או הפרויקט (מתוך כותרת החלון או עץ ה-Explorer).
    - `file`: הקובץ הפעיל (מתוך כותרת החלון או לשונית עורך פעילה יחידה).
    - `waiting_prompt` (ו-`waiting_for_user=true`): שאלת אישור עבור דיאלוגים אמיתיים (שאלת אמון בספרייה "Do you trust the authors...", התקנת הרחבות, או שמירת קובץ), מאומתת מול כפתורי הפעולה של הדיאלוג.
    - `failures`: שגיאות ואבחונים (`Error: ...`, `Exception:`) ומספר שגיאות מתוך חלונית Problems או מהרצת הטרמינל האחרונה (פלט ישן שקדם לפקודת מעטפת חדשה או לסיכום בדיקות מאוחר יותר מושמט אוטומטית).
    - `next_step`: הצעות לצעד הבא מתוך שורות הראיות.
- **Browser (`browser`, דפדפנים Chrome / Edge / Brave / Firefox)**:
  - **סטטוס עליון (`status`)**: ‏`error` בעמודי שגיאת רשת (כגון DNS probe, חיבור נדחה, אין אינטרנט); אחרת `unknown`.
  - **מפתחות `details`**:
    - `page_title` (ו-`title`): כותרת העמוד נקייה מסיומות דפדפן ומקטעי פרופיל Edge.
    - `url`: כתובת ה-URL הנוכחית מתוך שורת הכתובות או שורות הטקסט העליונות.
    - `tab`: כותרת הלשונית הפעילה (ללא תגיות צריכת זיכרון).
    - `headings`: עד 5 כותרות עמוד מתוך אזור תוכן העמוד (`RootWebArea`) או מהטקסט.
    - `failures`: שגיאות וכשלים מתוך גוף המסמך.
    - `next_step`: צעדים הבאים מתוך תוכן העמוד.
    - מסנן אוטומטית את סרגלי הדפדפן (toolbars, רצועת לשוניות, שורת כתובת, סרגל מועדפים) ומחלץ רק את תוכן עמוד האינטרנט.
- **Generic (`generic`)**:
  - **סטטוס עליון (`status`)**: תמיד `unknown`.
  - **מפתחות `details`**: כשלים (`failures`) שזוהו ב-30 השורות האחרונות של טקסט הסוף הנשלף (אם קיימים).

#### הכלל השמרני (Conservative rule)
כל המחלצים פועלים לפי עיקרון שמרני מובהק: החזרת סטטוס `unknown` ושמירת תוכן לא מסווג במקום לנחש או להסיק מצב מטקסט חופשי ועמום.

#### הערות מגבלה (Limitations)
- **חשיפת תוכן בדפדפנים**: דפדפנים עשויים שלא לחשוף תוכן עמוד למנגנון ה-UI Automation כאשר סשן ה-Windows נעול או כשחלון הדפדפן ממוזער (עץ הנגישות חושף במצב זה רק את מעטפת הדפדפן או אזור מסמך ריק).
- **כללי מצב השרשור ב-Codex desktop**: כללי זיהוי מצב השרשור נבנו ותוקפו על גבי דגימות סינתטיות ועשויים לדרוש כיול והתאמות מול עדכוני גרסה עתידיים של האפליקציה.

### הפעלה אוטומטית של Ollama
אם `OLLAMA_URL` מצביע על המחשב הזה (`127.0.0.1` או `localhost`) ו-Ollama לא עונה, השרת מפעיל בעצמו `ollama serve` ברקע, בחלון מוסתר, עם `OLLAMA_HOST=127.0.0.1:<הפורט מ-OLLAMA_URL>` **רק לתהליך הזה**. ה-`OLLAMA_HOST` הגלובלי שלך (למשל ה-Mac) לא משתנה. אחר כך הוא מחכה עד 10 שניות ש-`/api/tags` יענה.
- הבדיקה נעשית בכל קריאה **בלי** סשן, ובקריאה **הראשונה** של כל סשן, לא בכל קריאה בתוך סשן. אם קריאה באמצע סשן נכשלת עם `OLLAMA_UNREACHABLE`, השרת מפעיל את Ollama פעם אחת ומנסה שוב פעם אחת.
- בדיקת פורט וקובץ נעילה (`.sessions/ollama-autostart.lock`) מונעים הפעלה כפולה.
- את `ollama` הוא מחפש ב-PATH או ב-`%LOCALAPPDATA%\Programs\Ollama\ollama.exe`.
- ‏`MCP_CU_AUTOSTART_OLLAMA=0` מכבה את ההפעלה האוטומטית. ‏`health()` מדווח `ollama_autostarted`.

### שחרור זיכרון חכם
- קריאה בודדת בלי סשן פעיל: המודל נשאר טעון **3 דקות** (`MCP_CU_KEEP_ALIVE_IDLE`).
- כשיש סשן פעיל (קובץ ב-`.sessions` שעודכן ב-10 הדקות האחרונות) או כשנשלח `keep_warm=true`: **10 דקות** (`MCP_CU_KEEP_ALIVE_SESSION`). המודל משתחרר 10 דקות אחרי הקריאה האחרונה.
- לחץ זיכרון: לפני כל קריאה ואחריה נבדק כמה RAM פנוי. אם נשארו פחות מ-**2048MB** (`MCP_CU_MIN_FREE_MB`, ‏0 מכבה), המודל נפרק מיד אחרי התשובה, גם באמצע סשן. בנוסף, חוט רקע בשרת בודק כל 30 שניות כשהמודל טעון ועושה אותו דבר (`MCP_CU_MEMORY_WATCH=1`).
- כדאי שהלקוח יקרא ל-**`release_model()` בסוף המשימה**. הכלי פורק את המודל מיד ומחזיר כמה זיכרון שוחרר (`freed_mb`). בכל תשובה יש שדה `memory`.

### סשנים (הקשר בין קריאות, אופציונלי)
‏`client` (שם הלקוח, למשל `codex`) ו-`session_id` (שם שהלקוח בוחר, למשל `fix-ui`) הם **אופציונליים**. מותרים רק `A-Z a-z 0-9 _ -`, עד 64 תווים.
- **בלעדיהם** כל קריאה עומדת בפני עצמה: השרת קורא את כל המסך מחדש, אי אפשר לשאול "מה השתנה", ולא נכתב שום קובץ. אם לקוח שואל כמה פעמים ברצף בלי סשן, מוחזר `hint` שמציע להוסיף אותו.
- **איתם** השרת שומר קובץ JSON קטן אחד לכל זוג, ‏`<project>/.sessions/{client}__{session_id}.json`, עם האפליקציה, הפרויקט, הסשן, ההודעה האחרונה, הצעד הבא, המצב, 5 השאלות האחרונות עם תשובות קצרות וזמן העדכון. בכל קריאה השרת קורא את הקובץ ואת המסך, משווה ביניהם, עונה רק על מה שנשאל, מחזיר `changed_since_last` ומעדכן את הקובץ. אם נשאלה אותה שאלה והמסך לא השתנה, התשובה נלקחת מהקובץ בלי לפנות למודל (`cached: true`).
- **‏`session_id` חדש מחליף את הקודם** של אותו לקוח, והקובץ הקודם נמחק. לכל לקוח יש תמיד קובץ פעיל אחד. ‏`session_id` ללא `client` משתמש בלקוח ברירת המחדל `default`.
- קבצים שלא עודכנו 24 שעות נמחקים בכל קריאה (`MCP_CU_SESSION_TTL_HOURS`). את התיקייה אפשר לשנות עם `MCP_CU_SESSIONS_DIR`. שום דבר לא נשמר בזיכרון.

במקרה של כישלון חוזר `error` עם `code`, ‏`message`, ‏`retryable` ו-`suggestion`, ולצדו `extracted`, הטקסט הקצר שנשלף, כך שהסוכן יכול להמשיך גם בלי המודל.

**זמן קצוב חכם:** בסיס, ועוד תוספת לכל 1,000 תווים, בין מינימום למקסימום. אם המודל עוד לא טעון בזיכרון, מתווספת הקצבה לטעינה.

## התקנה ב-Windows (PowerShell)
דרישות: Python 3.10 ומעלה, ‏Node עם `npm i -g agent-cu`, ו-Ollama.
```powershell
cd C:\Develop\mcp-computer-use
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
pytest -q
```

### Ollama
כתובת ברירת המחדל היא `http://127.0.0.1:11435`. זה מופע מקומי על פורט נפרד, כי ה-`OLLAMA_HOST` הגלובלי שלך מפנה ל-Mac.
```powershell
$env:OLLAMA_HOST = "127.0.0.1:11435"; ollama serve      # בחלון נפרד
$env:OLLAMA_HOST = "127.0.0.1:11435"; ollama pull gpt-oss:20b
mcp-computer-use --health
mcp-computer-use --ask "Is Claude finished?" --app Claude
```

## הגדרות
ההגדרות נקבעות לפי סדר עדיפות: ברירות המחדל, מעליהן קובץ TOML, ומעל הכול משתני סביבה. פירוט מלא בטבלה בחלק האנגלי ובקובץ `mcp-computer-use.example.toml`.
משתנים חשובים נוספים עבור מנגנון הבטיחות:
- `MCP_CU_SAFETY_BLOCKLIST` ו-`MCP_CU_SAFETY_BLOCKLIST_EXTRA`: מוסיפים מילים חסומות לרשימת ברירת המחדל.
- `MCP_CU_ACTION_LOG_TTL_HOURS`: זמן שימור רשומות ביומן הפעולות בשעות (ברירת מחדל: 24).
- `MCP_CU_RESOLVE_WITH_MODEL`: שימוש במודל מקומי להכרעה בין מועמדים עמומים (ברירת מחדל: `true`).
- `MCP_CU_MAX_TYPE_CHARS`: אורך טקסט מרבי להזנה ב-`type_text` (ברירת מחדל: 2000).

את בדיקת התקינות דרך HTTP מפעילים עם `MCP_CU_HEALTH_HTTP=1`, ואז היא זמינה בכתובת `GET http://127.0.0.1:8765/health`.

## רישום ב-Codex וב-Claude Code
ב-Codex מוסיפים את הבלוק `[mcp_servers.computer-use]` לקובץ `%USERPROFILE%\.codex\config.toml`. ב-Claude Code מריצים `claude mcp add computer-use ...`. הדוגמאות המדויקות מופיעות בחלק האנגלי.
כדאי להוסיף גם ל-`AGENTS.md` או ל-`CLAUDE.md` את קטע ההוראות מתוך `docs/AGENTS_SNIPPET.md`.

## איך סוכנים צריכים לפנות לשרת
השרת שולח את הכללים האלה ללקוח כבר בהתחברות (`instructions`) וגם בתיאורי הכלים:
- את `question` כותבים **באנגלית** בלבד. הסוכן מתרגם בעצמו את מה שהמשתמש ביקש.
- **שאלה אחת קצרה וממוקדת בכל קריאה**, עד 300 תווים (אפשר לשנות ב-`MCP_CU_MAX_QUESTION_CHARS`).
- כשהתוכנה ידועה, מעבירים `app`.
- בדיקה גדולה מפצלים ל**שלבים**: קודם המצב, אחר כך הפרטים.
- **קריאה תחילה**: קוראים עם `look_at_screen` כאשר לא בטוחים במצב החלון או בתוויות הפקדים.
- **לחיצה לפי תווית גלויה**: מעבירים תווית גלויה (1 עד 200 תווים), לעולם לא מזהי רפרנס `@eN` (המשתנים בכל snapshot).
- **רצף שלבים ידוע מראש**: משתמשים ב-`steps` לרצף של עד 10 צעדים ידועים (למשל מקשי מחשבון).
- **בטיחות**: לעולם אין להעביר `confirm_dangerous=true` ללא קבלת אישור אנושי מפורש בצ'אט.
- **שימוש ב-agent-cu**: אין להריץ את `agent-cu` ישירות ללחיצה או להקלדה אלא אם הכלי נכשל.
- אפשר לשלוח `client` ו-`session_id` ולהשתמש באותו זוג לאורך כל המשימה לקבלת הקשר ו-`changed_since_last`.
- בסוף המשימה קוראים ל-`release_model()`.
- כשחוזרת שגיאה, פועלים לפי `suggestion`.

שאלה ארוכה מדי מחזירה `QUESTION_TOO_LONG`. שאלה מרובת חלקים, או שאלה שכתובה ברובה לא באותיות לטיניות (למשל עברית), עדיין מקבלת תשובה, ואיתה `hint` עם הצעה לשיפור.

טובות:
- `look_at_screen("Is Claude working or finished?", "Claude")`
- `click("מחשבון", target="7")`
- `type_text("מחשבון", target="שדה הזנה", text="123")`

פחות טובות:
- `look_at_screen("מה קלוד עושה?")` (עובד, אך מוחזר `hint` לשאול באנגלית)
- `click("מחשבון", target="@e12")` (מזהי רפרנס נדחים; יש להעביר תווית גלויה)
- ביצוע פעולה חסומה עם `confirm_dangerous=true` ללא אישור מפורש מהמשתמש האנושי

את קטע ההוראות לסוכנים (באנגלית, בחלק האנגלי וגם בקובץ `docs/AGENTS_SNIPPET.md`) מדביקים בקובץ `AGENTS.md` הגלובלי של Codex או בקובץ `CLAUDE.md` של Claude Code.

## פתיחת תוכנות: `scripts/cu-open.ps1`
הפקודה `agent-cu open calc.exe --wait` פותחת את המחשבון, אבל נכשלת בזמן קצוב: היא מחכה לתוכנה בשם `calc.exe`, בזמן שתוכנות UWP רצות בתוך `ApplicationFrameHost` עם כותרת מתורגמת (למשל `מחשבון`).
הסקריפט `cu-open.ps1` פותח כל מה ש-`Start-Process` יודע לפתוח: שם קובץ, נתיב מלא, או כתובות כמו `ms-settings:` ו-`calculator:`. כל חצי שנייה הוא בודק אם נפתח חלון חדש (או אם חלון קיים של תוכנה שרצה במופע יחיד עבר לחזית), מוודא ש-`agent-cu snapshot -a <app>` עובד, ומדפיס שורת JSON אחת עם `ok`, ‏`app` (השם לשימוש ב-`agent-cu -a`), ‏`title`, ‏`process`, ‏`pid`, ‏`seconds` ו-`hint`.
```powershell
powershell -ExecutionPolicy Bypass -File C:\Develop\mcp-computer-use\scripts\cu-open.ps1 calc.exe
```
אפשרויות: `-Args '<ארגומנטים>'` (עם `-File` מעבירים כמה ארגומנטים כמחרוזת אחת במרכאות), ‏`-TimeoutSec 15`, ו-`-NoVerify` לדילוג על הבדיקה. אם לא נפתח חלון בזמן, מוחזר `ok:false` עם הסבר, החלונות החדשים שנראו והחלון שבחזית. הסקריפט לא סוגר ולא מזיז חלונות אחרים, והפלט ב-UTF-8 כך שכותרות בעברית נשמרות.
סוכנים צריכים לפתוח תוכנות תמיד דרך `cu-open.ps1` ולא דרך `agent-cu open`.

## דוגמאות שימוש
- `look_at_screen("Is Claude finished?", "Claude")`
- `click("מחשבון", steps=["7", "+", "5", "="])`
- `type_text("מחשבון", "שדה הזנה", "100")`

## פיתוח
מבנה הקבצים ב-`src/mcp_computer_use/`: `server.py` (שרת MCP ו-CLI), `core.py` (לוגיקה ראשית), `agent_cu.py`, `matching.py`, `safety.py`, חבילת `extractors/` (`registry.py`, `codex_desktop.py`, `browser.py`, `editor.py`, `claude_code.py`, `generic.py`), `ollama_client.py`, `timeouts.py`, `config.py`, `errors.py`, `guidance.py`, `prompts/system_prompt.md`. הרצת בדיקות עם `pytest -q`. תומך ב-`mcp` 1.x ו-2.x.

### הוספת פרופיל חדש (Adding a profile)
כדי להוסיף פרופיל מחלץ עבור אפליקציה חדשה:
1. **רישום הפרופיל**: רושמים באמצעות `register_profile(name, matcher, extractor, snapshot_details)` בקובץ `src/mcp_computer_use/extractors/registry.py` (והתאמה משנית לפי כותרת חלון ב-`select_profile` ב-`src/mcp_computer_use/extractors/__init__.py`).
2. **הוספת קובצי בדיקה (fixtures)**: מוסיפים צילומי snapshot וטקסט אנונימיים תחת `tests/fixtures/<profile>/` (ללא מידע אישי, שמות משתמש, טוקנים או נתיבים מקומיים).
3. **בדיקות מבוססות fixtures**: כותבים לפחות 3 בדיקות (≥ 3 fixture tests) המאמתות זיהוי סטטוס, חילוץ שדות details ומקרי קצה (לדוגמה `tests/test_extractor_<profile>.py`).

## רישיון
MIT
</div>
