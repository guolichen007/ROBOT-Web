from __future__ import annotations

import asyncio
import json
import os
from datetime import UTC, datetime
from uuid import uuid4

import httpx
import websockets
from redis import Redis
from websockets.exceptions import ConnectionClosed, InvalidStatus


async def main() -> None:
    password = os.environ["E2E_ADMIN_PASSWORD"]
    http_base = os.getenv("E2E_BASE_URL", "http://nginx")
    ws_base = http_base.replace("http://", "ws://").replace("https://", "wss://")
    origin = http_base.rstrip("/")
    with httpx.Client(base_url=http_base, timeout=10) as client:
        login = client.post("/api/v1/auth/login", json={"username": "admin", "password": password})
        login.raise_for_status()
        headers = {"Authorization": f"Bearer {login.json()['access_token']}", "Origin": origin}
        snapshot = client.get("/api/v1/monitor/snapshot", headers=headers)
        snapshot.raise_for_status()
        watermark = snapshot.json()["snapshot_watermark"]

        marker = str(uuid4())
        redis = Redis.from_url(
            os.getenv("REDIS_URL", "redis://redis:6379/0"),
            decode_responses=True,
        )
        redis.xadd(
            "firebot:events",
            {
                "event": json.dumps(
                    {
                        "event_type": "system.acceptance_marker",
                        "server_received_at": datetime.now(UTC).isoformat(),
                        "payload": {"marker": marker},
                    }
                )
            },
        )
        replay_ticket = client.post("/api/v1/auth/ws-ticket", headers=headers)
        replay_ticket.raise_for_status()

        replay_uri = (
            f"{ws_base}/ws/v1/monitor?ticket={replay_ticket.json()['ticket']}&after={watermark}"
        )
        replayed = False
        async with websockets.connect(replay_uri, origin=origin) as socket:
            for _ in range(20):
                event = json.loads(await asyncio.wait_for(socket.recv(), timeout=5))
                if event.get("data", {}).get("marker") == marker:
                    replayed = True
                    break
        if not replayed:
            raise AssertionError("event created after snapshot watermark was not replayed")

        gap_ticket = client.post("/api/v1/auth/ws-ticket", headers=headers)
        gap_ticket.raise_for_status()
        ticket = gap_ticket.json()["ticket"]
        gap_uri = f"{ws_base}/ws/v1/monitor?ticket={ticket}&after=1-0"
        async with websockets.connect(gap_uri, origin=origin) as socket:
            event = json.loads(await asyncio.wait_for(socket.recv(), timeout=5))
            if event.get("event_type") != "resync_required":
                raise AssertionError(f"expected resync_required, received {event}")

        reuse_rejected = False
        try:
            async with websockets.connect(gap_uri, origin=origin) as socket:
                await asyncio.wait_for(socket.recv(), timeout=5)
        except ConnectionClosed as exc:
            reuse_rejected = exc.code == 4401
        except InvalidStatus as exc:
            reuse_rejected = exc.response.status_code == 403
        if not reuse_rejected:
            raise AssertionError("one-time WebSocket ticket was reusable")

    print(
        json.dumps(
            {
                "snapshot_watermark": watermark,
                "event_replayed": replayed,
                "gap_resync_required": True,
                "ticket_reuse_rejected": reuse_rejected,
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
