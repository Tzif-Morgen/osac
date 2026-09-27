from __future__ import annotations

import subprocess

from tests.e2e.core.grpc_client import GRPCClient


def delete_instance_type_if_present(*, grpc: GRPCClient, name: str) -> None:
    """Delete a test InstanceType, tolerating cleanup after an earlier delete."""
    try:
        grpc.delete_instance_type(name=name)
    except subprocess.CalledProcessError as exc:
        output = ((exc.stdout or "") + (exc.stderr or "")).lower()
        if "not found" not in output:
            raise
