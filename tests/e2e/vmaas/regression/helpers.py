from __future__ import annotations

from tests.e2e.core.grpc_client import GRPCClient
from tests.e2e.core.helpers import wait_for_new_vmi, wait_for_restart, wait_for_running
from tests.e2e.core.k8s_client import K8sClient


def restart_compute_instance_and_wait_for_vmi(
    grpc: GRPCClient,
    k8s: K8sClient,
    k8s_virt: K8sClient,
    *,
    uuid: str,
    name: str,
    vmi_namespace: str,
    vm_template: str,
    restart_timestamp: str,
    initial_last_restarted_at: str,
    initial_vmi_timestamp: str,
) -> str:
    """Restart the ComputeInstance and wait for a new VirtualMachineInstance (VMI)."""
    grpc.update_restart(uuid=uuid, template=vm_template, timestamp=restart_timestamp)
    wait_for_restart(k8s=k8s, name=name, initial=initial_last_restarted_at, restart_ts=restart_timestamp)
    wait_for_running(k8s=k8s, name=name)
    return wait_for_new_vmi(
        k8s=k8s_virt, vmi_namespace=vmi_namespace, compute_instance_name=name, initial_timestamp=initial_vmi_timestamp
    )
