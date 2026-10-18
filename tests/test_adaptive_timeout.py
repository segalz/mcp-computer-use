"""Per-call wall budget (call_budget_seconds) and the adaptive model timeout."""
import json

import httpx
import pytest

from conftest import make_ollama
from mcp_computer_use import autostart, core, matching, timeouts
from mcp_computer_use.agent_cu import Element
from mcp_computer_use.config import Config, load_config
from mcp_computer_use.timeouts import TimingStats, budget_cap, model_timeout


def stats(*warm, bump=1.0):
    st = TimingStats(None)
    st.warm, st.bump = list(warm), bump
    return st


# ---- adaptive formula -------------------------------------------------------

def test_no_samples_uses_static_base():
    t, info = model_timeout(0, Config(), stats=stats())
    assert t == 30 and info == {"timeout_source": "static", "samples": 0}


def test_fast_samples_hit_lower_bound():
    t, info = model_timeout(0, Config(), stats=stats(3, 4, 5))
    assert t == 20 and info == {"timeout_source": "adaptive", "samples": 3}


def test_slow_samples_scale_and_size_term_added_on_top():
    cfg = Config()
    assert model_timeout(0, cfg, stats=stats(20, 25, 30))[0] == 60       # 2 x p90 (max of 3)
    assert model_timeout(2000, cfg, stats=stats(20, 25, 30))[0] == 68    # + 4 s per 1k chars


def test_clamped_to_min_and_max():
    cfg = Config(timeout_min=15, timeout_max=50)
    assert model_timeout(0, cfg, stats=stats(1))[0] == 15
    assert model_timeout(0, cfg, stats=stats(60))[0] == 50


def test_p90_ignores_a_single_outlier_in_a_large_window():
    st = stats(*([10.0] * 19 + [80.0]))
    assert model_timeout(0, Config(), stats=st)[0] == 20  # p90 = 10 s -> 20 s


def test_cold_load_adds_extra_after_clamp():
    assert model_timeout(0, Config(), model_loaded=False, stats=stats(60))[0] == 90 + 45


def test_timeout_bumps_then_success_resets():
    st = stats(10, 10, 10)
    assert model_timeout(0, Config(), stats=st)[0] == 20
    st.record_timeout()
    t, info = model_timeout(0, Config(), stats=st)
    assert t == 30 and info["timeout_source"] == "bumped"
    st.record_timeout()
    assert model_timeout(0, Config(), stats=st)[0] == 45
    st.record_success(9.0, cold=False)
    t, info = model_timeout(0, Config(), stats=st)
    assert t == 20 and info["timeout_source"] == "adaptive"


def test_bump_never_exceeds_timeout_max():
    st = stats(40)
    for _ in range(10):
        st.record_timeout()
    assert st.bump == timeouts.MAX_BUMP
    assert model_timeout(0, Config(), stats=st)[0] == 90


def test_bump_applies_without_samples_too():
    assert model_timeout(0, Config(), stats=stats(bump=1.5))[0] == 45


def test_disabled_uses_static_formula():
    cfg = Config(adaptive_timeout=False)
    t, info = model_timeout(2000, cfg, stats=stats(3, 3, bump=3.0))
    assert t == timeouts.compute_timeout(2000, cfg) and info["timeout_source"] == "static"


def test_cold_calls_are_not_warm_samples_and_window_rolls():
    st = TimingStats(None, window=8)
    st.record_success(70.0, cold=True)
    assert st.warm == [] and st.cold == [70.0]
    for i in range(10):
        st.record_success(float(i + 1), cold=False)
    assert st.warm == [float(i) for i in range(3, 11)]


# ---- persistence ------------------------------------------------------------

def test_persistence_round_trip(tmp_path):
    path = tmp_path / "s" / timeouts.STATS_FILE
    st = TimingStats(path)
    st.record_success(12.5, cold=False)
    st.record_success(60.0, cold=True)
    st.record_timeout()
    again = TimingStats(path).load()
    assert again.warm == [12.5] and again.cold == [60.0] and again.bump == 1.5
    assert [p.name for p in path.parent.iterdir()] == [timeouts.STATS_FILE]  # no temp files left


@pytest.mark.parametrize("content", ["{not json", "[1, 2]", '{"warm": ["x"]}', '{"bump": "nan"}', ""])
def test_corrupt_or_missing_file_means_no_history(tmp_path, content):
    path = tmp_path / timeouts.STATS_FILE
    assert TimingStats(path).load().warm == []
    path.write_text(content, encoding="utf-8")
    st = TimingStats(path).load()
    assert st.warm == [] and st.bump == 1.0
    st.record_success(5.0, cold=False)  # and the file is repaired on the next write
    assert json.loads(path.read_text(encoding="utf-8"))["warm"] == [5.0]


def test_new_process_starts_with_history(cfg):
    timeouts.stats_for(cfg).record_success(40.0, cold=False)
    timeouts._registry.clear()  # simulates a fresh process (codex exec)
    t, info = model_timeout(0, cfg)
    assert t == 80 and info["samples"] == 1


def test_stats_file_survives_session_cleanup(cfg):
    from mcp_computer_use.sessions import cleanup, sessions_dir
    timeouts.stats_for(cfg).record_success(5.0, cold=False)
    cleanup(sessions_dir(cfg), ttl_hours=1e-9)
    assert (sessions_dir(cfg) / timeouts.STATS_FILE).is_file()


# ---- call budget ------------------------------------------------------------

def test_budget_cap():
    cfg = Config(call_budget_seconds=110)
    assert budget_cap(30, cfg, 10) == (30, False)
    assert budget_cap(135, cfg, 45) == (65, True)
    assert budget_cap(30, cfg, 106) == (None, True)          # < 5 s left: fail fast
    assert budget_cap(500, Config(call_budget_seconds=0), 400) == (500, False)  # 0 = off


def test_default_cold_ceiling_plus_autostart_fits_the_budget():
    cfg = Config()
    assert cfg.call_budget_seconds < 120  # Codex tool_timeout_sec
    worst_model, _ = model_timeout(100_000, cfg, model_loaded=False, stats=stats(500, bump=timeouts.MAX_BUMP))
    assert worst_model == cfg.timeout_max + cfg.cold_load_extra  # 135 s uncapped: over the host limit
    autostart_worst = autostart.WMI_TIMEOUT + cfg.autostart_timeout
    capped, was_capped = budget_cap(worst_model, cfg, autostart_worst)
    assert was_capped and autostart_worst + capped <= cfg.call_budget_seconds


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture
def clock(monkeypatch):
    c = FakeClock()
    monkeypatch.setattr(core, "_clock", c)
    return c


def test_look_caps_model_timeout_after_slow_autostart(cfg, fake_cu, clock, monkeypatch):
    def slow_start(c, ollama):
        clock.now += 70  # WMI launch + cold `ollama serve`
        return {"reachable": True, "started": True, "action": "started", "seconds": 70}
    monkeypatch.setattr(autostart, "ensure_ollama", slow_start)
    sent = []
    res = core.look_at_screen("q", "Claude", cfg, make_ollama(cfg, loaded=False, record=sent))
    assert res["timing"]["timeout_seconds"] == 40 and res["timing"]["budget_capped"]
    assert res["timing"]["cold"] is True and len(sent) == 1


def test_look_shrinks_autostart_wait_and_fails_fast_when_budget_is_gone(cfg, fake_cu, clock, monkeypatch):
    seen = []
    def start(c, ollama):
        seen.append(c.autostart_timeout)
        return {"reachable": True, "started": False, "action": "already_running", "seconds": 0}
    monkeypatch.setattr(autostart, "ensure_ollama", start)
    original = core.extract
    def slow_extract(*a, **k):
        clock.now += 106  # window reading ate the budget
        return original(*a, **k)
    monkeypatch.setattr(core, "extract", slow_extract)
    sent = []
    res = core.look_at_screen("q", "Claude", cfg, make_ollama(cfg, record=sent))
    assert seen == [0.0]
    assert res["error"]["code"] == "MODEL_TIMEOUT" and res["error"]["retryable"]
    assert "retry" in res["error"]["message"] and "timeout_seconds" not in res
    assert res["timing"]["timeout_seconds"] is None and res["timing"]["budget_capped"]
    assert sent == [] and res["extracted"]["last_message"]
    assert timeouts.stats_for(cfg).bump == 1.0  # the model never ran: no bump


def test_look_retry_path_is_budgeted(cfg, fake_cu, clock, monkeypatch):
    calls = []
    def start(c, ollama):
        calls.append(c.autostart_timeout)
        clock.now += 80
        return {"reachable": True, "started": True, "action": "started", "seconds": 80}
    monkeypatch.setattr(autostart, "ensure_ollama", start)
    state = {"n": 0}
    def chat(req, body):
        state["n"] += 1
        if state["n"] == 1:
            raise httpx.ConnectError("refused", request=req)
        return httpx.Response(200, json={"message": {"content": json.dumps(
            {"status": "finished", "summary": "ok", "confidence": 0.9})}})
    # mid-session call: the availability check is skipped, so the retry path runs autostart
    core.look_at_screen("q", "Claude", cfg, make_ollama(cfg), client="codex", session_id="s1")
    calls.clear()
    res = core.look_at_screen("q2", "Claude", cfg, make_ollama(cfg, chat=chat), client="codex", session_id="s1")
    assert len(calls) == 1
    assert res["timing"]["timeout_seconds"] == 30 and res["timing"]["cold"] is True  # 110 - 80
    assert res["timing"]["budget_capped"]


def test_look_reports_and_learns_timing(cfg, fake_cu):
    res = core.look_at_screen("q", "Claude", cfg, make_ollama(cfg))
    assert res["timing"]["timeout_source"] == "static" and res["timing"]["samples"] == 0
    size = res["timing"]["timeout_seconds"] - cfg.timeout_base  # per-1k-chars term of this window
    assert 0 < size < 5 and "model_wall_seconds" in res["timing"]
    res = core.look_at_screen("q", "Claude", cfg, make_ollama(cfg))
    assert res["timing"]["timeout_source"] == "adaptive" and res["timing"]["samples"] == 1
    assert res["timing"]["timeout_seconds"] == round(cfg.timeout_min + size, 2)  # a fast mock answer


def test_look_cold_call_not_a_warm_sample(cfg, fake_cu):
    core.look_at_screen("q", "Claude", cfg, make_ollama(cfg, loaded=False))
    st = timeouts.stats_for(cfg)
    assert st.warm == [] and len(st.cold) == 1


def test_look_timeout_bumps_next_call(cfg, fake_cu):
    def slow(req, body):
        raise httpx.ReadTimeout("slow", request=req)
    res = core.look_at_screen("q", "Claude", cfg, make_ollama(cfg, chat=slow))
    first = res["timing"]["timeout_seconds"]
    assert res["error"]["code"] == "MODEL_TIMEOUT" and res["timing"]["timeout_source"] == "static"
    res = core.look_at_screen("q", "Claude", cfg, make_ollama(cfg))
    assert res["timing"]["timeout_source"] == "bumped"
    assert res["timing"]["timeout_seconds"] == pytest.approx(first * 1.5, abs=0.01)
    res = core.look_at_screen("q", "Claude", cfg, make_ollama(cfg))
    assert res["timing"]["timeout_source"] == "adaptive"


def test_look_adaptive_disabled_writes_no_history(cfg, fake_cu):
    cfg.adaptive_timeout = False
    res = core.look_at_screen("q", "Claude", cfg, make_ollama(cfg))
    assert res["timing"]["timeout_source"] == "static"
    assert not (timeouts._stats_path(cfg)).exists()


def test_ping_fails_fast_when_budget_is_gone(cfg, clock, monkeypatch):
    def start(c, ollama):
        clock.now += 108
        return {"reachable": True, "started": True, "action": "started", "seconds": 108}
    monkeypatch.setattr(autostart, "ensure_ollama", start)
    sent = []
    out = core.ping(cfg, make_ollama(cfg, record=sent))
    assert not out["ok"] and out["error"].startswith("MODEL_TIMEOUT") and sent == []


def test_ping_timeout_is_capped(cfg, clock, monkeypatch):
    def start(c, ollama):
        clock.now += 50
        return {"reachable": True, "started": True, "action": "started", "seconds": 50}
    monkeypatch.setattr(autostart, "ensure_ollama", start)
    got = []
    ollama = make_ollama(cfg)
    monkeypatch.setattr(ollama, "ping_model", lambda t, keep_alive=None: got.append(t) or "pong")
    assert core.ping(cfg, ollama)["ok"] and got == [60]  # 30 + 45 cold, capped at 110 - 50


class PickOllama:
    def __init__(self):
        self.timeouts = []

    def is_loaded(self, *a, **k):
        return False

    def chat_schema(self, system, user, schema, timeout, **kw):
        self.timeouts.append(timeout)
        return {"ref": "@e1"}, {}


def test_pick_with_model_respects_budget():
    cfg = Config()
    cands = [Element("@e1", "button", "Save"), Element("@e2", "button", "Save as")]
    o = PickOllama()
    assert matching.pick_with_model("Save", cands, o, cfg, elapsed=90).ref == "@e1"
    assert o.timeouts == [20]  # 30 + 45 cold, capped at 110 - 90
    assert matching.pick_with_model("Save", cands, o, cfg, elapsed=107) is None
    assert o.timeouts == [20]  # no model call once the budget is gone


def test_config_keys_and_env_aliases():
    cfg = load_config(path="/nonexistent.toml", env={})
    assert (cfg.call_budget_seconds, cfg.adaptive_timeout, cfg.adaptive_window) == (110, True, 8)
    cfg = load_config(path="/nonexistent.toml", env={"MCP_CU_CALL_BUDGET_SECONDS": "90",
                                                     "MCP_CU_ADAPTIVE_TIMEOUT": "false",
                                                     "MCP_CU_ADAPTIVE_WINDOW": "4"})
    assert (cfg.call_budget_seconds, cfg.adaptive_timeout, cfg.adaptive_window) == (90, False, 4)
