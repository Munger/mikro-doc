#!/usr/bin/env python3
## @file mikro-doc.py
##
## @brief Generate a self-contained offline HTML API reference for RouterOS.
##
## Discovers the full RouterOS API tree from a live router via the REST
## interface, then produces a standalone HTML document with search,
## sidebar navigation, pagination and example curl commands — all usable
## directly from the file:// protocol without a web server.
##
## BIG FAT WARNING: This tool will query your router (potentially hundreds of times) to discover the API structure.
## It is designed to be safe and non-intrusive, but the documentation it generates will include actual configuration
## information from your router (e.g. interface names, user names). Do NOT share the generated documentation publicy.
## It is for your eyes only. If you passed --store-credentials, the credentials will be stored in plaintext in the
## output JSON file for future use. You have been warned.
##
## Usage:  mikro-doc --host H --user U --pass P [--store-credentials] [--output-dir DIR] [--endpoints] [--no-docs]
##         mikro-doc --schema PATH [--output-dir DIR] [--endpoints] [--no-docs]
##         mikro-doc PATH [--output-dir DIR] [--endpoints] [--no-docs]
##         mikro-doc --no-fetch [--host H|--schema PATH] [options]
##
## @copyright Copyright (c) 2026 Tim Hosking
## @see https://github.com/munger
## @par Licence: MIT

import argparse
import json
import logging
import os
import random
import re
import sys
import threading
import time
import tempfile
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

try:
    import requests
    from requests.auth import HTTPBasicAuth
    from requests.adapters import HTTPAdapter
except ImportError:
    print("mikro-doc requires 'requests'. Install with:  pip install requests", file=sys.stderr)
    sys.exit(1)

TOOL_DIR = Path(__file__).resolve().parent
CHECKPOINT_INTERVAL = 50
CPU_THRESHOLD = 60
MAX_WORKERS = 256
INITIAL_WORKERS = 2

log = logging.getLogger("mikro-doc")


class RouterOSClient:
    """Synchronous RouterOS REST API client with connection pooling."""

    def __init__(self, host, username, password, port=80, use_ssl=False):
        proto = "https" if use_ssl else "http"
        self.base = f"{proto}://{host}:{port}/rest"
        self.auth = HTTPBasicAuth(username, password)
        self.session = requests.Session()
        self.session.auth = self.auth
        self.session.verify = False
        adapter = HTTPAdapter(pool_connections=1024, pool_maxsize=1024)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)

    def inspect(self, request, path=""):
        resp = self.session.post(f"{self.base}/console/inspect", json={"request": request, "path": path})
        resp.raise_for_status()
        return resp.json()

    def get(self, path, command):
        resp = self.session.get(f"{self.base}{path}/{command}")
        resp.raise_for_status()
        return resp.json()

    def close(self):
        self.session.close()


def flush_output(output_path, data):
    """Atomically write JSON to a file via a temporary file and rename."""
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(output_path), suffix=".json")
    try:
        os.close(fd)
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, output_path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def format_duration(seconds):
    """Format a duration in seconds as m:ss."""
    m = int(seconds // 60)
    s = int(seconds % 60)
    return f"{m}:{s:02d}"


# ── Discovery ──────────────────────────────────────────────────────

def _get_first(r):
    if isinstance(r, dict):
        return r
    if isinstance(r, list) and r:
        return r[0]
    return None


def collect_router_info(client):
    """Fetch router identity and hardware information from the live device."""
    info = {}
    for path, cmd, keys in [
        ("/system", "resource", ("version", "board-name", "cpu", "cpu-frequency", "total-memory", "architecture-name", "uptime")),
        ("/system", "routerboard", ("model", "routerboard", "serial-number")),
    ]:
        try:
            r = client.get(path, cmd)
            first = _get_first(r)
            if first:
                for k in keys:
                    v = first.get(k)
                    if v:
                        info.setdefault(k, v)
        except Exception:
            pass
    try:
        r = client.get("/system", "identity")
        first = _get_first(r)
        if first:
            info["identity"] = first.get("name", "")
    except Exception:
        pass
    return info


def get_router_cpu(client):
    """Fetch the current CPU load percentage from the router."""
    try:
        r = client.get("/system", "resource")
        if isinstance(r, dict):
            return int(r.get("cpu-load", 0))
        if isinstance(r, list) and r:
            return int(r[0].get("cpu-load", 0))
    except Exception:
        pass
    return 0


def process_node(client, path, node):
    """Discover a single node: fetch children + syntax, build subtree.

    Returns list of (child_path, child_node, is_cmd) tuples for queuing.
    Mutates node in-place with discovered structure.
    """
    children_to_queue = []

    def fetch_children(p):
        try:
            return client.inspect("self", p)
        except Exception:
            return []

    def fetch_syntax(p):
        try:
            return client.inspect("syntax", p)
        except Exception:
            return []

    is_cmd = node.get("_is_cmd", False)
    items = fetch_children(path)
    if not items:
        return children_to_queue

    syntax_items = fetch_syntax(path) if path and not is_cmd else []

    descriptions = {}
    for s in syntax_items:
        if s.get("nested") == "1":
            descriptions[s["symbol"]] = s.get("text", "")

    for item in items:
        name = item.get("name")
        node_type = item.get("node-type")
        item_type = item.get("type")

        if item_type == "self":
            node["node_type"] = node_type
            node["name"] = name
            continue

        if "children" not in node:
            node["children"] = {}

        child_path = f"{path},{name}" if path else name
        full_path = f"/{child_path.replace(',', '/')}"

        child = {"node_type": node_type}
        if name in descriptions:
            child["description"] = descriptions[name]

        if node_type in ("dir", "path"):
            child["path"] = full_path
            node["children"][name] = child
            children_to_queue.append((child_path, child, False))

        elif node_type == "cmd":
            child["path"] = full_path
            child["_is_cmd"] = True
            node["children"][name] = child
            children_to_queue.append((child_path, child, True))

        elif node_type == "arg":
            node["children"][name] = child
            try:
                arg_syntax = fetch_syntax(child_path)
                if arg_syntax:
                    child["children"] = {}
                    for s in arg_syntax:
                        child["children"][f"syntax-{s.get('nested', '0')}"] = {
                            "node_type": "syntax",
                            "nested": s.get("nested", "0"),
                            "symbol": s.get("symbol", ""),
                            "symbol_type": s.get("symbol-type", ""),
                            "text": s.get("text", ""),
                            "type": s.get("type", ""),
                            "nonorm": s.get("nonorm", ""),
                        }
                    for s in arg_syntax:
                        if s.get("symbol") == "ValueName":
                            for s2 in arg_syntax:
                                if s2.get("nested") == "1" and s2.get("text"):
                                    child["completions"] = [c.strip() for c in s2["text"].split("|")]
                                    break
                            break
                comps = fetch_syntax(child_path.replace(",", "/"))
                if comps:
                    try:
                        comps_data = client.inspect("completion", child_path)
                        if comps_data:
                            child["completion_details"] = [
                                {
                                    "completion": c.get("completion", ""),
                                    "style": c.get("style", ""),
                                    "text": c.get("text", ""),
                                    "show": c.get("show", ""),
                                    "preference": c.get("preference", ""),
                                }
                                for c in comps_data
                            ]
                    except Exception:
                        pass
            except Exception:
                pass

    return children_to_queue


def discover(client, output_path=None, initial_workers=INITIAL_WORKERS, hostname=None):
    """Walk the entire RouterOS API tree with adaptive parallel fetching."""

    discovered_ts = time.strftime("%Y-%m-%dT%H:%M:%S")

    log.info("Collecting router info...")
    router_info = collect_router_info(client)
    router_info.setdefault("identity", hostname or "")

    root = {"node_type": "path", "path": "/", "children": {}}
    queue = [("", root, False)]
    completed = 0
    workers = initial_workers
    last_good = workers
    last_checkpoint = 0
    cpu_samples = []

    def do_checkpoint():
        nonlocal last_checkpoint
        if output_path:
            flush_output(output_path, {
                "discovered": discovered_ts,
                "router": router_info,
                "schema_workers": workers,
                "api": root,
            })
            last_checkpoint = completed

    log.info("Starting schema discovery (initial workers=%d)...", workers)

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        while queue:
            batch = queue[:workers]
            queue = queue[workers:]

            futures = {}
            for path, node, _ in batch:
                future = pool.submit(process_node, client, path, node)
                futures[future] = len(node.get("children", {}))

            for future in as_completed(futures):
                try:
                    new_children = future.result()
                    queue.extend(new_children)
                except Exception as e:
                    log.warning("Node error: %s", e)
                completed += 1

            log.info("  %d endpoints discovered", completed)

            if completed % 5 == 0 and completed > 0:
                cpu = get_router_cpu(client)
                cpu_samples.append(cpu)
                avg_cpu = sum(cpu_samples[-3:]) / len(cpu_samples[-3:]) if cpu_samples else cpu
                old_workers = workers
                if avg_cpu < CPU_THRESHOLD:
                    last_good = workers
                    workers = min(workers * 2, MAX_WORKERS)
                elif workers > 1:
                    workers = max((last_good + workers) // 2, 1)
                if workers != old_workers:
                    log.info("  CPU: %.0f%% | Workers: %d\u2192%d", avg_cpu, old_workers, workers)

            if output_path and completed - last_checkpoint >= CHECKPOINT_INTERVAL:
                do_checkpoint()

    optimal = workers
    log.info("Discovered %d endpoints in %s using %d workers", completed, format_duration(time.time() - time.mktime(time.strptime(discovered_ts, "%Y-%m-%dT%H:%M:%S"))), optimal)

    return root, router_info, discovered_ts, optimal, completed


# ── Doc generation ─────────────────────────────────────────────────

def _resolve(pname, ptype, type_map, cmd_path=""):
    """Look up a parameter name or type in the type-hints map, returning (value, matched_key)."""
    if not type_map:
        return None, None
    short = pname.rsplit(".", 1)[-1] if pname else ""
    for key in (pname, short, ptype):
        if key in type_map:
            val = type_map[key]
            if isinstance(val, dict):
                result = val.get("/" + cmd_path.rstrip("/")) or val.get("_")
                if result:
                    return result, key
            else:
                return val, key
    return None, None


def _resolve_magic(pname, ptype, type_map, cmd_path=""):
    """Check whether a parameter resolves to _bool_ or _time_ for special rendering."""
    val, _ = _resolve(pname, ptype, type_map, cmd_path)
    return val if val in ("_bool_", "_time_") else ""


def param_example(ptype, pvalues="", pname="", type_map=None, hint="", cmd_path=""):
    """Generate a plausible example value for a parameter using type hints or heuristics."""
    if pvalues and "|" in pvalues:
        vals = [v.strip() for v in pvalues.split("|")]
        vals = [v for v in vals if v and not any(c in v for c in "[(:.") and v != "-"]
        if vals:
            return random.choice(vals)
    if pname == "numbers":
        return random.choice(["0", "0,2,5", "0-5", "0,2-5,10"])
    if pname == "number":
        return random.choice(["0", "3"])
    val, _ = _resolve(pname, ptype, type_map, cmd_path)
    if val:
        if val == "_bool_":
            return random.choice(["yes", "no"])
        if val == "_time_":
            return random.choice(["1m", "30s", "5m", "1h", "00:00:30"])
        if val == "_password_":
            return random.choice(["P@ssw0rd!", "Secret#2026", "Admin_123!", "R0uter#O$!"])
        return val
    if pname:
        display = pname.rsplit(".", 1)[-1]
        return f"<{display}>"
    return "example"


def parse_param(syntax_items):
    """Extract the type name and acceptable pipe-separated values from RouterOS syntax items."""
    type_name = ""
    values_text = ""
    for s in syntax_items:
        nest = s.get("nested", "0")
        sym = s.get("symbol", "")
        txt = s.get("text", "").strip()
        st = s.get("symbol_type", "")
        if st == "definition" and sym:
            type_name = sym
            if txt:
                return sym, clean_values(txt)
        elif st == "definition" and not sym and txt and "|" in txt and not values_text:
            values_text = clean_values(txt)
        elif st == "explanation" and sym and not type_name:
            type_name = sym
        elif st == "explanation" and not type_name and txt:
            type_name = txt
    if type_name and values_text:
        return type_name, values_text
    if type_name:
        return type_name, ""
    return "str", ""


def parse_syntax_context(syntax_items):
    """Extract hint text, description, and completion string from RouterOS syntax items."""
    hint = ""
    desc_parts = []
    comp = ""
    for s in syntax_items:
        nest = s.get("nested", "0")
        sym = s.get("symbol", "")
        txt = s.get("text", "").strip()
        st = s.get("symbol_type", "")
        if nest == "0" and st == "definition" and sym and txt:
            hint = txt
        elif st == "definition" and not sym and txt and "|" in txt:
            comp = txt
        elif st == "explanation" and txt:
            desc_parts.append(txt)
    return hint, "; ".join(desc_parts) if desc_parts else "", comp


def clean_values(txt):
    txt = re.sub(r'\[,\w+\*\]$', '', txt).strip()
    txt = txt.strip("|").strip()
    return txt


def clean_path(base, name):
    return f"{base.rstrip('/')}/{name}"


def generate_markdown(api, type_map):
    """Generate Markdown documentation from the discovered API tree."""
    lines = ["# RouterOS API Reference\n"]

    def process(node, current_path, depth=1):
        children = node.get("children", {})
        heading_char = "#" * min(depth + 1, 6)

        cmd_nodes = {n: c for n, c in children.items() if c.get("node_type") == "cmd"}
        sub_nodes = {n: c for n, c in children.items() if c.get("node_type") in ("path", "dir")}

        if cmd_nodes and current_path:
            lines.append(f"\n## `{current_path}`\n")
            if node.get("description"):
                lines.append(f"{node['description']}\n")

        for name, child in cmd_nodes.items():
            method = "GET" if name in ("print", "get") else "POST"
            cmd_path = clean_path(current_path, name)
            desc = child.get("description", "")

            lines.append(f"\n## `{cmd_path}`\n")
            lines.append(f"### {method}\n")
            if desc:
                lines.append(f"{desc}\n")

            params = []
            for aname, achild in child.get("children", {}).items():
                if achild.get("node_type") != "arg":
                    continue
                syntax = list(achild.get("children", {}).values())
                ptype, pvalues = parse_param(syntax) if syntax else ("str", "")
                hint, desc_text, comps = parse_syntax_context(syntax) if syntax else ("", "", "")
                params.append((aname, ptype, pvalues, hint, desc_text, comps))

            if params:
                grouped = defaultdict(list)
                flat = []
                for pname, ptype, pvalues, hint, desc_text, comps in params:
                    if '.' in pname:
                        prefix = pname.split('.')[0]
                        grouped[prefix].append((pname, ptype, pvalues, hint, desc_text, comps))
                    else:
                        flat.append((pname, ptype, pvalues, hint, desc_text, comps))
                parent_set = {p for p, _, _, _, _, _ in flat}
                active_groups = {}
                orphaned = []
                for prefix, entries in grouped.items():
                    if prefix in parent_set or len(entries) >= 2:
                        active_groups[prefix] = entries
                    else:
                        orphaned.extend(entries)
                flat.extend(orphaned)

                lines.append("\n| Param | Type | Values |")
                lines.append("|-------|------|--------|")
                for pname, ptype, pvalues, hint, desc_text, comps in flat:
                    if pname in active_groups:
                        continue
                    ptype_display = ptype.replace("|", "&#124;")
                    if _resolve_magic(pname, ptype, type_map) == "_bool_":
                        pvalues_display = "yes &#124; no"
                    else:
                        pvalues_display = pvalues.replace("|", "&#124;") if pvalues else (hint.replace("|", "&#124;") if hint else (desc_text.replace("|", "&#124;") if desc_text else ""))
                    if not pvalues_display or pvalues_display == "...":
                        if pname == "numbers":
                            pvalues_display = f"0, 0,2,5, 0-5 - Items to {name}"
                        elif pname == "number":
                            pvalues_display = f"Index of item to {name}"
                        elif ptype == "str":
                            pvalues_display = "string value"
                    lines.append(f"| {pname} | {ptype_display} | {pvalues_display} |")

                for parent in sorted(active_groups):
                    if parent in parent_set:
                        ptype = next((t for n, t, _, _, _, _ in flat if n == parent), "")
                        ptype_display = ptype.replace("|", "&#124;")
                        lines.append(f"\n**{parent}** — _{ptype_display}_")
                    else:
                        lines.append(f"\n**{parent}**")
                    lines.append("\n| Sub-param | Type | Values |")
                    lines.append("|-----------|------|--------|")
                    for cname, ctype, cvalues, hint, desc_text, comps in sorted(active_groups[parent]):
                        ctype_display = ctype.replace("|", "&#124;")
                        cshort = cname.split(".", 1)[-1]
                        if _resolve_magic(cname, ctype, type_map) == "_bool_":
                            cvalues_display = "yes &#124; no"
                        else:
                            cvalues_display = cvalues.replace("|", "&#124;") if cvalues else (hint.replace("|", "&#124;") if hint else (desc_text.replace("|", "&#124;") if desc_text else ""))
                        if not cvalues_display or cvalues_display == "...":
                            if cshort == "numbers":
                                cvalues_display = f"0, 0,2,5, 0-5 - Items to {name}"
                            elif cshort == "number":
                                cvalues_display = f"Index of item to {name}"
                            elif ctype == "str":
                                cvalues_display = "string value"
                        display = cname[len(parent) + 1:]
                        lines.append(f"| {display} | {ctype_display} | {cvalues_display} |")

            ex_params = {}
            for pname, ptype, pvalues, hint, desc_text, comps in params:
                example = param_example(ptype, pvalues, pname, type_map, hint, cmd_path)
                ex_params[pname] = example
            body = json.dumps(ex_params, indent=2) if ex_params else "{}"
            if method == "GET":
                if ex_params:
                    qs = "&".join(f"{k}={v}" for k, v in ex_params.items())
                    url = f"curl -X GET 'http://router/rest{cmd_path}?{qs}'"
                    if len(url) > 100:
                        params_list = [f"  -d '{k}={v}'" for k, v in ex_params.items()]
                        lines.append(f"\n```bash\ncurl -X GET -G 'http://router/rest{cmd_path}' \\\n" + " \\\n".join(params_list) + "\n```\n")
                    else:
                        lines.append(f"\n```bash\n{url}\n```\n")
                else:
                    lines.append(f"\n```bash\ncurl -X GET 'http://router/rest{cmd_path}'\n```\n")
            else:
                lines.append(f"\n```bash\ncurl -X POST 'http://router/rest{cmd_path}' \\\n  -d '{body}'\n```\n")

        for name, child in sub_nodes.items():
            sub_path = clean_path(current_path, name) if current_path else f"/{name}"
            process(child, sub_path, depth + 1)

    process(api, "", 1)
    return "\n".join(lines)


# ── HTML generation (from GenAPIDocsSite.py) ───────────────────────

def esc(s):
    return s.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;').replace('"', '&quot;')


def fmt_inline(s):
    parts = []
    for i, chunk in enumerate(s.split('`')):
        parts.append(esc(chunk) if i % 2 == 0 else '<code>' + esc(chunk) + '</code>')
    return ''.join(parts)


def md_to_html(md_text):
    """Convert a subset of Markdown (headings, tables, code blocks) to HTML."""
    parts = []
    in_table = False
    in_code = False
    table_lines = []
    code_buf = []
    for line in md_text.split('\n') + ['']:
        s = line.strip()
        if not in_code and s.startswith('```'):
            in_code = True
            code_buf = []
            continue
        if in_code and s.startswith('```'):
            in_code = False
            parts.append('<pre><code>' + esc('\n'.join(code_buf)) + '</code></pre>')
            code_buf = []
            continue
        if in_code:
            code_buf.append(line)
            continue
        if s.startswith('|') and s.endswith('|'):
            if not in_table:
                in_table = True
                table_lines = []
            table_lines.append(line)
            continue
        if in_table and s == '':
            in_table = False
            rows = []
            for tl in table_lines:
                cells = [esc(c.strip().replace('&#124;', '|')) for c in tl.strip('|').split('|')]
                rows.append(cells)
            if len(rows) >= 2:
                t = '<table><thead><tr>' + ''.join('<th>' + c + '</th>' for c in rows[0]) + '</tr></thead><tbody>'
                for r in rows[1:]:
                    if r == rows[1] and all(x == '---' or x.startswith('-') for x in r):
                        continue
                    t += '<tr>' + ''.join('<td>' + c + '</td>' for c in r) + '</tr>'
                t += '</tbody></table>'
                parts.append(t)
            table_lines = []
            continue
        if s.startswith('#'):
            lvl = len(line.split()[0])
            new_lvl = min(lvl, 6)
            parts.append(f'<h{new_lvl}>{fmt_inline(s.lstrip("#").strip())}</h{new_lvl}>')
        elif s:
            parts.append('<p>' + fmt_inline(s) + '</p>')
    return ''.join(parts)


def parse_sections(md_text):
    """Parse Markdown text into structured sections keyed by API path."""
    lines = md_text.split('\n')
    sections = []
    for i, line in enumerate(lines):
        if line.startswith('## `/'):
            path = line.strip('#` ').strip()
            if not path.startswith('/'):
                path = '/' + path
            sections.append({'path': path, 'start': i, 'method': '', 'description': '', 'params': []})
    for idx, sec in enumerate(sections):
        start = sec['start']
        end = sections[idx + 1]['start'] if idx + 1 < len(sections) else len(lines)
        sec['lines'] = lines[start:end]
        in_desc = False
        in_code = False
        for j in range(start, end):
            s = lines[j].strip()
            if s == '```':
                in_code = not in_code
                continue
            if in_code:
                continue
            if s.startswith('### '):
                sec['method'] = s[4:].strip()
                in_desc = True
            elif s.startswith('|') and s.endswith('|') and '---' not in s:
                cells = [c.strip() for c in s.strip('|').split('|')]
                if len(cells) >= 1 and cells[0] not in ('Param', ''):
                    sec['params'].append(cells[0])
                in_desc = False
            elif in_desc and s and not s.startswith('|'):
                sec['description'] = s
                in_desc = False
        sec.pop('start', None)
    return sections


def build_nav_tree(sections):
    """Build a hierarchical navigation tree from parsed sections."""
    tree = {}
    for s in sections:
        parts = [p for p in s['path'].strip('/').split('/') if p]
        node = tree
        for p in parts:
            node = node.setdefault(p, {})
        node['__section__'] = s
    return tree


def build_sidebar_html(tree):
    """Generate the sidebar HTML with collapsible groups and a tree view."""
    groups = {}
    commands = {}
    for key, val in tree.items():
        if key.startswith('__'):
            continue
        children = {k: v for k, v in val.items() if not k.startswith('__')}
        if children:
            groups[key] = val
        else:
            commands[key] = val

    def build_subtree(node):
        parts = ['<ul class="tree">']
        for key in sorted(node.keys()):
            if key.startswith('__'):
                continue
            val = node[key]
            sec = val.get('__section__')
            children = {k: v for k, v in val.items() if not k.startswith('__')}
            has_children = bool(children)
            parts.append('<li>')
            if sec and sec.get('method'):
                mc = 'get' if sec['method'].lower() == 'get' else 'post'
                parts.append(f'<span class="method {mc}">{sec["method"]}</span> '
                            f'<a href="javascript:show(\'{sec["path"]}\')">{esc(key)}</a>')
            elif sec:
                parts.append(f'<a href="javascript:show(\'{sec["path"]}\')">{esc(key)}</a>')
            else:
                parts.append(f'<span class="dir">{esc(key)}</span>')
            if has_children:
                parts.append(build_subtree(children))
            parts.append('</li>')
        parts.append('</ul>')
        return '\n'.join(parts)

    out = []
    out.append('<div class="section-wrap" id="section-endpoints">')
    out.append('<div class="section-header" onclick="toggleSection(\'endpoints\')"><span class="arrow">&#9660;</span> ENDPOINTS</div>')
    out.append('<div class="section-body">')
    for key in sorted(groups):
        val = groups[key]
        sec = val.get('__section__')
        children = {k: v for k, v in val.items() if not k.startswith('__')}
        tree_parts = ['<ul class="tree">', '<li>']
        if sec:
            tree_parts.append(f'<a href="javascript:show(\'{sec["path"]}\')">{esc(key)}</a>')
        else:
            tree_parts.append(f'<span class="dir">{esc(key)}</span>')
        tree_parts.append(build_subtree(children))
        tree_parts.extend(['</li>', '</ul>'])
        out.append('\n'.join(tree_parts))
    out.append('</div></div>')

    if commands:
        out.append('<div class="section-divider"></div>')
        out.append('<div class="section-wrap" id="section-utilities">')
        out.append('<div class="section-header" onclick="toggleSection(\'utilities\')"><span class="arrow">&#9660;</span> UTILITIES</div>')
        out.append('<div class="section-body"><ul class="tree">')
        for key in sorted(commands):
            val = commands[key]
            sec = val.get('__section__')
            out.append('<li>')
            if sec and sec.get('method'):
                mc = 'get' if sec['method'].lower() == 'get' else 'post'
                out.append(f'<span class="method {mc}">{sec["method"]}</span> '
                          f'<a href="javascript:show(\'{sec["path"]}\')">{esc(key)}</a>')
            elif sec:
                out.append(f'<a href="javascript:show(\'{sec["path"]}\')">{esc(key)}</a>')
            else:
                out.append(f'<span class="dir">{esc(key)}</span>')
            out.append('</li>')
        out.append('</ul></div></div>')

    return '\n'.join(out)


def fmt_ram(v):
    """Format a byte count as a human-readable RAM string."""
    try:
        b = int(v)
        for u in ('B','KB','MB','GB','TB'):
            if b < 1024:
                return f"{b} {u}"
            b //= 1024
        return f"{b} PB"
    except (ValueError, TypeError):
        return str(v)

def fmt_uptime(v):
    """Format a RouterOS uptime string (e.g. 7w6d4h4m13s) as human-readable."""
    s = str(v)
    # RouterOS format: 7w6d4h4m13s, or just 4h4m13s, etc.
    parts = {'w':0,'d':0,'h':0,'m':0,'s':0}
    for m in re.finditer(r'(\d+)([wdhms])', s):
        parts[m.group(2)] = int(m.group(1))
    out = []
    if parts['d'] or parts['w']:
        total = parts['d'] + parts['w'] * 7
        out.append(f"{total}d")
    if parts['h']:
        out.append(f"{parts['h']}h")
    if parts['m']:
        out.append(f"{parts['m']}m")
    if parts['s']:
        out.append(f"{parts['s']}s")
    return ' '.join(out) if out else s

def build_banner_html(info):
    """Build the sticky router-info banner HTML shown at the top of the page."""
    left_keys = ['_host', '_make', 'board-name', 'version']
    left_labels = {'_host': 'Router', '_make': 'Make', 'board-name': 'Model', 'version': 'Firmware'}
    right_keys = ['architecture-name', 'cpu', 'cpu-frequency', 'total-memory', 'uptime', '_generated']
    right_labels = {'architecture-name': 'Arch', 'cpu': 'CPU', 'cpu-frequency': 'Clock',
                    'total-memory': 'RAM', 'uptime': 'Uptime', '_generated': 'Generated'}

    def item_html(key, label):
        val = info.get(key)
        if not val:
            return ''
        if key == 'total-memory':
            val = fmt_ram(val)
        elif key == 'uptime':
            val = fmt_uptime(val)
        elif key == 'cpu-frequency':
            val = str(val).rstrip('MHz').strip()
            if val.isdigit():
                val += ' MHz'
        elif key == '_generated':
            val = str(val).replace('T', ' ')
        return f'<span class="rb-item"><span class="rb-label">{label}:</span><span class="rb-value">{esc(str(val))}</span></span>'

    left_html = ''.join(item_html(k, left_labels[k]) for k in left_keys)
    right_html = ''.join(item_html(k, right_labels[k]) for k in right_keys)
    if not left_html and not right_html:
        return ''

    repo = 'https://github.com/Munger/mikro-doc'
    warn = '<span class="rb-warn">Exposes network configuration<br>DO NOT SHARE</span>'
    credit = (f'<div class="rb-credit">'
              f'<a href="{repo}" target="_blank"><img src="https://avatars.githubusercontent.com/Munger?s=96" '
              f'width="96" height="96" alt="avatar" style="border-radius:6px;display:block"></a>'
              f'<div class="rb-credit-text">&copy; 2026 <a href="{repo}" target="_blank">Tim Hosking</a><br>'
              f'<a href="{repo}/blob/main/LICENSE" target="_blank">MIT License</a></div></div>')

    return (f'<div id="router-banner">'
            f'<div id="banner-top"><div class="rb-group">{left_html}</div>'
            f'<div class="rb-group">{right_html}</div>'
            f'{warn}{credit}</div><nav id="banner-pagination"></nav></div>')


def escape_js(s):
    return s.replace('\\', '\\\\').replace("'", "\\'").replace('\n', '\\n').replace('</', r'<\/')


CSS = '''\
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,Helvetica,Arial,sans-serif;line-height:1.6;color:#222;background:#fff}
#layout{min-height:100vh}
#sidebar{position:fixed;left:0;top:0;width:300px;height:100vh;background:#f5f5f5;border-right:1px solid #ddd;padding:1em;font-size:.85em;display:flex;flex-direction:column;z-index:100}
#sidebar-top{flex-shrink:0}
#sidebar-tree{flex:1;overflow-y:auto;margin-top:.6em}
#sidebar h2{font-size:1em;margin-bottom:.5em}
#sidebar #search{width:100%;padding:.4em .6em;border:1px solid #ccc;border-radius:4px;font-size:.9em}
#search-wrap{position:relative;margin-bottom:.8em}
#search-results{position:absolute;top:100%;left:0;right:0;background:#fff;border:1px solid #ddd;border-radius:4px;max-height:60vh;overflow-y:auto;z-index:100;display:none;box-shadow:0 4px 12px rgba(0,0,0,.15)}
#search-results a{display:block;padding:.4em .6em;text-decoration:none;color:#333;border-bottom:1px solid #eee}
#search-results a:hover,#search-results a.selected{background:#e3f2fd}
#search-results .sr-method{display:inline-block;font-size:.7em;padding:0 .35em;border-radius:3px;font-weight:700;color:#fff;margin-right:.4em;min-width:2.4em;text-align:center}
#search-results .sr-path{font-weight:600;color:#1565c0}
#search-results .sr-desc{font-size:.85em;color:#888;margin-left:.4em}
#section-pills{display:flex;gap:.4em;margin-bottom:.6em}
#section-pills .pill{flex:1;text-align:center;font-size:.75em;text-transform:uppercase;padding:.35em;border:1px solid #ccc;border-radius:4px;cursor:pointer;color:#888;background:#fff;text-decoration:none;font-weight:600}
#section-pills .pill:hover{background:#e3f2fd;border-color:#1565c0;color:#1565c0}
.section-header{font-size:.75em;text-transform:uppercase;color:#888;padding:.35em .6em;cursor:pointer;user-select:none;display:flex;align-items:center;gap:.4em;font-weight:600}
.section-header:hover{color:#555}
.section-header .arrow{font-size:.6em;transition:transform .15s}
.section-body.collapsed{display:none}
.section-divider{height:1px;background:#ddd;margin:.5em .6em}
#content{margin-left:300px;padding:2em;max-width:900px;min-width:0}
#content h2{font-size:2.4em;margin:.3em 0 .15em}
#content h2:target{background:#fff8dc}
#content h3{font-size:1.2em;color:#666;margin:0 0 .5em}
#content h4{font-size:1.1em;margin:1em 0 .3em}
#content h5{font-size:1em;margin:1em 0 .2em;color:#666}
#content p{margin:.5em 0}
#content code{background:#f0f0f0;padding:.1em .3em;border-radius:3px;font-size:.9em}
#content pre{background:#f5f5f5;padding:1em;border-radius:4px;overflow-x:auto;margin:.5em 0;font-size:.9em}
#content table{border-collapse:collapse;width:100%;margin:.5em 0}
#content th,#content td{border:1px solid #ccc;padding:.3em .6em;text-align:left}
#content tbody td:nth-child(1),#content tbody td:nth-child(2){white-space:nowrap}
#content tbody td:nth-child(3){word-break:break-word;overflow-wrap:break-word}
#content th{background:#e8e8e8}
.section{display:none}
.section.active{display:block}
#banner-pagination{display:flex;justify-content:center;align-items:center;padding:.3em .5em;background:#fff;border-bottom:2px solid #90caf9;min-height:2.4em}
#banner-pagination .pagination{display:flex;gap:.3em;font-size:1.1em;align-items:center}
#banner-pagination .pagination a{display:inline-block;color:#1565c0;text-decoration:none;padding:.2em .7em;border:1px solid #ccc;border-radius:4px;cursor:pointer;background:#fff}
#banner-pagination .pagination a:hover{background:#e3f2fd}
.tree{list-style:none;padding-left:1.2em}
.tree>li{margin:.15em 0}
.tree .method{display:inline-block;font-size:.7em;padding:0 .35em;border-radius:3px;font-weight:700;color:#fff;min-width:2.4em;text-align:center}
.tree .method.get{background:#2e7d32}
.tree .method.post{background:#1565c0}
.tree a{color:#1565c0;text-decoration:none;cursor:pointer}
.tree a:hover{text-decoration:underline}
.tree .active>a{font-weight:700;color:#c62828}
.tree .dir{color:#888;cursor:default}
.method-sm{display:inline-block;font-size:.7em;padding:0 .35em;border-radius:3px;font-weight:700;color:#fff;background:#888}
.tree ul{display:none}
.tree .active ul{display:block}
.tree li:hover>ul{display:block}
.tree>li>ul{display:block}
#router-banner{position:sticky;top:0;z-index:50;margin-left:300px;display:flex;flex-direction:column;box-shadow:0 2px 8px rgba(0,0,0,.08)}
#banner-top{display:flex;gap:2em;align-items:flex-start;padding:.6em 1em;font-size:.82em;background:#e3f2fd;color:#222}
#banner-top .rb-group{display:flex;flex-direction:column;gap:1px}
#banner-top .rb-item{display:flex;gap:.3em;align-items:baseline}
#banner-top .rb-label{color:#1565c0;font-weight:600;text-transform:uppercase;font-size:.78em;min-width:5.5em}
#banner-top .rb-value{color:#222;font-weight:500}
#banner-top .rb-warn{color:#c62828;font-size:1.56em;font-weight:600;padding:.2em .6em;flex:1;text-align:center;align-self:center}
#banner-top .rb-credit{display:flex;flex-direction:column;align-items:center;justify-content:center;gap:2px;flex-shrink:0;align-self:stretch}
#banner-top .rb-credit-text{font-size:1.1em;text-align:center;line-height:1.4}
#banner-top .rb-credit-text a{color:#1565c0;text-decoration:none}
#banner-top .rb-credit-text a:hover{text-decoration:underline}
@media(max-width:768px){#sidebar{position:static;width:100%;height:auto;border-right:none;border-bottom:1px solid #ddd}#content{margin-left:0;padding:1em}#router-banner{margin-left:0}#banner-top{flex-direction:column;gap:.6em}#banner-top .rb-label{min-width:4em}}
'''


def build_sections_html(sections):
    """Convert all parsed sections into HTML content blocks and structured data."""
    all_html = []
    all_data = []
    for idx, s in enumerate(sections):
        path = s['path']
        html = md_to_html('\n'.join(s['lines']))
        is_endpoint = bool(s.get('method'))
        if not is_endpoint:
            children_paths = [x['path'] for x in sections
                             if x['path'] != path
                             and x['path'].startswith(path.rstrip('/') + '/')
                             and len([p for p in x['path'].strip('/').split('/') if p]) == len([p for p in path.strip('/').split('/') if p]) + 1]
            if children_paths:
                html += '<ul>'
                for c in children_paths:
                    c_sec = next((x for x in sections if x['path'] == c), None)
                    c_name = c.strip('/').split('/')[-1]
                    c_method = c_sec['method'] if c_sec else ''
                    if c_method:
                        html += f'<li><a href="javascript:show(\'{c}\')">/{c_name}/</a> <span class="method-sm">{c_method}</span></li>'
                    else:
                        html += f'<li><a href="javascript:show(\'{c}\')">/{c_name}/</a></li>'
                html += '</ul>'
        all_html.append(f'<div id="sec{"_"+path[1:].replace("/","_")}" class="section">\n{html}\n</div>')
        all_data.append({
            'path': path,
            'title': ' / '.join(path.strip('/').split('/')),
            'method': s.get('method', ''),
            'description': s.get('description', ''),
            'params': s.get('params', []),
        })
    return all_html, all_data


def generate_html(sections, router_info, output_path):
    """Generate the complete standalone HTML document with embedded CSS and JavaScript."""
    endpoints = [s for s in sections if s.get('method')]
    all_html, all_data = build_sections_html(sections)

    home_html = '''<div id="sec_home" class="section">
<h2>RouterOS API Reference</h2>
<p>Endpoints are grouped by API area in the sidebar. Expand a group to browse paths, or use the search box to filter across all endpoints.</p>
<p>Every endpoint shows its HTTP method, path, description, and parameters with types and default values. Click an endpoint's <strong>path heading</strong> to collapse/expand its details.</p>
</div>'''
    all_html.insert(0, home_html)
    all_data.insert(0, {'path': '/', 'title': 'RouterOS API Reference', 'method': '', 'description': '', 'params': []})

    sections_html = '\n'.join(all_html)
    sections_json = json.dumps(all_data).replace('</', r'<\/')
    router_info['_endpoints'] = len(endpoints)
    router_info['_groups'] = len(sections) - len(endpoints)
    banner_html = build_banner_html(router_info)
    sidebar_html = build_sidebar_html(build_nav_tree(sections))
    sidebar_js = escape_js(sidebar_html)

    html = f'''<!DOCTYPE html>
<html><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>RouterOS API Reference</title>
<style>
{CSS}
</style>
</head><body>
<div id="layout">
<nav id="sidebar">
<div id="sidebar-top">
<h2><a href="javascript:show('/')" style="color:inherit;text-decoration:none">RouterOS API</a></h2>
<div id="search-wrap">
<input type="text" id="search" placeholder="Search" autocomplete="off">
<div id="search-results"></div>
</div>
<div id="section-pills">
<a href="#" class="pill" onclick="showSection('endpoints');return false">Endpoints</a>
<a href="#" class="pill" onclick="showSection('utilities');return false">Utilities</a>
</div>
</div>
<div id="sidebar-tree">
{sidebar_html}
</div>
</nav>
{banner_html}
<main id="content">{sections_html}</main>
</div>
<script>
var sections = {sections_json};
var currentPath = '';
var navDepth = 0;
function toggleSection(name) {{
  var body = document.querySelector('#section-' + name + ' .section-body');
  var header = document.querySelector('#section-' + name + ' .section-header');
  if (!body) return;
  var collapsed = body.classList.toggle('collapsed');
  var arrow = header.querySelector('.arrow');
  if (arrow) arrow.style.transform = collapsed ? 'rotate(-90deg)' : '';
  try {{ localStorage.setItem('sec_' + name, collapsed ? '1' : ''); }} catch(e) {{}}
  updatePills();
}}
function showSection(name) {{
  var body = document.querySelector('#section-' + name + ' .section-body');
  var wrap = document.getElementById('section-' + name);
  if (!body || !wrap) return;
  if (body.classList.contains('collapsed')) toggleSection(name);
  wrap.scrollIntoView({{behavior:'smooth', block:'start'}});
}}
function updatePills() {{
  document.querySelectorAll('.pill').forEach(function(p) {{
    var name = p.getAttribute('onclick').match(/showSection\\('(\\w+)'\\)/);
    if (name) {{
      var body = document.querySelector('#section-' + name[1] + ' .section-body');
      if (body && body.classList.contains('collapsed')) p.style.opacity = '0.5';
      else p.style.opacity = '1';
    }}
  }});
}}
(function() {{
  try {{
    ['endpoints','utilities'].forEach(function(name) {{
      if (localStorage.getItem('sec_' + name)) toggleSection(name);
    }});
  }} catch(e) {{}}
}})();
function show(path, isNav) {{
  document.querySelectorAll('.section.active').forEach(function(el) {{ el.classList.remove('active'); }});
  document.querySelectorAll('.tree .active').forEach(function(el) {{ el.classList.remove('active'); }});
  currentPath = path;
  if (!isNav) {{
    var depth = path === '/' ? 0 : path.replace(/\\/$/,'').split('/').filter(Boolean).length;
    navDepth = depth;
  }}
  var id = path === '/' ? 'sec_home' : 'sec' + path.replace(/\\//g, '_');
  var el = document.getElementById(id);
  if (el) el.classList.add('active');
  document.querySelectorAll('.tree a').forEach(function(a) {{
    if (a.getAttribute('href') === "javascript:show('" + path + "')") {{
      var li = a.parentElement;
      li.classList.add('active');
      var p = li.parentElement;
      while (p && !p.classList.contains('sidebar')) {{
        if (p.tagName === 'LI') p.classList.add('active');
        p = p.parentElement;
      }}
    }}
  }});
  var sb = document.getElementById('sidebar-tree');
  if (path === '/') {{ if (sb) sb.scrollTop = 0; }} else {{
  var activeItems = document.querySelectorAll('.tree .active');
  var activeLi = activeItems.length > 0 ? activeItems[activeItems.length - 1] : null;
  if (sb && activeLi) {{
    var sbRect = sb.getBoundingClientRect();
    var liRect = activeLi.getBoundingClientRect();
    var visibleTop = liRect.top - sbRect.top;
    var targetTop = sb.clientHeight/2 - activeLi.offsetHeight/2;
    sb.scrollTop += visibleTop - targetTop;
  }}
  }}
  updateNav(path);
  window.scrollTo(0, 0);
}}
function depthOf(p) {{ return p === '/' ? 0 : p.split('/').filter(Boolean).length; }}
function updateNav(path) {{
  var idx = -1;
  for (var i = 0; i < sections.length; i++) {{
    if (sections[i].path === path) {{ idx = i; break; }}
  }}
  if (idx < 0) return;
  var s = sections[idx];
  if (path === '/') {{
    document.getElementById('banner-pagination').innerHTML = '<div class="pagination"><span style="color:#888;font-size:.9em">RouterOS API Reference</span></div>';
    return;
  }}
  var parts = s.path.replace(/^\\//, '').split('/').filter(Boolean);
  var nextSib = null;
  for (var i = idx + 1; i < sections.length; i++) {{ if (depthOf(sections[i].path) === navDepth) {{ nextSib = sections[i].path; break; }} }}
  if (nextSib === null && idx + 1 < sections.length) nextSib = sections[idx + 1].path;
  var prevSib = null;
  for (var i = idx - 1; i >= 0; i--) {{ if (depthOf(sections[i].path) === navDepth) {{ prevSib = sections[i].path; break; }} }}
  if (prevSib === null && idx > 0) prevSib = sections[idx - 1].path;
  var prefix = parts.length > 1 ? parts.slice(0, -1).join('/') : '';
  function isSibling(p) {{
    var pp = p.split('/').filter(Boolean);
    return pp.length === parts.length && (parts.length <= 1 || pp.slice(0, -1).join('/') === prefix);
  }}
  var firstSib = path, lastSib = path;
  for (var i = 0; i < sections.length; i++) {{ if (isSibling(sections[i].path)) {{ firstSib = sections[i].path; break; }} }}
  for (var i = sections.length - 1; i >= 0; i--) {{ if (isSibling(sections[i].path)) {{ lastSib = sections[i].path; break; }} }}
  var parentPath = parts.length > 1 ? '/' + parts.slice(0, -1).join('/') : '/';
  var pagination = '<div class="pagination">';
  pagination += '<a onclick="navDepth=' + (parts.length - 1) + ';show(\\'' + parentPath + '\\',true)" title="Up">&#8593;</a>';
  pagination += '<a onclick="show(\\'' + (firstSib || path) + '\\',true)" title="First">&#8249;&#8249;</a>';
  pagination += '<a onclick="show(\\'' + (prevSib || path) + '\\',true)" title="Previous">&#8249;</a>';
  pagination += '<a onclick="show(\\'' + (nextSib || path) + '\\',true)" title="Next">&#8250;</a>';
  pagination += '<a onclick="resetHome()" title="Home">&#8250;&#8250;</a>';
  pagination += '</div>';
  document.getElementById('banner-pagination').innerHTML = pagination;
}}
function esc(s) {{ return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }}
function resetHome() {{
  document.querySelectorAll('.section.active,.tree .active').forEach(function(e){{e.classList.remove('active')}});
  document.getElementById('sec_home').classList.add('active');
  document.getElementById('sidebar-tree').scrollTop = 0;
  document.getElementById('banner-pagination').innerHTML = '<div class="pagination"><span style="color:#888;font-size:.9em">RouterOS API Reference</span></div>';
}}
document.getElementById('search').addEventListener('input', function() {{
  var q = this.value.trim().toLowerCase();
  var results = document.getElementById('search-results');
  if (!q) {{ results.style.display = 'none'; return; }}
  var hits = [];
  for (var i = 0; i < sections.length; i++) {{
    var s = sections[i];
    var score = 0;
    if (s.title.toLowerCase().indexOf(q) >= 0) score += 3;
    if (s.path.toLowerCase().indexOf(q) >= 0) score += 2;
    if (s.description.toLowerCase().indexOf(q) >= 0) score += 1;
    for (var j = 0; j < s.params.length; j++) {{
      if (s.params[j].toLowerCase().indexOf(q) >= 0) score += 1;
    }}
    if (score > 0) hits.push({{item: s, score: score}});
  }}
  hits.sort(function(a,b) {{
    function segPos(segs, s) {{ for (var k = 0; k < segs.length; k++) {{ if (segs[k].indexOf(s) >= 0) return k + 1; }} return 999; }}
    var pa = a.item.path.toLowerCase().split('/').filter(Boolean);
    var pb = b.item.path.toLowerCase().split('/').filter(Boolean);
    var qa = segPos(pa, q), qb = segPos(pb, q);
    if (qa !== qb) return qa - qb;
    if (a.item.path < b.item.path) return -1;
    if (a.item.path > b.item.path) return 1;
    return 0;
  }});
  if (!hits.length) {{ results.innerHTML = '<a style="color:#999">No results</a>'; results.style.display = 'block'; return; }}
  var html = '';
  for (var i = 0; i < hits.length; i++) {{
    var item = hits[i].item;
    html += '<a onclick="show(\\'' + item.path + '\\');document.getElementById(\\'search-results\\').style.display=\\'none\\';document.getElementById(\\'search\\').value=\\'\\'">' +
      (item.method ? '<span class="sr-method" style="background:' + (item.method === 'GET' ? '#2e7d32' : '#1565c0') + '">' + item.method + '</span>' : '') +
      '<span class="sr-path">' + item.path + '</span>' +
      (item.description ? ' <span class="sr-desc">' + esc(item.description).slice(0, 60) + '</span>' : '') +
      '</a>';
  }}
  results.innerHTML = html;
  results.style.display = 'block';
  results._idx = -1;
}});
document.getElementById('search').addEventListener('keydown', function(e) {{
  var results = document.getElementById('search-results');
  var links = results.querySelectorAll('a');
  if (e.key === 'Escape') {{
    this.value = '';
    results.style.display = 'none';
    return;
  }}
  if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {{
    e.preventDefault();
    var idx = results._idx != null ? results._idx : -1;
    if (e.key === 'ArrowDown') idx = Math.min(idx + 1, links.length - 1);
    else idx = Math.max(idx - 1, -1);
    links.forEach(function(a, i) {{ a.classList.toggle('selected', i === idx); }});
    results._idx = idx;
    if (idx >= 0) links[idx].scrollIntoView({{block: 'nearest'}});
    return;
  }}
  if (e.key === 'Enter') {{
    var idx = results._idx;
    if (idx >= 0 && idx < links.length) {{
      e.preventDefault();
      links[idx].click();
    }}
  }}
}});
document.addEventListener('click', function(e) {{
  var input = document.getElementById('search');
  var results = document.getElementById('search-results');
  if (!input.contains(e.target) && !results.contains(e.target)) results.style.display = 'none';
}});
show('/');
</script>
</body></html>'''

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(html)
    return len(html)


# ── Main ───────────────────────────────────────────────────────────


def build_endpoints_tree(sections):
    """Build a hierarchical endpoints tree (path, methods, children) from parsed sections."""
    tree = {"path": "/", "methods": [], "children": {}}
    for s in sections:
        parts = s['path'].strip('/').split('/') if s['path'] != '/' else []
        node = tree
        acc = ""
        for p in parts:
            acc += "/" + p
            if 'children' not in node:
                node['children'] = {}
            if p not in node['children']:
                node['children'][p] = {"path": acc, "methods": []}
            node = node['children'][p]
        if s['method'] and s['method'] not in node['methods']:
            node['methods'].append(s['method'])
    return {"endpoints": tree}


def main():
    """Main entry point: parse arguments, discover or load the schema, generate outputs."""
    parser = argparse.ArgumentParser(description="Generate RouterOS API reference docs")
    parser.add_argument("--host", help="Router hostname/IP")
    parser.add_argument("--user", help="Router username")
    parser.add_argument("--pass", dest="password", help="Router password")
    parser.add_argument("--output-dir", help="Output directory")
    parser.add_argument("--store-credentials", action="store_true", help="Store credentials in schema.json (insecure)")
    parser.add_argument("--schema", help="Existing schema file to regenerate docs from")
    parser.add_argument("--endpoints", action="store_true", help="Generate hierarchical endpoints JSON")
    parser.add_argument("--no-docs", action="store_true", help="Skip HTML/MD doc generation")
    parser.add_argument("--no-fetch", action="store_true", help="Use existing schema instead of live discovery")
    parser.add_argument("--quiet", action="store_true", help="Suppress progress output")
    parser.add_argument("--open", action="store_true", help="Open generated HTML in browser")
    parser.add_argument("schema_pos", nargs="?", help="Schema file path (same as --schema)")
    args = parser.parse_args()

    if args.schema_pos and not args.schema:
        args.schema = args.schema_pos
        delattr(args, 'schema_pos')

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    sys.stdout.reconfigure(line_buffering=True)

    if not args.schema and not args.host:
        parser.error("Either --schema or --host is required")

    # ── Output directory & logging ──
    if args.output_dir:
        out_dir = Path(args.output_dir).resolve()
    elif args.schema:
        out_dir = Path(args.schema).parent.resolve()
    else:
        sys.stderr.write("No output directory specified.\n")
        resp = input("Use current directory? [Y/n] ").strip().lower()
        if resp in ('', 'y', 'yes'):
            out_dir = Path.cwd().resolve()
        else:
            out_dir = Path(input("Output directory: ").strip()).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    if args.quiet:
        logging.getLogger().setLevel(logging.WARNING)

    type_map_path = TOOL_DIR / "mikro-doc.hints"
    type_map = json.loads(type_map_path.read_text()) if type_map_path.exists() else {}
    if not type_map:
        log.warning("mikro-doc.hints not found — examples will use placeholder values")

    # ── Schema source ──
    schema = None
    router_info = {}
    api = {}
    router_name = args.host or "?"

    if args.no_fetch or args.schema:
        # Load from existing schema file
        if args.schema:
            schema_path = Path(args.schema)
        elif args.host:
            candidates = sorted(out_dir.glob("*.schema.json"))
            if len(candidates) == 1:
                schema_path = candidates[0]
            elif len(candidates) > 1:
                parser.error("Multiple schema files in output dir; use --schema to specify")
            else:
                parser.error("No schema file found; use --schema to specify")
        else:
            parser.error("--no-fetch requires --schema or --host")
        if not schema_path.exists():
            log.error("Schema not found: %s", schema_path)
            sys.exit(1)
        log.info("Loading %s...", schema_path)
        schema = json.loads(schema_path.read_text())
        router_info = schema.get("router", {})
        router_info["_host"] = args.host or router_info.get("_host",
            schema.get("_credentials", {}).get("host", router_info.get("identity", "?")))
        router_info["_make"] = "MikroTik"
        router_info["_generated"] = schema.get("discovered", "unknown")
        api = schema.get("api", {})
        router_name = router_info.get("identity", schema_path.stem)

    elif args.host:
        # Live discovery — resolve credentials
        username = args.user or ""
        password = args.password or ""

        if not username or not password:
            candidates = sorted(out_dir.glob("*.schema.json"))
            if candidates:
                try:
                    stored = json.loads(candidates[0].read_text()).get("_credentials", {})
                    if not username:
                        username = stored.get("username", "")
                    if not password:
                        password = stored.get("password", "")
                except Exception:
                    pass

        if not username:
            username = input("Username: ")
        if not password:
            import getpass
            password = getpass.getpass("Password: ")

        if not username or not password:
            parser.error("Username and password are required")

        client = RouterOSClient(host=args.host, username=username, password=password)
        t0 = time.time()
        try:
            api, router_info, discovered_ts, optimal, total = discover(
                client, hostname=args.host)
        finally:
            client.close()

        router_name = router_info.get("identity", args.host)
        router_info["_host"] = args.host
        router_info["_make"] = "MikroTik"
        router_info["_generated"] = discovered_ts

        schema_output = {
            "discovered": discovered_ts,
            "router": router_info,
            "schema_workers": optimal,
            "api": api,
        }
        if args.store_credentials:
            schema_output["_credentials"] = {
                "host": args.host,
                "username": username,
                "password": password,
            }
        schema_path = out_dir / f"{router_name}.schema.json"
        flush_output(str(schema_path), schema_output)
        log.info("Schema written to %s", schema_path)

    # ── Generate outputs ──
    md = generate_markdown(api, type_map)
    sections = parse_sections(md)

    if args.endpoints:
        tree = build_endpoints_tree(sections)
        ep_path = out_dir / f"{router_name}_Endpoints.json"
        flush_output(str(ep_path), tree)
        log.info("Endpoints written to %s", ep_path)

    if not args.no_docs:
        (out_dir / f"{router_name}.md").write_text(md)
        html_path = out_dir / f"{router_name}.index.html"
        size = generate_html(sections, router_info, html_path)
        log.info("Done. %d KB → %s", size // 1024, html_path)
        if args.open:
            import webbrowser
            webbrowser.open(f"file://{html_path.resolve()}")


if __name__ == "__main__":
    main()
