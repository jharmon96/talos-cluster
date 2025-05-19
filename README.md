# Talos Kubernetes Cluster
**Talos v1.10.2 · Kubernetes 1.33 · Cilium CNI · MetalLB L2**

> **Part 1 of 2** – High-level overview, repo layout, VM automation, prerequisites, and Talos config generation.  
> Part 2 covers manual VM specs, cluster bring-up commands, CNI/MetalLB installation, Git workflow, handy Talos commands, and references.

---

## Table of Contents (Part 1)

1. [Project scope](#1--project-scope)  
2. [Topology](#2--topology)  
3. [Directory layout](#3--directory-layout)  
4. [`talos_vm_manager.sh` (VM automation)](#4--talos_vm_managersh-vm-automation)  
   * 4.1 Quick start  
   * 4.2 Command matrix  
   * 4.3 Requirements  
5. [Generate Talos node configs](#5--generate-talos-node-configs)  

*(Part 2 adds sections 6-10 – manual VM specs, Talos bring-up, networking, Git workflow, handy commands, references)*

---

## 1 · Project scope
Everything you need—**Python, Bash, YAML templates**—to bootstrap a **3-node HA control-plane + 3 worker** Talos cluster on a Proxmox VE hyper-converged lab in minutes.

---

## 2 · Topology

| Role | Hostname | Mgmt IP | VIP | VLAN | Notes |
|------|----------|---------|-----|------|-------|
| CP1 | `talos-cp1` | **172.22.40.11** | rowspan=3 **172.22.40.10** | **40** | VIP floats |
| CP2 | `talos-cp2` | 172.22.40.12 | | 40 | |
| CP3 | `talos-cp3` | 172.22.40.13 | | 40 | |
| W1-3 | `talos-w{1–3}` | 172.22.40.21-23 | | 40 | workers |

* **Gateway** `172.22.40.1` • **Nameserver** `172.22.10.1` • **Subnet** `/24`

---

## 3 · Directory layout

```text
talos-cluster/
├── talos_vm_manager.sh         # 1-shot gold-standard VM helper (§4)
├── generate_talos_configs.py   # Python utility to regenerate node YAML
├── controlplane.yaml           # pristine base template
├── worker.yaml                 # pristine base template
├── output/
│   ├── cp1.yaml … cp3.yaml
│   ├── w1.yaml … w3.yaml
│   └── talosconfig             # cluster secrets (git-ignored)
└── .gitignore
```

---

## 4 · `talos_vm_manager.sh` (VM automation)

A single Bash script that **creates, replaces, deletes, or audits** the six Proxmox VMs.

| Command | Action | Wait (boot) | Notes |
|---------|--------|-------------|-------|
| `create`  | Build **missing** VMs, leave existing ones intact, enforce boot order everywhere | **✓ 90 s** | Day-to-day “heal” mode |
| `replace` | Delete & rebuild **all six** VMs from scratch | **✓ 90 s** | Fresh disks |
| `delete`  | Stop & purge all VMs (disks, snapshots) | ✗ | Lab teardown |
| `remove_iso` | Completely remove ISO and set boot order to disk-only, then restart VMs | **✓ 30 s** | Critical for bootstrap |
| `network` | Print **inventory table** (VMID ▸ NAME ▸ MAC ▸ IPv4) **plus** list of *all Talos IPs found* on subnet | ✗ | Audit / troubleshooting |

### Environment Variables

The script supports configuration via environment variables:

| Environment Variable | Description | Default |
|----------------------|-------------|---------|
| `TALOS_CONFIG_PROXMOX_ISO` | Talos ISO storage location | *Required* |
| `TALOS_CONFIG_PROXMOX_STORAGE` | Storage pool name | *Required* |
| `TALOS_CONFIG_PROXMOX_EFI_STORAGE` | EFI disk storage | Same as main storage |
| `TALOS_CONFIG_NETWORK_BRIDGE` | Network bridge device | `vmbr0` |
| `TALOS_CONFIG_NETWORK_VLAN` | VLAN tag number | `40` |
| `TALOS_CONFIG_NETWORK_SUBNET` | Network subnet CIDR | `172.22.40.0/24` |
| `TALOS_CONFIG_VM_MEMORY` | VM memory in MB | `4096` |
| `TALOS_CONFIG_VM_CORES` | VM CPU cores | `2` |
| `TALOS_CONFIG_VM_SOCKETS` | VM CPU sockets | `1` |
| `TALOS_CONFIG_VM_CPU_TYPE` | VM CPU type | `host` |
| `TALOS_CONFIG_VM_SCSI_HW` | VM SCSI hardware | `virtio-scsi-single` |

If the required variables are not set, the script will prompt for input when run interactively.

> Boot-order enforcement uses safe quoting (`order=ide2;scsi0`) so semicolons never break remote shells.

### 4.1 Quick start

```bash
chmod +x talos_vm_manager.sh

# Build missing nodes only
./talos_vm_manager.sh create

# Wipe & rebuild everything (fresh disks)
./talos_vm_manager.sh replace

# Remove entire cluster
./talos_vm_manager.sh delete

# Zero-touch inventory (runs instantly)
./talos_vm_manager.sh network
```

### 4.2 Command output

`network` (or after `create/replace`) prints two tables:

```
📜  Node inventory:
VMID  NAME       MAC                IPv4
----  ---------- -----------------  -----------
7101  talos-cp1  bc:24:11:f6:1d:2b  172.22.40.49
…     …          …                  …

🌐  All Talos IPv4 addresses found on 172.22.40.0/24:
  • 172.22.40.49
  • 172.22.40.50
  • …
```

### 4.3 Requirements (control host)

* SSH key access to every Proxmox node as **root**  
* `nmap` ≥ 7.90  
* `talosctl` ≥ 1.10  
* Bash 4+ (macOS or Linux)  
* If you run as non-root, `sudo` must be password-less for `nmap`.

All Proxmox nodes need **no extra packages**—only the `qm` CLI that ships with PVE.

---

## 5 · Generate Talos node configs

Run once whenever you change IPs, Talos version, or cluster name.

```bash
python3 generate_talos_configs.py \
  --cluster-name my-k8s-cluster \
  --endpoint 172.22.40.11:6443 \
  --vip 172.22.40.10 \
  --control-planes 172.22.40.11,172.22.40.12,172.22.40.13 \
  --workers 172.22.40.21,172.22.40.22,172.22.40.23 \
  --gateway 172.22.40.1 \
  --nameserver 172.22.10.1 \
  --talos-version v1.10.2 \
  --out ./output \
  --force
```

Generated YAMLs land in `output/`.  
`talosconfig` (cluster secrets) is git-ignored by default.

### Environment Variables

The script also supports configuration via environment variables:

| Environment Variable | Description | Default |
|----------------------|-------------|---------|
| `TALOS_CONFIG_CLUSTER_NAME` | Kubernetes cluster name | `my-k8s-cluster` |
| `TALOS_CONFIG_ENDPOINT` | Initial Kubernetes API endpoint | *Required* |
| `TALOS_CONFIG_NETWORK_VIP` | Shared virtual IP for HA | Optional |
| `TALOS_CONFIG_CONTROL_PLANES` | Comma-separated control plane IPs | *Required* |
| `TALOS_CONFIG_WORKERS` | Comma-separated worker node IPs | *Required* |
| `TALOS_CONFIG_NETWORK_GATEWAY` | Network gateway IP | *Required* |
| `TALOS_CONFIG_NETWORK_DNS` | DNS server IP | `1.1.1.1` |
| `TALOS_CONFIG_VERSION` | Talos version | `v1.10.2` |
| `TALOS_CONFIG_NETWORK_CIDR` | Network CIDR prefix | `24` |
| `TALOS_CONFIG_NETWORK_INTERFACE` | Network interface name | `ens18` |
| `TALOS_CONFIG_VM_DISK` | Installation target disk | `/dev/vda` |
| `TALOS_CONFIG_OUTPUT_DIR` | Output directory | `./output` |
| `TALOS_CONFIG_BOOTSTRAP_PHASE` | Enable bootstrap mode | `false` |
| `TALOS_CONFIG_HA_PHASE` | Enable HA mode | `false` |
| `TALOS_CONFIG_ENABLE_CCM` | Enable cloud controller manager | `false` |

When environment variables are set, they provide defaults but can still be overridden by command-line arguments.

---

> **Continue to Part 2** for manual VM specs, Talos bring-up, CNI/MetalLB installs, Git workflow, handy commands, and references.

# Talos Kubernetes Cluster
**Talos v1.10.2 · Kubernetes 1.33 · Cilium CNI · MetalLB L2**

> **Part 2 of 2** – Manual VM specs, Talos bring-up, CNI/MetalLB install, Git workflow, handy commands, references, and license.  
> *(Part 1 covers project scope, repo layout, the `talos_vm_manager.sh` helper, and config generation.)*

---

## Table of Contents (Part 2)

6. [Manual VM specs (optional)](#6--manual-vm-specs-optional)  
7. [Talos cluster bring-up](#7--talos-cluster-bring-up)  
8. [CNI & NetworkPolicy](#8--cni--networkpolicy)  
9. [MetalLB installation](#9--metallb-installation)  
10. [Git workflow](#10--git-workflow)  
11. [Handy Talos commands](#11--handy-talos-commands)  
12. [References](#12--references)  
13. [Troubleshooting](#13--troubleshooting)  
14. [License](#14--license)  

---

## 6 · Manual VM specs (optional)

Skip this section if you use **`talos_vm_manager.sh replace`**—the script builds the VMs for you.  
For a manual GUI build, match these specs:

| Setting            | Control-plane | Worker |
|--------------------|--------------|--------|
| vCPU / RAM         | 2 vCPU / 4 GiB | same |
| Disk               | 30 GiB qcow2 | 20 GiB qcow2 |
| NIC                | VirtIO on `vmbr0`, **VLAN 40 tag** |
| Boot ISO           | `talos-v1.10.2-installer.iso` |
| Boot order         | Disk first, ISO second |

Create six VMs: **`talos-cp1…3`** and **`talos-w1…3`**.

---

## 7 · Talos cluster bring-up

```bash
# ❶ Point TALOSCONFIG to the generated secrets
export TALOSCONFIG=$PWD/output/talosconfig

# ❷ Push control-plane configs
talosctl apply-config --insecure --nodes 172.22.40.11 --file output/cp1.yaml
talosctl apply-config --insecure --nodes 172.22.40.12 --file output/cp2.yaml
talosctl apply-config --insecure --nodes 172.22.40.13 --file output/cp3.yaml

# ❸ Remove ISOs and set boot to disk-only (CRITICAL step)
./talos_vm_manager.sh remove_iso

# ❹ Bootstrap etcd (run once)
talosctl bootstrap --nodes 172.22.40.11

# ❺ Push worker configs
talosctl apply-config --insecure --nodes 172.22.40.21 --file output/w1.yaml
talosctl apply-config --insecure --nodes 172.22.40.22 --file output/w2.yaml
talosctl apply-config --insecure --nodes 172.22.40.23 --file output/w3.yaml
```

Validate the cluster:

```bash
talosctl config endpoint 172.22.40.11 172.22.40.12 172.22.40.13
talosctl health
talosctl kubeconfig .
export KUBECONFIG=$PWD/kubeconfig
kubectl get nodes
```

---

## 8 · CNI & NetworkPolicy

* **Cilium v1.15** is pre-installed by Talos; Kubernetes `NetworkPolicy` objects work out-of-the-box.  
* Existing Calico policies are API-compatible—Cilium honours them.

> **Want Calico instead?**  
> 1. Add `cluster.network.cni.name: none` to every node YAML.  
> 2. Re-apply configs or regenerate.  
> 3. Install Calico via the Tigera operator manifests.

---

## 9 · MetalLB installation

```bash
# Deploy operator & CRDs
kubectl apply -f https://raw.githubusercontent.com/metallb/metallb/v0.14.5/config/manifests/metallb-native.yaml

# Create IPv4 pool 172.22.40.50-99
cat <<EOF | kubectl apply -f -
apiVersion: metallb.io/v1beta1
kind: IPAddressPool
metadata:
  name: vlan40-pool
  namespace: metallb-system
spec:
  addresses:
  - 172.22.40.50-172.22.40.99
---
apiVersion: metallb.io/v1beta1
kind: L2Advertisement
metadata:
  name: vlan40
  namespace: metallb-system
spec:
  ipAddressPools: [vlan40-pool]
EOF
```

`Service/LoadBalancer` objects now receive IPs from `.50–.99`.

---

## 10 · Git workflow

```bash
git clone git@github.com:yourusername/talos-cluster.git
cd talos-cluster

# edit generate_talos_configs.py args → regenerate → commit
python3 generate_talos_configs.py … --force
git add output/*.yaml
git commit -m "regen configs for v1.10.3 maintenance window"
git push
```

> `output/talosconfig` stays out of Git—keep it private.

---

## 11 · Handy Talos commands

```bash
talosctl dmesg -n 172.22.40.11                  # kernel ring buffer
talosctl logs -n 172.22.40.11 kubelet           # unit logs
talosctl upgrade --image ghcr.io/siderolabs/installer:v1.10.3
talosctl containers --all -n 172.22.40.12       # list CRI containers
talosctl etcd snapshot save etcd.backup.zip -n 172.22.40.11
```

---

## 12 · References

* [Talos – Network configuration](https://www.talos.dev/v1.10/network/)  
* [Cilium – NetworkPolicy compatibility](https://docs.cilium.io/en/stable/network/network_policy/)  
* [MetalLB – Layer-2 mode](https://metallb.universe.tf/configuration/#layer-2-configuration)

---

## 13 · Troubleshooting

### Certificate issues

If you encounter x509 certificate errors during bootstrap:

```bash
error executing bootstrap: rpc error: code = Unavailable desc = connection error: desc = "transport: authentication handshake failed: tls: failed to verify certificate: x509: certificate signed by unknown authority"
```

The most common cause of certificate issues is **failing to remove the ISO before bootstrapping**. After applying configs but before bootstrapping, you MUST ensure all VMs boot from disk only:

```bash
# Critical step - removes ISO and sets boot order to disk-only
./talos_vm_manager.sh remove_iso
```

Skipping this step causes nodes to boot from the ISO instead of disk, losing your applied configurations and generating new certificates on each boot.

If you've already confirmed the ISO is removed and still see certificate issues, try these approaches:

1. Create an insecure configuration that skips certificate verification:

```bash
cat > talosconfig.insecure << EOF
context: my-k8s-cluster
contexts:
    my-k8s-cluster:
        endpoints:
            - <control-plane-ip-1>
            - <control-plane-ip-2>
            - <control-plane-ip-3>
        ca: $(cat output/talosconfig | grep ca: | awk '{print $2}')
        crt: $(cat output/talosconfig | grep crt: | awk '{print $2}')
        key: $(cat output/talosconfig | grep key: | awk '{print $2}')
        insecure: true
EOF

export TALOSCONFIG="$PWD/talosconfig.insecure"
```

2. Ensure all control plane nodes are using the correct boot sequence:
   - **Remove ISO completely** after applying configurations (use `./talos_vm_manager.sh remove_iso`)
   - Even if disk is set as first boot device, any presence of the ISO can cause issues
   - ISO booting will not use your applied configurations and will regenerate certificates on each boot

3. Verify the nodes have properly applied configurations:

```bash
talosctl version --nodes <control-plane-ip>
```

A response showing "maintenance mode" indicates the node is ready for bootstrapping.

### VIP Configuration

For control plane HA using a shared VIP:

- Ensure all control plane nodes have the same VIP configuration in network interfaces section
- The VIP must be on the same L2 subnet as the control plane nodes
- Never use the VIP as a Talos API endpoint (use real node IPs instead)
- The VIP will only become active after successful bootstrap

### Cluster initialization best practices

- Always wait 60-120 seconds after applying configs before bootstrapping
- **CRITICAL**: Run `./talos_vm_manager.sh remove_iso` after applying configs but before bootstrapping
- Verify boot order is disk-only with no ISO attached before proceeding to bootstrap
- If nodes are stuck in maintenance mode, try reapplying configurations
- For persistent issues, consider wiping and recreating VMs with `./talos_vm_manager.sh replace`
- When regenerating configs after VM recreation, use the new IPs reported by `./talos_vm_manager.sh network`

---

## 14 · License

© 2025 — MIT License. See `LICENSE` for details.
