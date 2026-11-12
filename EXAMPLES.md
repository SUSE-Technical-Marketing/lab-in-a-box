# More Examples

A larger collection of ready-to-adapt lab definitions, kept here instead of in the main
[README](README.md#examples) so that file doesn't get too cluttered. Same format, same
conventions — see [Lab definition format](README.md#lab-definition-format) for the full field
reference.

## Table of contents

- [AlmaLinux, Debian, Rocky Linux, Alibaba Cloud Linux — a mixed cloud-image lab](#almalinux-debian-rocky-linux-alibaba-cloud-linux--a-mixed-cloud-image-lab)
- [Raspberry Pi OS (aarch64) — not yet supported](#raspberry-pi-os-aarch64--not-yet-supported)

## AlmaLinux, Debian, Rocky Linux, Alibaba Cloud Linux — a mixed cloud-image lab

Four different distros in one lab. `common.VM_MEM`/`VM_CPU` here are intentionally small (1
vCPU, 2 GiB) — enough for a quick smoke test of the distro itself, not a real workload. `VM_DSK`
is set per-node to each source image's own real floor (see
[Deploying a legacy image (CentOS 7)](README.md#deploying-a-legacy-image-centos-7) for what
happens if it's set below that).

Confirmed live 2026-09-03 — and **two of the four needed real per-node fixes**, found only by
actually booting them and checking: AlmaLinux and Rocky Linux worked with every default
unchanged, but Debian and Alibaba Cloud Linux each hit a genuinely different problem.

```jsonc
{
  "common": {
    "VM_MEM": 2048,
    "VM_CPU": 1,
    "VM_DSK": 3,                        // smallest of the four sources (Debian) — every
                                         // node below overrides this with its own real floor
    "VM_BOOT": "uefi",
    "VM_DSK_BUS": "virtio",
    "VM_NET_MODEL": "virtio",
    "config_method": "cloud-init",
    "VM_ROOT_PASS": "12345678",
    "backend": "libvirt"
  },
  "nodes": {
    "alma.mydemo.lab": {
      "myip": "192.168.88.120",
      "SOURCE_IMAGE": "AlmaLinux-10-GenericCloud-latest.x86_64.qcow2",
      "VM_DSK": 10                      // source image is a 10 GiB GenericCloud qcow2 —
                                         // every common default works unchanged
    },
    "debian.mydemo.lab": {
      "myip": "192.168.88.121",
      "SOURCE_IMAGE": "debian-13-genericcloud-amd64.qcow2",
      "VM_DSK": 3,                      // source image is a 3 GiB genericcloud qcow2 —
                                         // NOT the "nocloud" variant (see the note below)
      "network_renderer": "networkd"    // REQUIRED: this image has no NetworkManager at all
                                         // (systemd-networkd only) — without this override the
                                         // guest boots fine but gets zero network config at all,
                                         // same root cause already documented for Ubuntu Server
    },
    "rockylinux.mydemo.lab": {
      "myip": "192.168.88.122",
      "SOURCE_IMAGE": "Rocky-10-GenericCloud-Base.latest.x86_64.qcow2",
      "VM_DSK": 10                      // source image is a 10 GiB GenericCloud qcow2 —
                                         // every common default works unchanged
    },
    "alibaba.mydemo.lab": {
      "myip": "192.168.88.123",
      "SOURCE_IMAGE": "aliyun_2_1903_x64_20G_nocloud_alibase_20230103.qcow2",
      "VM_DSK": 20,                     // source image is a 20 GiB qcow2 — despite the
                                         // "nocloud" in its own filename, this image genuinely
                                         // ships cloud-init (Alibaba's own naming quirk;
                                         // unrelated to Debian's real nocloud/no-cloud-init
                                         // variant, and NOT why it needs config_method below)
      "VM_BOOT": "bios",                // REQUIRED: this image has an MBR partition table with
                                         // no EFI System Partition — boots to "No bootable
                                         // option or device was found" under this project's
                                         // UEFI default, exactly like CentOS 7
      "config_method": "virt_customize" // REQUIRED: this image's cloud-init is hardcoded to
                                         // Alibaba's own production ECS metadata service
                                         // (100.100.100.200) and never even looks at the
                                         // NoCloud seed CD the plain "cloud-init" config_method
                                         // attaches — it just retries that unreachable address
                                         // forever. virt_customize sidesteps this entirely by
                                         // writing network config straight into the guest
                                         // filesystem before boot and disabling cloud-init
    }
  }
}
```

```shell
setup_lab.py os-matrix-test.json
```

> [!NOTE]
> Debian ships a second, genuinely cloud-init-less "nocloud" image variant (filenames like
> `debian-13-nocloud-amd64.qcow2`) — that one needs `config_method: "virt_customize"` instead,
> and the preflight check will warn if you use `cloud-init` with it.

> [!NOTE]
> Alibaba's own hostname-management service resets the guest's hostname after boot regardless
> of what `virt_customize`/cloud-init set it to — SSH, the static IP, and root login all work
> correctly, but don't be surprised if `hostname` reports something other than the node name.

## Raspberry Pi OS (aarch64) — not yet supported

**Status: investigated live 2026-09-03, not working yet — documented here as a known limitation,
not a working example.** Unlike every other image in this project, Raspberry Pi OS is a genuine
architecture mismatch: this project's hypervisors are x86_64, and the official
`*-raspios-*-arm64.img` releases are aarch64 raw disk images built specifically for real
Raspberry Pi hardware's own GPU-firmware boot chain (`bootcode.bin`/`start*.elf`/`config.txt`),
not a UEFI or BIOS boot path at all — `config_method`/`VM_MACHINE` as they exist today have
nothing to attach to here.

What's confirmed so far:

- QEMU (`qemu-arm` + `qemu-uefi-aarch64` packages) provides real `-M raspi3b`/`raspi4b` machine
  models that emulate actual Pi hardware closely enough to be the right starting point — a
  generic `-M virt` aarch64 machine is the wrong target (different SoC, different everything;
  the Pi's own `kernel8.img` won't recognize it).
- The SD card image itself must be resized to a power-of-2 size for QEMU's SD emulation
  (`if=sd`) to accept it — e.g. a 6.05 GiB source needs `qemu-img resize` to 8 GiB, not just
  "big enough".
- Raspberry Pi OS's boot partition already ships a genuine cloud-init NoCloud datasource
  (`/boot/firmware/user-data`, `network-config`, `meta-data` — Raspberry Pi Imager's own
  official customization mechanism), so once it boots, configuring it should need no new
  mechanism at all.
- Getting any boot console output at all has not succeeded yet: `enable_uart=1` in
  `config.txt` and the mini-UART/PL011 dual-`-serial` split (both commonly cited fixes for
  "no console output on raspi4b") were tried against a real image with no result. QEMU's
  `raspi3b`/`raspi4b` machine models are upstream-acknowledged as still incomplete for exactly
  this kind of peripheral fidelity.

Not attempted yet: confirming boot succeeds at all (with working console output), wiring a
new backend/config_method path for cross-architecture boot, and figuring out how this
project's existing virtio-based network/disk conventions map onto the Pi's own USB-network and
SD-card-only hardware model (raspi3b/4b have no PCI bus, so none of `VM_NET_MODEL`/
`VM_DSK_BUS`'s existing values apply).
