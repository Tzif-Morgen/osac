from __future__ import annotations

import subprocess
from collections.abc import Callable
from unittest.mock import Mock

import pytest

from tests.e2e.core import helpers
from tests.e2e.core.grpc_client import GRPCClient
from tests.e2e.core.k8s_client import K8sClient


def test_wait_for_new_vmi_waits_for_a_different_nonempty_timestamp(monkeypatch: pytest.MonkeyPatch) -> None:
    k8s = Mock(spec=K8sClient)
    k8s.get_vmi_creation_timestamp.side_effect = ["original", "", "recreated"]

    def invoke_poll(
        *, fn: Callable[[], str], until: Callable[[str], bool], retries: int, delay: float, description: str
    ) -> str:
        while True:
            timestamp = fn()
            if until(timestamp):
                return timestamp

    monkeypatch.setattr(helpers, "poll_until", invoke_poll)

    result = helpers.wait_for_new_vmi(
        k8s=k8s, vmi_namespace="vm-namespace", compute_instance_name="compute-instance", initial_timestamp="original"
    )

    assert result == "recreated"
    assert k8s.get_vmi_creation_timestamp.call_count == 3


def test_delete_instance_type_if_present_ignores_not_found() -> None:
    grpc = Mock(spec=GRPCClient)
    grpc.delete_instance_type.side_effect = subprocess.CalledProcessError(
        returncode=1, cmd="grpcurl", stderr="instance type not found"
    )

    helpers.delete_instance_type_if_present(grpc=grpc, name="test-instance-type")


def test_delete_instance_type_if_present_propagates_other_errors() -> None:
    grpc = Mock(spec=GRPCClient)
    error = subprocess.CalledProcessError(returncode=1, cmd="grpcurl", stderr="permission denied")
    grpc.delete_instance_type.side_effect = error

    with pytest.raises(subprocess.CalledProcessError) as exc_info:
        helpers.delete_instance_type_if_present(grpc=grpc, name="test-instance-type")

    assert exc_info.value is error
