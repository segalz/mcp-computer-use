from mcp_computer_use.config import Config, load_config
from mcp_computer_use.timeouts import compute_timeout


def test_timeout_scales_with_size():
    cfg = Config(timeout_base=10, timeout_per_1k_chars=4, timeout_min=8, timeout_max=60, cold_load_extra=45)
    assert compute_timeout(0, cfg) == 10
    assert compute_timeout(2000, cfg) == 18
    assert compute_timeout(1000, cfg) < compute_timeout(5000, cfg)


def test_timeout_clamped():
    cfg = Config(timeout_base=1, timeout_per_1k_chars=100, timeout_min=8, timeout_max=60)
    assert compute_timeout(0, cfg) == 8
    assert compute_timeout(100_000, cfg) == 60


def test_cold_load_allowance():
    cfg = Config(cold_load_extra=45)
    assert compute_timeout(1000, cfg, model_loaded=False) == compute_timeout(1000, cfg) + 45


def test_default_warm_budget_has_headroom():
    # gpt-oss:20b at ~11 tok/s answers warm in ~12s; the old 10s base timed out (MODEL_TIMEOUT at 10.71s)
    cfg = Config()
    for chars in (0, 300, 1000, 2000):
        assert compute_timeout(chars, cfg, model_loaded=True) >= 25
    assert compute_timeout(2000, cfg) <= cfg.timeout_max
    assert compute_timeout(2000, cfg, model_loaded=False) == compute_timeout(2000, cfg) + cfg.cold_load_extra


def test_default_timeouts_from_load_config():
    cfg = load_config(path="/nonexistent.toml", env={})
    assert (cfg.timeout_base, cfg.timeout_min, cfg.timeout_max, cfg.cold_load_extra) == (30, 20, 90, 45)
    cfg = load_config(path="/nonexistent.toml", env={"MCP_CU_TIMEOUT_BASE": "12"})
    assert cfg.timeout_base == 12


def test_env_overrides(tmp_path):
    f = tmp_path / "c.toml"
    f.write_text('model = "qwen3:4b"\nmax_chars = 1500\n[unused]\n', encoding="utf-8")
    cfg = load_config(f, env={})
    assert cfg.model == "qwen3:4b" and cfg.max_chars == 1500
    cfg = load_config(f, env={"MODEL": "llama3", "MCP_CU_MAX_CHARS": "900",
                              "OLLAMA_URL": "127.0.0.1:11434/", "MCP_CU_HEALTH_HTTP": "true"})
    assert cfg.model == "llama3" and cfg.max_chars == 900
    assert cfg.ollama_url == "http://127.0.0.1:11434" and cfg.health_http is True


def test_defaults():
    cfg = load_config(path="/nonexistent.toml", env={})
    assert cfg.ollama_url == "http://127.0.0.1:11435" and cfg.model == "gpt-oss:20b"
