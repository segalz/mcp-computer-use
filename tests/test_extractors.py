from mcp_computer_use.extractors import extract, extract_claude, extract_generic, claude_status

from conftest import CLAUDE_TEXT


def test_claude_finished_and_last_message():
    ex = extract(CLAUDE_TEXT, "Claude")
    assert ex.extractor == "claude"
    assert ex.status == "finished"
    assert ex.last_message.splitlines() == [
        "I ran the whole suite.",
        "All 916 UI tests pass and 52 Rust crates pass.",
        "Pushed commit be8b48e and built a new installer.",
        "Claude finished the response",
    ]
    for noise in ("Ran 3", "Used Bash", "How is Claude", "New session", "run the tests"):
        assert noise not in ex.last_message


def test_claude_noise_removed():
    ex = extract(CLAUDE_TEXT, "Claude")
    lines = ex.last_message.splitlines()
    assert "checked" not in lines and "unchecked" not in lines
    assert not any(l.startswith(("Ran", "Used")) for l in lines)


def test_claude_uses_last_you_said():
    text = "You said: first\nx\nold answer\nYou said: second\nx\nnew answer\nChat mode\nafter"
    ex = extract_claude(text.splitlines(), app="Claude", max_chars=2000)
    assert ex.last_message == "new answer"


def test_claude_status_working_wins():
    assert claude_status(["Claude is responding", "Claude finished the response"]) == "working"
    assert claude_status(["nothing"]) == "unknown"


def test_cap_keeps_tail():
    text = "You said: q\nx\n" + "\n".join(f"line {i} " + "a" * 50 for i in range(200)) + "\nNew session"
    ex = extract(text, "Claude", max_chars=500)
    assert len(ex.last_message) <= 500 and ex.truncated
    assert "line 199" in ex.last_message


def test_generic_fallback_tail_and_dedup():
    text = "\n".join(["menu", "menu", "File", "checked"] + [f"row {i}" for i in range(500)])
    ex = extract(text, "Notepad", max_chars=2000, tail_chars=300)
    assert ex.extractor == "generic" and ex.status == "unknown"
    assert len(ex.last_message) <= 300 and ex.last_message.endswith("row 499") and ex.truncated


def test_claude_without_markers_falls_back_to_generic():
    ex = extract("just some text\nmore", "Claude")
    assert ex.extractor == "generic" and "more" in ex.last_message


def test_generic_dedup():
    ex = extract_generic(["a", "a", "b"], app="X", max_chars=100)
    assert ex.last_message == "a\nb"
