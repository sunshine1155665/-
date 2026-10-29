"""Probe a local System One (Jev-compatible) decision endpoint.

The fatfish gate talks to ``POST /v1/systemone``. This module is a small,
dependency-free smoke test: it sends one request containing a ``noul`` and a
``choice`` question, then reports the round-trip latency and the raw answers.

Usage::

    python -m fatfish.decision.probe --base-url http://127.0.0.1:8009
    python -m fatfish.decision.probe --base-url http://127.0.0.1:8009 --model von-1.2

Exit status is 0 on a valid response, 2 when the endpoint cannot be reached or
returns something that is not a System One answer object.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from typing import Any

DEFAULT_BASE_URL = "http://127.0.0.1:8009"
DEFAULT_TIMEOUT = 3.0

SAMPLE_STATE = (
    "The user asked about tomorrow's weather in Shanghai and wants a short answer."
)

SAMPLE_QUESTIONS: dict[str, dict[str, Any]] = {
    "is_weather": {
        "type": "noul",
        "instructions": "Does the state ask about weather or a forecast?",
    },
    "handler": {
        "type": "choice",
        "instructions": "Which handler should answer this request?",
        "criteria": {
            "weather": "The user is asking about weather or a forecast.",
            "memory": "The user is asking about past conversations or stored facts.",
        },
    },
}


class ProbeError(RuntimeError):
    """The endpoint is unreachable or did not return a System One answer object."""


def build_payload(model: str = "") -> dict[str, Any]:
    """Return the one-request payload used by the probe."""
    payload: dict[str, Any] = {
        "state": SAMPLE_STATE,
        "questions": SAMPLE_QUESTIONS,
    }
    if model:
        payload["model"] = model
    return payload


def probe(base_url: str, model: str = "", timeout: float = DEFAULT_TIMEOUT) -> dict[str, Any]:
    """Send one System One request and return ``{ok, latency_ms, answers}``.

    Raises :class:`ProbeError` for any connection, HTTP, JSON, or schema failure.
    """
    url = base_url.rstrip("/") + "/v1/systemone"
    body = json.dumps(build_payload(model), ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={"content-type": "application/json"},
        method="POST",
    )

    started = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300]
        raise ProbeError(f"HTTP {exc.code} from {url}: {detail}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        raise ProbeError(f"cannot reach {url}: {reason}") from exc
    latency_ms = round((time.perf_counter() - started) * 1000.0, 2)

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ProbeError(f"invalid JSON from {url}: {exc}") from exc
    if not isinstance(data, dict):
        raise ProbeError(f"response from {url} is not a JSON object")

    answers = data.get("answers")
    if not isinstance(answers, dict):
        raise ProbeError(f"response from {url} has no 'answers' object")

    return {"ok": True, "latency_ms": latency_ms, "answers": answers}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m fatfish.decision.probe",
        description="Send one noul/choice request to a local /v1/systemone endpoint.",
    )
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help="Base URL of the decision server.")
    parser.add_argument("--model", default="", help="Optional model name sent in the payload.")
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT,
        help=f"Request timeout in seconds (default: {DEFAULT_TIMEOUT}).",
    )
    args = parser.parse_args(argv)

    try:
        result = probe(args.base_url, model=args.model, timeout=args.timeout)
    except ProbeError as exc:
        print(f"probe: {exc}", file=sys.stderr)
        return 2

    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
