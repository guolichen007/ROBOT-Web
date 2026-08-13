from app.core.websocket import STREAM_BLOCK_MS


def test_websocket_stream_block_stays_below_redis_socket_timeout() -> None:
    """An idle event stream must emit heartbeats instead of timing out."""

    assert 0 < STREAM_BLOCK_MS < 2_000
