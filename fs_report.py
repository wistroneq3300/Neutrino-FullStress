#!/usr/bin/env python3
"""HTML report for Fullstress1016 runs.

Self-contained, offline. Follows the visual style of the cycle report
(brand bar, tabs, node browser, badges) but is tuned for a stress run:
  * Overview tab: all 4 nodes side by side (device PASS/WARN, elapsed, events)
  * Nodes tab:   per-node panels; device logs (cpu/dimm/ssd/bf4) embedded and
                 collapsible; sensor.txt and other bulky evidence linked
  * Findings tab: every FAIL / WARN / CRITICAL / ERROR line scraped from the
                 collected logs, so problems are visible without opening files

Layout it expects (all under a single run directory):
    <run>/<node-tag>/...           # collected node logs
    <run>/report.html              # this file
"""

import html
import os
import re

# Device logs shown embedded (collapsible). Everything else is linked.
EMBED_LOGS = ("cpu.log", "dimm.log", "ssd.log",
              "bf4_server.log", "bf4_client.log",
              "dmesg.log", "bmc_sel.log", "bmc_event.log")
# Long, high-frequency samplers: linked, not embedded.
LINK_LOGS = ("sensor.txt", "resource.log", "run.log")
# Evidence files that are linked, not embedded (large / tabular).
LINK_EXTS = (".txt", ".dat", ".log", ".json")

# Lines worth surfacing in the Findings tab. Kept deliberately broad and
# case-insensitive; the cycle report uses the same idea (surface, do not judge).
FINDING_RE = re.compile(
    r"\b(FAIL|FAILED|FAILURE|WARN|WARNING|CRITICAL|FATAL|ERROR|PANIC|"
    r"MCE|OOM|SEGV|ABORT|TIMEOUT|TIMED OUT|not found|unreachable)\b", re.I)

CSS = """
:root{color-scheme:light;--brand:#005f91;--brand-dark:#123b59;--ink:#142e43;--muted:#526579;
--line:#d8e2e9;--paper:#fff;--canvas:#f3f6f9;--fail:#9d2929;--fail-bg:#fff0ee;--warn:#815400;
--warn-bg:#fff5d8;--ok-bg:#e4f3eb;--radius:6px}
*{box-sizing:border-box}body{margin:0;background:var(--canvas);color:var(--ink);
font:15px/1.55 "Segoe UI",Arial,sans-serif;font-variant-numeric:tabular-nums}
a{color:var(--brand);text-underline-offset:3px;overflow-wrap:anywhere}a:hover{color:var(--brand-dark)}
code,pre{font-family:Consolas,"Liberation Mono",monospace;font-size:13px}
.brandbar{background:#fff;color:#123b59;border-bottom:1px solid #00a0be;padding:16px max(24px,calc((100vw - 1320px)/2))}
.brand{font-size:24px;font-weight:650;letter-spacing:-.02em}.brand small{font-size:14px;font-weight:400;margin-left:18px;color:#526579}
header,main,footer{max-width:1376px;margin:auto;padding:24px 28px}header{padding-bottom:0}
h1{font-size:34px;line-height:1.15;letter-spacing:-.025em;font-weight:650;margin:5px 0 12px}
h2{font-size:23px;line-height:1.3;margin:0 0 18px;font-weight:650}h3{font-size:17px;margin:24px 0 12px}
.muted{color:var(--muted)}.runline{display:flex;flex-wrap:wrap;gap:8px 24px;align-items:center;margin:12px 0 22px}
.tabs{display:flex;gap:4px;overflow-x:auto;border-bottom:1px solid var(--line);padding-top:8px}
.tabs button{white-space:nowrap;flex-shrink:0;border:0;border-radius:0;background:transparent;
padding:12px 18px;cursor:pointer;font:inherit;color:var(--ink)}
.tabs button[aria-selected=true]{background:var(--paper);box-shadow:inset 0 -3px 0 var(--brand);font-weight:650;color:var(--brand-dark)}
.tabs button:hover{background:#e8f1f7}[hidden]{display:none!important}
.sheet{background:var(--paper);border:1px solid var(--line);border-radius:12px;padding:26px;margin-bottom:24px}
.badge{display:inline-block;border-radius:4px;padding:3px 8px;font-size:12px;line-height:1.6;
font-weight:700;letter-spacing:.025em;white-space:nowrap;background:#e8edef;color:#34484f}
.badge.fail{background:var(--fail-bg);color:var(--fail)}
.badge.warn{background:var(--warn-bg);color:var(--warn)}
.badge.pass{background:var(--ok-bg);color:#165b37}
.badge.info{background:#e8eef8;color:#365272}
.tablewrap{overflow-x:auto;width:100%}table{border-collapse:collapse;width:100%;text-align:left}
th{font-size:12px;font-weight:600;color:var(--muted);background:#f3f6f9}
th,td{padding:13px 14px;border-bottom:1px solid var(--line);vertical-align:top}
td small{display:block;color:var(--muted)}tr:last-child td{border-bottom:0}
.target{font-weight:650;white-space:nowrap}
details{border-top:1px solid var(--line)}details:last-child{border-bottom:1px solid var(--line)}
summary{cursor:pointer;padding:15px 4px;overflow-wrap:anywhere}summary:hover{background:#f1f7fa}
summary .badge{margin:0 8px}.detail-body{padding:0 16px 18px}
.detail-body>pre{white-space:pre-wrap;overflow-wrap:anywhere;max-height:400px;overflow:auto;
background:#f3f6f9;border:1px solid var(--line);padding:14px}
.brandbar .brand-caption{color:#526579}
.node-browser{display:grid;grid-template-columns:250px minmax(0,1fr);gap:32px}
.node-browser aside{border-right:1px solid var(--line);padding-right:24px}
.node-list{max-height:65vh;overflow:auto}
.node-choice{display:block;padding:10px 8px;margin-bottom:4px;text-decoration:none;border-radius:4px;color:var(--ink)}
.node-choice small{display:block;color:var(--muted)}
.node-choice[aria-current]{background:#e8f1f7;outline:1px solid #8ab6cf}
.node-content{min-width:0}.node-panel{min-width:0}
.finding{background:#fff;border:1px solid var(--line);border-left:4px solid var(--fail);
border-radius:6px;padding:10px 14px;margin:10px 0;overflow-wrap:anywhere}
.finding.warn{border-left-color:#c98a00}
.finding .loc{color:var(--muted);font-size:12px;font-weight:600}
.snippet{white-space:pre-wrap;overflow-wrap:anywhere;margin:6px 0 0;padding:9px 11px;
background:#f6f2ea;border:1px solid #e4dccb;border-radius:4px;color:#4a3d1a;max-height:220px;overflow:auto}
.empty{padding:20px;background:#f1f7fa;color:var(--muted);border-radius:6px}
.outcome{display:flex;justify-content:space-between;gap:24px;flex-wrap:wrap;align-items:flex-start;padding:16px 18px;border:1px solid var(--line);border-radius:8px;background:#fbfdfe;margin-bottom:18px}.outcome h2{margin:0 0 6px}.outcome-badges .label{display:block;font-size:12px;color:var(--muted);margin-bottom:5px}.facts{display:flex;gap:22px 38px;flex-wrap:wrap;margin:20px 0 0;padding-top:18px;border-top:1px solid var(--line)}
.facts dt{font-size:13px;color:var(--muted)}.facts dd{margin:3px 0 0;font-weight:600;overflow-wrap:anywhere}
.filelink{display:inline-block;margin:3px 10px 3px 0}
footer{color:var(--muted);font-size:13px}
@media(max-width:900px){.node-browser{grid-template-columns:200px minmax(0,1fr);gap:20px}.node-browser aside{padding-right:16px}}
@media(max-width:700px){body{font-size:14px}.brandbar,header,main,footer{padding:16px}h1{font-size:29px}
.node-browser{grid-template-columns:1fr}.node-browser aside{border-right:0;border-bottom:1px solid var(--line);padding:0 0 16px}}
"""

JS = """
(function(){
  var tabs=[].slice.call(document.querySelectorAll('[role=tab]'));
  var panels=[].slice.call(document.querySelectorAll('.node-panel'));
  var tabpanels=[].slice.call(document.querySelectorAll('[role=tabpanel]'));
  function selectTab(t){
    tabs.forEach(function(x){var on=x===t;x.setAttribute('aria-selected',String(on));document.getElementById(x.getAttribute('aria-controls')).hidden=!on;});
  }
  function selectNode(id){
    panels.forEach(function(p){p.hidden=p.id!==id;});
    document.querySelectorAll('.node-choice').forEach(function(a){
      if(a.getAttribute('data-panel')===id)a.setAttribute('aria-current','true');else a.removeAttribute('aria-current');});
  }
  tabs.forEach(function(t){t.addEventListener('click',function(){selectTab(t);});});
  document.querySelectorAll('[data-panel]').forEach(function(l){
    l.addEventListener('click',function(ev){
      var id=l.getAttribute('data-panel');
      var t=document.getElementById('tab-nodes');if(t)selectTab(t);
      if(id.indexOf('node-')===0){selectNode(id);var h=document.getElementById(id);if(h)h.scrollIntoView({block:'start'});}
      ev.preventDefault();
    });
  });
  if(panels.length){selectNode(panels[0].id);}
  if(tabs.length){selectTab(tabs[0]);}
  var pf=document.getElementById('print-report');if(pf)pf.addEventListener('click',function(){window.print();});
})();
"""


def _esc(v):
    return html.escape("" if v is None else str(v))


def _badge(value):
    v = (value or "").upper()
    cls = {"PASS": "pass", "OK": "pass", "FAIL": "fail",
           "WARN": "warn", "WARNING": "warn"}.get(v, "info")
    return f'<span class="badge {cls}">{_esc(value)}</span>'


def _read(path):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return ""


def _analyze_node(ndir):
    """Return per-node summary: log files, findings, sensor summary."""
    logs = sorted(os.listdir(ndir)) if os.path.isdir(ndir) else []
    embedded, linked = [], []
    for name in logs:
        full = os.path.join(ndir, name)
        if not os.path.isfile(full):
            continue
        if name in LINK_LOGS:
            linked.append(name)
        elif name in EMBED_LOGS:
            embedded.append(name)
        else:
            linked.append(name)

    findings = []
    for name in embedded:
        full = os.path.join(ndir, name)
        for ln, line in enumerate(_read(full).splitlines(), 1):
            if FINDING_RE.search(line):
                sev = "FAIL" if re.search(r"fail|error|critical|fatal|panic|mce|abort", line, re.I) else "WARN"
                findings.append((name, ln, sev, line.strip()))

    # sensor summary: rows / ticks from the ### headers
    sensor_path = os.path.join(ndir, "sensor.txt")
    sensor_ticks = 0
    if os.path.isfile(sensor_path):
        sensor_ticks = _read(sensor_path).count("\n### ")
        if _read(sensor_path).startswith("### "):
            sensor_ticks += 1

    # resource summary: peak memory % and peak load from resource.log
    mem_peak = None
    load_peak = None
    res_path = os.path.join(ndir, "resource.log")
    if os.path.isfile(res_path):
        for line in _read(res_path).splitlines():
            m = re.search(r"mem:\s*used=\d+MB\s*/\s*total=\d+MB\s*\(([\d.]+)%\)", line)
            if m:
                try:
                    v = float(m.group(1))
                    mem_peak = v if mem_peak is None else max(mem_peak, v)
                except ValueError:
                    pass
            m = re.search(r"cpu:\s*load1=([\d.]+)", line)
            if m:
                try:
                    v = float(m.group(1))
                    load_peak = v if load_peak is None else max(load_peak, v)
                except ValueError:
                    pass
    return {
        "logs": logs,
        "embedded": embedded,
        "linked": linked,
        "findings": findings,
        "sensor_path": sensor_path if os.path.isfile(sensor_path) else None,
        "sensor_ticks": sensor_ticks,
        "mem_peak": mem_peak,
        "load_peak": load_peak,
    }


def build_report(run_dir, nodes):
    """nodes: list of (tag, os_ip, bmc_ip). Writes run_dir/report.html."""
    run_dir = os.path.abspath(run_dir)
    stamp = os.path.basename(run_dir.rstrip("/"))
    per_node = {}
    for tag, os_ip, bmc_ip in nodes:
        ndir = os.path.join(run_dir, tag)
        per_node[tag] = _analyze_node(ndir) if os.path.isdir(ndir) else \
            {"logs": [], "embedded": [], "linked": [], "findings": [],
             "sensor_path": None, "sensor_ticks": 0,
             "mem_peak": None, "load_peak": None}

    # ---- overview table -------------------------------------------------- #
    rows = []
    total_findings = 0
    for i, (tag, os_ip, bmc_ip) in enumerate(nodes):
        info = per_node[tag]
        nf = len(info["findings"])
        total_findings += nf
        status = "PASS" if nf == 0 else ("FAIL" if any(f[2] == "FAIL" for f in info["findings"]) else "WARN")
        mp = info.get("mem_peak")
        lp = info.get("load_peak")
        mem_txt = f"{mp:.1f}%" if mp is not None else "-"
        load_txt = f"{lp:.1f}" if lp is not None else "-"
        rows.append(
            f'<tr><td class="target"><a href="#node-{i}" data-panel="node-{i}">{_esc(tag)}</a>'
            f'<small>{_esc(os_ip)}</small></td>'
            f'<td>{_badge(status)}</td>'
            f'<td>{mem_txt}</td>'
            f'<td>{load_txt}</td>'
            f'<td>{info["sensor_ticks"]}</td>'
            f'<td>{nf}</td></tr>')
    overview_table = (
        '<div class="tablewrap"><table><thead><tr><th>Node</th><th>Status</th>'
        '<th>Peak mem</th><th>Peak load</th><th>Sensor samples</th><th>Findings</th></tr></thead>'
        f'<tbody>{"".join(rows)}</tbody></table></div>')

    # ---- findings tab ---------------------------------------------------- #
    finding_html = []
    for tag, os_ip, bmc_ip in nodes:
        info = per_node[tag]
        fs = info["findings"]
        finding_html.append(f'<h3>{_esc(tag)} <span class="muted">{_esc(os_ip)}</span> '
                            f'{_badge("FAIL" if any(f[2]=="FAIL" for f in fs) else ("WARN" if fs else "PASS"))} '
                            f'<span class="muted">{len(fs)} line(s)</span></h3>')
        if not fs:
            finding_html.append('<p class="empty">No FAIL / WARN lines found in device logs.</p>')
            continue
        for fname, ln, sev, line in fs:
            cls = "finding" if sev == "FAIL" else "finding warn"
            finding_html.append(
                f'<div class="{cls}"><div class="loc">{_badge(sev)} {_esc(fname)}:{ln}</div>'
                f'<pre class="snippet">{_esc(line)}</pre></div>')
    findings_panel_body = "".join(finding_html) or '<p class="empty">No findings.</p>'

    # ---- per-node panels ------------------------------------------------- #
    choices, panels = [], []
    for i, (tag, os_ip, bmc_ip) in enumerate(nodes):
        info = per_node[tag]
        nf = len(info["findings"])
        status = "PASS" if nf == 0 else ("FAIL" if any(f[2] == "FAIL" for f in info["findings"]) else "WARN")
        choices.append(f'<a class="node-choice" href="#node-{i}" data-panel="node-{i}">{_esc(tag)} '
                       f'{_badge(status)}<small>{_esc(os_ip)}</small></a>')

        # embedded device logs (collapsible)
        emb = []
        for name in info["embedded"]:
            body = _read(os.path.join(run_dir, tag, name))
            emb.append(
                f'<details><summary>{_esc(name)} <span class="muted">'
                f'({len(body.splitlines())} lines)</span></summary>'
                f'<div class="detail-body"><pre>{_esc(body)}</pre></div></details>')
        emb_html = "".join(emb) or '<p class="empty">No device logs collected.</p>'

        # linked evidence
        links = []
        for name in info["linked"]:
            rel = f"{tag}/{name}"
            links.append(f'<a class="filelink" href="{_esc(rel)}">{_esc(name)}</a>')
        links_html = "".join(links) or '<span class="muted">none</span>'

        mp = info.get("mem_peak")
        lp = info.get("load_peak")
        facts = (
            '<dl class="facts">'
            f'<div><dt>OS IP</dt><dd>{_esc(os_ip)}</dd></div>'
            f'<div><dt>BMC IP</dt><dd>{_esc(bmc_ip)}</dd></div>'
            f'<div><dt>Peak memory</dt><dd>{("%.1f%%" % mp) if mp is not None else "-"}</dd></div>'
            f'<div><dt>Peak load1</dt><dd>{("%.1f" % lp) if lp is not None else "-"}</dd></div>'
            f'<div><dt>Sensor samples</dt><dd>{info["sensor_ticks"]}</dd></div>'
            f'<div><dt>Findings</dt><dd>{nf}</dd></div>'
            '</dl>')

        # node-local findings (same data as Findings tab, scoped)
        nf_html = []
        for fname, ln, sev, line in info["findings"]:
            cls = "finding" if sev == "FAIL" else "finding warn"
            nf_html.append(f'<div class="{cls}"><div class="loc">{_badge(sev)} {_esc(fname)}:{ln}</div>'
                           f'<pre class="snippet">{_esc(line)}</pre></div>')
        nf_block = "".join(nf_html) or '<p class="empty">No FAIL / WARN lines.</p>'

        panels.append(
            f'<article class="node-panel" id="node-{i}" hidden><h2>{_esc(tag)} {_badge(status)}</h2>'
            f'{facts}'
            f'<h3>Findings</h3>{nf_block}'
            f'<h3>Device logs (embedded)</h3>{emb_html}'
            f'<h3>Other evidence (linked)</h3><div>{links_html}</div>'
            f'</article>')

    # ---- assemble -------------------------------------------------------- #
    tabs = [("overview", f"Overview"), ("nodes", f"Nodes ({len(nodes)})"),
            ("findings", f"Findings ({total_findings})")]
    tab_html = "".join(
        f'<button id="tab-{k}" role="tab" aria-controls="{k}" '
        f'aria-selected="{str(n==0).lower()}">{_esc(lbl)}</button>'
        for n, (k, lbl) in enumerate(tabs))

    # overall verdict across all nodes
    n_fail = sum(1 for tag, _o, _b in nodes
                 if any(f[2] == "FAIL" for f in per_node[tag]["findings"]))
    n_warn = sum(1 for tag, _o, _b in nodes
                 if per_node[tag]["findings"] and
                 not any(f[2] == "FAIL" for f in per_node[tag]["findings"]))
    if total_findings == 0:
        overall, overall_note = "PASS", f"all {len(nodes)} nodes clean — no FAIL/WARN lines"
    elif n_fail:
        overall, overall_note = "FAIL", f"{n_fail} node(s) with FAIL, {total_findings} finding(s) total"
    else:
        overall, overall_note = "WARN", f"{n_warn} node(s) with WARN, {total_findings} finding(s) total"
    overall_banner = (
        '<div class="outcome"><div><h2>Run result</h2>'
        f'<p class="muted">{_esc(overall_note)}</p></div>'
        f'<div class="outcome-badges"><span class="label">Overall</span>'
        f'{_badge(overall)}</div></div>')
    overview = (
        f'<section role="tabpanel" id="overview" aria-labelledby="tab-overview">'
        f'<div class="sheet"><h2>Run summary</h2>{overall_banner}'
        f'<p class="muted">All {len(nodes)} nodes. Status reflects FAIL/WARN lines scraped '
        f'from the collected logs. Sensor/resource evidence is linked per node.</p>'
        f'{overview_table}</div>'
        f'<div class="sheet"><h2>Findings</h2>{findings_panel_body}</div>'
        f'</section>')

    nodes_panel = (
        f'<section role="tabpanel" id="nodes" aria-labelledby="tab-nodes" hidden>'
        f'<div class="sheet node-browser"><aside><h2>Nodes</h2>'
        f'<nav class="node-list">{ "".join(choices) }</nav></aside>'
        f'<div class="node-content">{"".join(panels)}</div></div></section>')

    findings_panel = (
        f'<section role="tabpanel" id="findings" aria-labelledby="tab-findings" hidden>'
        f'<div class="sheet"><h2>All findings</h2>'
        f'<p class="muted">Every FAIL / WARN / CRITICAL / ERROR line, by node and log file.</p>'
        f'{findings_panel_body}</div></section>')

    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Fullstress {_esc(stamp)}</title>
<style>{CSS}</style></head><body>
<div class="brandbar"><div class="brand">Neutrino / Vera L10 <small>Full-stress run {_esc(stamp)}</small></div></div>
<header><button id="print-report">Print</button><h1>Full-stress report</h1>
<div class="runline"><code>{_esc(stamp)}</code><span class="muted">{len(nodes)} nodes</span></div>
<nav class="tabs" role="tablist">{tab_html}</nav></header>
<main>{overview}{nodes_panel}{findings_panel}</main>
<footer><p>Generated offline. Keep this report with its run directory so links resolve.</p></footer>
<script>{JS}</script></body></html>"""
