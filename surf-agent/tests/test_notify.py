"""Voice Monkey v3 notifier: live (mocked HTTP), retries, failures, and simulated mode."""

import json

import httpx

from surf_agent.notify.voicemonkey import VoiceMonkeyNotifier


def make(handler, token="secret-token", speaker="bedroom"):
    return VoiceMonkeyNotifier(
        token=token, trigger_device="surf-wake", speaker_device=speaker,
        transport=httpx.MockTransport(handler), sleep=lambda s: None,
    )


def ok(request):
    return httpx.Response(200, json={"success": True, "data": "OK"})


def test_simulated_without_token():
    n = VoiceMonkeyNotifier(token=None, trigger_device="surf-wake", speaker_device="bedroom")
    r = n.wake("Surf's up.")
    assert r.mode == "simulated" and r.woke
    assert [c.url.rsplit("/", 1)[-1] for c in r.calls] == ["trigger", "announce"]
    assert r.calls[1].body["speech"] == "Surf's up."


def test_live_trigger_then_announce_with_bearer_auth():
    seen = []

    def handler(request):
        seen.append((request.url.path, request.headers["authorization"], json.loads(request.content)))
        return ok(request)

    r = make(handler).wake("Surf's up.")
    assert r.mode == "live" and r.woke and r.announce_ok
    assert [s[0] for s in seen] == ["/trigger", "/announce"]
    assert all(s[1] == "Bearer secret-token" for s in seen)
    assert seen[0][2] == {"device": "surf-wake"}
    assert seen[1][2]["speech"] == "Surf's up."
    # The token never appears in the recorded calls.
    assert "secret-token" not in r.model_dump_json()


def test_retries_upstream_failure():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if request.url.path == "/trigger" and calls["n"] == 1:
            return httpx.Response(500, json={"success": False, "error": "ALEXA_TRIGGER_FAILED"})
        return ok(request)

    r = make(handler).wake("hi")
    assert r.woke and calls["n"] == 3  # trigger x2, announce x1


def test_bad_token_fails_fast_without_retry():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(401, json={"success": False, "error": "INVALID_TOKEN"})

    r = make(handler).wake("hi")
    assert not r.woke and calls["n"] == 1 and "401" in r.detail


def test_failed_announcement_still_counts_as_woken():
    def handler(request):
        if request.url.path == "/announce":
            return httpx.Response(404, json={"success": False, "error": "DEVICE_NOT_FOUND"})
        return ok(request)

    r = make(handler).wake("hi")
    assert r.woke and r.announce_ok is False
