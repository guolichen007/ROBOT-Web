"""复用 G2 E2E 的隔离测试管理员（不触碰 bootstrap admin）。"""

from contextlib import contextmanager


@contextmanager
def admin_client(app):
    import importlib.util

    from conftest import REPO_ROOT  # noqa: F401
    from fastapi.testclient import TestClient

    spec = importlib.util.spec_from_file_location(
        "g2_conftest", REPO_ROOT / "integration" / "g2-command-loop-e2e" / "conftest.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with TestClient(app) as client:
        username, password = module.ensure_isolated_test_admin()
        response = client.post(
            "/api/v1/auth/login", json={"username": username, "password": password}
        )
        assert response.status_code == 200, response.text
        client.headers["Authorization"] = "Bearer " + response.json()["access_token"]
        yield client
