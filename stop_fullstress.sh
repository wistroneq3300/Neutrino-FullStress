#!/usr/bin/env bash
# Stop a running Fullstress1016 run.
#
# Fullstress1016 is a foreground menu on the jumper (R340) that fans out one
# bash runner per node over SSH. Ctrl-C on the jumper stops the jumper but can
# leave the per-node stress (stress-ng / stressapptest / fio / ib_write_bw) and
# the OOB sensor sampler running on the nodes. This wrapper finds and stops
# all of it, on the jumper and on every node.
#
# Usage:
#   ./stop_fullstress.sh              Interactive: show what's running, ask
#   ./stop_fullstress.sh --status     Only report, change nothing
#   ./stop_fullstress.sh --force      Stop everything without asking
#
# Node OS IPs are read from the generated runner scripts and the environment
# (FULLSTRESS_NODES="ip1 ip2 ..."), defaulting to the pinned NODE_TO_BMC map.
set -u

STATUS=0
FORCE=0
for arg in "$@"; do
    case "$arg" in
        --status) STATUS=1 ;;
        --force) FORCE=1 ;;
        -h|--help) echo "Usage: $0 [--status|--force]"; exit 2 ;;
        *) echo "Unknown option: $arg" >&2; exit 2 ;;
    esac
done

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)

# --- what stress/tool names count as "our" processes on a node ------------- #
# matched against `ps` around the generated runner so unrelated tools are safe
NODE_MATCH='fullstress_node_|stress-ng|stressapptest|fio |ib_write_bw|fullstress_logs'
RUNNER_MATCH='fullstress_node_|fullstress\.py'

# --- discover node IPs ----------------------------------------------------- #
# 1) explicit env  2) IPs seen in this run's generated runners  3) NODE_TO_BMC
discover_nodes() {
    if [ -n "${FULLSTRESS_NODES:-}" ]; then
        echo "$FULLSTRESS_NODES"; return
    fi
    local ip
    if [ -d "$SCRIPT_DIR/generated" ]; then
        for f in "$SCRIPT_DIR"/generated/node_*.sh; do
            [ -e "$f" ] || continue
            ip=$(basename "$f" .sh | sed -E 's/^node_//; s/_/./g')
            case "$ip" in
                *.*.*.*) echo "$ip" ;;
            esac
        done
    fi
    # fall back to the pinned map in fullstress.py
    python3 - "$SCRIPT_DIR" <<'PY' 2>/dev/null
import re, sys
try:
    src = open(f"{sys.argv[1]}/fullstress.py", encoding="utf-8").read()
except OSError:
    raise SystemExit(0)
m = re.search(r"NODE_TO_BMC\s*=\s*\{(.*?)\}", src, re.S)
if m:
    for k in re.findall(r'"([0-9.]+)"\s*:', m.group(1)):
        print(k)
PY
}

SSH_OPTS=(-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=6)
NODE_USER="${FULLSTRESS_USER:-root}"
NODE_PASS="${FULLSTRESS_PASS:-password}"

# --- find jumperside processes --------------------------------------------- #
jumper_pids() {
    # the menu process + any bash runner it launched (avoid matching this script)
    pgrep -f "$RUNNER_MATCH" 2>/dev/null | while read -r pid; do
        [ "$pid" = "$$" ] && continue
        # skip ourselves / our parent shell
        case "$(tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null)" in
            *stop_fullstress.sh*) continue ;;
        esac
        echo "$pid"
    done
}

# --- find node processes --------------------------------------------------- #
node_procs() {
    local ip="$1"
    sshpass -p "$NODE_PASS" ssh "${SSH_OPTS[@]}" "$NODE_USER@$ip" \
        "ps -eo pid,etime,args | grep -E '$NODE_MATCH' | grep -v grep" 2>/dev/null
}

report() {
    echo "== Fullstress1016: what is running =="
    local jp
    jp=$(jumper_pids)
    if [ -n "$jp" ]; then
        echo "Jumper (this host) processes:"
        for pid in $jp; do
            echo "  pid $pid  $(tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null | cut -c1-120)"
        done
    else
        echo "Jumper: no fullstress process found."
    fi
    echo
    local any=0
    for ip in $(discover_nodes); do
        local out
        out=$(node_procs "$ip")
        if [ -n "$out" ]; then
            any=1
            echo "Node $ip:"
            echo "$out" | sed 's/^/  /'
        fi
    done
    [ "$any" = 0 ] && echo "Nodes: no stress / sampler processes found."
}

stop_jumper() {
    local jp
    jp=$(jumper_pids)
    if [ -z "$jp" ]; then
        echo "Jumper: nothing to stop."
        return 0
    fi
    echo "Stopping jumper process(es): $(echo $jp | tr '\n' ' ')"
    for pid in $jp; do kill -TERM "$pid" 2>/dev/null || true; done
    sleep 2
    for pid in $jp; do
        if kill -0 "$pid" 2>/dev/null; then
            echo "  pid $pid ignored SIGTERM; SIGKILL"
            kill -KILL "$pid" 2>/dev/null || true
        fi
    done
}

stop_nodes() {
    for ip in $(discover_nodes); do
        local out
        out=$(node_procs "$ip")
        if [ -z "$out" ]; then
            echo "Node $ip: nothing to stop."
            continue
        fi
        echo "Node $ip: stopping stress + sensor sampler ..."
        # kill the runner tree first (it owns the sampler), then any stragglers
        sshpass -p "$NODE_PASS" ssh "${SSH_OPTS[@]}" "$NODE_USER@$ip" \
            "pkill -TERM -f 'fullstress_node_' 2>/dev/null; \
             pkill -TERM -f 'stress-ng' 2>/dev/null; \
             pkill -TERM -f 'stressapptest' 2>/dev/null; \
             pkill -TERM -f 'fio ' 2>/dev/null; \
             pkill -TERM -f 'ib_write_bw' 2>/dev/null; \
             sleep 2; \
             pkill -KILL -f 'stress-ng' 2>/dev/null; \
             pkill -KILL -f 'stressapptest' 2>/dev/null; \
             pkill -KILL -f 'ib_write_bw' 2>/dev/null; \
             echo done" 2>/dev/null | sed 's/^/  /'
    done
}

if [ "$STATUS" = 1 ]; then
    report
    exit 0
fi

report
echo
if [ "$FORCE" != 1 ]; then
    if [ ! -t 0 ]; then
        echo "Non-interactive shell; re-run with --force to stop."
        exit 0
    fi
    printf 'Stop all of the above? [y/N]: '
    read -r reply
    case "$reply" in
        y|Y|yes|YES) ;;
        *) echo "Left running."; exit 0 ;;
    esac
fi

echo
stop_jumper
stop_nodes
echo
echo "Done. Note: the jumper was stopped with SIGTERM, so the automatic"
echo "log-collection + HTML report step did not run. Node logs remain under"
echo "/root/fullstress_logs/<tag>/<stamp>/ on each node."
