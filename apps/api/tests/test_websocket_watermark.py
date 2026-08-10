from app.core.websocket import stream_tuple


def test_stream_id_ordering() -> None:
    assert stream_tuple("10-2") > stream_tuple("10-1")
    assert stream_tuple("11-0") > stream_tuple("10-999")
