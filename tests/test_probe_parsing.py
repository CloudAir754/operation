"""探针与输出归一化测试：分区状态、缺失 GPU、协议校验与输出上限。"""

from __future__ import annotations

import json
import threading

import pytest

from lab_ops_panel.remote.parsing import (
    error_snapshot_data,
    normalize_services_section,
    normalize_snapshot_sections,
    snapshot_status,
)
from lab_ops_panel.remote.probe import (
    PROBE_SOURCE,
    PROBE_VERSION,
    ProbeError,
    build_probe_command,
    parse_probe_payload,
    run_probe,
)
from lab_ops_panel.ssh.transport import CommandResult, FakeTransport, SshTarget

TARGET = SshTarget(alias="lab-01", hostname="10.0.0.1", user="lab")


def _result(**overrides) -> CommandResult:
    defaults = {
        "argv": [],
        "command": "probe",
        "exit_code": 0,
        "stdout": "",
        "stderr": "",
        "duration_ms": 1,
    }
    defaults.update(overrides)
    return CommandResult(**defaults)


@pytest.mark.parametrize("mode", ["snapshot", "services"])
def test_probe_command_embeds_encoded_source(mode: str) -> None:
    command = build_probe_command(mode)

    assert "base64 -d" in command
    assert command.endswith(mode)
    assert "/usr/bin/python3" in command


def test_probe_command_rejects_unknown_mode() -> None:
    with pytest.raises(ValueError, match="未知探针模式"):
        build_probe_command("unknown")


def test_probe_source_declares_protocol_version() -> None:
    assert f"VERSION = {PROBE_VERSION}" in PROBE_SOURCE


def test_parse_payload_accepts_wrapped_json() -> None:
    payload = {"probeVersion": PROBE_VERSION, "sections": {}}

    parsed = parse_probe_payload("warning\n" + json.dumps(payload) + "\n")

    assert parsed["sections"] == {}


def test_parse_payload_rejects_non_json() -> None:
    with pytest.raises(ProbeError, match="不是 JSON"):
        parse_probe_payload("not json at all")


def test_parse_payload_rejects_version_mismatch() -> None:
    with pytest.raises(ProbeError, match="协议版本"):
        parse_probe_payload(json.dumps({"probeVersion": 999}))


def test_parse_payload_rejects_empty_output() -> None:
    with pytest.raises(ProbeError, match="没有输出"):
        parse_probe_payload("   ")


def test_run_probe_parses_stdout(probe_payload) -> None:
    transport = FakeTransport(default_stdout=json.dumps(probe_payload()))

    payload = run_probe(transport, TARGET)

    assert payload["sections"]["system"]["data"]["hostname"] == "lab-01"
    assert transport.calls[0].max_output_bytes is None


def test_run_probe_timeout_raises(probe_payload) -> None:
    transport = FakeTransport(responses=[_result(exit_code=None, timed_out=True, stdout="")])

    with pytest.raises(ProbeError, match="超时"):
        run_probe(transport, TARGET)


def test_run_probe_truncated_output_is_rejected(probe_payload) -> None:
    transport = FakeTransport(
        responses=[_result(stdout=json.dumps(probe_payload()), truncated=True)]
    )

    with pytest.raises(ProbeError, match="超出上限"):
        run_probe(transport, TARGET)


def test_run_probe_maps_auth_failure(probe_payload) -> None:
    transport = FakeTransport(
        responses=[_result(exit_code=255, stderr="lab@host: Permission denied (publickey).")]
    )

    with pytest.raises(ProbeError, match="认证失败"):
        run_probe(transport, TARGET)


def test_run_probe_honours_cancel_event(probe_payload) -> None:
    event = threading.Event()
    event.set()
    transport = FakeTransport(responses=[_result(exit_code=None, cancelled=True)])

    with pytest.raises(ProbeError, match="取消"):
        run_probe(transport, TARGET, cancel_event=event)


def test_snapshot_sections_normalize_status(probe_payload) -> None:
    data = normalize_snapshot_sections(probe_payload()["sections"])

    assert data.system.status == "ok"
    assert data.cpu.data is not None
    assert data.cpu.data.usage_percent == 12.5
    assert snapshot_status(data) == "ok"


def test_unavailable_gpu_has_no_data(probe_payload) -> None:
    payload = probe_payload(
        {
            "gpu": {
                "status": "unavailable",
                "error": "未检测到 nvidia-smi",
                "data": {"devices": [{"index": 0, "name": "should be dropped"}]},
            }
        }
    )

    data = normalize_snapshot_sections(payload["sections"])

    assert data.gpu.status == "unavailable"
    assert data.gpu.data is None
    assert data.gpu.error == "未检测到 nvidia-smi"
    assert snapshot_status(data) == "partial"


def test_permission_denied_section_keeps_error(probe_payload) -> None:
    payload = probe_payload(
        {"services": {"status": "permission_denied", "error": "Access denied", "data": None}}
    )

    data = normalize_snapshot_sections(payload["sections"])

    assert data.services.status == "permission_denied"
    assert data.services.error == "Access denied"
    assert data.services.data is None


def test_missing_section_becomes_error(probe_payload) -> None:
    payload = probe_payload()
    payload["sections"].pop("disks")

    data = normalize_snapshot_sections(payload["sections"])

    assert data.disks.status == "error"
    assert "disks" in (data.disks.error or "")
    assert data.system.status == "ok"


def test_unparsable_section_does_not_break_others(probe_payload) -> None:
    payload = probe_payload({"cpu": {"status": "ok", "error": None, "data": {"cores": "many"}}})

    data = normalize_snapshot_sections(payload["sections"])

    assert data.cpu.status == "error"
    assert data.memory.status == "ok"


def test_services_are_marked_protected(probe_payload) -> None:
    data = normalize_snapshot_sections(probe_payload()["sections"])

    units = {unit.name: unit for unit in data.services.data.units}
    assert units["ssh.service"].protected is True
    assert units["nginx.service"].protected is False


def test_services_section_from_probe_payload(probe_payload) -> None:
    payload = probe_payload()

    section = normalize_services_section(payload["sections"]["services"])

    assert section.status == "ok"
    assert section.data is not None
    assert len(section.data.units) == 2


def test_services_section_error_is_returned_not_raised() -> None:
    section = normalize_services_section({"status": "weird"})

    assert section.status == "error"
    assert section.data is None
    assert section.error


def test_error_snapshot_data_marks_every_section() -> None:
    data = error_snapshot_data("连接超时")

    assert snapshot_status(data) == "failed"
    assert data.system.status == "error"
    assert data.gpu.error == "连接超时"
