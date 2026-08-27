"""GPU 能力降级测试。"""

import sys

from lab_ops_panel.infrastructure.gpu import gpu_snapshot


def test_missing_nvml_is_reported_as_unavailable(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """无 NVIDIA 驱动环境必须返回 N/A 语义而不是抛出异常。"""

    monkeypatch.setitem(sys.modules, "pynvml", None)
    snapshot = gpu_snapshot()
    assert snapshot["available"] is False
    assert snapshot["devices"] == []
    assert snapshot["reason"]
