"""Auto-start of the local Ollama (mocked subprocess / network)."""
import json

import httpx
import pytest

from mcp_computer_use import autostart, core
from mcp_computer_use.config import Config, load_config
from mcp_computer_use.ollama_client import OllamaClient
from mcp_computer_use.sessions import sessions_dir


class World:
    """Fake local Ollama that is DOWN until spawn() is called (optionally after N polls)."""

    def __init__(self, cfg, up=False, up_after_polls=0, fail_chat_times=0):
        self.up, self.up_after, self.polls = up, up_after_polls, 0
        self.spawned = []
        self.fail_chat_times = fail_chat_times
        self.chat_calls = 0
        self.client = OllamaClient(cfg, transport=httpx.MockTransport(self.handle))

    def spawn(self, exe, host, port, log_path=None):
        self.spawned.append((exe, host, port))
        self.starting = True

    def handle(self, req):
        p = req.url.path
        if not self.up:
            if getattr(self, "starting", False):
                self.polls += 1
                if self.polls > self.up_after:
                    self.up = True
            if not self.up:
                raise httpx.ConnectError("refused", request=req)
        if p == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": "gpt-oss:20b"}]})
        if p == "/api/ps":
            return httpx.Response(200, json={"models": []})
        if p == "/api/chat":
            self.chat_calls += 1
            if self.fail_chat_times > 0:
                self.fail_chat_times -= 1
                self.up = False
                self.starting = False
                raise httpx.ConnectError("refused", request=req)
            return httpx.Response(200, json={"message": {"content": json.dumps(
                {"status": "finished", "summary": "ok", "confidence": 0.9})},
                "eval_count": 5, "eval_duration": 10**9, "total_duration": 10**9})
        return httpx.Response(404)


@pytest.fixture
def local_cfg(tmp_path):
    return Config(ollama_url="http://127.0.0.1:11435", model="gpt-oss:20b",
                  sessions_dir=str(tmp_path / ".sessions"), autostart_timeout=3)


@pytest.fixture
def env(monkeypatch, local_cfg):
    def make(**kw):
        w = World(local_cfg, **kw)
        monkeypatch.setattr(autostart, "_spawn", w.spawn)
        monkeypatch.setattr(autostart, "find_ollama_exe", lambda: r"C:\Ollama\ollama.exe")
        monkeypatch.setattr(autostart, "port_open", lambda h, p, timeout=0.5: w.up or bool(w.spawned))
        monkeypatch.setattr(autostart.time, "sleep", lambda s: None)
        return w
    return make


def test_target_only_local():
    assert autostart.target(Config(ollama_url="http://127.0.0.1:11435")) == ("127.0.0.1", 11435)
    assert autostart.target(Config(ollama_url="http://localhost")) == ("127.0.0.1", 11434)
    assert autostart.target(Config(ollama_url="http://Zvis-MacBook-Pro.local:11434")) is None


def test_starts_when_down_and_waits_for_tags(local_cfg, env):
    w = env(up_after_polls=2)
    r = autostart.ensure_ollama(local_cfg, w.client)
    assert r["started"] and r["reachable"] and r["action"] == "started"
    assert w.spawned == [(r"C:\Ollama\ollama.exe", "127.0.0.1", 11435)]
    assert autostart.state["autostarted"] is True
    assert not (sessions_dir(local_cfg) / autostart.LOCK_NAME).exists()  # lock released


def test_no_start_when_running(local_cfg, env):
    w = env(up=True)
    assert autostart.ensure_ollama(local_cfg, w.client)["action"] == "already_running"
    assert w.spawned == []


def test_disabled_and_remote_never_start(local_cfg, env, monkeypatch):
    w = env()
    local_cfg.autostart_ollama = False
    assert autostart.ensure_ollama(local_cfg, w.client)["action"] == "disabled"
    local_cfg.autostart_ollama = True
    local_cfg.ollama_url = "http://Zvis-MacBook-Pro.local:11434"
    assert autostart.ensure_ollama(local_cfg, w.client)["action"] == "not_local"
    assert w.spawned == []


def test_lock_prevents_double_start(local_cfg, env):
    w = env()
    lock = sessions_dir(local_cfg) / autostart.LOCK_NAME
    lock.parent.mkdir(parents=True)
    lock.write_text("999")  # another server process is starting it
    local_cfg.autostart_timeout = 0.2
    r = autostart.ensure_ollama(local_cfg, w.client)
    assert r["action"] == "waiting_for_other_start" and w.spawned == [] and not r["reachable"]


def test_stale_lock_is_ignored(local_cfg, env):
    import os, time
    w = env()
    lock = sessions_dir(local_cfg) / autostart.LOCK_NAME
    lock.parent.mkdir(parents=True)
    lock.write_text("999")
    old = time.time() - 120
    os.utime(lock, (old, old))
    assert autostart.ensure_ollama(local_cfg, w.client)["started"] is True


def test_port_busy_does_not_spawn(local_cfg, env, monkeypatch):
    w = env()
    monkeypatch.setattr(autostart, "port_open", lambda h, p, timeout=0.5: True)
    local_cfg.autostart_timeout = 0.2
    r = autostart.ensure_ollama(local_cfg, w.client)
    assert r["action"] == "port_busy_waiting" and w.spawned == []


def test_missing_exe(local_cfg, env, monkeypatch):
    w = env()
    monkeypatch.setattr(autostart, "find_ollama_exe", lambda: None)
    assert autostart.ensure_ollama(local_cfg, w.client)["action"] == "ollama_not_found"


def test_find_exe_localappdata(tmp_path, monkeypatch):
    exe = tmp_path / "Programs" / "Ollama" / "ollama.exe"
    exe.parent.mkdir(parents=True)
    exe.write_text("")
    monkeypatch.setattr(autostart.shutil, "which", lambda n: None)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert autostart.find_ollama_exe() == str(exe)


class FakeHandle:
    closed = False

    def Close(self):
        self.closed = True


class FakePopen:
    """Records Popen calls; optionally refuses CREATE_BREAKAWAY_FROM_JOB like a locked job."""
    calls: list = []
    refuse_breakaway = False

    def __init__(self, args, **kw):
        self.args, self.kw, self.pid, self.killed = args, kw, 4242, False
        self._handle, self._child_created = FakeHandle(), True
        FakePopen.calls.append(self)
        if FakePopen.refuse_breakaway and kw.get("creationflags", 0) & autostart._CREATE_BREAKAWAY_FROM_JOB:
            raise PermissionError(5, "Access is denied")

    def kill(self):
        self.killed = True

    def wait(self, timeout=None):
        return 0


@pytest.fixture
def popen(monkeypatch):
    FakePopen.calls, FakePopen.refuse_breakaway = [], False
    monkeypatch.setattr(autostart.subprocess, "Popen", FakePopen)
    monkeypatch.setattr(autostart.sys, "platform", "win32")
    wmi = []
    monkeypatch.setattr(autostart, "_spawn_wmi", lambda exe, host, port, log: wmi.append((exe, host, port, log)) or 77)
    return wmi


def test_spawn_sets_ollama_host_only_for_child(monkeypatch, popen, tmp_path):
    import os
    monkeypatch.setattr(autostart, "in_job", lambda pid: False)
    monkeypatch.setenv("OLLAMA_HOST", "Zvis-MacBook-Pro.local:11434")
    assert autostart._spawn_impl("ollama", "127.0.0.1", 11435, log_path=tmp_path / "x.log") == "breakaway"
    p = FakePopen.calls[0]
    assert p.args == ["ollama", "serve"] and p.kw["env"]["OLLAMA_HOST"] == "127.0.0.1:11435"
    assert p.kw["creationflags"] & autostart._CREATE_BREAKAWAY_FROM_JOB
    assert os.environ["OLLAMA_HOST"] == "Zvis-MacBook-Pro.local:11434"  # parent untouched
    assert p.kw["stdin"] is autostart.subprocess.DEVNULL and p.kw["stdout"].name == str(tmp_path / "x.log")
    assert p.kw["stdout"].closed  # the parent does not keep the log handle
    assert popen == []  # no WMI needed
    assert p._handle.closed and p._child_created is False and not p.killed  # detached, still running


def test_spawn_ipv6_host_is_bracketed(monkeypatch, popen):
    monkeypatch.setattr(autostart, "in_job", lambda pid: False)
    autostart._spawn_impl("ollama", "::1", 11435)
    assert FakePopen.calls[0].kw["env"]["OLLAMA_HOST"] == "[::1]:11435"
    assert autostart.host_port("127.0.0.1", 1) == "127.0.0.1:1"
    assert "set OLLAMA_HOST=[::1]:11435&&" in autostart._wmi_cmdline("o.exe", "::1", 11435, autostart.Path("l"))


def test_detach_real_child_no_resource_warning():
    import gc, sys, warnings
    proc = autostart.subprocess.Popen([sys.executable, "-c", "pass"])
    autostart._detach(proc)
    with warnings.catch_warnings():
        warnings.simplefilter("error", ResourceWarning)
        del proc
        gc.collect()


def test_spawn_without_log_uses_devnull(monkeypatch, popen):
    monkeypatch.setattr(autostart, "in_job", lambda pid: None)  # cannot verify: keep breakaway
    assert autostart._spawn_impl("ollama", "127.0.0.1", 11435) == "breakaway"
    assert FakePopen.calls[0].kw["stdout"] is autostart.subprocess.DEVNULL


def test_spawn_uses_wmi_when_breakaway_refused(monkeypatch, popen, tmp_path):
    FakePopen.refuse_breakaway = True
    monkeypatch.setattr(autostart, "in_job", lambda pid: pytest.fail("no child to check"))
    log = tmp_path / "s" / "ollama-autostart.log"
    assert autostart._spawn_impl(r"C:\O\ollama.exe", "127.0.0.1", 11435, log_path=log) == "wmi"
    assert popen == [(r"C:\O\ollama.exe", "127.0.0.1", 11435, log)]
    assert len(FakePopen.calls) == 1  # only the refused breakaway attempt


def test_spawn_uses_wmi_when_child_still_in_job(monkeypatch, popen, tmp_path):
    monkeypatch.setattr(autostart, "in_job", lambda pid: True)
    assert autostart._spawn_impl("ollama", "127.0.0.1", 11435, log_path=tmp_path / "l.log") == "wmi"
    assert FakePopen.calls[0].killed and len(popen) == 1


def test_spawn_last_resort_popen_when_wmi_fails(monkeypatch, popen, tmp_path):
    FakePopen.refuse_breakaway = True
    def fail(*a):
        raise OSError("WMI down")
    monkeypatch.setattr(autostart, "_spawn_wmi", fail)
    assert autostart._spawn_impl("ollama", "127.0.0.1", 11435, log_path=tmp_path / "l.log") == "popen"
    assert not FakePopen.calls[-1].kw["creationflags"] & autostart._CREATE_BREAKAWAY_FROM_JOB
    assert FakePopen.calls[-1]._handle.closed and not FakePopen.calls[-1].killed


def test_spawn_posix_new_session(monkeypatch, popen):
    monkeypatch.setattr(autostart.sys, "platform", "linux")
    assert autostart._spawn_impl("ollama", "127.0.0.1", 11435) == "setsid"
    assert FakePopen.calls[0].kw["start_new_session"] is True


def test_wmi_cmdline_sets_host_for_child_and_logs():
    cl = autostart._wmi_cmdline(r"C:\Program Files\Ollama\ollama.exe", "127.0.0.1", 11435,
                                autostart.Path(r"C:\s\ollama-autostart.log"))
    assert cl == (r'cmd.exe /d /s /c "set OLLAMA_HOST=127.0.0.1:11435&& '
                  r'"C:\Program Files\Ollama\ollama.exe" serve > "C:\s\ollama-autostart.log" 2>&1"')
    with pytest.raises(OSError):
        autostart._wmi_cmdline(r"C:\100%\ollama.exe", "127.0.0.1", 1, autostart.Path("x"))


@pytest.mark.parametrize("bad", ['C:\\a"b', "C:\\a\rb", "C:\\a\nb", "C:\\a\x00b"])
def test_wmi_cmdline_rejects_unquotable_paths(bad):
    with pytest.raises(OSError):
        autostart._wmi_cmdline(bad + ".exe", "127.0.0.1", 1, autostart.Path("x"))
    with pytest.raises(OSError):
        autostart._wmi_cmdline("ollama.exe", "127.0.0.1", 1, autostart.Path(bad + ".log"))


def test_spawn_wmi_runs_powershell_with_env_inputs(monkeypatch, tmp_path):
    seen = {}
    def run(args, **kw):
        seen["args"], seen["kw"] = args, kw
        return autostart.subprocess.CompletedProcess(args, 0, "0 5150\n", "")
    monkeypatch.setattr(autostart.subprocess, "run", run)
    monkeypatch.setenv("OLLAMA_HOST", "Zvis-MacBook-Pro.local:11434")
    pid = autostart._spawn_wmi(r"C:\O\ollama.exe", "127.0.0.1", 11435, tmp_path / "l.log")
    assert pid == 5150 and seen["args"][0] == "powershell.exe" and "Win32_Process" in seen["args"][-1]
    env = seen["kw"]["env"]
    assert "OLLAMA_HOST=127.0.0.1:11435" in env["MCP_CU_WMI_CMDLINE"] and env["MCP_CU_WMI_CWD"] == r"C:\O"
    assert env["OLLAMA_HOST"] == "Zvis-MacBook-Pro.local:11434"  # only the cmd child gets the local value
    assert r"C:\O\ollama.exe" not in seen["args"][-1]  # paths travel in env, not in the script


@pytest.mark.parametrize("out,rc", [("8 0\n", 0), ("", 1), ("garbage", 0)])
def test_spawn_wmi_failure_raises(monkeypatch, tmp_path, out, rc):
    monkeypatch.setattr(autostart.subprocess, "run",
                        lambda args, **kw: autostart.subprocess.CompletedProcess(args, rc, out, "err"))
    with pytest.raises(OSError):
        autostart._spawn_wmi("ollama.exe", "127.0.0.1", 11435, tmp_path / "l.log")


@pytest.mark.parametrize("out", [
    "WARNING: something odd\n0 4242\n",
    "0 1\nWARNING: 2 noisy words\n0 4242\n\n",
])
def test_spawn_wmi_parses_last_result_line(monkeypatch, tmp_path, out):
    monkeypatch.setattr(autostart.subprocess, "run",
                        lambda args, **kw: autostart.subprocess.CompletedProcess(args, 0, out, ""))
    assert autostart._spawn_wmi("ollama.exe", "127.0.0.1", 11435, tmp_path / "l.log") == 4242


def test_spawn_wmi_timeout_is_oserror(monkeypatch, tmp_path):
    def run(args, **kw):
        raise autostart.subprocess.TimeoutExpired(args, 30)
    monkeypatch.setattr(autostart.subprocess, "run", run)
    with pytest.raises(OSError):
        autostart._spawn_wmi("ollama.exe", "127.0.0.1", 11435, tmp_path / "l.log")


_JOBQ = '''\
import ctypes


def job_info():
    """(pids in the innermost job of this process, its LimitFlags); ([], 0) when in no job."""
    k = ctypes.windll.kernel32
    k.GetCurrentProcess.restype = ctypes.c_void_p
    res = ctypes.c_int(0)
    if not k.IsProcessInJob(ctypes.c_void_p(k.GetCurrentProcess()), None, ctypes.byref(res)) or not res.value:
        return [], 0

    class Ids(ctypes.Structure):
        _fields_ = [("assigned", ctypes.c_uint32), ("listed", ctypes.c_uint32), ("ids", ctypes.c_size_t * 4096)]
    ids, limits = Ids(), (ctypes.c_uint32 * 64)()
    k.QueryInformationJobObject(None, 3, ctypes.byref(ids), ctypes.sizeof(ids), None)  # BasicProcessIdList
    k.QueryInformationJobObject(None, 2, limits, ctypes.sizeof(limits), None)  # BasicLimitInformation
    return [ids.ids[i] for i in range(min(ids.listed, 4096))], limits[4]  # LimitFlags at offset 16
'''

_CHILD = _JOBQ + '''
import os, sys, time
pids, flags = job_info()
with open(sys.argv[1] + ".tmp", "w") as f:
    f.write("%s|%d|%d" % (os.environ.get("OLLAMA_HOST", ""), os.getpid(), flags))
os.replace(sys.argv[1] + ".tmp", sys.argv[1])
end = time.time() + 15  # stay alive until the test has compared jobs
while not os.path.exists(sys.argv[1] + ".done") and time.time() < end:
    time.sleep(0.1)
'''
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000


@pytest.mark.skipif(autostart.sys.platform != "win32", reason="WMI is Windows-only")
def test_wmi_path_real_child_gets_host_and_leaves_our_job(tmp_path, monkeypatch):
    """Real PowerShell/WMI launch of the real cmd.exe command line, with a harmless python child
    in place of `ollama serve`. Skipped when PowerShell/WMI is unavailable."""
    import sys
    if not autostart.shutil.which("powershell.exe"):
        pytest.skip("powershell.exe not available")
    script, out, log = tmp_path / "child.py", tmp_path / "out.txt", tmp_path / "child.log"
    script.write_text(_CHILD)
    monkeypatch.setenv("OLLAMA_HOST", "Zvis-MacBook-Pro.local:11434")  # must not reach the child
    exe = getattr(sys, "_base_executable", sys.executable)  # not the venv redirector (it adds its own job)
    t0 = autostart.time.monotonic()
    try:
        autostart._spawn_wmi(exe, "127.0.0.1", 11435, log, args=f'-I "{script}" "{out}"')
    except OSError as e:
        pytest.skip(f"WMI unavailable: {e}")
    try:
        while not out.exists() and autostart.time.monotonic() - t0 < 18:
            autostart.time.sleep(0.1)
        assert out.exists(), log.read_text(errors="replace") if log.exists() else "no child output"
        host, child_pid, child_flags = out.read_text().split("|")
        ns: dict = {}
        exec(_JOBQ, ns)
        our_pids, _ = ns["job_info"]()
        assert host == "127.0.0.1:11435"
        assert int(child_pid) not in our_pids  # did not inherit the test process's job
        assert not int(child_flags) & JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE  # any job it is in won't kill it
    finally:
        (tmp_path / "out.txt.done").write_text("")


@pytest.mark.skipif(autostart.sys.platform != "win32", reason="kernel32")
def test_in_job_real_and_bad_pid():
    import os
    assert autostart.in_job(os.getpid()) in (True, False)
    assert autostart.in_job(0) is None  # cannot open: unknown, never raises


def test_ensure_records_method_and_log_path(local_cfg, env, monkeypatch):
    w = env(up_after_polls=0)
    seen = {}
    def spawn(exe, host, port, log_path=None):
        seen["log"] = log_path
        w.spawn(exe, host, port)
        return "wmi"
    monkeypatch.setattr(autostart, "_spawn", spawn)
    r = autostart.ensure_ollama(local_cfg, w.client)
    assert r["method"] == "wmi" and autostart.state["method"] == "wmi"
    assert seen["log"] == sessions_dir(local_cfg) / autostart.LOG_NAME


def test_slow_spawn_does_not_eat_the_readiness_budget(local_cfg, env, monkeypatch):
    w = env(up_after_polls=3)
    t = [0.0]
    def slow_spawn(exe, host, port, log_path=None):
        t[0] += 20  # e.g. a slow PowerShell/WMI launch, longer than autostart_timeout (3 s)
        w.spawn(exe, host, port)
        return "wmi"
    def sleep(s):
        t[0] += s
    monkeypatch.setattr(autostart, "_spawn", slow_spawn)
    r = autostart.ensure_ollama(local_cfg, w.client, sleep=sleep, clock=lambda: t[0])
    assert r["reachable"] and r["started"] and r["seconds"] >= 20
    assert autostart.WMI_TIMEOUT < Config().autostart_timeout


def test_ensure_keeps_polling_until_ready(local_cfg, env):
    w = env(up_after_polls=40)  # ~20 s of 0.5 s polls: longer than the old 10 s default
    local_cfg.autostart_timeout = Config().autostart_timeout
    t = [0.0]
    def sleep(s):
        t[0] += s
    r = autostart.ensure_ollama(local_cfg, w.client, sleep=sleep, clock=lambda: t[0])
    assert r["reachable"] and r["started"] and 15 <= r["seconds"] < 30


def test_look_at_screen_autostarts_on_stateless_call(local_cfg, env, fake_cu):
    w = env(up_after_polls=1)
    res = core.look_at_screen("Is Claude finished?", "Claude", local_cfg, w.client)
    assert "error" not in res and res["summary"] == "ok"
    assert res["memory"]["ollama_autostarted"] is True and len(w.spawned) == 1


def test_check_only_on_first_call_of_session(local_cfg, env, fake_cu, monkeypatch):
    w = env(up=True)
    calls = []
    orig = autostart.ensure_ollama
    monkeypatch.setattr(autostart, "ensure_ollama", lambda c, o, **k: calls.append(1) or orig(c, o, **k))
    core.look_at_screen("q1", "Claude", local_cfg, w.client, client="codex", session_id="s")
    core.look_at_screen("q2", "Claude", local_cfg, w.client, client="codex", session_id="s")
    core.look_at_screen("q3", "Claude", local_cfg, w.client, client="codex", session_id="s")
    assert len(calls) == 1  # only the first call of the session
    core.look_at_screen("q4", "Claude", local_cfg, w.client)
    core.look_at_screen("q5", "Claude", local_cfg, w.client)
    assert len(calls) == 3  # every call without session_id


def test_mid_session_unreachable_restarts_once_and_retries(local_cfg, env, fake_cu):
    w = env(up=True)
    core.look_at_screen("q1", "Claude", local_cfg, w.client, client="codex", session_id="s")
    w.fail_chat_times = 1  # Ollama dies before the next chat
    res = core.look_at_screen("q2", "Claude", local_cfg, w.client, client="codex", session_id="s")
    assert "error" not in res and res["summary"] == "ok"
    assert len(w.spawned) == 1 and w.chat_calls == 3


def test_mid_session_gives_up_after_one_retry(local_cfg, env, fake_cu):
    w = env(up=True)
    core.look_at_screen("q1", "Claude", local_cfg, w.client, client="codex", session_id="s")
    w.fail_chat_times = 5
    res = core.look_at_screen("q2", "Claude", local_cfg, w.client, client="codex", session_id="s")
    assert res["error"]["code"] == "OLLAMA_UNREACHABLE" and len(w.spawned) == 1 and w.chat_calls == 3


def test_health_reports_autostart(local_cfg, env):
    w = env(up_after_polls=0)
    autostart.ensure_ollama(local_cfg, w.client)
    h = core.health(local_cfg, w.client)
    assert h["ollama_autostarted"] is True and h["autostart_enabled"] is True


def test_config_env_autostart():
    c = load_config(env={"MCP_CU_AUTOSTART_OLLAMA": "0", "MCP_CU_AUTOSTART_TIMEOUT": "20"})
    assert c.autostart_ollama is False and c.autostart_timeout == 20.0
    assert Config().autostart_ollama is True and Config().autostart_timeout == 30.0


# ---------------------------------------------------------------- ping
class PingOllama:
    def __init__(self, cfg, reply="pong", loaded=False, down=False, status=200):
        self.reply, self.loaded, self.down, self.status = reply, loaded, down, status
        self.bodies = []
        self.client = OllamaClient(cfg, transport=httpx.MockTransport(self.handle))

    def handle(self, req):
        if self.down:
            raise httpx.ConnectError("refused", request=req)
        p = req.url.path
        if p == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": "gpt-oss:20b"}]})
        if p == "/api/ps":
            return httpx.Response(200, json={"models": [{"name": "gpt-oss:20b", "size": 1}] if self.loaded else []})
        if p == "/api/chat":
            self.bodies.append(json.loads(req.content))
            if self.status != 200:
                return httpx.Response(self.status, json={})
            return httpx.Response(200, json={"message": {"content": self.reply}})
        return httpx.Response(404)


def test_ping_ok_cold(cfg, fake_cu):
    o = PingOllama(cfg, reply="pong")
    r = core.ping(cfg, o.client)
    assert r["ok"] and r["server"] and r["ollama"] and r["model_reply_ok"] and r["cold"] is True
    assert set(r) >= {"ok", "server", "ollama", "model_reply_ok", "model", "seconds", "cold"} and "error" not in r
    b = o.bodies[0]
    assert b["options"]["num_ctx"] == cfg.num_ctx  # same as chat: the model ping loads is reused
    assert b["keep_alive"] == "3m" and b["options"]["num_predict"] <= 128 and "pong" in b["messages"][0]["content"]
    assert fake_cu.calls == []  # no screen reading


def test_ping_warm_and_bad_reply(cfg):
    r = core.ping(cfg, PingOllama(cfg, reply="Pong.", loaded=True).client)
    assert r["ok"] and r["cold"] is False
    r = core.ping(cfg, PingOllama(cfg, reply="hello", loaded=True).client)
    assert not r["ok"] and r["ollama"] and not r["model_reply_ok"] and "unexpected" in r["error"]


def test_ping_ollama_down_not_local(cfg):
    r = core.ping(cfg, PingOllama(cfg, down=True).client)  # cfg URL is not localhost: no autostart
    assert r == {**r, "ok": False, "server": True, "ollama": False, "model_reply_ok": False}
    assert "not_local" in r["error"]


def test_ping_autostarts(local_cfg, env):
    w = env(up_after_polls=1)
    r = core.ping(local_cfg, w.client)
    assert r["ollama"] and r["ollama_autostarted"] and len(w.spawned) == 1


def test_ping_model_missing(cfg):
    r = core.ping(cfg, PingOllama(cfg, status=404).client)
    assert not r["ok"] and r["error"].startswith("MODEL_NOT_FOUND")


def test_ping_after_autostart_is_cold(local_cfg, env):
    w = env(up_after_polls=0)
    r = core.ping(local_cfg, w.client)
    assert r["ollama_autostarted"] and r["cold"] is True


def test_look_after_autostart_gets_cold_budget(local_cfg, env, fake_cu, monkeypatch):
    w = env(up_after_polls=0)
    monkeypatch.setattr(w.client, "is_loaded", lambda *a, **k: True)  # /api/ps says loaded
    res = core.look_at_screen("Is Claude finished?", "Claude", local_cfg, w.client)
    assert res["memory"]["was_loaded"] is False and res["memory"]["ollama_autostarted"] is True


@pytest.mark.parametrize("ctx,loaded", [(4096, True), (1024, False), (None, True), (0, True),
                                        ("4096", True), ("1024", False), ("abc", True), ([1], True)])
def test_is_loaded_checks_context_length(cfg, ctx, loaded):
    entry = {"name": "gpt-oss:20b", "size": 1}
    if ctx is not None:
        entry["context_length"] = ctx
    client = OllamaClient(cfg, transport=httpx.MockTransport(
        lambda req: httpx.Response(200, json={"models": [entry]})))
    assert client.is_loaded(num_ctx=4096) is loaded
    assert client.is_loaded() is True  # without num_ctx: presence only (release/health unchanged)


def test_look_with_other_context_length_is_cold(cfg, fake_cu):
    ps = {"models": [{"name": "gpt-oss:20b", "size": 1, "context_length": 1024}]}
    def handler(req):
        if req.url.path == "/api/ps":
            return httpx.Response(200, json=ps)
        return httpx.Response(200, json={"message": {"content": json.dumps(
            {"status": "finished", "summary": "ok", "confidence": 0.9})}})
    res = core.look_at_screen("Is Claude finished?", "Claude", cfg,
                              OllamaClient(cfg, transport=httpx.MockTransport(handler)))
    assert res["memory"]["was_loaded"] is False
