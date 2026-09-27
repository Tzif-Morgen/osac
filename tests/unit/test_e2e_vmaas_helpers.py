from __future__ import annotations

import subprocess
from unittest.mock import Mock

import pytest

from tests.e2e.core.grpc_client import GRPCClient
from tests.e2e.vmaas.helpers import delete_instance_type_if_present


def test_delete_instance_type_if_present_ignores_not_found() -> None:
    grpc = Mock(spec=GRPCClient)
    grpc.delete_instance_type.side_effect = subprocess.CalledProcessError(
        returncode=1, cmd="grpcurl", stderr="instance type not found"
    )

    delete_instance_type_if_present(grpc=grpc, name="test-instance-type")


def test_delete_instance_type_if_present_propagates_other_errors() -> None:
    grpc = Mock(spec=GRPCClient)
    error = subprocess.CalledProcessError(returncode=1, cmd="grpcurl", stderr="permission denied")
    grpc.delete_instance_type.side_effect = error

    with pytest.raises(subprocess.CalledProcessError) as exc_info:
        delete_instance_type_if_present(grpc=grpc, name="test-instance-type")

    assert exc_info.value is error
