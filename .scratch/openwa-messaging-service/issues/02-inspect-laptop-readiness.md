Type: task
Status: resolved
Blocked by:

## Question

What are the laptop's live OS version, free disk, swap, memory pressure, Docker/Compose/Git availability, current listeners and services, firewall state, CPU architecture, and ability to reach the required upstream registries and WhatsApp endpoints over `ssh samik@192.168.1.250`?

This ticket is read-only: record facts without installing, upgrading, enabling, or reconfiguring anything.

## Comments

## Answer

Inspected read-only over SSH on 2026-07-31. No permission blocker was encountered; `samik` can run non-interactive `sudo` for the read-only checks used here.

### Host and capacity

- Host: `sami-lenovo`, Ubuntu 26.04 LTS, kernel `7.0.0-15-generic`, `x86_64`.
- CPU: Intel Pentium N3540, 4 physical cores / 4 threads, 2.16 GHz nominal and about 2.67 GHz maximum.
- RAM: 3.7 GiB total; at inspection time 1.6 GiB was used and 2.1 GiB was available.
- Memory pressure was currently quiet: `/proc/pressure/memory` reported zero 10/60/300-second pressure averages, and a five-second `vmstat` sample showed no active swap-in or swap-out.
- Swap is undersized and fully occupied: the only swap device is a 512 MiB `/swapfile`, with essentially all 512 MiB used. This is a provisioning prerequisite for a Chromium-backed service, even though there was no active paging during the sample.
- Root storage is a 465.8 GB rotational SATA disk (`WDC WD5000MPCK-24AWHT0`), ext4, with about 418 GB free. Disk capacity is not a blocker, but swap-heavy operation on this HDD would be slow.
- The graphical LXQt desktop is active. A `glmatrix` screensaver process alone was using about 14% CPU during inspection; the desktop and nonessential resident services reduce the headroom available to OpenWA.

### Runtime prerequisites

- Docker is not installed (`docker: command not found`); neither Docker Compose nor a Docker service is present.
- Git 2.53.0 and curl 8.18.0 are installed.
- No existing containers could exist under Docker because the runtime is absent.

### Network and exposure

- The Wi-Fi interface `wlp1s0` is up with the intended static address `192.168.1.250/24`; the connection is configured to autoconnect, and the default route is `192.168.1.254`.
- Outbound DNS/HTTPS checks succeeded: the OpenWA GitHub repository returned HTTP 200, GHCR returned the expected unauthenticated registry response HTTP 401, and WhatsApp Web returned HTTP 200.
- LAN listeners were limited to SSH on TCP 22. CUPS and DNS listeners were loopback-only; mDNS listened on UDP 5353.
- UFW is inactive. The active iptables policies are `ACCEPT` for input, forward, and output, with no restrictive nftables rules observed. OpenWA must remain loopback-only until a deliberate LAN firewall rule is installed.

### Server-lifecycle posture

- SSH is active. The machine boots to `graphical.target` rather than a headless target.
- Lid-close behavior is explicitly `ignore` on battery and external power, which is suitable for a closed-lid server.
- The battery reported 60% charge and about 70.4% remaining design capacity.
- The Wi-Fi address is static and the connection autoconnects.

### Readiness conclusion

The laptop is conditionally suitable for one low-volume `whatsapp-web.js` session plus a lightweight cloud-backed assistant, but it is not ready to provision yet. The runtime-layout decision must account for installing Docker/Compose, increasing swap (with the HDD-performance tradeoff made explicit), enabling a default-deny firewall before any LAN bind, and reducing avoidable graphical/background CPU and memory use. Disk space, architecture, SSH access, stable addressing, and outbound reachability are all adequate.
