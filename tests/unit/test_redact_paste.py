"""scripts/redact_paste.py: what a Phase 15 paste-back loses before it is pasted (N1)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

# planted values, not real secrets
HF_TOKEN = "hf_AbCdEfGhIjKlMnOpQrStUvWx0123"  # pragma: allowlist secret
SK_TOKEN = "sk-live0123456789abcdefXYZ"  # pragma: allowlist secret
PROXY_CREDENTIALS = "alice:s3cretPass"  # pragma: allowlist secret
SAMPLE = f"""\
| NVIDIA-SMI 570.124.06   Driver Version: 570.124.06   CUDA Version: 12.8 |
start 2026-09-25T10:00:01Z
INFO 09-25 10:00:03 [loader.py:447] Loading weights took 12.5 seconds
torch 2.10.0 (cuda 12.8), vllm 0.19.0; listening on 127.0.0.1:8000, peer 192.168.10.23
$ HF_TOKEN={HF_TOKEN} uv run hf download Qwen/Qwen3-0.6B
Authorization: Bearer {SK_TOKEN}
proxy https://{PROXY_CREDENTIALS}@proxy.example.com:3128 and git@github.com:Agent-One-Lab/verl.git
{{"customer_email": "jane.doe@example.com", "phone": "+1 (555) 123-4567", "alt": "555.123.4567"}}
{{"card": "4111 1111 1111 1111", "ts": 1790275047.4735959, "tokens": 12586}}
{{"phone": "555-111-2222", "mobile": "+1-312-555-0100", "contact": "+12125550123", "cn": "13812345678"}}
WORKBENCH_LLM__API_KEY_ENV=DEEPSEEK_API_KEY; "cost": 0.015688, size 4998781440 shard-1.safetensors
Mem:            251          12         200
Agent execution complete. Total iterations: 2
2026-09-24 17:13:42.078 | INFO | awm.core.agent:run_agent:575 - done
"""


def run(tmp_path: Path, text: str) -> tuple[str, str]:
    src = tmp_path / "paste-15a.txt"
    src.write_text(text, encoding="utf-8")
    out = subprocess.run(
        [sys.executable, "scripts/redact_paste.py", str(src)], capture_output=True, text=True, check=True
    )
    assert src.read_text(encoding="utf-8") == text  # the input is left as it is
    return (tmp_path / "paste-15a.redacted.txt").read_text(encoding="utf-8"), out.stdout


def test_secrets_and_personal_data_are_replaced(tmp_path: Path) -> None:
    red, report = run(tmp_path, SAMPLE)
    for secret in (
        HF_TOKEN,
        SK_TOKEN,
        PROXY_CREDENTIALS,
        "jane.doe@example.com",
        "555) 123-4567",
        "555.123.4567",
        "4111 1111 1111 1111",
        "555-111-2222",
        "312-555-0100",
        "+12125550123",
        "13812345678",
    ):
        assert secret not in red, secret
    assert "HF_TOKEN=[REDACTED]" in red and "Bearer [TOKEN]" in red and "https://[CREDENTIALS]@proxy" in red
    assert (
        "'token': 2" in report and "'email': 1" in report and "'phone': 6" in report and "'card': 1" in report
    )


def test_engineering_facts_survive(tmp_path: Path) -> None:
    red, _ = run(tmp_path, SAMPLE)
    for fact in (
        "Driver Version: 570.124.06",
        "CUDA Version: 12.8",
        "2026-09-25T10:00:01Z",
        "INFO 09-25 10:00:03",
        "torch 2.10.0",
        "127.0.0.1:8000",
        "192.168.10.23",
        "Qwen/Qwen3-0.6B",
        "1790275047.4735959",
        "git@github.com:Agent-One-Lab/verl.git",
        "API_KEY_ENV=DEEPSEEK_API_KEY",
        '"cost": 0.015688',
        "size 4998781440",
        "Mem:            251          12         200",
        # a number at the end of a line and a timestamp on the next stay two lines
        "Total iterations: 2\n2026-09-24 17:13:42.078 | INFO",
        '"tokens": 12586',
    ):
        assert fact in red, fact
