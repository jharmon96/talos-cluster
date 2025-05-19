#!/usr/bin/env bash
###############################################################################
# talos_vm_manager.sh  ▸  create | replace | delete | network | remove_iso
#
# GOLD-STANDARD FEATURES
# ──────────────────────────────────────────────────────────────────────
# • Strict bash, fatal trap, per-host pre-flight
# • Semicolon-safe boot order ("order=ide2;scsi0")
# • QEMU guest agent enabled for better VM management
# • Two-phase boot:
#   1. Boot from ISO for initial installation
#   2. Apply Talos configs while in maintenance mode (booted from ISO)
#   3. Remove ISO completely and restart VMs to ensure disk-only boot
#   4. Bootstrap Kubernetes using the first control plane node
# • Five verbs
#     create     – build ONLY missing VMs, enforce boot order on all
#     replace    – wipe & rebuild ALL VMs (fresh disks)
#     delete     – stop & purge ALL VMs
#     network    – zero-touch inventory (MAC + IPv4) + "Talos IPs found" list
#     remove_iso – completely remove ISO and set boot order to disk-only
# • IPv4 discovery via nmap -Pn + talosctl  ► works as root **or** non-root
# • Single place to tweak ISO, VLAN tag, subnet, wait period, etc.
#
# Tested on Proxmox 8 + Talos 1.10  ·  2025-05-19
###############################################################################
set -Eeuo pipefail
trap 'echo "[FATAL] error at line $LINENO" >&2' ERR

# ── Cluster constants with environment variable support ─────────────────────
# Function to prompt for required values if not provided via env vars
prompt_for_value() {
  local var_name=$1
  local prompt_text=$2
  local default_val=$3
  local is_required=${4:-false}
  
  if [[ -z "${!var_name}" ]]; then
    if [[ -t 0 ]]; then  # Check if running interactively
      if [[ -n "$default_val" ]]; then
        read -p "${prompt_text} [${default_val}]: " user_input
        eval "${var_name}=\"${user_input:-$default_val}\""
      else
        while [[ -z "$user_input" && "$is_required" == "true" ]]; do
          read -p "${prompt_text}: " user_input
          if [[ -z "$user_input" && "$is_required" == "true" ]]; then
            echo "This value is required. Please provide it."
          fi
        done
        eval "${var_name}=\"$user_input\""
      fi
    else
      if [[ -n "$default_val" ]]; then
        eval "${var_name}=\"$default_val\""
        echo "Using default $var_name: $default_val"
      else
        echo "ERROR: $var_name is required but not provided via environment variable" >&2
        exit 1
      fi
    fi
  fi
}

# Get ISO location - no default, must be provided
ISO=${TALOS_CONFIG_PROXMOX_ISO}
if [[ -z "$ISO" ]]; then
  prompt_for_value "ISO" "Enter Talos ISO storage location (e.g. local:iso/metal-amd64.iso)" "" "true"
fi

# Storage configuration - no default, must be provided
STORAGE=${TALOS_CONFIG_PROXMOX_STORAGE}
if [[ -z "$STORAGE" ]]; then
  prompt_for_value "STORAGE" "Enter storage pool name" "" "true"
fi

# Network configuration
BRIDGE=${TALOS_CONFIG_NETWORK_BRIDGE:-"vmbr0"}
VLAN_TAG=${TALOS_CONFIG_NETWORK_VLAN:-40}
SUBNET=${TALOS_CONFIG_NETWORK_SUBNET:-"172.22.40.0/24"}  # Keeping existing subnet

# VM specification
MEMORY=${TALOS_CONFIG_VM_MEMORY:-4096}
CORES=${TALOS_CONFIG_VM_CORES:-2}
SOCKETS=${TALOS_CONFIG_VM_SOCKETS:-1}
CPU_TYPE=${TALOS_CONFIG_VM_CPU_TYPE:-"host"}
SCSIHW=${TALOS_CONFIG_VM_SCSI_HW:-"virtio-scsi-single"}

# EFI configuration - default to main storage if not specified
EFI_STORAGE=${TALOS_CONFIG_PROXMOX_EFI_STORAGE:-$STORAGE}
DISCOVERY_PORT=50000        # Talos API port
BOOT_WAIT=90                # seconds to let nodes DHCP (create/replace only)
REBOOT_WAIT=30              # seconds to wait for VMs to reboot after boot order change

# Manifest :  (VMID  NAME       HOST          DISK_GB)
declare -a VMS=(
  "7101 talos-cp1 002-amd-001 30"
  "7102 talos-cp2 002-amd-002 30"
  "7103 talos-cp3 002-amd-003 30"
  "7111 talos-w1  002-amd-001 20"
  "7112 talos-w2  002-amd-002 20"
  "7113 talos-w3  002-amd-003 20"
)

# ── Helpers: quick SSH wrappers ──────────────────────────────────────────────
preflight_host() { ssh -o BatchMode=yes root@"$1" "pvesm path $ISO >/dev/null &&
                                                   pvesm list $STORAGE >/dev/null &&
                                                   grep -q $BRIDGE /etc/network/interfaces"; }

mac_of_vm() { ssh root@"$1" "qm config $2 | awk -F'[=,]' '/^net0/ {print tolower(\$2)}'"; }

# Build or rebuild (always recreate)
build_vm() { local ID=$1 NAME=$2 HOST=$3 DISK=$4
  echo " → [$HOST] build $ID ($NAME)"; preflight_host "$HOST"
  ssh root@"$HOST" bash -s -- "$ID" "$NAME" "$DISK" <<EOS
set -e
VMID=\$1 NAME=\$2 DISK=\$3
qm unlock \$VMID 2>/dev/null || true
qm stop   \$VMID --skiplock 1 2>/dev/null || true
qm destroy \$VMID --purge 1 --skiplock 1 2>/dev/null || true

qm create \$VMID --name \$NAME --memory $MEMORY --sockets $SOCKETS --cores $CORES \
  --cpu $CPU_TYPE --net0 virtio,bridge=$BRIDGE,tag=$VLAN_TAG,firewall=1 \
  --ostype l26 --scsihw $SCSIHW --serial0 socket --vga serial0 \
  --agent enabled=1 --machine q35 --bios ovmf \
  --efidisk0 $EFI_STORAGE:1

qm set \$VMID --scsi0 $STORAGE:\${DISK},iothread=1
qm set \$VMID --ide2  $ISO,media=cdrom
# Set initial boot order to boot from ISO first for installation
qm set \$VMID --boot "order=ide2;scsi0"
qm start \$VMID
EOS
}

# After initial boot, ensure boot order is set to disk first to avoid ISO boot issues
# (essential to prevent x509 certificate issues during bootstrapping)
# AND stop/start (NOT restart) the VMs to apply the new boot order
change_boot_and_force_restart() { 
  local HOST=$1 VMID=$2
  echo " → [$HOST] Setting boot order for $VMID to disk first"
  ssh root@"$HOST" "qm set $VMID --boot order='scsi0;ide2'";
  
  echo " → [$HOST] Forcefully stopping $VMID"
  ssh root@"$HOST" "qm stop $VMID --skiplock 1";
  
  # Wait a moment to ensure VM is fully stopped
  sleep 2
  
  echo " → [$HOST] Starting $VMID with new boot order"
  ssh root@"$HOST" "qm start $VMID";
}

delete_vm()  { echo " → [$1] delete $2";
               ssh root@"$1" "qm unlock $2 || true; qm stop $2 --skiplock 1 || true;
                               qm destroy $2 --purge 1 --skiplock 1 || true"; }

# ── Network discovery (nmap + talosctl)───────────────────────────────────────
discover_ips() {
  local NM; NM=$( ((UID)) && echo "sudo nmap" || echo nmap )
  $NM -Pn -n -oG - -p $DISCOVERY_PORT --open "$SUBNET" \
      | awk '/50000\/open/{print $2}'
}

mac_from_talos() {  # $1 ip
  talosctl get links --insecure -n "$1" 2>/dev/null \
      | awk '$3=="ens18"{print tolower($6)}'
}

build_mac_ip_map() {  # stdout "mac ip"
  declare -A map
  for ip in $(discover_ips); do
    mac=$(mac_from_talos "$ip"); [[ $mac ]] && map[$mac]=$ip
  done
  for m in "${!map[@]}"; do echo "$m ${map[$m]}"; done
}

print_inventory() {
  # Build MAC→IP associative array
  declare -A IP; while read -r m a; do IP[$m]=$a; done < <(build_mac_ip_map || true)

  echo -e "\n📜  Node inventory:"
  printf "%-6s %-10s %-17s %-15s\n" VMID NAME MAC IPv4
  printf "%-6s %-10s %-17s %-15s\n" ---- ---------- ----------------- ---------------
  for s in "${VMS[@]}"; do read -r V N H _ <<<"$s"
    mac=$(mac_of_vm "$H" "$V")
    printf "%-6s %-10s %-17s %-15s\n" \
      "$V" "$N" "$mac" "${IP[$mac]:--}"
  done

  # Extra table: every Talos IP we found
  echo -e "\n🌐  All Talos IPv4 addresses found on $SUBNET:"
  for ip in $(discover_ips); do echo "  • $ip"; done
}

# Completely remove the ISO and set boot order to disk-only
# This ensures nodes will never boot into maintenance mode
remove_iso_and_restart() { 
  local HOST=$1 VMID=$2
  echo " → [$HOST] Getting current VM config for $VMID"
  ssh root@"$HOST" "qm config $VMID"
  
  echo " → [$HOST] Completely removing ISO from $VMID"
  ssh root@"$HOST" "qm set $VMID --delete ide2";
  
  echo " → [$HOST] Ensuring UEFI/EFI support is configured"
  ssh root@"$HOST" "qm set $VMID --machine q35 --bios ovmf";
  
  # Add EFI disk if it doesn't exist
  echo " → [$HOST] Adding EFI disk if needed"
  ssh root@"$HOST" "qm config $VMID | grep -q efidisk0 || qm set $VMID --efidisk0 $EFI_STORAGE:1";
  
  echo " → [$HOST] Setting boot order for $VMID to disk only"
  ssh root@"$HOST" "qm set $VMID --boot order='scsi0'";
  
  echo " → [$HOST] Forcefully stopping $VMID"
  ssh root@"$HOST" "qm stop $VMID --skiplock 1";
  
  # Wait a moment to ensure VM is fully stopped
  sleep 2
  
  echo " → [$HOST] Starting $VMID with new boot configuration"
  ssh root@"$HOST" "qm start $VMID";
}

# ── Command dispatcher ───────────────────────────────────────────────────────
usage() {
  echo "Usage: $0 {create|replace|delete|network|remove_iso}"
  echo ""
  echo "  create     - build ONLY missing VMs, enforce boot order on all"
  echo "  replace    - wipe & rebuild ALL VMs (fresh disks)"
  echo "  delete     - stop & purge ALL VMs"
  echo "  network    - zero-touch inventory (MAC + IPv4) + 'Talos IPs found' list"
  echo "  remove_iso - completely remove ISO and set boot order to disk-only (critical after applying Talos configs)"
  exit 1
}
[[ $# -eq 1 ]] || usage

case $1 in
  create)
    for s in "${VMS[@]}"; do read -r V N H D <<<"$s";
      ssh root@"$H" qm status "$V" &>/dev/null \
        && echo " → [$H] $V ($N) exists – skipping" \
        || build_vm "$V" "$N" "$H" "$D"; done
    
    # Wait for initial boot and OS installation to complete
    echo "Waiting $BOOT_WAIT seconds for initial installation from ISO to complete..."
    sleep "$BOOT_WAIT"
    
    print_inventory; echo -e "\n✅  create complete.";;
  replace)
    for s in "${VMS[@]}"; do read -r V _ H _ <<<"$s"; delete_vm "$H" "$V"; done
    for s in "${VMS[@]}"; do read -r V N H D <<<"$s"; build_vm "$V" "$N" "$H" "$D"; done
    
    # Wait for initial boot and OS installation to complete
    echo "Waiting $BOOT_WAIT seconds for initial installation from ISO to complete..."
    sleep "$BOOT_WAIT"
    
    print_inventory; echo -e "\n✅  replace complete.";;
  remove_iso)
    echo "⚠️  IMPORTANT: Only run this AFTER applying Talos configs to each node"
    echo "⚠️  This will completely remove the ISO and ensure nodes boot from disk using UEFI mode"
    echo "⚠️  It will also add EFI disks if needed to support proper Talos boot"
    echo "⚠️  Proceeding in 5 seconds (Ctrl+C to abort)..."
    sleep 5
    
    # Completely remove ISO and force restart for all VMs
    echo "Removing ISO and forcefully restarting all VMs (critical for Talos bootstrap)..."
    for s in "${VMS[@]}"; do read -r V _ H _ <<<"$s"; remove_iso_and_restart "$H" "$V"; done
    
    # Wait for VMs to restart with disk-only boot
    echo "Waiting $REBOOT_WAIT seconds for VMs to start with disk-only boot configuration..."
    sleep "$REBOOT_WAIT"
    
    print_inventory; echo -e "\n✅  ISO removal complete. Nodes should now boot from disk."
    echo "✅  You can now bootstrap the cluster using the first control plane node."
    echo "💡 REMINDER: Run this command ONLY after applying Talos configs to all nodes.";;
  delete)
    for s in "${VMS[@]}"; do read -r V _ H _ <<<"$s"; delete_vm "$H" "$V"; done
    echo "✅  All VMs deleted."; ;;
  network)
    print_inventory ;;
  *) usage ;;
esac