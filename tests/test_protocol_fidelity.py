#!/usr/bin/env python3
"""Protocol-fidelity tests: prove the new sidecar path forwards the FULL
Anthropic /v1/messages request UNCHANGED (no flattening).

The OLD local_auth_bridge.py took only the last user text and dropped system,
assistant turns, tool defs, tool_result, thinking, images, cache_control,
metadata, and stream. The new path launches `claude` directly against the
raine sidecar, which receives claude's raw body and forwards it intact — the
wrapper never touches the request body.

We substitute a fake `claude` (fake_claude.py) that POSTs a canned FULL
Anthropic request, and a recording sidecar (recording_sidecar.py) that records
the raw body verbatim. We then assert every anti-flattening field survived.

Never points at the real auth root; never manipulates live creds; disposable
lease dir per test.
"""
import json, os, sys, time, subprocess
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE.parent))

# Point the router at our fake claude BEFORE importing it (CLAUDE_BIN is
# resolved at import time from FRY_CLAUDE_BIN). On Windows you cannot exec a
# .py directly (WinError 193), so use sys.executable as the bin and pass the
# fake script as the first passthrough arg.
os.environ["FRY_CLAUDE_BIN"] = sys.executable
FAKE_CLAUDE = str(HERE / "fake_claude.py")

import fry_anthropic_router as rtr
import fry_proxy_sidecar as sc

REC_SIDECAR = HERE / "recording_sidecar.py"


def _exe_hash(path):
    return sc._exe_sha256(str(path))


def _make_mgr(lease_dir):
    h = _exe_hash(REC_SIDECAR)
    def args(port):
        return [sys.executable, str(REC_SIDECAR), "--port", str(port)]
    return sc.RaineSidecarManager(REC_SIDECAR, h, lease_dir, serve_args_factory=args)


def _run_and_capture(tmp_path, provider, model_spec):
    """Run launch_via_sidecar with the fake claude + recording sidecar; return
    the recorded raw body (parsed JSON) and the child exit code."""
    rec_out = tmp_path / "rec_body.json"
    sent_out = tmp_path / "sent_body.json"
    # The recording sidecar reads FRY_REC_OUT from ITS env; the fake claude
    # reads FRY_FAKE_SENT_OUT from ITS env. Both inherit the env we set here,
    # but launch_via_sidecar builds a COPIED child env via _copy_env_for_sidecar
    # which preserves non-ANTHROPIC vars. Set them in os.environ now.
    os.environ["FRY_REC_OUT"] = str(rec_out)
    os.environ["FRY_FAKE_SENT_OUT"] = str(sent_out)
    try:
        os.remove(rec_out)
    except OSError:
        pass
    mgr = _make_mgr(tmp_path)
    # capture claude's stdout/stderr so they don't pollute pytest output
    rc = rtr.launch_via_sidecar(
        {}, "claude", model_spec, [FAKE_CLAUDE],
        dry_run=False, provider=provider, sidecar_manager=mgr,
        stdin=subprocess.DEVNULL,
        stdout=open(tmp_path / "claude_out.txt", "w"),
        stderr=open(tmp_path / "claude_err.txt", "w"),
    )
    # wait briefly for the recording file to flush
    for _ in range(20):
        if rec_out.exists() and rec_out.stat().st_size > 0:
            break
        time.sleep(0.1)
    body = None
    if rec_out.exists():
        try:
            body = json.loads(rec_out.read_text(encoding="utf-8"))
        except Exception:
            body = None
    return body, rc


def test_full_request_not_flattened(tmp_path):
    """The hallmark anti-flattening test: system blocks, tools, tool_result,
    thinking, image, cache_control, metadata, and stream ALL survive end-to-end
    through the new sidecar path."""
    body, rc = _run_and_capture(tmp_path, "xai", "xai,grok-4.5")
    assert rc == 0, f"fake claude exited {rc}"
    assert body is not None, "recording sidecar received no body (flattening or routing failure)"
    # system: array of 2 blocks, with cache_control + a system-reminder preserved
    assert isinstance(body.get("system"), list), "system must be an array (bridge flattened to nothing)"
    assert len(body["system"]) == 2, "both system blocks must survive"
    assert body["system"][0].get("cache_control") == {"type": "ephemeral"}, "cache_control dropped"
    assert "system-reminder" in body["system"][1]["text"], "system-reminder dropped"
    # tools + tool_choice
    assert isinstance(body.get("tools"), list) and len(body["tools"]) == 2, "tool defs dropped"
    assert {t["name"] for t in body["tools"]} == {"Read", "Bash"}, "tool names mangled"
    assert body.get("tool_choice") == {"type": "auto"}, "tool_choice dropped"
    # thinking
    assert body.get("thinking") == {"type": "enabled", "budget_tokens": 2048}, "thinking dropped"
    # metadata
    assert body.get("metadata") == {"user_id": "test-user"}, "metadata dropped"
    # stream
    assert body.get("stream") is True, "stream flag dropped (no streaming = the 120s kill bug)"
    # message history: 3 turns, last user has tool_result + image + text
    msgs = body.get("messages")
    assert isinstance(msgs, list) and len(msgs) == 3, "message history flattened to last turn only"
    assert msgs[0]["role"] == "user" and msgs[1]["role"] == "assistant", "prior turns dropped"
    last = msgs[2]
    types = sorted(b.get("type") for b in last["content"])
    assert "tool_result" in types and "image" in types and "text" in types, \
        f"tool_result/image/text content dropped from last turn; got {types}"
    assert last["content"][0].get("tool_use_id") == "tu_1", "tool_result lost its tool_use_id"
    # model routed to the requested provider (xai -> grok catalog -> grok-4.5)
    assert body.get("model") == "grok-4.5", f"model not routed to grok; got {body.get('model')}"


def test_sent_body_equals_recorded_body(tmp_path):
    """The body the fake claude SENT equals the body the sidecar RECORDED —
    i.e., nothing in the path mutated a single byte of the request."""
    body, rc = _run_and_capture(tmp_path, "xai", "xai,grok-4.5")
    assert rc == 0 and body is not None
    sent = json.loads((tmp_path / "sent_body.json").read_text(encoding="utf-8"))
    assert sent == body, "request body was mutated between claude and sidecar"


def test_env_points_claude_at_sidecar_not_legacy(tmp_path):
    """The child env must point ANTHROPIC_BASE_URL at the sidecar port and must
    NOT leak the legacy ollama/CCR base url."""
    os.environ["ANTHROPIC_BASE_URL"] = "http://0.0.0.0:11434"  # simulate legacy parent env
    os.environ["ANTHROPIC_AUTH_TOKEN"] = "ollama"
    os.environ["ANTHROPIC_API_KEY"] = "legacy-key"
    try:
        env = rtr._copy_env_for_sidecar(45123, "grok-4.5", "grok-4.5")
    finally:
        os.environ.pop("ANTHROPIC_BASE_URL", None)
        os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)
        os.environ.pop("ANTHROPIC_API_KEY", None)
    assert env["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:45123", "base url not redirected to sidecar"
    assert env["ANTHROPIC_AUTH_TOKEN"] != "ollama", "legacy ollama token leaked"
    assert "ANTHROPIC_API_KEY" not in env or env["ANTHROPIC_API_KEY"] != "legacy-key", "legacy api key leaked"
    assert env["ANTHROPIC_MODEL"] == "grok-4.5"
    assert env["ANTHROPIC_SMALL_FAST_MODEL"] == "grok-4.5", "small/fast model cross-routed"


def test_small_fast_model_same_provider(tmp_path):
    """Background/title/token-count traffic must NOT cross-route to another
    provider. small/fast resolves to the SAME provider catalog."""
    main, small = rtr._resolve_models("grok", "grok-4.5")
    assert main == "grok-4.5"
    assert small in sc.RAINE_MODEL_CATALOG["grok"], f"small model {small} not in grok catalog (cross-route)"
    main, small = rtr._resolve_models("codex", "gpt-5.4")
    assert main == "gpt-5.4"
    assert small in sc.RAINE_MODEL_CATALOG["codex"], f"small model {small} not in codex catalog (cross-route)"


def test_model_spec_parsing():
    # fry router provider keys: openai/xai/kimi (xai -> grok catalog)
    assert rtr._parse_model_spec("xai,grok-4.5", None) == ("xai", "grok-4.5")
    assert rtr._parse_model_spec(None, "kimi") == ("kimi", None)
    assert rtr._parse_model_spec("openai", None) == ("openai", None)  # bare provider
    try:
        rtr._parse_model_spec("bogus-model", None); assert False
    except rtr.RouterError:
        pass


def test_is_sidecar_provider():
    # fry router keys: openai/xai/kimi/opencode
    assert rtr.is_sidecar_provider("xai") is True
    assert rtr.is_sidecar_provider("openai") is True
    assert rtr.is_sidecar_provider("kimi") is True
    assert rtr.is_sidecar_provider("opencode") is True
    assert rtr.is_sidecar_provider("ollama") is False
    assert rtr.is_sidecar_provider(None) is False


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))