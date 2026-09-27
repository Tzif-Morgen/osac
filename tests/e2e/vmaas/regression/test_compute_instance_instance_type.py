from __future__ import annotations

import re
import subprocess
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest

from tests.e2e.core.grpc_client import GRPCClient
from tests.e2e.core.helpers import (
    assert_grpc_rejected,
    unique_name,
    wait_for_cr,
    wait_for_deletion,
    wait_for_new_vmi,
    wait_for_provision,
    wait_for_restart,
    wait_for_running,
)
from tests.e2e.core.k8s_client import K8sClient
from tests.e2e.core.osac_cli import OsacCLI
from tests.e2e.core.runner import poll_until, run_unchecked

pytestmark = pytest.mark.regression

IT_VCPUS: int = 2
IT_MEMORY_GIB: int = 4
RESIZE_TYPE_RESOURCES: dict[str, tuple[int, int]] = {"small": (IT_VCPUS, IT_MEMORY_GIB), "medium": (4, 8)}


def _build_create_ci_args(
    cli: OsacCLI, vm_template: str, default_subnet: str, it_name: str, disk_image: str, ci_name: str | None = None
) -> list[str]:
    args = [cli.binary, "--config", cli.config_dir, "create", "computeinstance"]
    if ci_name is not None:
        args += ["--name", ci_name]
    storage_tier = cli.default_storage_tier
    if not storage_tier:
        raise ValueError("cli.default_storage_tier must be set")
    args += [
        "--template",
        vm_template,
        "--network-attachment",
        f"subnet={default_subnet}",
        "--instance-type",
        it_name,
        "--boot-disk-size",
        "20",
        "--disk-image",
        disk_image,
        "--boot-disk-storage-tier",
        storage_tier,
        "--run-strategy",
        "Always",
    ]
    return args


@pytest.fixture
def active_instance_type(private_grpc: GRPCClient) -> Iterator[str]:
    """Create an ACTIVE instance type for testing; clean up after."""
    it_name = f"e2e-ci-it-{uuid4().hex[:8]}"
    private_grpc.create_instance_type(
        name=it_name, vcpus=IT_VCPUS, memory_gib=IT_MEMORY_GIB, description="E2E compute instance test type"
    )
    yield it_name
    try:
        private_grpc.delete_instance_type(name=it_name)
    except subprocess.CalledProcessError as e:
        output = ((e.stdout or "") + (e.stderr or "")).lower()
        if "not found" not in output:
            raise


@pytest.fixture
def resize_instance_types(private_grpc: GRPCClient, active_instance_type: str) -> Iterator[dict[str, str]]:
    medium_name = f"e2e-resize-medium-{uuid4().hex[:8]}"
    medium_vcpus, medium_memory_gib = RESIZE_TYPE_RESOURCES["medium"]
    private_grpc.create_instance_type(
        name=medium_name, vcpus=medium_vcpus, memory_gib=medium_memory_gib, description="E2E VM resize test type"
    )
    try:
        yield {"small": active_instance_type, "medium": medium_name}
    finally:
        try:
            private_grpc.delete_instance_type(name=medium_name)
        except subprocess.CalledProcessError as e:
            output = ((e.stdout or "") + (e.stderr or "")).lower()
            if "not found" not in output:
                raise


def _condition_status(compute_instance: dict[str, Any], condition_type: str) -> str:
    conditions: list[dict[str, Any]] = compute_instance.get("status", {}).get("conditions", [])
    return next(
        (condition.get("status", "") for condition in conditions if condition.get("type") == condition_type), ""
    )


def _wait_for_configuration_applied(
    k8s: K8sClient, *, name: str, vcpus: int, memory_gib: int, previous_config_version: str | None = None
) -> dict[str, Any]:
    def current_configuration() -> dict[str, Any] | None:
        compute_instance = k8s.get_json(resource="computeinstance", name=name)
        spec = compute_instance.get("spec", {})
        desired_config_version = compute_instance.get("status", {}).get("desiredConfigVersion", "")
        if (
            spec.get("vcpus") == vcpus
            and spec.get("memoryGiB") == memory_gib
            and (previous_config_version is None or desired_config_version != previous_config_version)
            and _condition_status(compute_instance, "ConfigurationApplied") == "True"
        ):
            return compute_instance
        return None

    return poll_until(
        fn=current_configuration,
        until=lambda result: result is not None,
        retries=120,
        delay=5,
        description=f"{name} ConfigurationApplied with {vcpus} vCPUs and {memory_gib} GiB",
    )


def _instance_type_name(compute_instance: dict[str, Any]) -> str:
    instance_type = compute_instance["object"]["spec"].get("instanceType", {})
    return instance_type.get("name") or instance_type.get("id", "")


def _configuration_snapshot(compute_instance: dict[str, Any]) -> tuple[Any, ...]:
    status = compute_instance.get("status", {})
    job_ids = tuple(job.get("jobID", "") for job in status.get("provisioningJobs", []))
    return (compute_instance.get("metadata", {}).get("generation"), status.get("desiredConfigVersion", ""), job_ids)


def _vmi_has_resources(vmi: dict[str, Any], *, vcpus: int, memory_gib: int) -> bool:
    domain = vmi.get("spec", {}).get("domain", {})
    cpu = domain.get("cpu", {})
    memory = domain.get("memory", {}).get("guest", "")
    return (
        cpu.get("sockets") == vcpus
        and cpu.get("cores", 1) == 1
        and cpu.get("threads", 1) == 1
        and memory == f"{memory_gib}Gi"
    )


def _wait_for_vmi_resources(
    k8s: K8sClient, *, vmi_namespace: str, ci_name: str, vcpus: int, memory_gib: int
) -> dict[str, Any]:
    return poll_until(
        fn=lambda: k8s.get_vmi_json(vmi_namespace=vmi_namespace, compute_instance_name=ci_name, checked=False),
        until=lambda vmi: bool(vmi) and _vmi_has_resources(vmi, vcpus=vcpus, memory_gib=memory_gib),
        retries=60,
        delay=5,
        description=f"{ci_name} VMI with {vcpus} sockets and {memory_gib} GiB",
    )


def _next_restart_timestamp(last_restarted_at: str) -> str:
    timestamp = datetime.now(tz=UTC).replace(microsecond=0)
    if last_restarted_at:
        previous = datetime.fromisoformat(last_restarted_at.replace("Z", "+00:00"))
        if timestamp <= previous:
            timestamp = previous + timedelta(seconds=1)
    return timestamp.strftime("%Y-%m-%dT%H:%M:%SZ")


def _restart_after_resize(
    grpc: GRPCClient,
    k8s_hub: K8sClient,
    k8s_virt: K8sClient,
    *,
    ci_uuid: str,
    ci_name: str,
    vmi_namespace: str,
    vm_template: str,
    initial_vmi_timestamp: str,
) -> None:
    previous_last_restarted = k8s_hub.get_compute_instance_last_restarted_at(name=ci_name)
    restart_timestamp = _next_restart_timestamp(previous_last_restarted)
    grpc.update_restart(uuid=ci_uuid, template=vm_template, timestamp=restart_timestamp)
    wait_for_restart(k8s=k8s_hub, name=ci_name, initial=previous_last_restarted, restart_ts=restart_timestamp)
    wait_for_running(k8s=k8s_hub, name=ci_name)
    wait_for_new_vmi(
        k8s=k8s_virt,
        vmi_namespace=vmi_namespace,
        compute_instance_name=ci_name,
        initial_timestamp=initial_vmi_timestamp,
    )
    poll_until(
        fn=lambda: k8s_hub.get_compute_instance_condition_status(
            name=ci_name, condition_type="RestartRequired", checked=False
        ),
        until=lambda status: status in ("", "False"),
        retries=60,
        delay=5,
        description=f"{ci_name} RestartRequired cleared after manual resize restart",
    )


@pytest.fixture
def running_compute_instance_factory(
    cli: OsacCLI,
    k8s_hub_client: K8sClient,
    default_subnet: str,
    vm_template: str,
    resize_instance_types: dict[str, str],
) -> Iterator[Callable[[str], tuple[str, str]]]:
    created: list[tuple[str, str]] = []

    def create(instance_type_key: str) -> tuple[str, str]:
        vcpus, memory_gib = RESIZE_TYPE_RESOURCES[instance_type_key]
        ci_uuid = cli.create_compute_instance(
            name=unique_name("e2e-resize"),
            template=vm_template,
            network_attachments=[{"subnet": default_subnet}],
            instance_type=resize_instance_types[instance_type_key],
            run_strategy="Always",
        )
        ci_name: str | None = None
        try:
            ci_name = wait_for_cr(k8s=k8s_hub_client, uuid=ci_uuid)
            wait_for_provision(k8s=k8s_hub_client, name=ci_name)
            wait_for_running(k8s=k8s_hub_client, name=ci_name)
            _wait_for_configuration_applied(k8s_hub_client, name=ci_name, vcpus=vcpus, memory_gib=memory_gib)
        except Exception:
            cli.delete_compute_instance(uuid=ci_uuid)
            if ci_name is not None:
                wait_for_deletion(k8s=k8s_hub_client, name=ci_name)
            raise
        created.append((ci_uuid, ci_name))
        return ci_uuid, ci_name

    yield create

    for ci_uuid, ci_name in reversed(created):
        cli.delete_compute_instance(uuid=ci_uuid)
        wait_for_deletion(k8s=k8s_hub_client, name=ci_name)


def test_compute_instance_happy_path(
    cli: OsacCLI,
    grpc: GRPCClient,
    k8s_hub_client: K8sClient,
    default_subnet: str,
    vm_template: str,
    active_instance_type: str,
) -> None:
    ci_uuid: str | None = None
    ci_name: str | None = None

    try:
        name = unique_name("e2e-ci")
        ci_uuid = cli.create_compute_instance(
            name=name,
            template=vm_template,
            network_attachments=[{"subnet": default_subnet}],
            instance_type=active_instance_type,
        )
        assert ci_uuid in grpc.list_compute_instance_ids(), f"ComputeInstance {ci_uuid} not found in list after create"

        # Wait for CR and verify reconciler expansion
        ci_name = wait_for_cr(k8s=k8s_hub_client, uuid=ci_uuid)
        ci_obj: dict[str, Any] = k8s_hub_client.get_json(resource="computeinstance", name=ci_name)
        spec: dict[str, Any] = ci_obj["spec"]
        assert spec["vcpus"] == IT_VCPUS, (
            f"E2E-02: reconciler should expand vCPUs from instance type: {spec['vcpus']} != {IT_VCPUS}"
        )
        assert spec["memoryGiB"] == IT_MEMORY_GIB, (
            f"E2E-02: reconciler should expand memory from instance type: {spec['memoryGiB']} != {IT_MEMORY_GIB}"
        )

        # Verify osac.openshift.io/instance-type-name label (E2E-03)
        labels: dict[str, str] = ci_obj["metadata"].get("labels", {})
        assert labels.get("osac.openshift.io/instance-type-name") == active_instance_type, (
            f"E2E-03: osac.openshift.io/instance-type-name label mismatch: "
            f"{labels.get('osac.openshift.io/instance-type-name')!r} != {active_instance_type!r}"
        )
    finally:
        if ci_uuid is not None:
            cli.delete_compute_instance(uuid=ci_uuid)
            if ci_name is not None:
                wait_for_deletion(k8s=k8s_hub_client, name=ci_name)


def test_compute_instance_deletion_protection(
    cli: OsacCLI,
    private_cli: OsacCLI,
    grpc: GRPCClient,
    k8s_hub_client: K8sClient,
    default_subnet: str,
    vm_template: str,
    active_instance_type: str,
) -> None:
    ci_uuid: str | None = None
    ci_name: str | None = None

    try:
        name = unique_name("e2e-ci")
        ci_uuid = cli.create_compute_instance(
            name=name,
            template=vm_template,
            network_attachments=[{"subnet": default_subnet}],
            instance_type=active_instance_type,
        )
        assert ci_uuid in grpc.list_compute_instance_ids(), f"ComputeInstance {ci_uuid} not found in list after create"
        ci_name = wait_for_cr(k8s=k8s_hub_client, uuid=ci_uuid)

        output, rc = run_unchecked(
            private_cli.binary, "--config", private_cli.config_dir, "delete", "instancetype", active_instance_type
        )
        assert rc != 0, "delete should be rejected when ComputeInstance references instance type"
        error_lower = output.lower()
        assert any(term in error_lower for term in ["409", "conflict", "referenced", "failedprecondition", "in use"]), (
            f"Expected conflict/reference error, got: {output}"
        )
    finally:
        if ci_uuid is not None:
            cli.delete_compute_instance(uuid=ci_uuid)
            if ci_name is not None:
                wait_for_deletion(k8s=k8s_hub_client, name=ci_name)


def test_compute_instance_deprecated_warning(
    cli: OsacCLI,
    private_grpc: GRPCClient,
    k8s_hub_client: K8sClient,
    default_subnet: str,
    vm_template: str,
    active_instance_type: str,
    default_disk_image: str,
) -> None:
    deprecated_ci_uuid: str | None = None
    deprecated_ci_name: str | None = None

    try:
        private_grpc.update_instance_type(name=active_instance_type, state="INSTANCE_TYPE_STATE_DEPRECATED")

        dep_output, dep_rc = run_unchecked(
            *_build_create_ci_args(
                cli,
                vm_template,
                default_subnet,
                active_instance_type,
                default_disk_image,
                ci_name=unique_name("e2e-ci"),
            )
        )
        assert dep_rc == 0, f"create with DEPRECATED type should succeed, got: {dep_output}"
        assert "deprecat" in dep_output.lower() or "warning" in dep_output.lower(), (
            f"Expected deprecation warning in output, got: {dep_output}"
        )

        uuid_match: re.Match[str] | None = re.search(
            r"'([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})'", dep_output
        )
        assert uuid_match is not None, f"Failed to parse UUID from CLI output: {dep_output}"
        deprecated_ci_uuid = uuid_match.group(1)
        deprecated_ci_name = wait_for_cr(k8s=k8s_hub_client, uuid=deprecated_ci_uuid)
    finally:
        if deprecated_ci_uuid is not None:
            cli.delete_compute_instance(uuid=deprecated_ci_uuid)
            if deprecated_ci_name is not None:
                wait_for_deletion(k8s=k8s_hub_client, name=deprecated_ci_name)


def test_compute_instance_nonexistent_instance_type(
    cli: OsacCLI, k8s_hub_client: K8sClient, default_subnet: str, vm_template: str, default_disk_image: str
) -> None:
    missing_it_name = f"nonexistent-it-{uuid4().hex[:8]}"
    ci_name = f"e2e-neg-{uuid4().hex[:8]}"
    output, rc = run_unchecked(
        *_build_create_ci_args(cli, vm_template, default_subnet, missing_it_name, default_disk_image, ci_name=ci_name)
    )
    if rc == 0:
        uuid_match = re.search(r"'([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})'", output)
        if uuid_match:
            ci_uuid = uuid_match.group(1)
            cr_name = wait_for_cr(k8s=k8s_hub_client, uuid=ci_uuid)
            cli.delete_compute_instance(uuid=ci_uuid)
            wait_for_deletion(k8s=k8s_hub_client, name=cr_name)
        elif k8s_hub_client.is_present(resource="computeinstance", name=ci_name):
            pytest.fail(f"ComputeInstance CR {ci_name} leaked but UUID could not be parsed from output: {output}")
    assert rc != 0, f"create with nonexistent instance type should fail, got: {output}"
    error_lower = output.lower()
    assert any(term in error_lower for term in ["not found", "404", "notfound"]), (
        f"Expected not-found error, got: {output}"
    )


def test_compute_instance_obsolete_instance_type(
    cli: OsacCLI,
    private_grpc: GRPCClient,
    k8s_hub_client: K8sClient,
    default_subnet: str,
    vm_template: str,
    active_instance_type: str,
    default_disk_image: str,
) -> None:
    private_grpc.update_instance_type(name=active_instance_type, state="INSTANCE_TYPE_STATE_DEPRECATED")
    private_grpc.update_instance_type(name=active_instance_type, state="INSTANCE_TYPE_STATE_OBSOLETE")

    ci_name = f"e2e-obs-{uuid4().hex[:8]}"
    output, rc = run_unchecked(
        *_build_create_ci_args(
            cli, vm_template, default_subnet, active_instance_type, default_disk_image, ci_name=ci_name
        )
    )
    if rc == 0:
        uuid_match = re.search(r"'([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})'", output)
        if uuid_match:
            ci_uuid = uuid_match.group(1)
            cr_name = wait_for_cr(k8s=k8s_hub_client, uuid=ci_uuid)
            cli.delete_compute_instance(uuid=ci_uuid)
            wait_for_deletion(k8s=k8s_hub_client, name=cr_name)
        elif k8s_hub_client.is_present(resource="computeinstance", name=ci_name):
            pytest.fail(f"ComputeInstance CR {ci_name} leaked but UUID could not be parsed from output: {output}")
    assert rc != 0, f"create with OBSOLETE instance type should be rejected, got: {output}"
    error_lower = output.lower()
    assert "obsolete" in error_lower and "failedprecondition" in error_lower, (
        f"Expected FailedPrecondition for obsolete instance type, got: {output}"
    )


def test_compute_instance_resize_up(
    grpc: GRPCClient,
    k8s_hub_client: K8sClient,
    resize_instance_types: dict[str, str],
    running_compute_instance_factory: Callable[[str], tuple[str, str]],
) -> None:
    ci_uuid, ci_name = running_compute_instance_factory("small")
    original = k8s_hub_client.get_json(resource="computeinstance", name=ci_name)
    original_version = original.get("status", {}).get("desiredConfigVersion", "")

    response = grpc.update_compute_instance_instance_type(ci_id=ci_uuid, instance_type=resize_instance_types["medium"])

    assert response.get("warnings", []) == []
    assert _instance_type_name(response) == resize_instance_types["medium"]
    updated = _wait_for_configuration_applied(
        k8s_hub_client, name=ci_name, vcpus=4, memory_gib=8, previous_config_version=original_version
    )
    assert updated["status"]["desiredConfigVersion"] != original_version
    assert updated["spec"]["vcpus"] == 4
    assert updated["spec"]["memoryGiB"] == 8


def test_compute_instance_resize_down(
    grpc: GRPCClient,
    k8s_hub_client: K8sClient,
    resize_instance_types: dict[str, str],
    running_compute_instance_factory: Callable[[str], tuple[str, str]],
) -> None:
    ci_uuid, ci_name = running_compute_instance_factory("medium")
    original = k8s_hub_client.get_json(resource="computeinstance", name=ci_name)
    original_version = original.get("status", {}).get("desiredConfigVersion", "")

    response = grpc.update_compute_instance_instance_type(ci_id=ci_uuid, instance_type=resize_instance_types["small"])

    assert response.get("warnings", []) == []
    assert _instance_type_name(response) == resize_instance_types["small"]
    updated = _wait_for_configuration_applied(
        k8s_hub_client, name=ci_name, vcpus=2, memory_gib=4, previous_config_version=original_version
    )
    assert updated["status"]["desiredConfigVersion"] != original_version
    assert updated["spec"]["vcpus"] == 2
    assert updated["spec"]["memoryGiB"] == 4


def test_compute_instance_resize_to_deprecated_type_returns_warning(
    grpc: GRPCClient,
    private_grpc: GRPCClient,
    k8s_hub_client: K8sClient,
    resize_instance_types: dict[str, str],
    running_compute_instance_factory: Callable[[str], tuple[str, str]],
) -> None:
    ci_uuid, ci_name = running_compute_instance_factory("small")
    target = resize_instance_types["medium"]
    original = k8s_hub_client.get_json(resource="computeinstance", name=ci_name)
    original_version = original.get("status", {}).get("desiredConfigVersion", "")
    private_grpc.update_instance_type(name=target, state="INSTANCE_TYPE_STATE_DEPRECATED")
    private_grpc.call(
        service="osac.private.v1.InstanceTypes/Update",
        data={
            "object": {
                "id": target,
                "spec": {
                    "deprecation": {
                        "replacement": {"name": resize_instance_types["small"]},
                        "obsolescence_timestamp": "2030-01-01T00:00:00Z",
                    }
                },
            },
            "updateMask": {"paths": ["spec.deprecation.replacement", "spec.deprecation.obsolescence_timestamp"]},
        },
    )

    response = grpc.update_compute_instance_instance_type(ci_id=ci_uuid, instance_type=target)

    warnings = response.get("warnings", [])
    assert len(warnings) == 1
    assert resize_instance_types["small"] in warnings[0]
    assert "2030-01-01" in warnings[0]
    assert _instance_type_name(response) == target
    updated = _wait_for_configuration_applied(
        k8s_hub_client, name=ci_name, vcpus=4, memory_gib=8, previous_config_version=original_version
    )
    assert updated["spec"]["vcpus"] == 4
    assert updated["spec"]["memoryGiB"] == 8


def test_compute_instance_resize_to_obsolete_type_is_rejected(
    grpc: GRPCClient,
    private_grpc: GRPCClient,
    k8s_hub_client: K8sClient,
    resize_instance_types: dict[str, str],
    running_compute_instance_factory: Callable[[str], tuple[str, str]],
) -> None:
    ci_uuid, ci_name = running_compute_instance_factory("small")
    target = resize_instance_types["medium"]
    private_grpc.update_instance_type(name=target, state="INSTANCE_TYPE_STATE_OBSOLETE")
    original_api = grpc.get_compute_instance(ci_id=ci_uuid)
    original_cr = k8s_hub_client.get_json(resource="computeinstance", name=ci_name)
    original_snapshot = _configuration_snapshot(original_cr)

    with pytest.raises(subprocess.CalledProcessError) as exc_info:
        grpc.update_compute_instance_instance_type(ci_id=ci_uuid, instance_type=target)

    assert_grpc_rejected(exc_info, "FailedPrecondition")
    assert _instance_type_name(grpc.get_compute_instance(ci_id=ci_uuid)) == _instance_type_name(original_api)
    assert (
        _configuration_snapshot(k8s_hub_client.get_json(resource="computeinstance", name=ci_name)) == original_snapshot
    )


def test_compute_instance_resize_to_current_type_is_noop(
    grpc: GRPCClient,
    k8s_hub_client: K8sClient,
    resize_instance_types: dict[str, str],
    running_compute_instance_factory: Callable[[str], tuple[str, str]],
) -> None:
    ci_uuid, ci_name = running_compute_instance_factory("small")
    original_api = grpc.get_compute_instance(ci_id=ci_uuid)
    original_cr = k8s_hub_client.get_json(resource="computeinstance", name=ci_name)
    original_snapshot = _configuration_snapshot(original_cr)

    response = grpc.update_compute_instance_instance_type(ci_id=ci_uuid, instance_type=resize_instance_types["small"])

    assert response.get("warnings", []) == []
    assert _instance_type_name(response) == _instance_type_name(original_api)
    current_cr = k8s_hub_client.get_json(resource="computeinstance", name=ci_name)
    assert _configuration_snapshot(current_cr) == original_snapshot
    assert _condition_status(current_cr, "ConfigurationApplied") == "True"
    assert _instance_type_name(grpc.get_compute_instance(ci_id=ci_uuid)) == resize_instance_types["small"]


def test_compute_instance_resize_rejects_different_gpu_spec(
    grpc: GRPCClient,
    private_grpc: GRPCClient,
    k8s_hub_client: K8sClient,
    resize_instance_types: dict[str, str],
    running_compute_instance_factory: Callable[[str], tuple[str, str]],
) -> None:
    ci_uuid, ci_name = running_compute_instance_factory("small")
    gpu_type = f"e2e-resize-gpu-{uuid4().hex[:8]}"
    private_grpc.create_instance_type(
        name=gpu_type,
        vcpus=4,
        memory_gib=8,
        description="E2E incompatible GPU resize type",
        gpu={"pci_device_selector": "10DE:25B6", "resource_name": "nvidia.com/gpu", "count": 1},
    )

    try:
        original_api = grpc.get_compute_instance(ci_id=ci_uuid)
        original_cr = k8s_hub_client.get_json(resource="computeinstance", name=ci_name)
        original_snapshot = _configuration_snapshot(original_cr)
        with pytest.raises(subprocess.CalledProcessError) as exc_info:
            grpc.update_compute_instance_instance_type(ci_id=ci_uuid, instance_type=gpu_type)

        assert_grpc_rejected(exc_info, "FailedPrecondition")
        assert _instance_type_name(grpc.get_compute_instance(ci_id=ci_uuid)) == _instance_type_name(original_api)
        assert _configuration_snapshot(k8s_hub_client.get_json(resource="computeinstance", name=ci_name)) == (
            original_snapshot
        )
    finally:
        try:
            private_grpc.delete_instance_type(name=gpu_type)
        except subprocess.CalledProcessError as e:
            output = ((e.stdout or "") + (e.stderr or "")).lower()
            if "not found" not in output:
                raise


def test_compute_instance_resize_while_stopped_applies_on_start(
    grpc: GRPCClient,
    k8s_hub_client: K8sClient,
    k8s_virt_client: K8sClient,
    resize_instance_types: dict[str, str],
    running_compute_instance_factory: Callable[[str], tuple[str, str]],
) -> None:
    ci_uuid, ci_name = running_compute_instance_factory("small")
    grpc.update_compute_instance_run_strategy(ci_id=ci_uuid, run_strategy="Halted")
    poll_until(
        fn=lambda: k8s_hub_client.get_compute_instance_phase(name=ci_name, checked=False),
        until=lambda phase: phase == "Stopped",
        retries=60,
        delay=5,
        description=f"{ci_name} stopped before resize",
    )
    original = k8s_hub_client.get_json(resource="computeinstance", name=ci_name)
    original_version = original.get("status", {}).get("desiredConfigVersion", "")

    response = grpc.update_compute_instance_instance_type(ci_id=ci_uuid, instance_type=resize_instance_types["medium"])

    assert _instance_type_name(response) == resize_instance_types["medium"]
    grpc.update_compute_instance_run_strategy(ci_id=ci_uuid, run_strategy="Always")
    wait_for_running(k8s=k8s_hub_client, name=ci_name)
    _wait_for_configuration_applied(
        k8s_hub_client, name=ci_name, vcpus=4, memory_gib=8, previous_config_version=original_version
    )
    restart_required = k8s_hub_client.get_compute_instance_condition_status(
        name=ci_name, condition_type="RestartRequired", checked=False
    )
    assert restart_required in ("", "False")

    vm_namespace = k8s_hub_client.get_compute_instance_vm_namespace(name=ci_name)
    _wait_for_vmi_resources(k8s_virt_client, vmi_namespace=vm_namespace, ci_name=ci_name, vcpus=4, memory_gib=8)


def test_compute_instance_resize_requires_restart_and_applies_new_resources(
    grpc: GRPCClient,
    k8s_hub_client: K8sClient,
    k8s_virt_client: K8sClient,
    vm_template: str,
    resize_instance_types: dict[str, str],
    running_compute_instance_factory: Callable[[str], tuple[str, str]],
) -> None:
    ci_uuid, ci_name = running_compute_instance_factory("small")
    vm_namespace = k8s_hub_client.get_compute_instance_vm_namespace(name=ci_name)
    _wait_for_vmi_resources(k8s_virt_client, vmi_namespace=vm_namespace, ci_name=ci_name, vcpus=2, memory_gib=4)

    current_key = "small"
    for target_key in ("medium", "small"):
        current_vcpus, current_memory = RESIZE_TYPE_RESOURCES[current_key]
        target_vcpus, target_memory = RESIZE_TYPE_RESOURCES[target_key]
        original = k8s_hub_client.get_json(resource="computeinstance", name=ci_name)
        original_version = original.get("status", {}).get("desiredConfigVersion", "")
        response = grpc.update_compute_instance_instance_type(
            ci_id=ci_uuid, instance_type=resize_instance_types[target_key]
        )
        assert response.get("warnings", []) == []
        updated = _wait_for_configuration_applied(
            k8s_hub_client,
            name=ci_name,
            vcpus=target_vcpus,
            memory_gib=target_memory,
            previous_config_version=original_version,
        )
        assert _instance_type_name(grpc.get_compute_instance(ci_id=ci_uuid)) == resize_instance_types[target_key]
        assert _condition_status(updated, "RestartRequired") == "True"

        _wait_for_vmi_resources(
            k8s_virt_client, vmi_namespace=vm_namespace, ci_name=ci_name, vcpus=current_vcpus, memory_gib=current_memory
        )
        previous_vmi_timestamp = k8s_virt_client.get_vmi_creation_timestamp(
            vmi_namespace=vm_namespace, compute_instance_name=ci_name
        )
        _restart_after_resize(
            grpc,
            k8s_hub_client,
            k8s_virt_client,
            ci_uuid=ci_uuid,
            ci_name=ci_name,
            vmi_namespace=vm_namespace,
            vm_template=vm_template,
            initial_vmi_timestamp=previous_vmi_timestamp,
        )
        _wait_for_vmi_resources(
            k8s_virt_client, vmi_namespace=vm_namespace, ci_name=ci_name, vcpus=target_vcpus, memory_gib=target_memory
        )
        current_key = target_key


def test_compute_instance_resize_from_catalog_item(
    grpc: GRPCClient,
    k8s_hub_client: K8sClient,
    k8s_virt_client: K8sClient,
    vm_template: str,
    default_subnet: str,
    default_storage_tier: str,
    default_disk_image: str,
    resize_instance_types: dict[str, str],
) -> None:
    """Verify a CatalogItem with an editable instance type can provision and resize a VM."""
    fields = {
        "boot_disk": {
            "storage_tier": {"editable": {"default_value": {"name": default_storage_tier}}},
            "size_gib": {"editable": {}},
        },
        "network_attachments": {"editable": {}},
        "disk_image": {"editable": {}},
        "instance_type": {"editable": {}},
        "run_strategy": {"editable": {}},
    }
    catalog_item_id = grpc.create_compute_instance_catalog_item(
        name=unique_name("e2e-resize-catalog"), template=vm_template, published=True, fields=fields
    )
    ci_uuid: str | None = None
    ci_name: str | None = None

    try:
        catalog_item = grpc.get_compute_instance_catalog_item(catalog_item_id=catalog_item_id)["object"]
        assert catalog_item["fields"]["instanceType"]["editable"] == {}

        response = grpc.call(
            service="osac.public.v1.ComputeInstances/Create",
            data={
                "object": {
                    "metadata": {"name": unique_name("e2e-resize-from-catalog")},
                    "spec": {
                        "catalog_item": {"id": catalog_item_id},
                        "instance_type": {"name": resize_instance_types["small"]},
                        "boot_disk": {"size_gib": 20, "storage_tier": {"name": default_storage_tier}},
                        "network_attachments": [{"subnet": {"id": default_subnet}}],
                        "disk_image": {"name": default_disk_image},
                        "run_strategy": "Always",
                    },
                }
            },
        )
        ci_uuid = response["object"]["id"]
        ci_name = wait_for_cr(k8s=k8s_hub_client, uuid=ci_uuid)
        wait_for_provision(k8s=k8s_hub_client, name=ci_name)
        wait_for_running(k8s=k8s_hub_client, name=ci_name)
        current = _wait_for_configuration_applied(k8s_hub_client, name=ci_name, vcpus=2, memory_gib=4)
        vm_namespace = k8s_hub_client.get_compute_instance_vm_namespace(name=ci_name)
        _wait_for_vmi_resources(k8s_virt_client, vmi_namespace=vm_namespace, ci_name=ci_name, vcpus=2, memory_gib=4)
        previous_vmi_timestamp = k8s_virt_client.get_vmi_creation_timestamp(
            vmi_namespace=vm_namespace, compute_instance_name=ci_name
        )
        assert _instance_type_name(grpc.get_compute_instance(ci_id=ci_uuid)) == resize_instance_types["small"]

        original_version = current.get("status", {}).get("desiredConfigVersion", "")
        update_response = grpc.update_compute_instance_instance_type(
            ci_id=ci_uuid, instance_type=resize_instance_types["medium"]
        )

        assert update_response.get("warnings", []) == []
        assert _instance_type_name(update_response) == resize_instance_types["medium"]
        updated = _wait_for_configuration_applied(
            k8s_hub_client, name=ci_name, vcpus=4, memory_gib=8, previous_config_version=original_version
        )
        assert updated["status"]["desiredConfigVersion"] != original_version
        assert _instance_type_name(grpc.get_compute_instance(ci_id=ci_uuid)) == resize_instance_types["medium"]

        assert _condition_status(updated, "RestartRequired") == "True"
        _restart_after_resize(
            grpc,
            k8s_hub_client,
            k8s_virt_client,
            ci_uuid=ci_uuid,
            ci_name=ci_name,
            vmi_namespace=vm_namespace,
            vm_template=vm_template,
            initial_vmi_timestamp=previous_vmi_timestamp,
        )

        _wait_for_vmi_resources(k8s_virt_client, vmi_namespace=vm_namespace, ci_name=ci_name, vcpus=4, memory_gib=8)
    finally:
        try:
            if ci_uuid is not None:
                grpc.delete_compute_instance(ci_id=ci_uuid)
                if ci_name is not None:
                    wait_for_deletion(k8s=k8s_hub_client, name=ci_name)
        finally:
            grpc.delete_compute_instance_catalog_item(catalog_item_id=catalog_item_id)
