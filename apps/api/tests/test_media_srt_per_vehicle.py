"""per-vehicle SRT 发布鉴权测试：HMAC 派生 token + 车辆/流绑定。"""

from app.core.config import get_settings
from app.main import app
from app.modules.media.router import vehicle_srt_publish_token
from fastapi.testclient import TestClient


def _client() -> TestClient:
    return TestClient(app)


def _auth_payload(protocol: str, user: str, password: str, path: str) -> dict:
    return {
        "action": "publish",
        "path": path,
        "protocol": protocol,
        "user": user,
        "password": password,
        "token": "",
    }


def test_per_vehicle_srt_auth() -> None:
    r001 = vehicle_srt_publish_token("R001")
    r002 = vehicle_srt_publish_token("R002")
    with _client() as client:
        # R001 token + R001 RGB = PASS
        resp = client.post(
            "/api/v1/media/authorize", json=_auth_payload("srt", "R001", r001, "R001-roof_rgb")
        )
        assert resp.status_code == 200, resp.text
        # R001 token + R001 thermal = PASS
        resp = client.post(
            "/api/v1/media/authorize", json=_auth_payload("srt", "R001", r001, "R001-roof_thermal")
        )
        assert resp.status_code == 200, resp.text
        # R001 token + R002 stream = DENY（R001 注册表流归属 R001）
        resp = client.post(
            "/api/v1/media/authorize", json=_auth_payload("srt", "R001", r001, "R002-roof_rgb")
        )
        assert resp.status_code in (401, 403), resp.text
        # R002 token + R001 stream = DENY
        resp = client.post(
            "/api/v1/media/authorize", json=_auth_payload("srt", "R002", r002, "R001-roof_rgb")
        )
        assert resp.status_code in (401, 403), resp.text
        # wrong token = DENY
        resp = client.post(
            "/api/v1/media/authorize", json=_auth_payload("srt", "R001", "WRONG", "R001-roof_rgb")
        )
        assert resp.status_code == 401, resp.text
        # unknown stream = DENY
        resp = client.post(
            "/api/v1/media/authorize", json=_auth_payload("srt", "R001", r001, "UNKNOWN-stream")
        )
        assert resp.status_code == 403, resp.text


def test_vehicle_token_is_derived_and_distinct() -> None:
    assert vehicle_srt_publish_token("R001") != vehicle_srt_publish_token("R002")
    # 确定性（同 master 重复派生一致）
    assert vehicle_srt_publish_token("R001") == vehicle_srt_publish_token("R001")
    # 不与 master 相同
    assert vehicle_srt_publish_token("R001") != get_settings().effective_media_publish_token
