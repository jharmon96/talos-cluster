#!/usr/bin/env python3
"""
generate_talos_configs.py  –  Talos 1.10 static-IP generator
------------------------------------------------------------

* Generates base configs with the **first control plane node IP as the initial Kubernetes endpoint**.
* After bootstrap is complete, the configs should be regenerated with VIP as the endpoint.
* Patches each node with a static address, gateway, nameserver.
* Adds the same `vip.ip` block to **every** control-plane node (per Talos docs).
* Deep-copy guarantees no cross-node bleed-over.
* Each YAML is validated with `talosctl validate --mode=metal`.
* Creates an insecure talosconfig to help with bootstrapping.
"""

from __future__ import annotations

import argparse
import copy
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Iterable, List

import yaml

IFNAME_DEFAULT = "ens18"
DISK_DEFAULT = "/dev/vda"


# --------------------------------------------------------------------------- #
# helpers                                                                     #
# --------------------------------------------------------------------------- #
def sh(cmd: List[str], **kw):
    """Run a shell command; exit if it returns non-zero."""
    try:
        subprocess.run(cmd, check=True, **kw)
    except subprocess.CalledProcessError as exc:
        print(f"✗ Command failed: {' '.join(cmd)}", file=sys.stderr)
        sys.exit(exc.returncode)


def yaml_load(p: Path) -> dict:
    with p.open() as f:
        return yaml.safe_load(f)


def yaml_dump(data: dict, p: Path):
    with p.open("w") as f:
        yaml.safe_dump(data, f, sort_keys=False)


# --------------------------------------------------------------------------- #
# talos-specific bits                                                         #
# --------------------------------------------------------------------------- #
def gen_base_configs(a, out: Path, bootstrap_phase: bool = True):
    """
    Call `talosctl gen config …` writing results to *out*.
    During initial bootstrap, we use the first CP node's IP as the endpoint,
    otherwise we use the VIP for HA.
    """
    # --- build endpoint & SAN list -----------------------------------------
    if bootstrap_phase and a.vip:
        # During bootstrap, use the first CP node's IP as endpoint to avoid chicken-egg problem
        # (VIP won't exist until after cluster is bootstrapped)
        endpoint_url = f"https://{a.control_planes[0]}:6443"
    else:
        # After bootstrap, we can use the VIP as the endpoint for HA
        endpoint_url = f"https://{a.vip}:6443" if a.vip else f"https://{a.endpoint}"

    sans: list[str] = []
    if a.vip:
        sans.append(a.vip)
    sans.extend(a.control_planes)
    sans = list(dict.fromkeys(sans))  # de-dupe, keep order

    cmd = [
        "talosctl",
        "gen",
        "config",
        a.cluster_name,
        endpoint_url,
        "--output-dir",
        str(out),
        "--talos-version",
        a.talos_version,
        "--with-cluster-discovery",  # Enable cluster discovery for better node joining
        "--with-kubespan",           # Enable KubeSpan for secure cross-node communication
        "--with-secrets",            # Generate secrets
        "--force",
    ]
    for san in sans:
        cmd += ["--additional-sans", san]

    sh(cmd, stdout=subprocess.DEVNULL)

    # Modify talosconfig to make it insecure for easier bootstrapping
    talosconfig_path = out / "talosconfig"
    if talosconfig_path.exists():
        config = yaml_load(talosconfig_path)
        # Set insecure: true for handling x509 certificate issues during bootstrap
        for context_name in config.get("contexts", {}):
            config["contexts"][context_name]["insecure"] = True
        yaml_dump(config, talosconfig_path)
        # Create an explicitly named insecure copy for reference
        yaml_dump(config, out / "talosconfig.insecure")


def patch_node(
    base: dict,
    *,
    ip: str,
    cidr: str,
    gw: str,
    ns: str,
    ifname: str,
    disk: str,
    vip: str | None = None,
    enable_ccm: bool = False,
) -> dict:
    """Return a deep-copied, per-node-patched machine config."""
    cfg = copy.deepcopy(base)

    # ---------------- install target ----------------
    install_config = cfg.setdefault("machine", {}).setdefault("install", {})
    install_config["disk"] = disk
    
    # Add QEMU guest agent extension for Proxmox integration
    install_config["extensions"] = [
        {"image": "ghcr.io/siderolabs/qemu-guest-agent:9.2.0"}
    ]
    
    # Add kernel args for predictable network interface naming
    install_config["extraKernelArgs"] = ["net.ifnames=0"]

    # ---------------- network interface -------------
    mnet = cfg["machine"].setdefault("network", {})
    iface = {
        "interface": ifname,
        "dhcp": False,
        "addresses": [f"{ip}/{cidr}"],
        "routes": [{"network": "0.0.0.0/0", "gateway": gw}],
    }
    if vip:
        iface["vip"] = {"ip": vip}
    mnet["interfaces"] = [iface]
    mnet["nameservers"] = [ns]
    
    # Configure kubelet for proper integration
    kubelet = cfg["machine"].setdefault("kubelet", {})
    kubelet.setdefault("extraArgs", {})
    
    # Configure certificate rotation for enhanced security
    kubelet["extraArgs"]["rotate-server-certificates"] = "true"
    
    # Enable cloud provider if requested
    if enable_ccm:
        kubelet["extraArgs"]["cloud-provider"] = "external"
        
        # Enable features for Talos API access (needed for CCM)
        features = cfg["machine"].setdefault("features", {})
        features["kubernetesTalosAPIAccess"] = {
            "enabled": True,
            "allowedRoles": ["os:reader"],
            "allowedKubernetesNamespaces": ["kube-system"]
        }
        
        # For control planes, enable external cloud provider
        if cfg.get("cluster", {}).get("controlPlane", {}):
            cluster = cfg.setdefault("cluster", {})
            cluster["externalCloudProvider"] = {
                "enabled": True,
                "manifests": [
                    "https://raw.githubusercontent.com/siderolabs/talos-cloud-controller-manager/main/docs/deploy/cloud-controller-manager.yml",
                    "https://raw.githubusercontent.com/alex1989hu/kubelet-serving-cert-approver/main/deploy/standalone-install.yaml"
                ]
            }

    return cfg


def validate_files(paths: Iterable[Path]):
    """Schema-validate each machine-config file."""
    for p in paths:
        sh(
            ["talosctl", "validate", "--mode=metal", "--config", str(p)],
            stdout=subprocess.PIPE,
        )


def create_node_configs(a, out: Path):
    cp_tpl = yaml_load(out / "controlplane.yaml")
    wk_tpl = yaml_load(out / "worker.yaml")

    generated: list[Path] = []

    # -------- control planes --------
    for i, ip in enumerate(a.control_planes, 1):
        out_file = out / f"cp{i}.yaml"
        yaml_dump(
            patch_node(
                cp_tpl,
                ip=ip,
                cidr=a.cidr,
                gw=a.gateway,
                ns=a.nameserver,
                ifname=a.ifname,
                disk=a.disk,
                vip=a.vip,         # <-- VIP on *every* CP node
                enable_ccm=getattr(a, "enable_ccm", False),
            ),
            out_file,
        )
        generated.append(out_file)

    # -------- workers ---------------
    for i, ip in enumerate(a.workers, 1):
        out_file = out / f"w{i}.yaml"
        yaml_dump(
            patch_node(
                wk_tpl,
                ip=ip,
                cidr=a.cidr,
                gw=a.gateway,
                ns=a.nameserver,
                ifname=a.ifname,
                disk=a.disk,
                enable_ccm=getattr(a, "enable_ccm", False),
            ),
            out_file,
        )
        generated.append(out_file)

    validate_files(generated)


# --------------------------------------------------------------------------- #
# CLI                                                                         #
# --------------------------------------------------------------------------- #
def main():
    import os
    
    # Set default values from environment variables or use defaults
    env_defaults = {
        "cluster_name": os.environ.get("TALOS_CONFIG_CLUSTER_NAME", "my-k8s-cluster"),
        "endpoint": os.environ.get("TALOS_CONFIG_ENDPOINT", None),
        "vip": os.environ.get("TALOS_CONFIG_NETWORK_VIP", None),
        "control_planes": os.environ.get("TALOS_CONFIG_CONTROL_PLANES", None),
        "workers": os.environ.get("TALOS_CONFIG_WORKERS", None),
        "gateway": os.environ.get("TALOS_CONFIG_NETWORK_GATEWAY", None),
        "nameserver": os.environ.get("TALOS_CONFIG_NETWORK_DNS", "1.1.1.1"),
        "talos_version": os.environ.get("TALOS_CONFIG_VERSION", "v1.10.2"),
        "cidr": os.environ.get("TALOS_CONFIG_NETWORK_CIDR", "24"),
        "ifname": os.environ.get("TALOS_CONFIG_NETWORK_INTERFACE", IFNAME_DEFAULT),
        "disk": os.environ.get("TALOS_CONFIG_VM_DISK", DISK_DEFAULT),
        "out": os.environ.get("TALOS_CONFIG_OUTPUT_DIR", "./output"),
        "bootstrap_phase": os.environ.get("TALOS_CONFIG_BOOTSTRAP_PHASE", "") != "",
        "ha_phase": os.environ.get("TALOS_CONFIG_HA_PHASE", "") != "",
        "enable_ccm": os.environ.get("TALOS_CONFIG_ENABLE_CCM", "") != "",
    }
    
    ap = argparse.ArgumentParser(description="Generate Talos 1.10 machine configs")
    ap.add_argument("--cluster-name", default=env_defaults["cluster_name"],
                    help=f"Cluster name (default: {env_defaults['cluster_name']})")
    ap.add_argument("--endpoint", default=env_defaults["endpoint"], 
                    help="bootstrap-CP ip:6443")
    ap.add_argument("--vip", default=env_defaults["vip"],
                    help="Layer-2 shared Kubernetes VIP (optional)")
    ap.add_argument("--control-planes", default=env_defaults["control_planes"],
                    help="Comma-separated list of control plane IPs")
    ap.add_argument("--workers", default=env_defaults["workers"],
                    help="Comma-separated list of worker IPs")
    ap.add_argument("--gateway", default=env_defaults["gateway"],
                    help=f"Network gateway IP")
    ap.add_argument("--nameserver", default=env_defaults["nameserver"],
                    help=f"DNS server IP (default: {env_defaults['nameserver']})")
    ap.add_argument("--talos-version", default=env_defaults["talos_version"],
                    help=f"Talos version (default: {env_defaults['talos_version']})")
    ap.add_argument("--cidr", default=env_defaults["cidr"],
                    help=f"Network CIDR (default: {env_defaults['cidr']})")
    ap.add_argument("--ifname", default=env_defaults["ifname"],
                    help=f"Network interface name (default: {env_defaults['ifname']})")
    ap.add_argument("--disk", default=env_defaults["disk"],
                    help=f"Installation disk (default: {env_defaults['disk']})")
    ap.add_argument("--out", default=env_defaults["out"],
                    help=f"Output directory (default: {env_defaults['out']})")
    ap.add_argument("--force", action="store_true",
                    help="Force overwrite existing output directory")
    ap.add_argument("--bootstrap-phase", action="store_true", default=env_defaults["bootstrap_phase"],
                    help="Generate configs for initial bootstrap (uses first CP node as endpoint)")
    ap.add_argument("--ha-phase", action="store_true", default=env_defaults["ha_phase"],
                    help="Generate configs for HA operation after bootstrap (uses VIP as endpoint)")
    ap.add_argument("--enable-ccm", action="store_true", default=env_defaults["enable_ccm"],
                    help="Enable Cloud Controller Manager for Talos (recommended for Proxmox)")
    a = ap.parse_args()
    
    # Validate required arguments
    required_args = ["endpoint", "control_planes", "workers", "gateway"]
    missing_args = [arg for arg in required_args if getattr(a, arg.replace("-", "_")) is None]
    
    if missing_args:
        ap.error(f"the following arguments are required: {', '.join('--' + arg for arg in missing_args)}")

    # ---------- output dir ----------
    out = Path(a.out).expanduser().resolve()
    if out.exists():
        if not a.force:
            print(f"✗ {out} exists (use --force)", file=sys.stderr)
            sys.exit(1)
        try:
            shutil.rmtree(out)
        except OSError as e:
            print(f"Warning: Could not remove {out}: {e}. Will attempt to continue anyway.")
    try:
        out.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        print(f"Warning: Could not create {out}: {e}. Will attempt to continue anyway.")

    # ---------- split CSV args ------
    a.control_planes = [s.strip() for s in a.control_planes.split(",") if s.strip()]
    a.workers = [s.strip() for s in a.workers.split(",") if s.strip()]

    # Default to bootstrap phase if neither is specified
    bootstrap_phase = a.bootstrap_phase or not a.ha_phase

    gen_base_configs(a, out, bootstrap_phase=bootstrap_phase)
    create_node_configs(a, out)

    # ---------- done ----------------
    print(f"✓ Talos configs written to {out}\n")
    cp_paths = " ".join(str(out / f"cp{i}.yaml") for i in range(1, len(a.control_planes) + 1))
    
    # Add CCM status to output
    ccm_status = "ENABLED" if getattr(a, "enable_ccm", False) else "DISABLED"
    
    if bootstrap_phase:
        print(
            "BOOTSTRAP PHASE: Initial cluster creation\n"
            "=======================================\n"
            f"Talos Cloud Controller Manager (CCM): {ccm_status}\n\n"
            "1. Configure talosctl to talk to node IPs (not the VIP)\n"
            f"   export TALOSCONFIG={out}/talosconfig.insecure\n"
            f"   talosctl config endpoint {' '.join(a.control_planes)}\n\n"
            "2. Push control-plane configs\n"
            + "\n".join(
                f"   talosctl apply-config --insecure --nodes {ip} --file {out / f'cp{idx}.yaml'}"
                for idx, ip in enumerate(a.control_planes, 1)
            )
            + "\n\n3. ⚠️ CRITICAL: Remove ISO and force disk-only boot with UEFI support (prevents x509 certificate issues)\n"
            f"   ./talos_vm_manager.sh remove_iso\n\n"
            "4. Bootstrap once (target first CP node's *real* IP)\n"
            f"   talosctl bootstrap --insecure --nodes {a.control_planes[0]}\n\n"
            "5. Wait 60-120 seconds for etcd to initialize\n\n"
            "6. Push worker configs\n"
            + "\n".join(
                f"   talosctl apply-config --insecure --nodes {ip} --file {out / f'w{idx}.yaml'}"
                for idx, ip in enumerate(a.workers, 1)
            )
            + "\n\n7. Verify cluster status\n"
            f"   talosctl health --insecure --nodes {a.control_planes[0]}\n"
            f"   talosctl kubeconfig --insecure --nodes {a.control_planes[0]} -e {a.control_planes[0]}\n\n"
            "8. After successful bootstrap, regenerate configs with --ha-phase\n"
            f"   python3 {sys.argv[0]} --ha-phase --enable-ccm [other args...]\n\n"
            "9. Apply the new configs to switch to VIP-based HA:\n"
            + "\n".join(
                f"   talosctl apply-config --nodes {ip} --file {out / f'cp{idx}.yaml'}"
                for idx, ip in enumerate(a.control_planes, 1)
            )
            + "\n"
            + "\n".join(
                f"   talosctl apply-config --nodes {ip} --file {out / f'w{idx}.yaml'}"
                for idx, ip in enumerate(a.workers, 1)
            )
        )
    else:
        print(
            "HA PHASE: Post-bootstrap configuration\n"
            "====================================\n"
            f"Talos Cloud Controller Manager (CCM): {ccm_status}\n\n"
            "Apply these configs to switch to VIP-based HA:\n"
            + "\n".join(
                f"talosctl apply-config --nodes {ip} --file {out / f'cp{idx}.yaml'}"
                for idx, ip in enumerate(a.control_planes, 1)
            )
            + "\n"
            + "\n".join(
                f"talosctl apply-config --nodes {ip} --file {out / f'w{idx}.yaml'}"
                for idx, ip in enumerate(a.workers, 1)
            )
        )


if __name__ == "__main__":
    main()