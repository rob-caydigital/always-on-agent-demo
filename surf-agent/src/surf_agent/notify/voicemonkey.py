"""Voice Monkey API v3 (https://voicemonkey.io/docs/api.html).

A wake-up is two calls, because a v3 routine trigger cannot carry text:
  1. POST /trigger  -> fires the user's "Surf Wake" Alexa routine (DND off, volume, alarm sound).
  2. POST /announce -> the Speaker device says the spoken summary.

Without VOICEMONKEY_TOKEN the notifier runs in SIMULATED mode: it builds and records the exact
calls it would send (for demos and tests) and reports the wake-up as made.
"""

from __future__ import annotations

import time
from collections.abc import Callable

import httpx

from .base import NotifyCall, NotifyResult

API = "https://api-v3.voicemonkey.io"
RETRYABLE = {429, 500, 502, 503, 504}


class VoiceMonkeyNotifier:
    def __init__(
        self,
        token: str | None,
        trigger_device: str,
        speaker_device: str | None,
        voice: str = "Joanna",
        language: str = "en-US",
        announce_delay_s: float = 20,
        retries: int = 2,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.token = token or None
        self.trigger_device = trigger_device
        self.speaker_device = speaker_device
        self.voice = voice
        self.language = language
        self.announce_delay_s = announce_delay_s
        self.retries = retries
        self.sleep = sleep
        self.client = httpx.Client(timeout=15, transport=transport)

    @property
    def simulated(self) -> bool:
        return self.token is None

    def _bodies(self, message: str) -> tuple[dict, dict | None]:
        trigger = {"device": self.trigger_device}
        announce = (
            {"device": self.speaker_device, "speech": message, "voice": self.voice, "language": self.language}
            if self.speaker_device
            else None
        )
        return trigger, announce

    def _post(self, path: str, body: dict) -> NotifyCall:
        call = NotifyCall(method="POST", url=f"{API}{path}", body=body)
        for attempt in range(self.retries + 1):
            try:
                r = self.client.post(call.url, json=body, headers={"Authorization": f"Bearer {self.token}"})
                call.status = r.status_code
                try:
                    call.response = r.json()
                except ValueError:
                    call.response = r.text[:200]
            except httpx.HTTPError as e:
                call.status, call.response = None, f"{type(e).__name__}: {e}"
            ok = call.status == 200 and isinstance(call.response, dict) and call.response.get("success")
            if ok or (call.status is not None and call.status not in RETRYABLE) or attempt == self.retries:
                return call
            lockout = call.response.get("lockoutUntil") if isinstance(call.response, dict) else None
            self.sleep(5 if lockout else 2 * (attempt + 1))
        return call

    @staticmethod
    def _ok(call: NotifyCall) -> bool:
        return call.status == 200 and isinstance(call.response, dict) and bool(call.response.get("success"))

    def wake(self, message: str) -> NotifyResult:
        trigger_body, announce_body = self._bodies(message)
        if self.simulated:
            calls = [NotifyCall(method="POST", url=f"{API}/trigger", body=trigger_body,
                                status=200, response={"success": True, "data": "SIMULATED"})]
            if announce_body:
                calls.append(NotifyCall(method="POST", url=f"{API}/announce", body=announce_body,
                                        status=200, response={"success": True, "data": "SIMULATED"}))
            return NotifyResult(
                mode="simulated", woke=True, announce_ok=bool(announce_body), calls=calls,
                detail="SIMULATED: no VOICEMONKEY_TOKEN set; assuming the routine fired and the speaker announced",
            )

        trig = self._post("/trigger", trigger_body)
        if not self._ok(trig):
            return NotifyResult(mode="live", woke=False, calls=[trig],
                                detail=f"trigger failed: HTTP {trig.status} {trig.response}")
        calls = [trig]
        announce_ok = None
        if announce_body:
            self.sleep(self.announce_delay_s)  # let the routine's alarm sound play first
            ann = self._post("/announce", announce_body)
            calls.append(ann)
            announce_ok = self._ok(ann)
        detail = "routine triggered" + ("" if announce_ok in (None, True) else "; announcement failed")
        # The routine is what wakes you; a failed announcement still counts as woken (ARCHITECTURE §7).
        return NotifyResult(mode="live", woke=True, calls=calls, announce_ok=announce_ok, detail=detail)
