from __future__ import annotations

from unittest.mock import Mock

import pytest

from tests.e2e.core.grpc_client import GRPCClient
from tests.e2e.core.k8s_client import K8sClient
from tests.e2e.vmaas.regression import helpers


def test_restart_compute_instance_and_wait_for_vmi_runs_lifecycle_waits(monkeypatch: pytest.MonkeyPatch) -> None:
    grpc = Mock(spec=GRPCClient)
    k8s = Mock(spec=K8sClient)
    k8s_virt = Mock(spec=K8sClient)
    waits: list[str] = []
    monkeypatch.setattr(helpers, "wait_for_restart", lambda **kwargs: waits.append("restart"))
    monkeypatch.setattr(helpers, "wait_for_running", lambda **kwargs: waits.append("running"))
    monkeypatch.setattr(helpers, "wait_for_new_vmi", lambda **kwargs: waits.append("vmi") or "new-vmi")

    result = helpers.restart_compute_instance_and_wait_for_vmi(
        grpc,
        k8s,
        k8s_virt,
        uuid="ci-uuid",
        name="ci-name",
        vmi_namespace="vm-namespace",
        vm_template="template",
        restart_timestamp="2026-09-27T00:00:00Z",
        initial_last_restarted_at="2026-09-26T00:00:00Z",
        initial_vmi_timestamp="old-vmi",
    )

    grpc.update_restart.assert_called_once_with(uuid="ci-uuid", template="template", timestamp="2026-09-27T00:00:00Z")
    assert waits == ["restart", "running", "vmi"]
    assert result == "new-vmi"
