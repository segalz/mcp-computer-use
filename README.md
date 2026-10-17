# mcp-computer-use

**English** · [עברית](#עברית)

A small, **read-only** MCP server that lets coding agents (Codex, Claude Code, Cline, Antigravity…) ask
*"what does that window show?"* and get back a **two-line JSON answer** instead of thousands of tokens of
raw window text.

> Intended to be published publicly on GitHub (MIT). **Not published yet.**

## Why (token savings)
When an agent runs `agent-cu` itself, the whole accessibility tree / window text lands in its context and is
re-sent on every following step. In our test one "is Claude done?" check cost ~206k tokens (~47k context per
step, ~3% of a ChatGPT Plus 5-hour quota).

This server does the reading **locally**:
1. `agent-cu text -a <App>` reads the window (no model).
2. **Code** extracts only the status and the last message (Claude Desktop has a dedicated extractor; other apps
   use a generic "last N chars" fallback). ~1.5 s.
3. A **local Ollama model** (default `gpt-oss:20b`) summarizes that short text. Measured 4–8 s, correct.
4. The agent receives ~50 tokens of JSON.

`qwen3:4b` was tested and kept emitting reasoning, so `gpt-oss:20b` is the default (configurable).

## Tools
| Tool | Description |
|---|---|
| `look_at_screen(question, app=None, client=None, session_id=None)` | Reads one window and answers. If `app` is omitted, the app named in the question (also Hebrew aliases like "קלוד") or the frontmost app is used. `client` / `session_id` are optional (see *Sessions*). |
| `release_model()` | Unloads the model now and returns `freed_mb`. Call it at the end of a task. |
| `health()` | Ollama reachable, model pulled, model loaded, memory, free RAM, current keep_alive policy, agent-cu installed. |

No click / type tools: it is read-only by design.

### Details extracted by code
For Claude (desktop / Claude Code) the server also reads the accessibility tree (`agent-cu snapshot`) and returns
`details`, filled by code (not the model): `project`, `session` (chat title), `branch`, `folder`, `model`,
`next_step` (suggestion sentences, English and Hebrew), `failures` (e.g. "Finished a background command (1 failed)"),
`files` (e.g. a new installer), `background_tasks`, `context_used`, `waiting_for_user` (Allow/Deny buttons).
Callers can use these directly. Disable the extra snapshot with `MCP_CU_SNAPSHOT_DETAILS=0`.

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
Error codes: `OLLAMA_UNREACHABLE`, `MODEL_NOT_FOUND`, `MODEL_TIMEOUT`, `MODEL_FAILED`, `AGENT_CU_NOT_INSTALLED`,
`AGENT_CU_FAILED`, `APP_NOT_FOUND` (includes `available_apps`), `QUESTION_TOO_LONG`, `EMPTY_QUESTION`.

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

| Env | Default |
|---|---|
| `MCP_CU_OLLAMA_URL` / `OLLAMA_URL` | `http://127.0.0.1:11435` |
| `MCP_CU_MODEL` / `MODEL` | `gpt-oss:20b` |
| `MCP_CU_MAX_CHARS` | `2000` |
| `MCP_CU_MAX_QUESTION_CHARS` | `300` |
| `MCP_CU_TIMEOUT_BASE` / `_PER_1K_CHARS` / `_MIN` / `_MAX` | `10` / `4` / `8` / `60` |
| `MCP_CU_COLD_LOAD_EXTRA` | `45` |
| `MCP_CU_AGENT_CU_CMD` / `MCP_CU_AGENT_CU_TIMEOUT` | `agent-cu` / `20` |
| `MCP_CU_HEALTH_HTTP` / `_HOST` / `_PORT` | `false` / `127.0.0.1` / `8765` |
| `MCP_CU_SYSTEM_PROMPT_FILE` | bundled `prompts/system_prompt.md` |
| `MCP_CU_SNAPSHOT_DETAILS` | `true` (extra `agent-cu snapshot` for Claude details) |
| `MCP_CU_SESSIONS_DIR` | `<project>/.sessions` (else `~/.mcp-computer-use/sessions`) |
| `MCP_CU_SESSION_TTL_HOURS` | `24` |
| `MCP_CU_KEEP_ALIVE_IDLE` / `MCP_CU_KEEP_ALIVE_SESSION` | `3m` / `10m` |
| `MCP_CU_SESSION_ACTIVE_MINUTES` | `10` |
| `MCP_CU_MIN_FREE_MB` | `2048` (0 = never unload for memory) |
| `MCP_CU_MEMORY_WATCH` / `MCP_CU_MEMORY_WATCH_INTERVAL` | `true` / `30` |

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
  never ask it to click/type, and follow `error.suggestion`.
- Optionally pass `client` + `session_id` and reuse them for the whole task to get context and `changed_since_last`.
- Call `release_model()` when the task is done.
- Longer questions return `QUESTION_TOO_LONG`. Multi-part questions (3+ `?` or a numbered/bulleted list) and
  mostly non-Latin questions are still answered, with a non-fatal `hint`.

| Good | Bad |
|---|---|
| `Is Claude working or finished?` | `מה קלוד עושה?` (works, but returns `hint: Ask in English for best results.`) |
| `What is Claude's last message about?` | `Is Claude done? Did tests pass? What commit? Did it build?` |
| `Which commit hash did Claude report?` | `Click Send in Claude and tell me what happens` (read-only) |
| `Does the Codex window show an error?` (`app="ChatGPT"`) | a 1,000-char paragraph describing everything to check |

Step-by-step example (user asks in Hebrew "תבדוק אם קלוד סיים ומה יצא"):
1. `look_at_screen("Is Claude working or finished?", "Claude", client="codex", session_id="check-claude")` → `status: finished`
2. `look_at_screen("What is Claude's last message about?", "Claude", client="codex", session_id="check-claude")` → short summary
3. Only if needed: `look_at_screen("Did Claude report any failing tests?", ...)`; later `"What changed?"` uses `changed_since_last`
Then answer the user in their language.

### Snippet for a global AGENTS.md / CLAUDE.md
Paste into `%USERPROFILE%\.codex\AGENTS.md` and/or `%USERPROFILE%\.claude\CLAUDE.md` (also in `docs/AGENTS_SNIPPET.md`):
```markdown
## On-screen checks (mcp-computer-use)
To see or check what is on this computer's screen (e.g. "is Claude finished?", "what does Codex show?"),
use the `computer-use` MCP tool `look_at_screen` instead of running agent-cu or screenshots yourself.
- Write `question` in English, even if I write to you in Hebrew or another language.
- One short, focused question per call (under 300 chars). Pass `app` when known ("Claude", "ChatGPT").
- For bigger checks go step by step: first "Is Claude working or finished?", then "What is Claude's last message about?", then any detail.
- Optional: pass `client="codex"` (your name) and a `session_id` you choose for the task (e.g. "check-claude"), and reuse both on every call of that task: answers get shorter and more accurate and `changed_since_last` shows what changed. A new session_id replaces the old one. Without them each call is independent (full re-read, no "what changed").
- Use `details` (project, session, branch, next_step, failures, files) from the result directly.
- When the task is done, call `release_model()` to free the local model's memory.
- It is read-only: never ask it to click or type.
- On `error`, follow `error.suggestion` (OLLAMA_UNREACHABLE → call `health()`; MODEL_TIMEOUT → retry once; QUESTION_TOO_LONG → split). If a `hint` is returned, apply it to the next call.
```

## Usage examples
- `look_at_screen("Is Claude finished? What did it report?", "Claude")`
- `look_at_screen("What is Claude doing right now?")` → app auto-picked from the question
- `look_at_screen("What is the last message in Codex?", "ChatGPT")`

## Development
`src/mcp_computer_use/`: `server.py` (MCP + CLI), `core.py` (logic), `agent_cu.py`, `extractors.py`
(add per-app extractors to `EXTRACTORS`), `ollama_client.py`, `timeouts.py`, `config.py`, `errors.py`,
`guidance.py` (instructions + question validation), `prompts/system_prompt.md`. Tests mock subprocess and Ollama: `pytest -q`. Works with `mcp` 1.x (`FastMCP`)
and 2.x (`MCPServer`).

## License
MIT

---

<div dir="rtl">

# עברית

שרת MCP קטן **לקריאה בלבד**, שמאפשר לסוכני קוד (Codex, ‏Claude Code, ‏Cline, ‏Antigravity) לשאול "מה מופיע בחלון הזה?"
ולקבל **תשובת JSON של שתי שורות** במקום אלפי טוקנים של טקסט גולמי מהחלון.

> הפרויקט מיועד לפרסום פומבי ב-GitHub (רישיון MIT). **עדיין לא פורסם.**

## למה זה חוסך טוקנים
כשהסוכן מריץ `agent-cu` בעצמו, כל הטקסט של החלון נכנס להקשר שלו ונשלח שוב בכל צעד. בבדיקה שלנו, שאלה אחת
כמו "קלוד סיים?" עלתה כ-206 אלף טוקנים, בערך 3% ממכסת 5 השעות של Plus.

השרת הזה עושה את הקריאה **מקומית**:
1. ‏`agent-cu text -a <App>` קורא את החלון, בלי מודל.
2. **קוד** שולף רק את המצב ואת ההודעה האחרונה (יש מחלץ ייעודי ל-Claude Desktop, ולשאר התוכנות לוקחים את N התווים האחרונים). כשנייה וחצי.
3. **מודל מקומי ב-Ollama** (ברירת מחדל `gpt-oss:20b`) מסכם את הטקסט הקצר. נמדד: 4 עד 8 שניות, עם תשובה נכונה.
4. הסוכן מקבל כ-50 טוקנים של JSON.

נבדק גם `qwen3:4b`, אבל הוא ממשיך "לחשוב בקול", ולכן ברירת המחדל היא `gpt-oss:20b`. אפשר לשנות אותה.

## הכלים
- ‏`look_at_screen(question, app=None, client=None, session_id=None)`: קורא חלון אחד ועונה. בלי `app`, נבחרת התוכנה שמוזכרת בשאלה (כולל כינויים בעברית כמו "קלוד" ו"קודקס"), ואם אין כזו, החלון שבחזית. ‏`client` ו-`session_id` הם אופציונליים (ראו "סשנים").
- ‏`release_model()`: פורק את המודל מהזיכרון מיד ומחזיר כמה זיכרון שוחרר. קוראים לו בסוף משימה.
- ‏`health()`: בודק אם Ollama זמין, אם המודל הורד ונטען, כמה זיכרון הוא תופס, כמה RAM פנוי, מה מדיניות ה-keep_alive הנוכחית ואם agent-cu מותקן.

אין כלים ללחיצה או להקלדה. השרת קורא בלבד.

### פרטים שהקוד שולף
ב-Claude ‏(desktop ו-Claude Code) השרת קורא גם את עץ הנגישות (`agent-cu snapshot`) ומחזיר שדה `details` שממלא הקוד, לא המודל: הפרויקט (`project`), שם הסשן (`session`), הענף (`branch`), התיקייה (`folder`), הצעד הבא שהוצע (`next_step`, באנגלית ובעברית), כשלים כמו פקודת רקע שנכשלה (`failures`), קבצים כמו התקנה חדשה (`files`), משימות רקע (`background_tasks`), וכפתורי אישור שממתינים למשתמש (`waiting_for_user`). אפשר לכבות עם `MCP_CU_SNAPSHOT_DETAILS=0`.

### שחרור זיכרון חכם
- קריאה בודדת בלי סשן פעיל: המודל נשאר טעון **3 דקות** (`MCP_CU_KEEP_ALIVE_IDLE`).
- כשיש סשן פעיל (קובץ ב-`.sessions` שעודכן ב-10 הדקות האחרונות) או כשנשלח `keep_warm=true`: **10 דקות** (`MCP_CU_KEEP_ALIVE_SESSION`). המודל משתחרר 10 דקות אחרי הקריאה האחרונה.
- לחץ זיכרון: לפני כל קריאה ואחריה נבדק כמה RAM פנוי. אם נשארו פחות מ-**2048MB** (`MCP_CU_MIN_FREE_MB`, ‏0 מכבה), המודל נפרק מיד אחרי התשובה, גם באמצע סשן. בנוסף, חוט רקע בשרת בודק כל 30 שניות כשהמודל טעון ועושה אותו דבר (`MCP_CU_MEMORY_WATCH=1`).
- כדאי שהלקוח יקרא ל-**`release_model()` בסוף המשימה**. הכלי פורק את המודל מיד ומחזיר כמה זיכרון שוחרר (`freed_mb`). בכל תשובה יש שדה `memory`.

### סשנים (הקשר בין קריאות, אופציונלי)
‏`client` (שם הלקוח, למשל `codex`) ו-`session_id` (שם שהלקוח בוחר, למשל `fix-ui`) הם **אופציונליים**. מותרים רק `A-Z a-z 0-9 _ -`, עד 64 תווים.
- **בלעדיהם** כל קריאה עומדת בפני עצמה: השרת קורא את כל המסך מחדש, אי אפשר לשאול "מה השתנה", ולא נכתב שום קובץ. אם לקוח שואל כמה פעמים ברצף בלי סשן, מוחזר `hint` שמציע להוסיף אותו.
- **איתם** השרת שומר קובץ JSON קטן אחד לכל זוג, ‏`<project>/.sessions/{client}__{session_id}.json`, עם האפליקציה, הפרויקט, הסשן, ההודעה האחרונה, הצעד הבא, המצב, 5 השאלות האחרונות עם תשובות קצרות וזמן העדכון. בכל קריאה השרת קורא את הקובץ ואת המסך, משווה ביניהם, עונה רק על מה שנשאל, מחזיר `changed_since_last` ומעדכן את הקובץ. אם נשאלה אותה שאלה והמסך לא השתנה, התשובה נלקחת מהקובץ בלי לפנות למודל (`cached: true`).
- **‏`session_id` חדש מחליף את הקודם** של אותו לקוח, והקובץ הקודם נמחק. לכל לקוח יש תמיד קובץ פעיל אחד.
- קבצים שלא עודכנו 24 שעות נמחקים בכל קריאה (`MCP_CU_SESSION_TTL_HOURS`). את התיקייה אפשר לשנות עם `MCP_CU_SESSIONS_DIR`. שום דבר לא נשמר בזיכרון.

במקרה של כישלון חוזר `error` עם `code`, ‏`message`, ‏`retryable` ו-`suggestion`, ולצדו `extracted`, הטקסט הקצר שנשלף, כך שהסוכן יכול להמשיך גם בלי המודל.
הקודים: ‏`OLLAMA_UNREACHABLE`, ‏`MODEL_NOT_FOUND`, ‏`MODEL_TIMEOUT`, ‏`MODEL_FAILED`, ‏`AGENT_CU_NOT_INSTALLED`, ‏`AGENT_CU_FAILED`, ‏`APP_NOT_FOUND`.

**זמן קצוב חכם:** בסיס, ועוד תוספת לכל 1,000 תווים, בין מינימום למקסימום. אם המודל עוד לא טעון בזיכרון, מתווספת הקצבה לטעינה.

## התקנה ב-Windows (PowerShell)
דרישות: Python 3.10 ומעלה, ‏Node עם `npm i -g agent-cu`, ו-Ollama.
</div>

```powershell
cd C:\Develop\mcp-computer-use
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
pytest -q
```

<div dir="rtl">

### Ollama
כתובת ברירת המחדל היא `http://127.0.0.1:11435`. זה מופע מקומי על פורט נפרד, כי ה-`OLLAMA_HOST` הגלובלי שלך מפנה ל-Mac.
</div>

```powershell
$env:OLLAMA_HOST = "127.0.0.1:11435"; ollama serve      # בחלון נפרד
$env:OLLAMA_HOST = "127.0.0.1:11435"; ollama pull gpt-oss:20b
mcp-computer-use --health
mcp-computer-use --ask "מה קלוד עושה?" --app Claude
```

<div dir="rtl">

## הגדרות
ההגדרות נקבעות לפי סדר עדיפות: ברירות המחדל, מעליהן קובץ TOML, ומעל הכול משתני סביבה. פירוט מלא בטבלה בחלק האנגלי ובקובץ `mcp-computer-use.example.toml`.
את בדיקת התקינות דרך HTTP מפעילים עם `MCP_CU_HEALTH_HTTP=1`, ואז היא זמינה בכתובת `GET http://127.0.0.1:8765/health`.

## רישום ב-Codex וב-Claude Code
ב-Codex מוסיפים את הבלוק `[mcp_servers.computer-use]` לקובץ `%USERPROFILE%\.codex\config.toml`. ב-Claude Code מריצים `claude mcp add computer-use ...`. הדוגמאות המדויקות מופיעות בחלק האנגלי.
כדאי להוסיף גם ל-`AGENTS.md` או ל-`CLAUDE.md` את ההוראה: "כדי לבדוק מה מופיע בחלון במחשב, קרא ל-`look_at_screen` ואל תריץ agent-cu בעצמך".

## איך סוכנים צריכים לפנות לשרת
השרת שולח את הכללים האלה ללקוח כבר בהתחברות (`instructions`) וגם בתיאורי הכלים:
- את `question` כותבים **באנגלית** בלבד. הסוכן מתרגם בעצמו את מה שהמשתמש ביקש.
- **שאלה אחת קצרה וממוקדת בכל קריאה**, עד 300 תווים (אפשר לשנות ב-`MCP_CU_MAX_QUESTION_CHARS`).
- כשהתוכנה ידועה, מעבירים `app`.
- בדיקה גדולה מפצלים ל**שלבים**: קודם המצב, אחר כך הפרטים.
- לא מבקשים מהשרת ללחוץ או להקליד.
- אפשר (לא חובה) לשלוח `client` ו-`session_id` ולהשתמש באותו זוג לאורך כל המשימה, כדי לקבל הקשר ו-`changed_since_last`.
- בסוף המשימה קוראים ל-`release_model()`.
- כשחוזרת שגיאה, פועלים לפי `suggestion`.

שאלה ארוכה מדי מחזירה `QUESTION_TOO_LONG`. שאלה מרובת חלקים, או שאלה שכתובה ברובה לא באותיות לטיניות (למשל עברית), עדיין מקבלת תשובה, ואיתה `hint` עם הצעה לשיפור.

טובות: ‏`Is Claude working or finished?`, ‏`What is Claude's last message about?`
פחות טובות: ‏`מה קלוד עושה?` (עובד, אבל מוחזרת המלצה לשאול באנגלית), ‏`Is Claude done? Did tests pass? What commit?` (כמה שאלות בקריאה אחת), ‏`Click Send...` (השרת קורא בלבד).

דוגמה לעבודה בשלבים: המשתמש כותב "תבדוק אם קלוד סיים ומה יצא". הסוכן שואל קודם `Is Claude working or finished?`, אחר כך `What is Claude's last message about?`, ועונה למשתמש בעברית.

את קטע ההוראות לסוכנים (באנגלית, בחלק האנגלי וגם בקובץ `docs/AGENTS_SNIPPET.md`) מדביקים בקובץ `AGENTS.md` הגלובלי של Codex או בקובץ `CLAUDE.md` של Claude Code.

## דוגמאות
- ‏`look_at_screen("Is Claude working or finished?", "Claude")`
- ‏`look_at_screen("Is Claude finished?", "Claude")`

## רישיון
MIT
</div>
