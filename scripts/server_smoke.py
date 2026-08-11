from __future__ import annotations

import argparse
import json
from pathlib import Path

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

SESSION = requests.Session()
SESSION.trust_env = False


def request(
    base_url: str,
    path: str,
    *,
    method: str = "GET",
    payload: dict[str, object] | None = None,
    token: str | None = None,
) -> tuple[int, dict[str, str], bytes]:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    response = SESSION.request(
        method,
        base_url + path,
        headers=headers,
        json=payload,
        verify=False,
        timeout=10,
    )
    return response.status_code, dict(response.headers), response.content


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate a locally running SERVER profile")
    parser.add_argument("--base-url", default="https://localhost")
    parser.add_argument("--username", default="admin")
    parser.add_argument("--password-file", type=Path, required=True)
    args = parser.parse_args()

    live_status, headers, _ = request(args.base_url, "/health/live")
    assert live_status == 200, f"health/live returned {live_status}"
    assert "strict-transport-security" in {key.lower() for key in headers}

    for path in ("/api/docs", "/api/openapi.json"):
        status, _, _ = request(args.base_url, path)
        assert status == 404, f"{path} returned {status}"

    password = args.password_file.read_text(encoding="utf-8").strip()
    login_status, _, body = request(
        args.base_url,
        "/api/v1/auth/login",
        method="POST",
        payload={"username": args.username, "password": password},
    )
    assert login_status == 200, f"login returned {login_status}: {body.decode()[:200]}"
    login = json.loads(body)
    token = login["access_token"]
    me_status, _, me_body = request(args.base_url, "/api/v1/auth/me", token=token)
    assert me_status == 200, f"auth/me returned {me_status}"
    me = json.loads(me_body)
    assert me["username"] == args.username
    assert me["must_change_password"] is True
    print("SERVER smoke PASS: TLS headers, docs off, bootstrap login, auth/me")


if __name__ == "__main__":
    main()
