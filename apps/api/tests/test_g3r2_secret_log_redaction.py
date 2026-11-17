"""G3-R2 SECRET_LOG_REDACTION：本机隔离测试失败日志不得输出测试口令。

根因（两条泄露路径）：
1. ``subprocess.run(["mosquitto_passwd", "-b", file, user, PASSWORD], check=True)`` 失败时，
   CalledProcessError.__str__ 携带完整 argv → pytest 失败断言/日志原样打印测试口令；
2. Playwright 失败时错误 call log 含 ``fill("<value>")``（登录口令），
   web_videocard.mjs 原先 ``String(err)`` 直接写入 stdout JSON → pytest 断言消息打印口令。
修法：口令经 stdin 交给 mosquitto_passwd；浏览器驱动输出前掩码 stdin 凭据；
Python 侧断言消息统一 redact。
本文件是静态 + 单元反证（CI backend 必跑），不依赖 Broker/浏览器。
"""

from __future__ import annotations

import importlib.util
import re
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
FIXTURES = [
    REPO / "integration/g2-command-loop-e2e/conftest.py",
    REPO / "integration/g3-media-ir-e2e/conftest.py",
    REPO / "integration/vehicle-agent-real-e2e/conftest.py",
    REPO / "integration/vehicle-agent-real-e2e/test_real_coexistence.py",
]


def test_no_password_in_mosquitto_passwd_argv():
    pattern = re.compile(r"\[\s*\"mosquitto_passwd\"\s*,\s*\"-b\"")
    offenders = [str(p.relative_to(REPO)) for p in FIXTURES if pattern.search(p.read_text("utf-8"))]
    assert offenders == [], offenders


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_add_broker_user_failure_message_has_no_password(tmp_path, monkeypatch):
    """模拟 mosquitto_passwd 失败：异常消息只含用户名与退出码，不含口令（即使工具回显口令）。"""
    secret = "unit-only-Secret-Pw-123"
    calls = []

    def fake_run(args, **kwargs):
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args, 1, stdout="", stderr=f"Error: bad {secret}")

    for path in FIXTURES[:2]:
        text = path.read_text("utf-8")
        name = "add_broker_user" if "def add_broker_user" in text else "_add_broker_user"
        # 只抽取 helper 函数源，避免执行整个 conftest（会触发 pytest fixture 注册/导入）
        src = re.search(rf"def {name}\(.*?(?=\n\n\n|\ndef |\n@)", text, re.S).group(0)
        namespace = {"subprocess": subprocess, "Path": Path}
        exec(compile("from __future__ import annotations\n" + src, str(path), "exec"), namespace)
        monkeypatch.setattr(subprocess, "run", fake_run)
        try:
            namespace[name](tmp_path / "passwd", "platform", secret)
        except RuntimeError as exc:
            assert secret not in str(exc) and "platform" in str(exc) and "rc=1" in str(exc)
        else:  # pragma: no cover
            raise AssertionError("expected failure")
        finally:
            monkeypatch.undo()
        args, kwargs = calls[-1]
        assert secret not in " ".join(map(str, args))  # 口令不进 argv
        assert kwargs["input"] == f"{secret}\n{secret}\n"


def test_browser_driver_redacts_credentials_before_output():
    """web_videocard.mjs：错误文本输出前经 redact（掩码 stdin 中的 password）。"""
    src = (REPO / "integration/g3-media-ir-e2e/browser/web_videocard.mjs").read_text("utf-8")
    assert "out.error = redact(err)" in src
    assert "out.error = String(err)" not in src
    # 端到端执行 redact()：从源码抽取函数并在 node 中验证 Playwright 风格的 fill 日志被掩码
    body = re.search(r"const SECRETS = .*?\n}\n", src, re.S).group(0)
    script = (
        "const input = { password: 'unit-Secret-Pw-xyz' };\n"
        + body
        + "const err = new Error('locator.fill: Timeout\\n  - fill(\"unit-Secret-Pw-xyz\")');\n"
        + "process.stdout.write(redact(err))"
    )
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    assert "unit-Secret-Pw-xyz" not in out.stdout and 'fill("***")' in out.stdout


def test_web_e2e_assertions_redact_before_printing():
    src = (REPO / "integration/g3-media-ir-e2e/test_web_videocard_e2e.py").read_text("utf-8")
    assert "def _redact(" in src
    # 所有把浏览器 stderr / 结果数据拼入断言消息的位置都经 _redact
    assert not re.search(r"assert [^\n]*,\s*result\.stderr\[", src)
    assert not re.search(r"assert data\.get\(\"ok\"\), \(data, err", src)
