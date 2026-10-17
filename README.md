# Neutrino / Vera L10 - Interactive Full-Stress Menu

# 在測試節點 (10.36.66.15 等) 上裝：
sudo apt install stress-ng stressapptest fio    # 或對應的套件管理

`fullstress.py` is a single-file interactive menu that runs **CPU / DIMM /
SSD / BF4(IB)** stress, either locally or across up to **4 nodes** (this node +
3 remote nodes) over SSH.

## Usage

```bash
cd /root/Neutrino/Fullstress
./fullstress.py          # or: python3 fullstress.py
```

The menu asks:

1. **What to run** - comma list: `0=ALL, 1=CPU, 2=DIMM, 3=SSD, 4=BF4`
2. **Duration** - `3600`, `30m`, `2h`, `8h` (applies to all items)
3. **Multi-node** - how many remote nodes (0-3), their **OS IPs**, SSH
   user/password (default `root/password`). This node always runs too, so
   3 remotes = 4 nodes total.
4. **[4] SSD check** - shows the pinned target disk (BDF) on every node.
5. **[5] BF4** - auto-detects the BlueField via `mst status -v` on every node.

Then it prints a summary and asks `GO?`. On confirm it launches one bash
script per node (local + remotes), all in parallel.

## Target hardware (Neutrino ETF1_n0..n3)

The 4 node OS IPs are **entered by hand at the prompt each run** (they change);
nothing is hard-coded. The table below is just an example from one campaign:

| Node | OS IP (example) |
|------|-----------------|
| n0   | 10.35.228.146   |
| n1   | 10.35.228.148   |
| n2   | 10.35.228.150   |
| n3   | 10.35.228.154   |

Disks (identical on all 4 nodes, **match by BDF not by /dev/nvmeXn1**):

| BDF            | Role                        |
|----------------|-----------------------------|
| `0004:01:00.0` | **OS drive - NEVER touch**  |
| `000c:01:00.0` | SIT data disk (stress this) |

> Device names differ per node (n2 reports the OS disk as `nvme0n1`), so the
> BDF is the only safe identifier. `0004:01:00.0` is hard-refused in code.

## What parameters each device uses

These mirror the NVIDIA OneDiag test scripts from the NEUTRINO_L10 partner
package. Vendor-private helpers (cpustress binary, MODS js/cfg) are not
redistributable, so CPU uses stress-ng while SSD/BF4 use the same generic
tools the vendor scripts invoke.

| Device | Vendor source   | Tool             | Params |
|--------|-----------------|------------------|--------|
| **CPU**  | `dgx_cpustress.py` | `stress-ng`   | `--cpu <nproc> --timeout <s>` |
| **DIMM** | `dgx_dimm_utility.py` | `stressapptest` | `-W -s <s> --cc_test -M <MB> --pause_delay <2s> -m <threads>` (85% multi / 95% single, keep >=16GB) |
| **SSD**  | `dgx_ssd.py`    | `fio`            | `--rw=randwrite --bs=128k --iodepth=128 --numjobs=16 --direct=1 --time_based` |
| **BF4**  | `dgx_ibstress.py` | `ib_write_bw`  | two ports from `mst status -v`; `-i 1 -p 11000 -q 16 -s 65536 -D <s> -F --report_gbits`; no `-R`, no `-b` |

### SSD (max-load)
One fio command per node, resolved from BDF `000c:01:00.0` at runtime so the
right raw device is used everywhere. Pure `randwrite` = highest power draw.
SIT disk only; `0004:01:00.0` (OS) is refused in code.

### BF4 (IB stress)
Not SSH-into-the-DPU: the BF4 is run **from each host node** against its own
two BlueField ports (same-machine). `mst status -v` is parsed by token shape
(not column position) to get, per port: the RDMA device (`mlx5_x`), the BDF,
and the netdev (`enP...`/`net-ens...`). Steps:

1. pin IPs on the two BF4 ports: server `10.10.10.1/22`, client `10.10.10.2/22`
   (`ip addr add`), so the client can reach the server by IP - works whether
   the link layer is IB or Ethernet.
2. server: `ib_write_bw -d <mlx5_srv> -i 1 -p 11000 -q 16 -s 65536 -D <s> -F --report_gbits`
3. client: same `-d <mlx5_cli>` plus `10.10.10.1` as the server address.

Matches NVIDIA `dgx_ibstress` defaults (`-q 16`, `-s 65536`, `-D <sec>`);
**no `-R`** (native IB / LID) and **no `-b`** (NVIDIA forbids bidirectional on
loopback). Each of the 4 nodes runs its own same-machine BF4 pair, so all 4
nodes may use the same IPs. No BF4 present -> the step is skipped cleanly.

## Logs

Each node writes to its own timestamped log dir:

```
logs/<node-tag>/<YYYYmmdd_HHMMSS>/
    run.log          # orchestration
    power.log        # BMC watts, sampled every 10s (ipmitool)
    cpu.log / dimm.log / ssd.log / bf4_*.log
```

* Local node -> `<script-dir>/logs/<tag>/...`
* Remote nodes -> `/tmp/fullstress_logs/<tag>/...` on that node

`power.log` samples `Chassi0HSCPower` / `CPU0Powe` / `CPU1Powe` / `PKG W`
via `ipmitool sensor list` - i.e. system watts while the stress runs.

## Notes

* Stdlib only - needs `sshpass`, `ssh`, `fio`, `stress-ng`,
  `stressapptest` (DIMM), `ipmitool` (power), `mst` (BF4), `ib_write_bw`.
* Remote nodes need the same tools installed.
