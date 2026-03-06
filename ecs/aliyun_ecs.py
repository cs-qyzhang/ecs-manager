from __future__ import annotations

import base64
import json
import os
import time
from dataclasses import dataclass
from typing import Any

from aliyunsdkcore.client import AcsClient
from aliyunsdkecs.request.v20140526.CreateInstanceRequest import CreateInstanceRequest
from aliyunsdkecs.request.v20140526.CreateNetworkInterfaceRequest import CreateNetworkInterfaceRequest
from aliyunsdkecs.request.v20140526.DeleteInstanceRequest import DeleteInstanceRequest
from aliyunsdkecs.request.v20140526.DeleteNetworkInterfaceRequest import DeleteNetworkInterfaceRequest
from aliyunsdkecs.request.v20140526.DescribeInstancesRequest import DescribeInstancesRequest
from aliyunsdkecs.request.v20140526.DescribeInstanceTypesRequest import DescribeInstanceTypesRequest
from aliyunsdkecs.request.v20140526.DescribeRegionsRequest import DescribeRegionsRequest
from aliyunsdkecs.request.v20140526.DescribeSecurityGroupsRequest import DescribeSecurityGroupsRequest
from aliyunsdkecs.request.v20140526.DescribeVSwitchesRequest import DescribeVSwitchesRequest
from aliyunsdkecs.request.v20140526.StartInstanceRequest import StartInstanceRequest
from aliyunsdkecs.request.v20140526.StopInstanceRequest import StopInstanceRequest
from aliyunsdkecs.request.v20140526.AllocatePublicIpAddressRequest import AllocatePublicIpAddressRequest
from aliyunsdkecs.request.v20140526.AttachNetworkInterfaceRequest import AttachNetworkInterfaceRequest
from aliyunsdkecs.request.v20140526.AuthorizeSecurityGroupRequest import AuthorizeSecurityGroupRequest
from aliyunsdkecs.request.v20140526.RevokeSecurityGroupRequest import RevokeSecurityGroupRequest
from aliyunsdkecs.request.v20140526.DescribeSecurityGroupAttributeRequest import DescribeSecurityGroupAttributeRequest
from aliyunsdkecs.request.v20140526.ModifyNetworkInterfaceAttributeRequest import ModifyNetworkInterfaceAttributeRequest

from .util import normalize_region_id


class EcsError(RuntimeError):
    pass


def _to_int(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        try:
            return int(s)
        except ValueError:
            return None
    try:
        return int(value)
    except Exception:
        return None


def _extract_instance_type_dicts(resp: dict[str, Any]) -> list[dict[str, Any]]:
    items = (resp.get("InstanceTypes") or {}).get("InstanceType") or []
    if isinstance(items, dict):
        items = [items]
    out: list[dict[str, Any]] = []
    if isinstance(items, list):
        for it in items:
            if isinstance(it, dict):
                out.append(it)
    return out


def assert_instance_type_supports_erdma(*, region_id: str, instance_type: str) -> None:
    """
    Raise EcsError if the instance_type does not support eRDMA (ERI).
    """
    region_id, _ = normalize_region_id(region_id)
    client = ecs_client(region_id)

    req = DescribeInstanceTypesRequest()
    if hasattr(req, "set_InstanceTypess"):
        req.set_InstanceTypess([instance_type])
    elif hasattr(req, "set_InstanceTypes"):
        req.set_InstanceTypes([instance_type])
    else:
        raise EcsError("SDK does not support setting InstanceTypes on DescribeInstanceTypesRequest")

    resp = _do_action_json(client, req)
    items = _extract_instance_type_dicts(resp)
    if not items:
        raise EcsError(f"Unknown instance_type: {instance_type}")

    rec = items[0]

    # Prefer explicit ERI quantity if present.
    eri_quantity = _to_int(rec.get("EriQuantity"))
    if eri_quantity is not None:
        if eri_quantity <= 0:
            raise EcsError(f"Instance type {instance_type} does not support eRDMA (ERI).")
        return

    # Fallback: some SDK/API variants expose a boolean support flag.
    for k in ("ErdmaSupport", "ErdmaSupported", "IsErdmaSupported"):
        v = rec.get(k)
        if isinstance(v, bool):
            if not v:
                raise EcsError(f"Instance type {instance_type} does not support eRDMA (ERI).")
            return
        if isinstance(v, str):
            low = v.strip().lower()
            if low in {"true", "yes", "supported", "support"}:
                return
            if low in {"false", "no", "unsupported", "notsupported", "not_supported"}:
                raise EcsError(f"Instance type {instance_type} does not support eRDMA (ERI).")

    # Fallback: ask ECS to filter by ERI capability.
    req2 = DescribeInstanceTypesRequest()
    if hasattr(req2, "set_MinimumEriQuantity"):
        req2.set_MinimumEriQuantity(1)
    else:
        raise EcsError("Cannot verify eRDMA (ERI) support: SDK does not support MinimumEriQuantity.")

    if hasattr(req2, "set_InstanceTypess"):
        req2.set_InstanceTypess([instance_type])
    elif hasattr(req2, "set_InstanceTypes"):
        req2.set_InstanceTypes([instance_type])

    resp2 = _do_action_json(client, req2)
    items2 = _extract_instance_type_dicts(resp2)
    if not items2:
        raise EcsError(f"Instance type {instance_type} does not support eRDMA (ERI).")


def create_erdma_network_interface(
    *,
    region_id: str,
    v_switch_id: str,
    security_group_id: str,
    name: str,
    description: str | None = None,
    tags: list[dict[str, str]] | None = None,
) -> str:
    """
    Create a HighPerformance ENI (ERI) for eRDMA.
    """
    region_id, _ = normalize_region_id(region_id)
    client = ecs_client(region_id)

    req = CreateNetworkInterfaceRequest()
    req.set_VSwitchId(v_switch_id)
    req.set_SecurityGroupId(security_group_id)
    req.set_NetworkInterfaceName(name)
    if description:
        req.set_Description(description)
    req.set_NetworkInterfaceTrafficMode("HighPerformance")
    if tags:
        req.set_Tags(tags)

    resp = _do_action_json(client, req)
    nid = resp.get("NetworkInterfaceId")
    if not isinstance(nid, str) or not nid:
        raise EcsError(f"CreateNetworkInterface response missing NetworkInterfaceId: {resp}")
    return nid


def attach_network_interface(*, region_id: str, instance_id: str, network_interface_id: str) -> None:
    region_id, _ = normalize_region_id(region_id)
    client = ecs_client(region_id)

    req = AttachNetworkInterfaceRequest()
    req.set_InstanceId(instance_id)
    req.set_NetworkInterfaceId(network_interface_id)
    _do_action_json(client, req)


def set_network_interface_delete_on_release(
    *, region_id: str, network_interface_id: str, delete_on_release: bool = True
) -> None:
    """
    Best-effort: delete ENI automatically when the instance is released.
    """
    region_id, _ = normalize_region_id(region_id)
    client = ecs_client(region_id)

    req = ModifyNetworkInterfaceAttributeRequest()
    req.set_NetworkInterfaceId(network_interface_id)
    req.set_DeleteOnRelease(bool(delete_on_release))
    _do_action_json(client, req)


def delete_network_interface(*, region_id: str, network_interface_id: str) -> None:
    region_id, _ = normalize_region_id(region_id)
    client = ecs_client(region_id)
    req = DeleteNetworkInterfaceRequest()
    req.set_NetworkInterfaceId(network_interface_id)
    _do_action_json(client, req)


@dataclass(frozen=True)
class VSwitchInfo:
    v_switch_id: str
    vpc_id: str | None
    zone_id: str | None
    is_default: bool | None
    raw: dict[str, Any]


def describe_vswitch(*, region_id: str, v_switch_id: str) -> VSwitchInfo | None:
    region_id, _ = normalize_region_id(region_id)
    client = ecs_client(region_id)
    req = DescribeVSwitchesRequest()
    req.set_VSwitchId(v_switch_id)
    resp = _do_action_json(client, req)
    items = (resp.get("VSwitches") or {}).get("VSwitch") or []
    if isinstance(items, dict):
        items = [items]
    if not isinstance(items, list) or not items:
        return None
    item = items[0]
    if not isinstance(item, dict):
        return None

    raw_is_default = item.get("IsDefault")
    is_default: bool | None = None
    if isinstance(raw_is_default, bool):
        is_default = raw_is_default
    elif isinstance(raw_is_default, str):
        low = raw_is_default.strip().lower()
        if low in {"true", "yes"}:
            is_default = True
        elif low in {"false", "no"}:
            is_default = False

    return VSwitchInfo(
        v_switch_id=str(item.get("VSwitchId") or v_switch_id),
        vpc_id=item.get("VpcId") if isinstance(item.get("VpcId"), str) else None,
        zone_id=item.get("ZoneId") if isinstance(item.get("ZoneId"), str) else None,
        is_default=is_default,
        raw=item,
    )


@dataclass(frozen=True)
class SecurityGroupInfo:
    security_group_id: str
    security_group_name: str | None
    vpc_id: str | None
    is_default: bool
    raw: dict[str, Any]


def _is_default_security_group(raw: dict[str, Any]) -> bool:
    v = raw.get("IsDefault")
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        if v.strip().lower() in {"true", "yes"}:
            return True
        if v.strip().lower() in {"false", "no"}:
            return False

    name = raw.get("SecurityGroupName")
    if isinstance(name, str) and name.strip().lower() == "default":
        return True
    return False


def list_security_groups(*, region_id: str, vpc_id: str, page_size: int = 100) -> list[SecurityGroupInfo]:
    """
    List security groups in a VPC.
    """
    region_id, _ = normalize_region_id(region_id)
    client = ecs_client(region_id)

    out: list[SecurityGroupInfo] = []
    page_number = 1
    while True:
        req = DescribeSecurityGroupsRequest()
        req.set_VpcId(vpc_id)
        req.set_PageSize(int(page_size))
        req.set_PageNumber(int(page_number))
        resp = _do_action_json(client, req)
        items = (resp.get("SecurityGroups") or {}).get("SecurityGroup") or []
        if isinstance(items, dict):
            items = [items]
        if not isinstance(items, list) or not items:
            break
        for item in items:
            if not isinstance(item, dict):
                continue
            sgid = item.get("SecurityGroupId")
            if not isinstance(sgid, str) or not sgid:
                continue
            out.append(
                SecurityGroupInfo(
                    security_group_id=sgid,
                    security_group_name=item.get("SecurityGroupName")
                    if isinstance(item.get("SecurityGroupName"), str)
                    else None,
                    vpc_id=item.get("VpcId") if isinstance(item.get("VpcId"), str) else None,
                    is_default=_is_default_security_group(item),
                    raw=item,
                )
            )
        total = _to_int(resp.get("TotalCount"))
        if total is not None and len(out) >= total:
            break
        page_number += 1

    return out


def resolve_security_group_id_from_vswitch(*, region_id: str, v_switch_id: str) -> str:
    """
    Best-effort derive security_group_id for `ecs create` from the vSwitch.

    Rules:
      1) Prefer the default security group in the same VPC.
      2) If only one security group exists, use it.
      3) Otherwise, raise EcsError and ask the user to specify security_group_id.
    """
    vsw = describe_vswitch(region_id=region_id, v_switch_id=v_switch_id)
    if not vsw or not vsw.vpc_id:
        raise EcsError(f"Failed to resolve VPC from v_switch_id: {v_switch_id}")

    sgs = list_security_groups(region_id=region_id, vpc_id=vsw.vpc_id)
    if not sgs:
        raise EcsError(f"No security groups found in VPC {vsw.vpc_id} (from v_switch_id {v_switch_id}).")

    defaults = [sg for sg in sgs if sg.is_default]
    if len(defaults) == 1:
        return defaults[0].security_group_id
    if len(defaults) > 1:
        ids = ", ".join(sorted(sg.security_group_id for sg in defaults))
        raise EcsError(f"Multiple default security groups found in VPC {vsw.vpc_id}: {ids}")

    if len(sgs) == 1:
        return sgs[0].security_group_id

    preview = ", ".join(
        f"{sg.security_group_id}({sg.security_group_name or '-'})" for sg in sorted(sgs, key=lambda x: x.security_group_id)
    )
    raise EcsError(
        "security_group_id is not set and cannot be inferred uniquely. "
        f"VPC {vsw.vpc_id} has multiple security groups: {preview}"
    )


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
    user_data: str | None = None,
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

    if user_data is not None and str(user_data).strip() != "":
        raw_user_data = str(user_data)
        raw_user_data_bytes = raw_user_data.encode("utf-8")
        if len(raw_user_data_bytes) > 32 * 1024:
            raise EcsError("user_data is too large; raw content must be at most 32 KB before Base64 encoding")
        encoded_user_data = base64.b64encode(raw_user_data_bytes).decode("ascii")
        if hasattr(req, "set_UserData"):
            req.set_UserData(encoded_user_data)
        else:
            raise EcsError("SDK does not support UserData on CreateInstanceRequest")

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


def get_instance_security_group_id(*, region_id: str, instance_id: str) -> str | None:
    """Get the first security group ID for an instance."""
    info = describe_instance(region_id=region_id, instance_id=instance_id)
    if not info:
        return None
    
    # Try to get from SecurityGroupIds
    raw = info.raw
    sg_ids = raw.get("SecurityGroupIds") or {}
    sg_list = sg_ids.get("SecurityGroupId") or []
    
    if isinstance(sg_list, list) and sg_list:
        sg_id = sg_list[0]
        if isinstance(sg_id, str) and sg_id:
            return sg_id
    
    # Try from SecurityGroupId (singular, older format)
    sg_id = raw.get("SecurityGroupId")
    if isinstance(sg_id, str) and sg_id:
        return sg_id
    
    return None


@dataclass(frozen=True)
class SecurityGroupRule:
    """Represents a security group ingress rule."""
    port_range: str  # e.g., "80/80", "22/22"
    protocol: str  # e.g., "tcp", "udp"
    source_cidr: str  # e.g., "0.0.0.0/0"
    description: str | None
    rule_id: str | None


def list_security_group_rules(*, region_id: str, security_group_id: str) -> list[SecurityGroupRule]:
    """List ingress rules for a security group."""
    region_id, _ = normalize_region_id(region_id)
    client = ecs_client(region_id)
    
    req = DescribeSecurityGroupAttributeRequest()
    if hasattr(req, "set_RegionId"):
        req.set_RegionId(region_id)
    req.set_SecurityGroupId(security_group_id)
    req.set_Direction("ingress")
    
    resp = _do_action_json(client, req)
    
    rules = []
    permissions = (resp.get("Permissions") or {}).get("Permission") or []
    if isinstance(permissions, dict):
        permissions = [permissions]
    
    if isinstance(permissions, list):
        for perm in permissions:
            if not isinstance(perm, dict):
                continue
            
            port_range = perm.get("PortRange") or ""
            protocol = perm.get("IpProtocol") or ""
            source_cidr = perm.get("SourceCidrIp") or ""
            description = perm.get("Description")
            rule_id = perm.get("SecurityGroupRuleId")
            
            if port_range and protocol:
                rules.append(
                    SecurityGroupRule(
                        port_range=str(port_range),
                        protocol=str(protocol).lower(),
                        source_cidr=str(source_cidr) or "0.0.0.0/0",
                        description=str(description) if description else None,
                        rule_id=str(rule_id) if rule_id else None,
                    )
                )
    
    return rules


def authorize_security_group_rule(
    *,
    region_id: str,
    security_group_id: str,
    port: int,
    protocol: str = "tcp",
    source_cidr: str = "0.0.0.0/0",
    description: str | None = None,
) -> None:
    """Add an ingress rule to a security group."""
    region_id, _ = normalize_region_id(region_id)
    client = ecs_client(region_id)
    
    req = AuthorizeSecurityGroupRequest()
    if hasattr(req, "set_RegionId"):
        req.set_RegionId(region_id)
    req.set_SecurityGroupId(security_group_id)
    req.set_IpProtocol(protocol.lower())
    req.set_PortRange(f"{port}/{port}")
    req.set_SourceCidrIp(source_cidr)
    if description:
        req.set_Description(description)
    
    _do_action_json(client, req)


def revoke_security_group_rule(
    *,
    region_id: str,
    security_group_id: str,
    port: int,
    protocol: str = "tcp",
    source_cidr: str = "0.0.0.0/0",
) -> None:
    """Remove an ingress rule from a security group."""
    region_id, _ = normalize_region_id(region_id)
    client = ecs_client(region_id)
    
    req = RevokeSecurityGroupRequest()
    if hasattr(req, "set_RegionId"):
        req.set_RegionId(region_id)
    req.set_SecurityGroupId(security_group_id)
    req.set_IpProtocol(protocol.lower())
    req.set_PortRange(f"{port}/{port}")
    req.set_SourceCidrIp(source_cidr)
    
    _do_action_json(client, req)
