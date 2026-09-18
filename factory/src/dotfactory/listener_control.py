"""Control the hosted receipt-only listener without exposing host credentials."""

from __future__ import annotations

import json
import math
import os
import re
import urllib.error
import urllib.request
from typing import Any, Mapping


RENDER_API_ORIGIN = "https://api.render.com"
_SERVICE_ID = re.compile(r"^srv-[a-z0-9]+$")


class ListenerControlError(RuntimeError):
    """A safe operator-facing listener control failure."""


class _RejectRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args: Any, **_kwargs: Any) -> None:
        return None


def _urlopen(request: urllib.request.Request, timeout: float) -> Any:
    return urllib.request.build_opener(_RejectRedirects()).open(
        request, timeout=timeout
    )


def _required_environment(environment: Mapping[str, str], name: str) -> str:
    value = environment.get(name, "").strip()
    if not value:
        raise ListenerControlError(f"{name} is required")
    return value


def _request(
    service_id: str,
    action: str,
    *,
    token: str,
    timeout: float,
) -> Any:
    suffix = "" if action == "status" else f"/{action}"
    request = urllib.request.Request(
        f"{RENDER_API_ORIGIN}/v1/services/{service_id}{suffix}",
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "dotfactory-listener-control/1",
        },
        method="GET" if action == "status" else "POST",
    )
    try:
        with _urlopen(request, timeout) as response:
            expected_status = 200 if action == "status" else 202
            if response.status != expected_status:
                raise ListenerControlError(
                    f"Render API {action} returned unexpected HTTP "
                    f"{response.status}"
                )
            body = response.read(1_048_577)
            if len(body) > 1_048_576:
                raise ListenerControlError("Render API response exceeded 1 MiB")
    except urllib.error.HTTPError as error:
        raise ListenerControlError(
            f"Render API {action} returned HTTP {error.code}"
        ) from error
    except urllib.error.URLError as error:
        raise ListenerControlError(f"Render API {action} was unreachable") from error
    except TimeoutError as error:
        raise ListenerControlError(f"Render API {action} timed out") from error
    if not body:
        return None
    try:
        return json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ListenerControlError("Render API returned invalid JSON") from error


def _public_status(payload: Any, service_id: str) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ListenerControlError("Render API returned an invalid service record")
    if payload.get("id") != service_id:
        raise ListenerControlError("Render API returned the wrong service record")
    return {
        key: payload[key]
        for key in ("id", "name", "url", "suspended", "updatedAt")
        if key in payload
    }


def manage_listener(
    action: str,
    *,
    service_id: str | None = None,
    token_env: str = "RENDER_API_KEY",
    environment: Mapping[str, str] | None = None,
    timeout: float = 10.0,
) -> dict[str, Any]:
    """Read or mutate Render listener state and return an allowlisted receipt."""
    if action not in ("status", "suspend", "resume"):
        raise ListenerControlError(f"unsupported listener action: {action}")
    current_environment = os.environ if environment is None else environment
    resolved_id = (
        service_id
        or current_environment.get("DOTFACTORY_LISTENER_SERVICE_ID", "")
    ).strip()
    if not _SERVICE_ID.fullmatch(resolved_id):
        raise ListenerControlError(
            "listener service ID must be supplied with --service-id or "
            "DOTFACTORY_LISTENER_SERVICE_ID and start with srv-"
        )
    if not token_env or not token_env.isidentifier():
        raise ListenerControlError("token environment variable name is invalid")
    if not math.isfinite(timeout) or timeout <= 0:
        raise ListenerControlError("timeout must be a positive finite number")
    token = _required_environment(current_environment, token_env)
    payload = _request(
        resolved_id, action, token=token, timeout=timeout,
    )
    if action == "status":
        return {
            "action": action,
            "service": _public_status(payload, resolved_id),
        }
    return {
        "action": action,
        "accepted": True,
        "service_id": resolved_id,
        "disk_action": "unchanged",
    }
