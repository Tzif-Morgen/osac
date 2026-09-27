from __future__ import annotations

from collections.abc import Callable
from unittest.mock import Mock

import pytest

from tests.e2e.core import helpers
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
