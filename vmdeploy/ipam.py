"""Optional Nexus IPAM integration: allocate the address and publish DNS as
part of the deploy, release both on failure.

Enabled only when IPAM_URL and IPAM_TOKEN are configured (env or the runtime
settings file); without them every path behaves exactly as before — the
deployer stays fully standalone.

The contract is one call each way:
  * provision:   POST /api/provision   → next free IP in IPAM_NETWORK plus the
                 network's L3 facts (prefixlen, gateway, dns, domain) AND the
                 name published to every DNS node — the VM boots resolvable.
  * deprovision: POST /api/deprovision → the exact inverse; used here as the
                 rollback when a clone fails after allocation.

Stdlib-only on purpose (same promise as govc.py/config.py — the CLI must not
grow dependencies).
"""
from __future__ import annotations

import json
import ssl
import urllib.error
import urllib.request

from . import config

_CTX = ssl._create_unverified_context()


class IpamError(RuntimeError):
    pass


def enabled() -> bool:
    return bool(config.get("IPAM_URL").strip() and config.get("IPAM_TOKEN").strip())


def _call(path: str, body: dict) -> dict:
    url = config.get("IPAM_URL").strip().rstrip("/") + path
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(), method="POST",
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer " + config.get("IPAM_TOKEN").strip()},
    )
    try:
        with urllib.request.urlopen(req, context=_CTX, timeout=60) as r:
            out = json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read() or b"{}").get("error", "")
        except ValueError:
            detail = ""
        raise IpamError(f"IPAM {path}: HTTP {e.code} {detail}".strip())
    except (urllib.error.URLError, OSError) as e:
        raise IpamError(f"IPAM unreachable at {url}: {e}")
    if not out.get("success"):
        raise IpamError(out.get("error") or f"IPAM {path} refused the request")
    return out


def provision(name: str, network: str | None = None) -> dict:
    """Returns {'ip', 'prefixlen', 'gateway', 'dns': [...], 'domain', 'push'}.
    `network` may be a CIDR or an IPAM network name; defaults to IPAM_NETWORK."""
    network = (network or config.get("IPAM_NETWORK")).strip()
    if not network:
        raise IpamError("IPAM_NETWORK is not configured (and no network was given)")
    out = _call("/api/provision", {"name": name, "network": network,
                                   "source": "vc-deployer", "ext_id": name,
                                   "description": "deployed by VC-Deployer"})
    return {"ip": out["address"], "prefixlen": out.get("prefixlen"),
            "gateway": out.get("gateway") or "", "dns": out.get("dns") or [],
            "domain": out.get("domain") or "", "push": out.get("push")}


def deprovision(name: str) -> None:
    _call("/api/deprovision", {"name": name})


def deprovision_quiet(name: str) -> None:
    """Rollback path — a failed rollback must never mask the deploy error."""
    try:
        deprovision(name)
    except IpamError:
        pass


def register_vm(name: str, address: str) -> None:
    """After a successful deploy: record the VM and attach its address, so the
    IPAM inventory shows the machine immediately (the nightly vCenter import
    later enriches it with the MoRef and sizing). Best-effort."""
    try:
        out = _call("/api/vms?upsert=1",
                    {"name": name, "platform": "vcenter", "status": "active",
                     "source": "vc-deployer", "ext_id": name,
                     "description": "deployed by VC-Deployer"})
        vm_id = out.get("id") or (out.get("vm") or {}).get("id")
        if vm_id:
            _call_addr_link(address, vm_id)
    except IpamError:
        pass


def _call_addr_link(address: str, vm_id: int) -> None:
    url = (config.get("IPAM_URL").strip().rstrip("/")
           + "/api/addresses/lookup?address=" + address)
    req = urllib.request.Request(
        url, headers={"Authorization": "Bearer " + config.get("IPAM_TOKEN").strip()})
    with urllib.request.urlopen(req, context=_CTX, timeout=30) as r:
        rec = (json.loads(r.read() or b"{}") or {}).get("record")
    if rec:
        _call(f"/api/addresses/{rec['id']}",
              {"assigned_kind": "vm", "assigned_id": vm_id})
