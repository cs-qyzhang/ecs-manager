from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from typing import Any

from aliyunsdkcore.client import AcsClient
from aliyunsdkecs.request.v20140526.CreateInstanceRequest import CreateInstanceRequest
from aliyunsdkecs.request.v20140526.DeleteInstanceRequest import DeleteInstanceRequest
from aliyunsdkecs.request.v20140526.DescribeInstancesRequest import DescribeInstancesRequest
from aliyunsdkecs.request.v20140526.DescribeRegionsRequest import DescribeRegionsRequest
from aliyunsdkecs.request.v20140526.StartInstanceRequest import StartInstanceRequest
from aliyunsdkecs.request.v20140526.StopInstanceRequest import StopInstanceRequest
from aliyunsdkecs.request.v20140526.AllocatePublicIpAddressRequest import AllocatePublicIpAddressRequest

from .util import normalize_region_id


class EcsError(RuntimeError):
    pass


def _get_credentials() -> tuple[str, str]:
    # Common env names people use.
    candidates = [
        ("ALIBABA_CLOUD_ACCESS_KEY_ID", "ALIBABA_CLOUD_ACCESS_KEY_SECRET"),
        ("ALIYUN_ACCESS_KEY_ID", "ALIYUN_ACCESS_KEY_SECRET"),
        ("ALICLOUD_ACCESS_KEY_ID", "ALICLOUD_ACCESS_KEY_SECRET"),
    ]
    for ak_key, sk_key in candidates:
        ak = os.getenv(ak_key)
        sk = os.getenv(sk_key)
        if ak and sk:
            return ak, sk
    raise EcsError(
        "Missing credentials. Set env vars ALIBABA_CLOUD_ACCESS_KEY_ID and "
        "ALIBABA_CLOUD_ACCESS_KEY_SECRET (or ALIYUN_ACCESS_KEY_ID/ALIYUN_ACCESS_KEY_SECRET)."
    )


def ecs_client(region_id: str) -> AcsClient:
    if not region_id:
        raise EcsError("region_id is required")
    region_id, _ = normalize_region_id(region_id)
    ak, sk = _get_credentials()
    return AcsClient(ak, sk, region_id)


def _do_action_json(client: AcsClient, request: Any) -> dict[str, Any]:
    request.set_accept_format("json")
    if hasattr(request, "set_protocol_type"):
        request.set_protocol_type("https")
    raw = client.do_action_with_exception(request)
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    return json.loads(raw)


def create_instance(
    *,
    region_id: str,
    image_id: str,
    instance_type: str,
    security_group_id: str,
    v_switch_id: str,
    key_pair_name: str,
    instance_name: str,
    hostname: str | None = None,
    tags: list[dict[str, str]] | None = None,
    system_disk_category: str | None = None,
    system_disk_size: int | None = None,
    system_disk_performance_level: str | None = None,
    internet_charge_type: str = "PayByTraffic",
    internet_max_bandwidth_out: int | None = 10,
    spot_strategy: str | None = "SpotAsPriceGo",
    spot_price_limit: float | str | None = None,
    spot_duration: int | None = None,
    spot_interruption_behavior: str | None = None,
) -> str:
    region_id, _ = normalize_region_id(region_id)
    client = ecs_client(region_id)

    req = CreateInstanceRequest()
    # Some SDK versions still require RegionId on request.
    if hasattr(req, "set_RegionId"):
        req.set_RegionId(region_id)

    req.set_ImageId(image_id)
    req.set_InstanceType(instance_type)
    req.set_SecurityGroupId(security_group_id)
    req.set_VSwitchId(v_switch_id)
    req.set_KeyPairName(key_pair_name)
    req.set_InstanceName(instance_name)
    req.set_InstanceChargeType("PostPaid")

    if hostname:
        if hasattr(req, "set_HostName"):
            req.set_HostName(hostname)
        else:
            raise EcsError("SDK does not support HostName on CreateInstanceRequest")

    if tags:
        # SDK encodes into Tag.1.Key/Tag.1.Value...
        req.set_Tags(tags)

    if system_disk_category:
        req.set_SystemDiskCategory(system_disk_category)
    if system_disk_size is not None:
        req.set_SystemDiskSize(int(system_disk_size))
    if system_disk_performance_level:
        req.set_SystemDiskPerformanceLevel(system_disk_performance_level)

    if spot_strategy:
        # ECS: NoSpot | SpotAsPriceGo | SpotWithPriceLimit
        if hasattr(req, "set_SpotStrategy"):
            req.set_SpotStrategy(spot_strategy)
        else:
            raise EcsError("SDK does not support SpotStrategy on CreateInstanceRequest")

        if spot_strategy == "SpotWithPriceLimit":
            if spot_price_limit is None or str(spot_price_limit).strip() == "":
                raise EcsError("spot_price_limit is required when spot_strategy=SpotWithPriceLimit")

        if spot_price_limit is not None and str(spot_price_limit).strip() != "":
            if hasattr(req, "set_SpotPriceLimit"):
                req.set_SpotPriceLimit(str(spot_price_limit))
            else:
                raise EcsError("SDK does not support SpotPriceLimit on CreateInstanceRequest")

        if spot_duration is not None:
            if hasattr(req, "set_SpotDuration"):
                req.set_SpotDuration(int(spot_duration))
            else:
                raise EcsError("SDK does not support SpotDuration on CreateInstanceRequest")

        if spot_interruption_behavior:
            if hasattr(req, "set_SpotInterruptionBehavior"):
                req.set_SpotInterruptionBehavior(spot_interruption_behavior)
            else:
                raise EcsError("SDK does not support SpotInterruptionBehavior on CreateInstanceRequest")

    if internet_max_bandwidth_out is not None:
        req.set_InternetMaxBandwidthOut(int(internet_max_bandwidth_out))
        if internet_charge_type:
            req.set_InternetChargeType(internet_charge_type)

    resp = _do_action_json(client, req)
    instance_id = resp.get("InstanceId")
    if not instance_id:
        raise EcsError(f"CreateInstance response missing InstanceId: {resp}")
    return instance_id


def start_instance(*, region_id: str, instance_id: str) -> None:
    region_id, _ = normalize_region_id(region_id)
    client = ecs_client(region_id)
    req = StartInstanceRequest()
    if hasattr(req, "set_RegionId"):
        req.set_RegionId(region_id)
    req.set_InstanceId(instance_id)
    _do_action_json(client, req)


def stop_instance(
    *,
    region_id: str,
    instance_id: str,
    force: bool = False,
    stopped_mode: str | None = "StopCharging",
) -> None:
    """
    Stop an instance.

    stopped_mode:
      - StopCharging: stop billing for pay-as-you-go compute (if supported by the instance/region)
      - KeepCharging: keep billing while stopped
      - None: let Aliyun decide default
    """
    region_id, _ = normalize_region_id(region_id)
    client = ecs_client(region_id)
    req = StopInstanceRequest()
    if hasattr(req, "set_RegionId"):
        req.set_RegionId(region_id)
    req.set_InstanceId(instance_id)
    if force:
        req.set_ForceStop(True)
    if stopped_mode:
        # ECS expects "StopCharging" or "KeepCharging"
        req.set_StoppedMode(stopped_mode)
    _do_action_json(client, req)


def delete_instance(*, region_id: str, instance_id: str, force: bool = True) -> None:
    region_id, _ = normalize_region_id(region_id)
    client = ecs_client(region_id)
    req = DeleteInstanceRequest()
    if hasattr(req, "set_RegionId"):
        req.set_RegionId(region_id)
    req.set_InstanceId(instance_id)
    if force:
        req.set_Force(True)
    _do_action_json(client, req)


def allocate_public_ip_address(*, region_id: str, instance_id: str) -> str:
    """
    Allocate an ephemeral public IPv4 address for an instance.

    Note: typically requires InternetMaxBandwidthOut > 0 on the instance.
    """
    region_id, _ = normalize_region_id(region_id)
    client = ecs_client(region_id)
    req = AllocatePublicIpAddressRequest()
    if hasattr(req, "set_RegionId"):
        req.set_RegionId(region_id)
    req.set_InstanceId(instance_id)
    resp = _do_action_json(client, req)
    ip = resp.get("IpAddress")
    if not isinstance(ip, str) or not ip:
        raise EcsError(f"AllocatePublicIpAddress response missing IpAddress: {resp}")
    return ip


def _first_ip(value: Any) -> str | None:
    if isinstance(value, list) and value:
        v0 = value[0]
        return v0 if isinstance(v0, str) and v0 else None
    if isinstance(value, str) and value:
        return value
    return None


@dataclass(frozen=True)
class InstanceInfo:
    instance_id: str
    status: str | None
    public_ip: str | None
    private_ip: str | None
    zone_id: str | None
    image_id: str | None
    instance_type: str | None
    instance_name: str | None
    raw: dict[str, Any]


def _instance_info_from_dict(inst: dict[str, Any]) -> InstanceInfo:
    instance_id = str(inst.get("InstanceId") or "")
    public_ip = None
    eip = (inst.get("EipAddress") or {}).get("IpAddress")
    if isinstance(eip, str) and eip:
        public_ip = eip
    if not public_ip:
        public_ip = _first_ip((inst.get("PublicIpAddress") or {}).get("IpAddress"))

    private_ip = _first_ip(
        ((inst.get("VpcAttributes") or {}).get("PrivateIpAddress") or {}).get("IpAddress")
    )
    if not private_ip:
        nics = (inst.get("NetworkInterfaces") or {}).get("NetworkInterface") or []
        if isinstance(nics, list) and nics:
            nic0 = nics[0]
            if isinstance(nic0, dict):
                p = nic0.get("PrimaryIpAddress")
                if isinstance(p, str) and p:
                    private_ip = p

    return InstanceInfo(
        instance_id=instance_id,
        status=inst.get("Status"),
        public_ip=public_ip,
        private_ip=private_ip,
        zone_id=inst.get("ZoneId"),
        image_id=inst.get("ImageId"),
        instance_type=inst.get("InstanceType"),
        instance_name=inst.get("InstanceName"),
        raw=inst,
    )


def list_instances(
    *,
    region_id: str,
    page_size: int = 100,
    tags: list[dict[str, str]] | None = None,
) -> list[InstanceInfo]:
    """
    List instances in a region (DescribeInstances pagination).

    If tags is provided, instances are filtered by tags.
    """
    region_id, _ = normalize_region_id(region_id)
    client = ecs_client(region_id)

    out: list[InstanceInfo] = []
    page_number = 1

    while True:
        req = DescribeInstancesRequest()
        if hasattr(req, "set_RegionId"):
            req.set_RegionId(region_id)
        req.set_PageSize(int(page_size))
        req.set_PageNumber(int(page_number))
        if tags:
            req.set_Tags(tags)

        resp = _do_action_json(client, req)
        total = int(resp.get("TotalCount") or 0)
        instances = (resp.get("Instances") or {}).get("Instance") or []
        if isinstance(instances, dict):
            instances = [instances]

        if isinstance(instances, list):
            for inst in instances:
                if isinstance(inst, dict):
                    out.append(_instance_info_from_dict(inst))

        if total and len(out) >= total:
            break
        if not instances:
            break
        page_number += 1

    return out


def list_regions(*, seed_region_id: str) -> list[str]:
    """
    List available ECS regions (DescribeRegions).

    The ECS 2014-05-26 API requires a client region for endpoint selection;
    `seed_region_id` can be any valid RegionId (commonly config region).
    """
    seed_region_id, _ = normalize_region_id(seed_region_id)
    client = ecs_client(seed_region_id)
    req = DescribeRegionsRequest()
    resp = _do_action_json(client, req)
    regions = (resp.get("Regions") or {}).get("Region") or []
    if isinstance(regions, dict):
        regions = [regions]
    out: list[str] = []
    if isinstance(regions, list):
        for r in regions:
            if isinstance(r, dict):
                rid = r.get("RegionId")
                if isinstance(rid, str) and rid:
                    out.append(rid)
    return out


def describe_instance(*, region_id: str, instance_id: str) -> InstanceInfo | None:
    region_id, _ = normalize_region_id(region_id)
    client = ecs_client(region_id)
    req = DescribeInstancesRequest()
    if hasattr(req, "set_RegionId"):
        req.set_RegionId(region_id)
    req.set_InstanceIds(json.dumps([instance_id]))

    resp = _do_action_json(client, req)
    instances = (resp.get("Instances") or {}).get("Instance") or []
    if not instances:
        return None
    inst = instances[0] if isinstance(instances, list) else instances
    if not isinstance(inst, dict):
        return None

    public_ip = None
    eip = (inst.get("EipAddress") or {}).get("IpAddress")
    if isinstance(eip, str) and eip:
        public_ip = eip
    if not public_ip:
        public_ip = _first_ip((inst.get("PublicIpAddress") or {}).get("IpAddress"))

    private_ip = _first_ip(
        ((inst.get("VpcAttributes") or {}).get("PrivateIpAddress") or {}).get("IpAddress")
    )
    if not private_ip:
        nics = (inst.get("NetworkInterfaces") or {}).get("NetworkInterface") or []
        if isinstance(nics, list) and nics:
            nic0 = nics[0]
            if isinstance(nic0, dict):
                p = nic0.get("PrimaryIpAddress")
                if isinstance(p, str) and p:
                    private_ip = p

    return InstanceInfo(
        instance_id=instance_id,
        status=inst.get("Status"),
        public_ip=public_ip,
        private_ip=private_ip,
        zone_id=inst.get("ZoneId"),
        image_id=inst.get("ImageId"),
        instance_type=inst.get("InstanceType"),
        instance_name=inst.get("InstanceName"),
        raw=inst,
    )


def wait_instance(
    *,
    region_id: str,
    instance_id: str,
    timeout_seconds: int = 600,
    poll_interval_seconds: int = 5,
    require_public_ip: bool = True,
) -> InstanceInfo:
    deadline = time.time() + max(1, int(timeout_seconds))
    last: InstanceInfo | None = None

    while time.time() < deadline:
        info = describe_instance(region_id=region_id, instance_id=instance_id)
        last = info
        if info and info.status == "Running":
            if require_public_ip:
                if info.public_ip:
                    return info
            else:
                if info.public_ip or info.private_ip:
                    return info
        time.sleep(max(1, int(poll_interval_seconds)))

    raise TimeoutError(
        f"Timed out waiting for instance {instance_id} to be Running "
        f"({'public ip' if require_public_ip else 'ip'}). Last={last}"
    )


def wait_instance_status(
    *,
    region_id: str,
    instance_id: str,
    desired_status: str,
    timeout_seconds: int = 600,
    poll_interval_seconds: int = 5,
) -> InstanceInfo:
    deadline = time.time() + max(1, int(timeout_seconds))
    last: InstanceInfo | None = None

    while time.time() < deadline:
        info = describe_instance(region_id=region_id, instance_id=instance_id)
        last = info
        if info and info.status == desired_status:
            return info
        time.sleep(max(1, int(poll_interval_seconds)))

    raise TimeoutError(
        f"Timed out waiting for instance {instance_id} to be {desired_status}. Last={last}"
    )


