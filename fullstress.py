#!/usr/bin/env python3
"""
Neutrino / Vera L10 interactive full-stress menu.

Runs CPU / DIMM / SSD / NIC(IB) stress either locally or across up to 4 nodes
(this node + 3 remote nodes) via SSH (sshpass).

Design notes
------------
* Parameter names/values mirror the NVIDIA OneDiag test scripts shipped in
  the NEUTRINO_L10 partner package:
    - CPU  : tests/scripts/tests/dgx_cpustress.py   (tool=durationS/mode/numCpus)
    - DIMM : tests/scripts/tests/dgx_thermal.py     (stressapptest -s/-M/-m/-W)
    - SSD  : tests/scripts/tests/dgx_ssd.py         (fio --direct=1 ...)
    - NIC  : tests/scripts/tests/dgx_ibstress.py    (ib_write_bw -q/-s/-D ...)
  The vendor-private helpers (cpustress/pwrsort binaries, MODS js/cfg, the
  tb500cbbcfg concurrent groups) are NOT redistributable, so CPU uses
  stress-ng as a functional stand-in while SSD/NIC/DIMM use the same generic
  tools (fio / ib_write_bw / stressapptest) the vendor scripts invoke.

* SSD safety: mounted OS drives (/, /boot, /boot/efi, /boot/efi variants) are
  excluded by default; write workloads require an explicit confirmation.
"""

import re
import os
import json
import shutil
import subprocess
import sys
import time

# --------------------------------------------------------------------------- #
# constants
# --------------------------------------------------------------------------- #

DEFAULT_SSH_USER = "root"
DEFAULT_SSH_PASS = "password"
DEFAULT_DURATION = "8h"          # <-- change this to set the default run time

HERE = os.path.dirname(os.path.abspath(__file__))
CMD_DIR = os.path.join(HERE, "generated")   # per-node generated runner scripts
LOG_ROOT = os.path.join(HERE, "logs")       # <log_root>/<node-tag>/<timestamp>/

# NIC perftest defaults (mirror dgx_ibstress._ConstructAlignedPerftestCommonArgs)
NIC_DEFAULTS = dict(num_qps=16, msg_size=65536, port=18515)

# BF4 (BlueField) IB-stress defaults. Mirrors NVIDIA dgx_ibstress: -q 16,
# -s 65536, -D <duration_secs>. No -R (native IB/LID), no -b (loopback).
# The two BF4 ports are pinned to these IPs so the client can reach the server
# by IP (works for both IB and Ethernet link layers).
BF4_TAG = "BlueField4"            # DEVICE_TYPE keyword in `mst status -v`
BF4_NUM_QPS = 16                  # NVIDIA default (-q 16)
BF4_MSG_SIZE = 65536              # NVIDIA default (-s 65536)
BF4_PORT = 11000                  # perftest listen/connect port (-p)
BF4_IP_SERVER = "10.10.10.1"      # server port IP
BF4_IP_CLIENT = "10.10.10.2"      # client port IP
BF4_PREFIX = 22                   # CIDR prefix for the two IPs

# SSD: the target data drive is pinned by BDF. On this platform the OS drive
# lives at domain 0004 (0004:01:00.0) and is NEVER touched; the SIT data disk
# is at domain 000c (000c:01:00.0). Match by BDF, not by /dev/nvmeXn1 — device
# names differ per node (e.g. n2 reports the OS disk as nvme0n1, not nvme1n1).
SSD_BDF_ALLOW = "000c:01:00"      # target data disk BDF
SSD_BDF_DENY = ("0004:01:00",)    # OS drive(s) — always refused

# SSD full-load fio (single command, max stress). randwrite only.
SSD_FIO = dict(rw="randwrite", bs="128k", iodepth=128, numjobs=16,
               direct=1, size="10%")

# CPU stress-ng defaults
CPU_WORKERS = 0            # 0 -> all cpus
CPU_METHOD = "cpu"         # stress-ng method (cache/alu heavy mix ~ cacheandalu)

# Power logging (background OOB sampler: SSH into the node's BMC and read
# `ipmitool sdr`). In-band /dev/ipmi0 is NOT available on this platform, so
# power must come from the BMC over the network. We snapshot ALL sensors each
# tick (240+ readings) so the log holds full telemetry, not just a few rails.
POWER_INTERVAL = 60        # seconds between full-sensor snapshots

# OOB / BMC access. The node cannot reach its own BMC in-band, so we SSH into
# the BMC (BusyBox) and run ipmitool there. BMC IPs are pinned per node OS IP.
BMC_USER = "root"
BMC_PASS = "0penBmc"
BMC_SSH_OPTS = ["-o", "StrictHostKeyChecking=no",
                "-o", "UserKnownHostsFile=/dev/null",
                "-o", "ConnectTimeout=8"]
# OS node IP  ->  BMC IP   (update when the rack / DHCP changes)
NODE_TO_BMC = {
    "10.36.66.250": "10.36.66.245",
    "10.36.66.251": "10.36.66.246",
    "10.36.66.252": "10.36.66.247",
    "10.36.66.253": "10.36.66.248",
}


# --------------------------------------------------------------------------- #
# small ui helpers
# --------------------------------------------------------------------------- #

def hr(ch="="):
    print(ch * 72)


# ANSI colors (only when stdout is a real terminal, so redirected logs stay clean)
def _c(txt, color):
    if not sys.stdout.isatty():
        return txt
    code = {"green": "32", "red": "31", "yellow": "33"}.get(color, "")
    return f"\033[{code}m{txt}\033[0m" if code else txt


def ping_ok(ip, count=2, timeout=1):
    """True if ip replies to ping. Silent (devnull)."""
    try:
        r = subprocess.run(["ping", "-c", str(count), "-W", str(timeout), ip],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=count * timeout + 3)
        return r.returncode == 0
    except Exception:
        return False


def os_hostname(ip, user=DEFAULT_SSH_USER, passwd=DEFAULT_SSH_PASS, timeout=8):
    """Return the remote OS hostname over SSH, or '' on failure. Silent."""
    try:
        out = subprocess.run(
            ["sshpass", "-p", passwd, "ssh",
             "-o", "StrictHostKeyChecking=no",
             "-o", "UserKnownHostsFile=/dev/null",
             "-o", f"ConnectTimeout={timeout}",
             f"{user}@{ip}", "hostname"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            universal_newlines=True, timeout=timeout + 4)
        return out.stdout.strip() if out.returncode == 0 else ""
    except Exception:
        return ""


def ask(prompt, default=None):
    suffix = f" [{default}]" if default is not None else ""
    val = input(f"{prompt}{suffix}: ").strip()
    if not val and default is not None:
        return default
    return val


def ask_yes(prompt, default=False):
    d = "Y/n" if default else "y/N"
    val = input(f"{prompt} [{d}]: ").strip().lower()
    if not val:
        return default
    return val in ("y", "yes")


def ask_int(prompt, default):
    while True:
        v = ask(prompt, str(default))
        try:
            return int(v)
        except ValueError:
            print("  ! please enter a number")


def parse_duration(text, default_hours=1):
    """Accept '3600', '30m', '2h', or blank -> default_hours."""
    if not text:
        return int(default_hours * 3600)
    t = text.strip().lower()
    try:
        if t.endswith("h"):
            return int(float(t[:-1]) * 3600)
        if t.endswith("m"):
            return int(float(t[:-1]) * 60)
        if t.endswith("s"):
            return int(float(t[:-1]))
        return int(float(t))
    except ValueError:
        print("  ! bad duration, using default")
        return int(default_hours * 3600)


def have(tool):
    return shutil.which(tool) is not None


# --------------------------------------------------------------------------- #
# node discovery (local + optional 3 remote)
# --------------------------------------------------------------------------- #

def discover_nvme():
    """Return list of dicts: {dev, bdf, size, mountpoints, is_os}."""
    out = []
    try:
        listing = subprocess.check_output(
            ["lsblk", "-dno", "NAME,SIZE,TYPE,MOUNTPOINT"],
            universal_newlines=True, stderr=subprocess.DEVNULL).splitlines()
    except Exception:
        return out

    for line in listing:
        parts = line.split(None, 3)
        if len(parts) < 3 or not parts[0].startswith("nvme"):
            continue
        dev = "/dev/" + parts[0]
        size = parts[1]
        mountpoints = parts[3] if len(parts) > 3 else ""
        bdf = ""
        try:
            real = os.path.realpath("/sys/block/" + parts[0])
            for seg in real.split("/"):
                if ":" in seg and "." in seg:
                    bdf = seg
        except Exception:
            pass
        mps = [m for m in mountpoints.split(",") if m]
        # whole-disk mountpoints are rare; also scan partitions
        try:
            pmnts = subprocess.check_output(
                ["lsblk", "-no", "MOUNTPOINT", dev],
                universal_newlines=True, stderr=subprocess.DEVNULL).split()
            mps = [m for m in pmnts if m]
        except Exception:
            pass
        is_os = any(m in ("/", "/boot", "/boot/efi", "/boot/efi/EFI") or
                    m.startswith("/boot/") for m in mps)
        out.append(dict(dev=dev, bdf=bdf, size=size, mountpoints=mps, is_os=is_os))
    return out


def dev_for_bdf(drives, bdf_want):
    """Return the drive dict whose BDF matches bdf_want (normalised)."""
    want = _norm_bdf(bdf_want)
    for d in drives:
        if _norm_bdf(d["bdf"]).startswith(want):
            return d
    return None


def is_denied_bdf(bdf):
    """True if this BDF is an OS/protected drive (must never be written)."""
    b = _norm_bdf(bdf)
    return any(b.startswith(_norm_bdf(x)) for x in SSD_BDF_DENY)


def mark_ssd_role(d):
    """Annotate a drive dict as target / protected / other."""
    b = _norm_bdf(d["bdf"])
    if any(b.startswith(_norm_bdf(x)) for x in SSD_BDF_DENY):
        d["role"] = "OS(protected)"
    elif b.startswith(_norm_bdf(SSD_BDF_ALLOW)):
        d["role"] = "TARGET"
    else:
        d["role"] = "other"
    return d


# --------------------------------------------------------------------------- #
# remote query helpers (ssh)
# --------------------------------------------------------------------------- #

SSH_OPTS = ["-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null",
            "-o", "ConnectTimeout=8"]


def _ssh(ip, user, passwd, cmd):
    """Run a command on a remote node over sshpass, return stdout."""
    full = ["sshpass", "-p", passwd, "ssh"] + SSH_OPTS + \
           [f"{user}@{ip}", cmd]
    try:
        return subprocess.check_output(full, universal_newlines=True,
                                       stderr=subprocess.DEVNULL, timeout=25)
    except Exception:
        return ""


def bmc_ip_for(os_ip):
    """Map an OS node IP to its BMC IP (pinned table). None if unknown."""
    return NODE_TO_BMC.get(os_ip)


def _bmc_ssh(os_ip, cmd, timeout=25):
    """Run a command on the node's BMC over SSH (sshpass), return stdout.

    In-band /dev/ipmi0 is absent on this platform, so all OOB reads (power,
    SEL) go through the BMC's own ipmitool over the network.
    """
    bmc = bmc_ip_for(os_ip)
    if not bmc:
        return ""
    full = ["sshpass", "-p", BMC_PASS, "ssh"] + BMC_SSH_OPTS + \
           [f"{BMC_USER}@{bmc}", cmd]
    try:
        return subprocess.check_output(full, universal_newlines=True,
                                       stderr=subprocess.DEVNULL, timeout=timeout)
    except Exception:
        return ""


def _redfish_get(os_ip, token, path, timeout=25):
    """GET a Redfish path on the node's BMC with the given token."""
    bmc = bmc_ip_for(os_ip)
    if not bmc:
        return ""
    try:
        out = subprocess.check_output(
            ["curl", "-sk", "-m", str(timeout), "-H", f"X-Auth-Token: {token}",
             f"https://{bmc}{path}"],
            universal_newlines=True, stderr=subprocess.DEVNULL)
        return out
    except Exception:
        return ""


def redfish_login(os_ip):
    """Create a Redfish session; return (token, location). ('', '') on failure."""
    bmc = bmc_ip_for(os_ip)
    if not bmc:
        return "", ""
    try:
        hdr = subprocess.check_output(
            ["curl", "-sk", "-m", "20", "-D", "-", "-o", "/dev/null", "-X", "POST",
             "-H", "Content-Type: application/json",
             "-d", '{"UserName": "%s", "Password": "%s"}' % (BMC_USER, BMC_PASS),
             f"https://{bmc}/redfish/v1/SessionService/Sessions"],
            universal_newlines=True, stderr=subprocess.DEVNULL)
    except Exception:
        return "", ""
    token, loc = "", ""
    for line in hdr.splitlines():
        if line.lower().startswith("x-auth-token:"):
            token = line.split(":", 1)[1].strip()
        elif line.lower().startswith("location:"):
            loc = line.split(":", 1)[1].strip()
    return token, loc


def redfish_logout(os_ip, token, location):
    """Best-effort session DELETE so we never leak a BMC session."""
    bmc = bmc_ip_for(os_ip)
    if not bmc or not token:
        return
    path = location if location.startswith("/redfish/") else "/redfish/v1/SessionService/Sessions"
    try:
        subprocess.run(["curl", "-sk", "-m", "10", "-X", "DELETE",
                        "-H", f"X-Auth-Token: {token}",
                        f"https://{bmc}{path}"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


def _redfish_log_paths(os_ip, token, service):
    """Return the Entries @odata.id for a named LogService (e.g. 'EventLog').

    'service' is discovered, not hard-coded: we list the System's LogServices
    and match on the trailing resource id, then return that service's Entries
    path. Returns '' when the service is absent or unreadable.
    """
    sys_out = _redfish_get(os_ip, token, "/redfish/v1/Systems")
    try:
        sysj = json.loads(sys_out)
        smembers = sysj.get("Members", []) or []
        system_id = str(smembers[0].get("@odata.id", "")).rstrip("/").split("/")[-1]
    except Exception:
        return ""
    if not system_id:
        return ""
    ls_out = _redfish_get(os_ip, token,
                          f"/redfish/v1/Systems/{system_id}/LogServices")
    try:
        lsj = json.loads(ls_out)
    except Exception:
        return ""
    for m in lsj.get("Members", []) or []:
        oid = str(m.get("@odata.id", "")).rstrip("/")
        if oid.split("/")[-1].lower() == service.lower():
            return oid + "/Entries"
    return ""


def redfish_entries(os_ip, token, service):
    """Fetch all Entries of a LogService as a list of dicts (memory-safe).

    Uses the Members collection; if the BMC paginates via Members@odata.nextLink
    we follow it. Returns [] when the service is absent/unreadable.
    """
    out = []
    path = _redfish_log_paths(os_ip, token, service)
    if not path:
        return out
    seen = set()
    while path and path not in seen:
        seen.add(path)
        payload = _redfish_get(os_ip, token, path)
        try:
            j = json.loads(payload)
        except Exception:
            break
        for m in j.get("Members", []) or []:
            if isinstance(m, dict) and m.get("Id") is not None:
                out.append(m)
        nxt = j.get("Members@odata.nextLink")
        if isinstance(nxt, str) and nxt.startswith("/redfish/"):
            path = nxt
        else:
            break
    return out


def sel_elist(os_ip):
    """Read the BMC SEL via ipmitool over SSH (BusyBox BMC). Returns text."""
    return _bmc_ssh(os_ip, "ipmitool sel elist 2>&1", timeout=20)


def sel_clear(os_ip):
    """Clear the BMC SEL (ipmitool over SSH). Returns the command output."""
    return _bmc_ssh(os_ip, "ipmitool sel clear 2>&1; echo SEL_CLEARED", timeout=20)


def redfish_clear_log(os_ip, token, service):
    """POST ClearLog for a LogService; best-effort. Returns True on success."""
    bmc = bmc_ip_for(os_ip)
    if not bmc or not token:
        return False
    path = _redfish_log_paths(os_ip, token, service)
    if not path:
        return False
    action = path[:-len("/Entries")] + "/Actions/LogService.ClearLog"
    try:
        subprocess.check_output(
            ["curl", "-sk", "-m", "20", "-X", "POST",
             "-H", f"X-Auth-Token: {token}",
             "-H", "Content-Type: application/json", "-d", "{}",
             f"https://{bmc}{action}"],
            universal_newlines=True, stderr=subprocess.DEVNULL)
        return True
    except Exception:
        return False


def _norm_bdf(bdf):
    """Normalise a BDF to 'dom:bus:dev' with leading zeros stripped per field.

    Handles both sysfs style '0000:0c:01.0' and the campaign style
    '000c:01:00.0' (where the first field is a 4-digit domain).
    """
    b = bdf.lower().strip()
    b = b.split(".")[0]                     # drop function
    parts = b.split(":")
    if len(parts) == 3:
        dom, bus, dev = parts
        return ":".join(p.lstrip("0") or "0" for p in (dom, bus, dev))
    return b


def remote_ssd_status(ip, user, passwd):
    """Return raw `dev|bdf|size|mounts` lines for all nvme*n1 on a node."""
    # take the LAST bdf of the device's sysfs realpath = the controller itself
    cmd = ('for n in /sys/block/nvme*n1; do d=$(basename $n); '
           'b=$(readlink -f $n/device | grep -oE "[0-9a-f]{4}:[0-9a-f]{2}:'
           '[0-9a-f]{2}\\.[0-9a-f]" | tail -1); '
           's=$(lsblk -dno SIZE /dev/$d 2>/dev/null | tr -d " "); '
           'm=$(lsblk -no MOUNTPOINT /dev/$d 2>/dev/null | tr -d " " | '
           'grep -v "^$" | paste -sd, -); '
           'echo "$d|$b|$s|$m"; done')
    return _ssh(ip, user, passwd, cmd)


def parse_remote_ssd(out, bdf_want):
    """Parse remote_ssd_status output, return dict for the wanted BDF."""
    want = _norm_bdf(bdf_want)
    for line in out.strip().splitlines():
        parts = line.split("|")
        if len(parts) < 4:
            continue
        dev, bdf, size, mounts = parts[0], parts[1], parts[2], parts[3]
        if _norm_bdf(bdf).startswith(want):
            return dict(dev="/dev/" + dev, bdf=bdf, size=size or "-",
                        mounts=[m for m in mounts.split(",") if m])
    return None


# --------------------------------------------------------------------------- #
# BF4 detection (mst status -v)
# --------------------------------------------------------------------------- #

_MST_PCI_RE = re.compile(r"\b(?:[0-9a-f]{4}:)?[0-9a-f]{2}:[0-9a-f]{2}\.[0-9a-f]\b")
_MST_RDMA_RE = re.compile(r"\bmlx5_\d+\b")
_MST_NET_RE = re.compile(r"\b(net-)?(en[Psx][0-9a-z]+n?p?\d*|ib[sp]\d+)\b")


def parse_bf4(mst_text, bf_tag="BlueField4"):
    """Parse `mst status -v` output, return one dict per BF device/port.

    `mst status -v` column order varies between platforms, so we match by
    token shape instead of position:
        PCI   -> 0003:01:00.0
        RDMA  -> mlx5_0
        NET   -> enP3p1s0f0np0  (or net-ens212f1np1)
    Returns list of {type, pci, rdma, net}.
    """
    out = []
    for line in mst_text.splitlines():
        if bf_tag not in line:
            continue
        pci_m = _MST_PCI_RE.search(line)
        rdma_m = _MST_RDMA_RE.search(line)
        if not rdma_m:
            continue
        # netdev: prefer a token that is clearly a netdev name
        net = ""
        for tok in line.split():
            t = tok[4:] if tok.startswith("net-") else tok
            if re.match(r"^(en[Ppsx]|ib[sp])", t) and not t.startswith("mlx5"):
                net = t
                break
        out.append(dict(type=bf_tag, pci=pci_m.group(0) if pci_m else "",
                        rdma=rdma_m.group(0), net=net))
    return out


def detect_bf4_local(bf_tag="BlueField4"):
    try:
        txt = subprocess.check_output(["mst", "status", "-v"], universal_newlines=True,
                                      stderr=subprocess.DEVNULL, timeout=20)
    except Exception:
        return None, []
    return txt, parse_bf4(txt, bf_tag)


def detect_bf4_remote(ip, user, passwd, bf_tag="BlueField4"):
    txt = _ssh(ip, user, passwd, "mst status -v 2>/dev/null")
    return txt, parse_bf4(txt, bf_tag)


def _print_ssd_row(node, d):
    """Print the target-BDF (000c) disk status on one node.

    Status column:
        OK/non-OS      -> it is the data disk (not the OS), safe to stress
        OS! (refuse)   -> this BDF is an OS drive, will NOT be touched
        (not found)    -> no disk at the target BDF on this node
    """
    if not d:
        print(f"    {node:<16} {'(not found)':<12} {'-':<7} -        (no target)")
        return
    bdf = d.get("bdf", "")
    is_os = is_denied_bdf(bdf)
    status = "OS! (refuse)" if is_os else "OK (non-OS)"
    mnt = ",".join(d.get("mountpoints") or d.get("mounts") or []) or "-"
    print(f"    {node:<16} {d['dev'].replace('/dev/', ''):<12} "
          f"{d['size']:<7} [{mnt}] {status}")


def _print_bf4_row(node, bf4):
    if not bf4:
        print(f"    {node:<16} (no {BF4_TAG})")
        return
    devs = ", ".join(
        f"{b['rdma']}({b['pci']}{'/' + b['net'] if b.get('net') else ''})"
        for b in bf4 if b["rdma"])
    print(f"    {node:<16} {devs}")


# --------------------------------------------------------------------------- #

def build_cpu_cmd(seconds):
    """CPU stress with stress-ng (full node, all cores)."""
    workers = CPU_WORKERS if CPU_WORKERS else (os.cpu_count() or 1)
    return (f"stress-ng --{CPU_METHOD} {workers} "
            f"--timeout {seconds}s --metrics-brief")


def build_dimm_cmd(seconds, mem_mb):
    """DIMM stress. stressapptest, mirrors dgx_dimm_utility.RunStressAppTest.
    Uses --cc_test (ECC) like the newer NVIDIA utility."""
    if not have("stressapptest"):
        return None
    threads = os.cpu_count() or 1
    return (f"nice -n 0 stressapptest -W -s {seconds} --cc_test "
            f"-M {mem_mb} --pause_delay {seconds * 2} -m {threads}")


def dimm_mem_mb(cfg):
    """Compute -M (MB) for stressapptest.

    Single item  -> 90% of total.
    Multi item   -> 80% of total, always keep >= 16 GB free.
    (CPU/SSD/BF4 also run in the multi case; 80% avoids OOM.)
    """
    total_mb = int((os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")) / 1e6)
    n_items = len(cfg.get("items", []))
    pct = 90 if n_items <= 1 else 80
    mem = int(total_mb * pct / 100)
    reserve = 16384  # keep 16 GB for OS + other stressors
    if total_mb - reserve > 0:
        mem = min(mem, total_mb - reserve)
    return max(mem, 1024), total_mb, pct


def build_ssd_cmd(seconds, drive, logdir="$LOGDIR"):
    """SSD full-load: ONE fio command, max stress (randwrite).

    Target drive is resolved from BDF 000c:01:00.0 at runtime, so the right
    raw device is used on every node (device names differ, e.g. on n2).
    """
    d = SSD_FIO
    return (f'SSDDEV=$(readlink -f /sys/block/$(basename {drive})/device 2>/dev/null) ; '
            f'echo "  SSD target: {drive} (bdf $(echo $SSDDEV | grep -oE '
            f'"[0-9a-f]{{4}}:[0-9a-f]{{2}}:[0-9a-f]{{2}}\\.[0-9a-f]"))" ; '
            f"fio --name=ssd_stress --filename={drive} --direct={d['direct']} "
            f"--rw={d['rw']} --bs={d['bs']} "
            f"--iodepth={d['iodepth']} --numjobs={d['numjobs']} "
            f"--size={d['size']} --runtime={seconds} --time_based "
            f"--group_reporting 2>&1 | tee {logdir}/ssd.log")


def _bf4_one(dev, seconds, server_ip=None, extra=""):
    """One ib_write_bw command (server: no ip; client: <server_ip>)."""
    target = f" {server_ip}" if server_ip else ""
    return (f"ib_write_bw -d {dev} -i 1 -p {BF4_PORT}{target} "
            f"-q {BF4_NUM_QPS} -s {BF4_MSG_SIZE} -D {seconds} "
            f"-F --report_gbits{extra}")


def build_bf4_cmd(seconds, server_dev, client_dev,
                  server_net="", client_net="",
                  server_ip=BF4_IP_SERVER, client_ip=BF4_IP_CLIENT,
                  logdir="$LOGDIR"):
    """BF4 IB stress, mirrors NV dgx_ibstress: single direction, -D <sec>.

    server_dev/client_dev : the two BF4 RDMA devices (mlx5_x) from mst.
    server_net/client_net : their netdev names (for `ip addr add`).
    The client reaches the server by IP (works for IB and Ethernet alike).
    No -R (native IB/LID, NVIDIA default); no -b (loopback per NVIDIA).
    """
    lines = []
    # pin IPs on the two BF4 ports so the client can reach the server by IP
    if server_net:
        lines.append(
            f"(ip addr flush dev {server_net} 2>/dev/null; "
            f"ip addr add {server_ip}/{BF4_PREFIX} dev {server_net} 2>/dev/null; "
            f"ip link set {server_net} up 2>/dev/null; true)")
    if client_net:
        lines.append(
            f"(ip addr flush dev {client_net} 2>/dev/null; "
            f"ip addr add {client_ip}/{BF4_PREFIX} dev {client_net} 2>/dev/null; "
            f"ip link set {client_net} up 2>/dev/null; true)")

    srv = _bf4_one(server_dev, seconds)
    cli = _bf4_one(client_dev, seconds, server_ip=server_ip)
    lines.append(f'echo "  BF4: server={server_dev} client={client_dev} '
                 f'client->{server_ip}"')
    lines.append(f"({srv} >{logdir}/bf4_server.log 2>&1 &)")
    lines.append('sleep 3')
    lines.append(f"({cli} 2>&1 | tee {logdir}/bf4_client.log)")
    lines.append(f'pkill -f "ib_write_bw -d {server_dev}" 2>/dev/null; true')
    return " ; ".join(lines)


# --------------------------------------------------------------------------- #
# remote execution via sshpass + ssh
# --------------------------------------------------------------------------- #

def remote_run(ip, user, passwd, script, background=True):
    """Ship `script` to a remote node and run it. Returns Popen or None."""
    tag = ip.replace(".", "_")
    local = os.path.join(CMD_DIR, f"node_{tag}.sh")
    with open(local, "w") as f:
        f.write("#!/bin/bash\n")
        f.write(script + "\n")
    os.chmod(local, 0o755)

    remote_path = f"/tmp/fullstress_node_{tag}.sh"
    scp = ["sshpass", "-p", passwd, "scp",
           "-o", "StrictHostKeyChecking=no",
           "-o", "UserKnownHostsFile=/dev/null",
           local, f"{user}@{ip}:{remote_path}"]
    ssh = ["sshpass", "-p", passwd, "ssh",
           "-o", "StrictHostKeyChecking=no",
           "-o", "UserKnownHostsFile=/dev/null",
           f"{user}@{ip}", f"bash {remote_path}"]

    if background:
        subprocess.run(scp, check=False)
        p = subprocess.Popen(ssh)
        return p
    else:
        subprocess.run(scp, check=False, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL)
        return subprocess.run(ssh)


def local_run(script, background=True):
    tag = "local"
    local = os.path.join(CMD_DIR, f"node_{tag}.sh")
    with open(local, "w") as f:
        f.write("#!/bin/bash\n" + script + "\n")
    os.chmod(local, 0o755)
    if background:
        return subprocess.Popen(["bash", local])
    return subprocess.run(["bash", local])


# --------------------------------------------------------------------------- #
# menu
# --------------------------------------------------------------------------- #

def banner():
    hr()
    print("  Neutrino / Vera L10 — Interactive Full-Stress Menu")
    print("  (CPU / DIMM / SSD / BF4 — local or 4-node)")
    hr()


def collect_menu():
    """Return a config dict describing what to run."""
    cfg = {}

    print("\n[1] What to run (comma separated)")
    print("    0 = ALL   (CPU + DIMM + SSD + BF4)")
    print("    1 = CPU   (stress-ng)")
    print("    2 = DIMM  (stressapptest)")
    print("    3 = SSD   (fio randwrite, max load)")
    print("    4 = BF4   (IB stress on BlueField via mst status -v)")
    sel = ask("  choose", "0")
    picked = set()
    # Accept "123", "1,2,3", "1 2 3", "0" (all). Split on any non-digit so a
    # space or comma between choices is optional.
    digits = [c for c in sel if c.isdigit()]
    if "0" in digits:
        picked.update({"cpu", "dimm", "ssd", "bf4"})
    else:
        for tok in digits:
            if tok == "1":
                picked.add("cpu")
            elif tok == "2":
                picked.add("dimm")
            elif tok == "3":
                picked.add("ssd")
            elif tok == "4":
                picked.add("bf4")
    if not picked:
        picked = {"cpu"}
    cfg["items"] = picked

    print("\n[2] Duration (e.g. 3600, 30m, 2h, 8h). Used for all items.")
    cfg["seconds"] = parse_duration(ask("  duration", DEFAULT_DURATION))

    # ---- multi-node (jumper mode: all 4 nodes are REMOTE, this box is not stressed) ----
    print("\n[3] Multi-node (jumper mode: 4 REMOTE nodes; this box is NOT stressed)")
    print("    Enter all 4 node OS IPs. Press ENTER to skip a node.")

    def _ask_ip(label):
        """Read an IP, then show ping + OS-hostname markers inline.

        The prompt echoes on the terminal already, so we don't print the
        label+IP again; we just append the two coloured markers.
        """
        val = (input(f"{label}: ") or "").strip()
        if not val:
            print(f"         {_c('[skip]', 'yellow')}")
            return val, True
        ok = ping_ok(val)
        pmark = _c("[ping OK]", "green") if ok else _c("[ping FAIL]", "red")
        host = os_hostname(val) if ok else ""
        if host:
            hmark = _c(f"[hostname OK: {host}]", "green")
        elif ok:
            hmark = _c("[hostname FAIL]", "red")
        else:
            hmark = _c("[ssh skipped]", "yellow")
        print(f"         {pmark} {hmark}")
        return val, (ok and bool(host))

    cfg["local_ip"] = ""   # jumper mode: never stress the local box
    cfg["remote_ips"] = []
    unreachable = []
    for i in range(1, 5):
        ip, ok = _ask_ip(f"  node #{i} IP")
        if ip:
            cfg["remote_ips"].append(ip)
            if not ok:
                unreachable.append(ip)
    # SSH creds are fixed for this rig (root/password) - not prompted
    cfg["ssh_user"] = DEFAULT_SSH_USER
    cfg["ssh_pass"] = DEFAULT_SSH_PASS
    print(f"    ssh: {DEFAULT_SSH_USER}/{DEFAULT_SSH_PASS}")

    # warn (but do not abort by default) if any entered node is unreachable
    if unreachable:
        print("  " + _c(f"! unreachable: {', '.join(unreachable)}", "red"))
        if not ask_yes("    continue anyway?", False):
            print("aborted.")
            sys.exit(1)

    # ---- per-item specifics ----
    if "ssd" in picked:
        print(f"\n[4] SSD check (target BDF {SSD_BDF_ALLOW}*)")
        # jumper mode: this box is not stressed; only the remote nodes matter
        target = None
        print(f"    {'node':<16} {'dev':<12} {'size':<7} mount    status")
        for ip in cfg["remote_ips"]:
            out = remote_ssd_status(ip, cfg["ssh_user"], cfg["ssh_pass"])
            r = parse_remote_ssd(out, SSD_BDF_ALLOW)
            _print_ssd_row(ip, r)
            if r and target is None and not is_denied_bdf(r.get("bdf", "")):
                target = r
        if target:
            cfg["ssd_drive"] = target["dev"]
            cfg["ssd_bdf"] = target["bdf"]
            print(f"  => will run fio (randwrite, max load) on "
                  f"{target['dev']} ({target['bdf']})")
        else:
            cfg["ssd_drive"] = None
        # hard guard: never allow the OS drive
        if target and is_denied_bdf(target["bdf"]):
            print("  !! target BDF matches an OS drive — refusing SSD run")
            cfg["ssd_drive"] = None

    if "bf4" in picked:
        print("\n[5] BF4 IB stress (auto-detect via mst status -v)")
        # jumper mode: this box is not stressed; detect BF4 on the remote nodes
        bf4 = []
        for ip in cfg["remote_ips"]:
            _, bfr = detect_bf4_remote(ip, cfg["ssh_user"], cfg["ssh_pass"],
                                       BF4_TAG)
            _print_bf4_row(ip, bfr)
            if not bf4 and bfr:
                bf4 = bfr
        if bf4:
            devs = [b["rdma"] for b in bf4 if b["rdma"]]
            nets = [b["net"] for b in bf4 if b["net"]]
            cfg["bf4_server"] = devs[0] if devs else None
            cfg["bf4_client"] = devs[1] if len(devs) > 1 else (
                devs[0] if devs else None)
            cfg["bf4_server_net"] = nets[0] if nets else ""
            cfg["bf4_client_net"] = nets[1] if len(nets) > 1 else (
                nets[0] if nets else "")
            print(f"  => BF4 pair: {cfg['bf4_server']}"
                  f"({cfg['bf4_server_net']}) <-> "
                  f"{cfg['bf4_client']}({cfg['bf4_client_net']})")
            print(f"     server ip={BF4_IP_SERVER}, client ip={BF4_IP_CLIENT}"
                  f" (client connects to server ip)")
        else:
            print(f"  ! no {BF4_TAG} found (no BF4 installed?) — will skip BF4")
            cfg["bf4_server"] = None
            cfg["bf4_server_net"] = ""
            cfg["bf4_client_net"] = ""

    if "dimm" in picked:
        print("\n[6] DIMM")
        _, total_mb, pct = dimm_mem_mb(cfg)
        print(f"  stressapptest will use ~{pct}% of RAM "
              f"(multi-item=80%, single=90%, keep >=16GB)")
        cfg["dimm_mb"] = 0

    return cfg


# --------------------------------------------------------------------------- #
# build per-node script
# --------------------------------------------------------------------------- #

def build_node_script(cfg, node_tag="local", log_root=None, stamp=None):
    """Compose one bash script for a node. Creates a per-node log dir.

    log_root defaults to LOG_ROOT (the script's own ./logs). Remote nodes
    should pass /root/fullstress_logs so logs live on that node's disk.
    stamp is shared across nodes so every node writes the same run dir.
    """
    stamp = stamp or time.strftime("%Y%m%d_%H%M%S")
    log_root = log_root or LOG_ROOT
    logdir = f"{log_root}/{node_tag}/{stamp}"
    lines = [
        "#!/bin/bash",
        "set +e",
        f'LOGDIR="{logdir}"',
        'mkdir -p "$LOGDIR"',
        'echo "=== fullstress start $(date) on $(hostname) ==="',
        'echo "logdir: $LOGDIR"',
        # tee the whole orchestration to run.log too
        "exec > >(tee -a \"$LOGDIR/run.log\") 2>&1",
        f'echo "node=$(hostname) items={",".join(sorted(cfg["items"]))} duration={cfg["seconds"]}s"',
    ]
    secs = cfg["seconds"]
    # node_tag is the node IP with dots as underscores (e.g. 10_36_66_15)
    os_ip = node_tag.replace("_", ".") if node_tag.count("_") == 3 else ""
    bmc_ip = bmc_ip_for(os_ip) or ""

    # BMC logs (SEL / EventLog / Redfish SEL) are cleared centrally in main()
    # before launch; here we only run the sampler + the stress itself.

    # background sensor sampler: SSH into this node's BMC and dump `ipmitool sdr`
    # (ALL sensors) every POWER_INTERVAL seconds. In-band ipmitool is
    # unavailable here, so the sample is taken OOB. Each tick is prefixed with a
    # timestamp line so the log stays parseable even though sdr has no time.
    if bmc_ip:
        sampler = (
            'while true; do '
            'echo "### $(date +%%s) $(date +%%Y-%%m-%%dT%%H:%%M:%%S)"; '
            "sshpass -p '%s' ssh %s %s@%s "
            "'ipmitool sdr' 2>/dev/null; "
            "sleep %d; done"
        ) % (BMC_PASS, " ".join(BMC_SSH_OPTS), BMC_USER, bmc_ip, POWER_INTERVAL)
    else:
        sampler = (
            'while true; do echo "### $(date +%%s)  ! no BMC mapping for this node"; '
            "sleep %d; done"
            % POWER_INTERVAL
        )
    lines.append(f'echo "--- sensor sampling every {POWER_INTERVAL}s (OOB via BMC {bmc_ip or "N/A"}) ---"')
    lines.append('PWR_LOG="$LOGDIR/sensor.txt"')
    lines.append('echo "# full ipmitool sdr snapshot every ' + str(POWER_INTERVAL) + 's (### <epoch> <iso> then one line per sensor)" > "$PWR_LOG"')
    lines.append(f'({sampler}) >> "$PWR_LOG" 2>/dev/null & PWR_PID=$!')
    lines.append('echo "  sensor sampler pid=$PWR_PID"')

    if "cpu" in cfg["items"]:
        lines.append('echo "--- CPU stress ---"')
        lines.append(f'({build_cpu_cmd(secs)}) 2>&1 | tee "$LOGDIR/cpu.log"')

    if "dimm" in cfg["items"]:
        lines.append('echo "--- DIMM stress ---"')
        mb, total_mb, pct = dimm_mem_mb(cfg)
        lines.append(f'echo "  DIMM: -M {mb}MB ({pct}% of {total_mb}MB total)"')
        cmd = build_dimm_cmd(secs, mb)
        if cmd:
            lines.append(f'({cmd}) 2>&1 | tee "$LOGDIR/dimm.log"')
        else:
            lines.append('echo "  ! stressapptest not installed; skipping DIMM" '
                         '| tee "$LOGDIR/dimm.log"')

    if "ssd" in cfg["items"]:
        lines.append('echo "--- SSD stress (fio randwrite, max load) ---"')
        drv = cfg.get("ssd_drive")
        if drv and not is_denied_bdf(cfg.get("ssd_bdf", "")):
            lines.append(f'echo "  target: {drv} ({cfg.get("ssd_bdf","")})"')
            lines.append(build_ssd_cmd(secs, drv))
        else:
            lines.append('echo "  ! no valid target SSD (BDF '
                         f'{SSD_BDF_ALLOW}*) — skipping SSD" '
                         '| tee "$LOGDIR/ssd.log"')

    if "bf4" in cfg["items"]:
        lines.append('echo "--- BF4 IB stress ---"')
        if cfg.get("bf4_server") and cfg.get("bf4_client"):
            lines.append(build_bf4_cmd(
                secs, cfg["bf4_server"], cfg["bf4_client"],
                server_net=cfg.get("bf4_server_net", ""),
                client_net=cfg.get("bf4_client_net", "")))
        else:
            lines.append('echo "  ! no BF4 device found — skipping BF4" '
                         '| tee "$LOGDIR/bf4.log"')

    lines.append('kill $PWR_PID 2>/dev/null; true')
    lines.append('echo "=== fullstress done $(date) ==="')
    lines.append(f'echo "logs in: {logdir}"')
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #

def _norm_event(e):
    """Normalise a Redfish log entry to a diffable key (Message + Severity)."""
    msg = str(e.get("Message", "")).strip()
    sev = str(e.get("Severity", "")).strip()
    created = str(e.get("Created", "")).strip()
    return f"[{sev}] {msg} @ {created}"


def clear_all_logs(ips):
    """PRE: clear IPMI SEL + Redfish EventLog + Redfish SEL on every BMC.

    IPMI SEL is probed (read) first: `sel clear` returns no contents, so a
    broken SEL is left intact rather than wiped blind (mirrors cycle_engine's
    pre_sel_probe -> pre_sel_clear policy). Returns {ip: {kind: status}}.
    """
    result = {}
    for ip in ips:
        st = {"sel_probe": "", "sel_clear": "", "eventlog_clear": "", "sel_rf_clear": ""}
        # 1. probe IPMI SEL (do not wipe a broken SEL blind)
        probe = sel_elist(ip)
        st["sel_probe"] = "ok" if probe and "error" not in probe.lower() else "unreadable"
        # 2. clear IPMI SEL only if readable
        if st["sel_probe"] == "ok":
            st["sel_clear"] = "SEL_CLEARED" if "SEL_CLEARED" in sel_clear(ip) else "failed"
        else:
            st["sel_clear"] = "skipped (SEL unreadable)"
        # 3/4. clear Redfish EventLog + SEL via ClearLog action
        token, loc = redfish_login(ip)
        if token:
            try:
                st["eventlog_clear"] = "ok" if redfish_clear_log(ip, token, "EventLog") else "failed/absent"
                st["sel_rf_clear"] = "ok" if redfish_clear_log(ip, token, "SEL") else "failed/absent"
            finally:
                redfish_logout(ip, token, loc)
        else:
            st["eventlog_clear"] = "no redfish session"
            st["sel_rf_clear"] = "no redfish session"
        result[ip] = st
        print(f"  [{ip}] SEL probe={st['sel_probe']} clear={st['sel_clear']} "
              f"EventLog clear={st['eventlog_clear']} RF-SEL clear={st['sel_rf_clear']}")
    return result


def snapshot_events(ips, outdir, tag):
    """Capture BMC SEL + Redfish EventLog/SEL for each node (read-only).

    Used for the POST capture (PRE clears the logs, so there is no PRE
    snapshot). Returns {os_ip: {"sel": set, "eventlog": set, "rf_sel": set}}
    and writes raw evidence under <outdir>/<tag>/.
    """
    d = os.path.join(outdir, tag)
    os.makedirs(d, exist_ok=True)
    result = {}
    for ip in ips:
        per = {"sel": set(), "eventlog": set(), "rf_sel": set()}
        # 1. raw SEL via ipmitool over SSH to the BMC
        sel_txt = sel_elist(ip)
        with open(os.path.join(d, f"sel_{ip}.txt"), "w",
                  encoding="utf-8", errors="replace") as f:
            f.write(sel_txt)
        for line in sel_txt.splitlines():
            if line.strip() and "no entries" not in line.lower() and "SEL_CLEARED" not in line:
                per["sel"].add(line.strip())
        # 2. Redfish EventLog + SEL
        token, loc = redfish_login(ip)
        if token:
            try:
                ev = redfish_entries(ip, token, "EventLog")
                with open(os.path.join(d, f"eventlog_{ip}.json"), "w",
                          encoding="utf-8", errors="replace") as f:
                    json.dump(ev, f, indent=1, ensure_ascii=False)
                per["eventlog"] = {_norm_event(e) for e in ev}
                rsel = redfish_entries(ip, token, "SEL")
                with open(os.path.join(d, f"rf_sel_{ip}.json"), "w",
                          encoding="utf-8", errors="replace") as f:
                    json.dump(rsel, f, indent=1, ensure_ascii=False)
                per["rf_sel"] = {_norm_event(e) for e in rsel}
            finally:
                redfish_logout(ip, token, loc)
        result[ip] = per
    return result


def print_event_report(post, ips):
    """Print the post-run events per node (logs were cleared at PRE)."""
    print("\n" + "-" * 72)
    print("  BMC LOGS AFTER RUN (cleared pre-run, so all entries are new)")
    print("-" * 72)
    total = 0
    for ip in ips:
        c = post.get(ip, {})
        for kind, label in (("sel", "SEL(ipmitool)"),
                            ("eventlog", "Redfish EventLog"),
                            ("rf_sel", "Redfish SEL")):
            ent = c.get(kind, set())
            if ent:
                total += len(ent)
                print(f"  [{ip}] {label} +{len(ent)}:")
                for line in sorted(ent):
                    print(f"      {line}")
        if not any(c.get(k) for k in ("sel", "eventlog", "rf_sel")):
            print(f"  [{ip}] no new events")
    print("-" * 72)
    print(f"  total new events: {total}")
    return total


def collect_node_logs(ips, user, passwd, stamp, run_dir):
    """scp each node's /root/fullstress_logs/<tag>/<stamp>/ back under run_dir."""
    os.makedirs(run_dir, exist_ok=True)
    for ip in ips:
        tag = ip.replace(".", "_")
        remote = f"/root/fullstress_logs/{tag}/{stamp}"
        dest = os.path.join(run_dir, tag)
        os.makedirs(dest, exist_ok=True)
        cmd = ["sshpass", "-p", passwd, "scp", "-r",
               "-o", "StrictHostKeyChecking=no",
               "-o", "UserKnownHostsFile=/dev/null",
               f"{user}@{ip}:{remote}/*", dest]
        r = subprocess.run(cmd, check=False, stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, universal_newlines=True)
        n = len([x for x in os.listdir(dest)]) if os.path.isdir(dest) else 0
        print(f"  [{ip}] collected {n} file(s) -> {dest}")
        if r.returncode != 0:
            print(f"  [{ip}] scp warning: {r.stderr.strip()[:200]}")


def main():
    banner()
    os.makedirs(CMD_DIR, exist_ok=True)
    cfg = collect_menu()

    print("\n" + "-" * 72)
    print("  SUMMARY")
    print(f"    items    : {', '.join(sorted(cfg['items']))}")
    print(f"    duration : {cfg['seconds']}s")
    print(f"    nodes    : {len(cfg['remote_ips'])} remote "
          f"({cfg['remote_ips']})  [jumper mode: local box NOT stressed]")
    print("-" * 72)
    if not ask_yes("  GO?", False):
        print("aborted.")
        return 1

    os.makedirs(CMD_DIR, exist_ok=True)
    os.makedirs(LOG_ROOT, exist_ok=True)

    # --- PRE: clear BMC SEL + Redfish EventLog/SEL (no PRE snapshot needed) - #
    stamp = time.strftime("%Y%m%d_%H%M%S")
    run_dir = os.path.join(LOG_ROOT, "fullstress_logs", stamp)
    ev_dir = os.path.join(LOG_ROOT, f"events_{stamp}")
    print("\n[*] pre-run: clearing BMC logs (SEL + EventLog + Redfish SEL) ...")
    pre_clear = clear_all_logs(cfg["remote_ips"])

    procs = []
    for ip in cfg["remote_ips"]:
        tag = ip.replace(".", "_")
        # remote nodes log to /tmp/fullstress_logs on their own disk
        rscript = build_node_script(cfg, node_tag=tag,
                                    log_root="/root/fullstress_logs",
                                    stamp=stamp)
        print(f"[*] launching remote node {ip} ... "
              f"(logs: /tmp/fullstress_logs/{tag} on that node)")
        p = remote_run(ip, cfg["ssh_user"], cfg["ssh_pass"], rscript,
                       background=True)
        procs.append((ip, p))

    print(f"\n[+] {len(procs)} node(s) running. Ctrl-C to stop.\n")
    try:
        while True:
            alive = [(n, p) for n, p in procs if p and p.poll() is None]
            if not alive:
                break
            time.sleep(5)
    except KeyboardInterrupt:
        print("\n[!] interrupt — terminating ...")
        for n, p in procs:
            try:
                p.terminate()
            except Exception:
                pass
    print("[+] done.")

    # --- POST: capture the (new) events and report them --------------------- #
    print("\n[*] post-run: capturing BMC events ...")
    post_events = snapshot_events(cfg["remote_ips"], ev_dir, "post")
    print_event_report(post_events, cfg["remote_ips"])
    print(f"\n[+] event evidence dir: {ev_dir}")

    # --- collect node logs back here, then build the HTML report ---------- #
    print("\n[*] collecting node logs to the jumper ...")
    collect_node_logs(cfg["remote_ips"], cfg["ssh_user"], cfg["ssh_pass"],
                      stamp, run_dir)
    nodes = [(ip.replace(".", "_"), ip, bmc_ip_for(ip) or "") for ip in cfg["remote_ips"]]
    try:
        import fs_report
        html_path = fs_report.build_report(run_dir, nodes)
        out = os.path.join(run_dir, "report.html")
        with open(out, "w", encoding="utf-8") as f:
            f.write(html_path)
        print(f"[+] report: {out}")
    except Exception as exc:
        print(f"[!] report generation failed: {exc}")
    print(f"\n[+] run directory: {run_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
