import socket
from pathlib import Path

import httpx

from workbench import doctor
from workbench.config import Settings


def test_python_version_gate() -> None:
    assert doctor.check_python((3, 12)).status == "ok"
    assert doctor.check_python((3, 11)).status == "fail"


def test_submodule_pinned_ok() -> None:
    s = Settings()
    r = doctor.check_submodule("awm", s.upstream.awm_dir, s.upstream.awm_sha)
    assert r.status == "ok", r.detail


def test_submodule_sha_mismatch() -> None:
    s = Settings()
    r = doctor.check_submodule("awm", s.upstream.awm_dir, "0" * 40)
    assert r.status == "fail"


def test_submodule_missing(tmp_path: Path) -> None:
    assert doctor.check_submodule("x", tmp_path / "nope", "a").status == "fail"


def test_dataset_missing_is_warning(tmp_path: Path) -> None:
    assert doctor.check_dataset(tmp_path).status == "warn"


def test_dataset_present(tmp_path: Path) -> None:
    for f in doctor.DATASET_FILES:
        (tmp_path / f).write_text("", encoding="utf-8")
    assert doctor.check_dataset(tmp_path).status == "ok"


def test_gpu_missing_is_warning() -> None:
    assert doctor.check_gpu(which=lambda _: None).status == "warn"
    assert doctor.check_gpu(which=lambda _: "/usr/bin/nvidia-smi").status == "ok"


def test_port_in_use_detection() -> None:
    with socket.socket() as srv:
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        port = srv.getsockname()[1]
        assert doctor.port_in_use("127.0.0.1", port)


def test_env_vars_required_for_openai_compat() -> None:
    s = Settings(llm={"backend": "openai_compat", "api_key_env": "X_KEY"})  # type: ignore[arg-type]
    assert doctor.check_env_vars(s, environ={}).status == "fail"
    assert doctor.check_env_vars(s, environ={"X_KEY": "k"}).status == "ok"


def test_llm_backend_connectivity() -> None:
    s = Settings(llm={"backend": "vllm", "base_url": "http://llm.test/v1"})  # type: ignore[arg-type]
    ok = httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(200, json={"data": []})))
    assert doctor.check_llm_backend(s, client=ok).status == "ok"

    def boom(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=req)

    bad = httpx.Client(transport=httpx.MockTransport(boom))
    assert doctor.check_llm_backend(s, client=bad).status == "fail"


def test_mock_backend_needs_fixture() -> None:
    assert doctor.check_llm_backend(Settings()).status == "ok"


def test_exit_code() -> None:
    ok = doctor.CheckResult("a", "ok", "")
    warn = doctor.CheckResult("b", "warn", "")
    fail = doctor.CheckResult("c", "fail", "")
    assert doctor.exit_code([ok, warn]) == 0
    assert doctor.exit_code([ok, fail]) == 1
