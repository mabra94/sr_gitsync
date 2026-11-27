#!/usr/bin/env python3
"""sros-git-sync: mirror the commit history of Nokia SR OS and SR Linux nodes into git.

Per node (no SSH; gRPC only) a platform driver reads the commit list and the config after
each commit, and the shared engine turns it into one git commit per node commit on branch <node>:

  author     = node user  <user@<interface>.<node>>   (git blame -e shows the interface)
  date       = node commit time
  message    = commit comment + <PREFIX>-* trailers   (SROS-* / SRL-*)
  tree       = config.cfg|config.json (as on the box) and config.flat (one leaf per line)

  SR OS (platform = "sros"), via gNOI File:
    cf3:/.commit-history/config.ch   index: commit-id, user, interface, client, comment
    cf3:/config.cfg[.N]              full config saved at each commit (rotating names)
    cf3:/.commit-history/*.is        per-commit incremental diff (fallback source)

  SR Linux (platform = "srlinux"), via gNMI + gNOI File (needs /system configuration auto-checkpoint):
    /system/configuration/commit      commit list (user, times, comment, session name)
    /system/configuration/checkpoint  "automatic checkpoint after commit N"
    /etc/opt/srlinux/checkpoint/checkpoint-<idx>.json   full config of that commit

The git branch itself is the state: each run imports only commits newer than the last
<PREFIX>-Commit-Id trailer, so it is safe (and idempotent) to run from cron/systemd nightly.
"""
from __future__ import annotations

import argparse
import calendar
import dataclasses
import fcntl
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib
from pathlib import Path
from typing import Callable, Optional, Protocol

log = logging.getLogger("sros-git-sync")

CONFIG_PATH = "config.cfg"
FLAT_PATH = "config.flat"
INDEX_PATH = "cf3:/.commit-history/config.ch"
RUNNING_SAVE = "cf3:/config.cfg"
PIPELINE_NAME = "SROS Git Sync"
NODE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

# --------------------------------------------------------------------------- parsing


@dataclasses.dataclass
class Entry:
    """One block of config.ch."""
    id: int
    ts: str                      # 2026-11-25T11:17:17.2Z
    user: str = "unknown"
    type: str = ""               # md-cli | netconf | grpc ...
    frm: str = ""
    location: Optional[str] = None
    increment: Optional[str] = None
    log: Optional[str] = None
    comment: Optional[str] = None
    label: Optional[str] = None                 # interface label if the platform knows it
    ref: Optional[dict] = None                  # driver private data (e.g. SR Linux checkpoint)
    extra_trailers: list = dataclasses.field(default_factory=list)


def _unquote(v: str) -> str:
    v = v.strip()
    if len(v) >= 2 and v[0] == '"' and v[-1] == '"':
        v = v[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    return v


def parse_commit_index(text: str) -> dict[int, Entry]:
    entries: dict[int, Entry] = {}
    cur: Optional[dict] = None
    for raw in text.replace("\r", "").split("\n"):
        line = raw.strip()
        if not line:
            continue
        m = re.fullmatch(r"commit-id (\d+) \{", line)
        if m:
            cur = {"id": int(m[1])}
            continue
        if line == "}":
            if cur is not None and "timestamp" in cur:
                entries[cur["id"]] = Entry(
                    id=cur["id"], ts=cur["timestamp"], user=cur.get("user", "unknown"),
                    type=cur.get("type", ""), frm=cur.get("from", ""),
                    location=cur.get("location"), increment=cur.get("increment-location"),
                    log=cur.get("log"), comment=cur.get("comment"))
            cur = None
            continue
        if cur is not None:
            key, _, val = line.partition(" ")
            cur[key] = _unquote(val)
    return entries


TS_RE = re.compile(r"^(\d{4})-(\d\d)-(\d\d)T(\d\d):(\d\d):(\d\d)(?:\.\d+)?(?:Z|\+00:00)$")


def epoch(ts: str) -> int:
    m = TS_RE.match(ts)
    if not m:
        raise ValueError(f"unparseable SR OS timestamp {ts!r}")
    return calendar.timegm(tuple(int(x) for x in m.groups()) + (0, 0, 0))


def parse_header(text: str) -> dict:
    """Leading '# ...' comment block of a full save or increment file.

    A full save may list several commits (the saved one first, then earlier commits that
    were never saved on their own, e.g. a boot). Only the first block describes the file.
    """
    h: dict = {}
    blocks = 0
    for line in text.replace("\r", "").split("\n"):
        if not line.strip():
            continue
        if not line.startswith("#"):
            break
        if m := re.match(r"#\s*Commit ID (\d+)\s*$", line):
            blocks += 1
            if blocks == 1:
                h["id"] = int(m[1])
        elif blocks != 1:
            continue
        elif m := re.match(r"#\s+Committed (\S+) by (\S+) \((.+?)\) from (\S+)\s*$", line):
            h["ts"], h["user"], h["label"], h["frm"] = m.groups()
        elif m := re.match(r'#\s+Comment\s+"(.*)"\s*$', line):
            h["comment"] = m[1].replace('\\"', '"')
    return h


# ----------------------------------------------------------------- config tree model


class Node:
    __slots__ = ("text", "block", "children", "marker")

    def __init__(self, text: str, block: bool, marker: str = " "):
        self.text, self.block, self.marker = text, block, marker
        self.children: list[Node] = []


def parse_tree(items: list[tuple[str, str]]) -> Node:
    root = Node("", True)
    stack = [root]
    for marker, text in items:
        if text == "}":
            if len(stack) == 1:
                raise ValueError("unbalanced '}'")
            stack.pop()
        elif text.endswith("{"):
            n = Node(text[:-1].rstrip(), True, marker)
            stack[-1].children.append(n)
            stack.append(n)
        else:
            stack[-1].children.append(Node(text, False, marker))
    if len(stack) != 1:
        raise ValueError("unbalanced '{'")
    return root


def render_tree(root: Node) -> str:
    out: list[str] = []

    def walk(n: Node, depth: int) -> None:
        for c in n.children:
            ind = "    " * depth
            if c.block:
                out.append(f"{ind}{c.text} {{")
                walk(c, depth + 1)
                out.append(f"{ind}}}")
            else:
                out.append(f"{ind}{c.text}")

    walk(root, 0)
    return "\n".join(out) + "\n" if out else ""


def flatten_tree(root: Node) -> str:
    """Equivalent of 'info flat': one leaf (or empty container) per line, full path."""
    out: list[str] = []

    def walk(n: Node, path: list[str]) -> None:
        for c in n.children:
            if c.block and c.children:
                walk(c, path + [c.text])
            else:
                out.append(" ".join(path + [c.text]))

    walk(root, [])
    return "\n".join(out) + "\n" if out else ""


def _first_token(t: str) -> str:
    return t.split(None, 1)[0] if t else t


def _natkey(t: str) -> list:
    return [int(p) if p.isdigit() else p for p in re.split(r"(\d+)", t)]


def _set_context(n: Node) -> None:
    n.marker = " "
    for c in n.children:
        _set_context(c)


def apply_increment(base: Node, inc: Node) -> None:
    """Apply a parsed '+/-' increment onto base (in place).

    New entries are placed next to their siblings of the same keyword, in natural sort
    order when the siblings are sorted; a '-' followed by '+' of the same keyword (a value
    change) is replaced in place. Ordering is best effort - SR OS orders by its schema.
    """
    def find(parent: Node, text: str, block: bool) -> Optional[int]:
        for i, c in enumerate(parent.children):
            if c.text == text and c.block == block:
                return i
        return None

    def walk(b: Node, i: Node) -> None:
        pending: Optional[tuple[int, str]] = None
        for n in i.children:
            if n.marker == "-":
                idx = find(b, n.text, n.block)
                if idx is not None:
                    del b.children[idx]
                    pending = (idx, _first_token(n.text))
                continue
            if n.marker == "+":
                _set_context(n)
                if find(b, n.text, n.block) is not None:
                    continue                      # already there (idempotent)
                if pending and pending[1] == _first_token(n.text):
                    pos = pending[0]
                else:
                    pos = _insert_pos(b, n)
                b.children.insert(pos, n)
                pending = None
                continue
            idx = find(b, n.text, True)           # context line: descend
            if idx is None:
                child = Node(n.text, True)
                b.children.append(child)
            else:
                child = b.children[idx]
            walk(child, n)

    def _insert_pos(parent: Node, new: Node) -> int:
        tok = _first_token(new.text)
        same = [k for k, c in enumerate(parent.children) if _first_token(c.text) == tok]
        if not same:
            return len(parent.children)
        keys = [_natkey(parent.children[k].text) for k in same]
        if keys == sorted(keys):
            nk = _natkey(new.text)
            for k, kk in zip(same, keys):
                if kk > nk:
                    return k
        return same[-1] + 1

    walk(base, inc)


def parse_increment(text: str, redact: Callable[[str], str]) -> list[tuple[str, str]]:
    """Body of an .is file as (marker, text) items; the 4-char diff gutter is removed."""
    items: list[tuple[str, str]] = []
    gutter = re.compile(r"^([+\- ]) {3}(.*)$")
    started = False
    for raw in text.replace("\r", "").split("\n"):
        if not raw.strip() or raw.startswith("#"):
            continue
        m = gutter.match(raw)
        if not m:
            if started:
                break
            continue
        started = True
        items.append((m[1], redact(m[2].strip())))
    return items


# --------------------------------------------------------------- normalisation


class Normalizer:
    """Turns a full save into the canonical text stored in git."""

    def __init__(self, strip_persistent: bool = True, redact: Optional[list[str]] = None):
        self.strip_persistent = strip_persistent
        self.patterns = [re.compile(p) for p in (redact or [])]

    def redact(self, text: str) -> str:
        for p in self.patterns:
            def sub(m: re.Match) -> str:
                if p.groups:
                    s, e = m.span(1)
                    return m.group(0)[: s - m.start()] + "<redacted>" + m.group(0)[e - m.start():]
                return "<redacted>"
            text = p.sub(sub, text)
        return text

    def clean(self, text: str) -> list[str]:
        lines, skipping = [], False
        for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
            line = raw.rstrip()
            if line.startswith("#"):
                continue
            if self.strip_persistent and line.startswith("persistent-indices"):
                skipping = True
            if skipping:
                if line == "}":
                    skipping = False
                continue
            if line:
                lines.append(self.redact(line))
        return lines

    def tree_of(self, text: str) -> Node:
        return parse_tree([(" ", l.strip()) for l in self.clean(text)])

    def full(self, text: str) -> tuple[str, Optional[str]]:
        """-> (config text, flat text or None if the text could not be parsed)."""
        try:
            t = self.tree_of(text)
            return render_tree(t), flatten_tree(t)
        except ValueError as e:
            log.warning("config did not parse (%s); storing it verbatim, no flat view", e)
            return "\n".join(self.clean(text)) + "\n", None


# ------------------------------------------------------------------- transport


class Fetcher(Protocol):
    def fetch(self, paths: list[str]) -> dict[str, Optional[bytes]]: ...


def norm_path(p: str) -> str:
    return p.replace("\\", "/")


class GnoicFetcher:
    """Reads files from a node over gNOI File using the 'gnoic' binary."""

    def __init__(self, node: dict):
        self.node = node

    def _base(self) -> list[str]:
        n = self.node
        cmd = [n.get("gnoic", "gnoic"), "-a", f"{n['address']}:{n.get('port', 57400)}",
               "--timeout", str(n.get("timeout", "30s"))]
        if n.get("insecure"):
            cmd.append("--insecure")
        elif n.get("skip_verify"):
            cmd.append("--skip-verify")
        for flag, key in (("--tls-ca", "tls_ca"), ("--tls-cert", "tls_cert"), ("--tls-key", "tls_key")):
            if n.get(key):
                cmd += [flag, str(n[key])]
        return cmd

    def _env(self) -> dict[str, str]:
        n = self.node
        env = dict(os.environ)
        user = os.environ.get(n.get("username_env", "SROS_USERNAME")) or n.get("username")
        pw = os.environ.get(n.get("password_env", "SROS_PASSWORD"))
        if not user or pw is None:
            raise RuntimeError(
                f"credentials missing: set ${n.get('username_env', 'SROS_USERNAME')} and "
                f"${n.get('password_env', 'SROS_PASSWORD')}")
        env["GNOIC_USERNAME"], env["GNOIC_PASSWORD"] = user, pw
        return env

    def _get(self, paths: list[str], dst: str) -> None:
        cmd = self._base() + ["file", "get", "--dst", dst]
        for p in paths:
            cmd += ["--file", p]
        r = subprocess.run(cmd, env=self._env(), capture_output=True, text=True, timeout=300)
        if r.returncode != 0:
            log.debug("gnoic exit %s: %s", r.returncode, (r.stderr or r.stdout).strip()[-400:])

    def fetch(self, paths: list[str]) -> dict[str, Optional[bytes]]:
        paths = list(dict.fromkeys(norm_path(p) for p in paths))
        out: dict[str, Optional[bytes]] = {}
        tmp = tempfile.mkdtemp(prefix="sros-git-sync-")
        try:
            def collect(todo: list[str]) -> list[str]:
                missing = []
                for p in todo:
                    f = Path(tmp) / p.lstrip("/")
                    if f.is_file():
                        out[p] = f.read_bytes()
                    else:
                        missing.append(p)
                return missing

            missing: list[str] = []
            if paths:
                self._get(paths, tmp)
                missing = collect(paths)
            for p in missing:                     # one bad path must not hide the others
                self._get([p], tmp)
                if collect([p]):
                    out[p] = None
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        return out


# -------------------------------------------------------------------------- git


class Repo:
    def __init__(self, path: Path):
        self.path = path

    def git(self, *args: str, input: Optional[bytes] = None, check: bool = True) -> subprocess.CompletedProcess:
        r = subprocess.run(["git", "-C", str(self.path), *args], input=input, capture_output=True)
        if check and r.returncode != 0:
            raise RuntimeError(f"git {' '.join(args)} failed: {r.stderr.decode(errors='replace').strip()}")
        return r

    def ensure(self, first_branch: str) -> None:
        if not (self.path / "HEAD").exists() and not (self.path / ".git").exists():
            self.path.mkdir(parents=True, exist_ok=True)
            subprocess.run(["git", "init", "--bare", "-q", str(self.path)], check=True)
            self.git("symbolic-ref", "HEAD", f"refs/heads/{first_branch}")

    def tip(self, branch: str) -> Optional[str]:
        r = self.git("rev-parse", "--verify", "-q", f"refs/heads/{branch}^{{commit}}", check=False)
        return r.stdout.decode().strip() if r.returncode == 0 else None

    def read(self, branch: str, path: str) -> Optional[str]:
        r = self.git("show", f"refs/heads/{branch}:{path}", check=False)
        return r.stdout.decode() if r.returncode == 0 else None

    def last_import(self, branch: str, prefix: str = "SROS") -> Optional[tuple[int, str]]:
        if not self.tip(branch):
            return None
        r = self.git("log", f"refs/heads/{branch}", f"--grep=^{prefix}-Commit-Id: ", "-n1", "--format=%B")
        tr = parse_trailers(r.stdout.decode())
        if f"{prefix}-Commit-Id" in tr:
            return int(tr[f"{prefix}-Commit-Id"]), tr.get(f"{prefix}-Timestamp", "")
        return None

    def fast_import(self, stream: bytes) -> None:
        self.git("fast-import", "--quiet", "--date-format=raw", input=stream)


def parse_trailers(msg: str) -> dict[str, str]:
    paras = [p for p in msg.strip().split("\n\n") if p.strip()]
    out: dict[str, str] = {}
    for line in (paras[-1].split("\n") if paras else []):
        if m := re.match(r"^([A-Za-z][A-Za-z0-9-]*): (.*)$", line):
            out[m[1]] = m[2]
    return out


# ----------------------------------------------------------------- commit model


@dataclasses.dataclass
class Spec:
    """Everything needed to emit one git commit."""
    name: str
    email: str
    when: int
    subject: str
    trailers: list[tuple[str, str]]
    config: Optional[str] = None         # None = tree unchanged
    flat: Optional[str] = None


@dataclasses.dataclass
class Material:
    """What a driver could read for one commit."""
    full: Optional[str] = None           # validated full config of exactly this commit
    inc: Optional[str] = None            # increment (diff) text, SR OS only
    hdr: dict = dataclasses.field(default_factory=dict)
    expected: bool = False               # the node says a config for this commit exists


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-") or "unknown"


def _clean_ident(s: str) -> str:
    return re.sub(r"[<>\n\r]", "", s).strip() or "unknown"


LABEL_BY_TYPE = {"md-cli": "MD-CLI", "netconf": "NETCONF", "grpc": "gRPC", "cli": "CLI", "snmp": "SNMP"}


def plan_import(idx: dict[int, Entry], last: Optional[tuple[int, str]]):
    """-> (ids to import, marker) where marker is None | ('gap', lo, hi) | ('reset',)"""
    ids = sorted(idx)
    if last is None:
        return ids, None
    lid, lts = last
    e = idx.get(lid)
    if e is not None:
        return ([i for i in ids if i > lid], None) if e.ts == lts else (ids, ("reset",))
    if ids and ids[0] > lid:
        return ids, ("gap", lid + 1, ids[0] - 1)
    return ids, ("reset",)


def build_specs(node: str, prefix: str, entries: list[Entry], mats: dict[int, Material],
                prev_cfg: str, norm) -> tuple[list[Spec], str]:
    specs: list[Spec] = []
    cur = prev_cfg
    for e in entries:
        m = mats.get(e.id) or Material()
        spec_cfg: Optional[str] = None
        flat: Optional[str] = None
        kind = "none"
        if m.full is not None:
            spec_cfg, flat = norm.full(m.full)
            kind = "full-save"
            if m.inc is not None and hasattr(norm, "tree_of"):   # self-check of the fallback path
                _selfcheck(e.id, cur, m.inc, spec_cfg, norm)
        elif m.inc is not None and hasattr(norm, "tree_of"):
            try:
                base = norm.tree_of(cur) if cur else Node("", True)
                apply_increment(base, parse_tree(parse_increment(m.inc, norm.redact)))
                spec_cfg, flat = render_tree(base), flatten_tree(base)
                kind = "reconstructed"
                log.warning("commit %s: full save gone, config reconstructed from increment", e.id)
            except ValueError as err:
                kind = "unavailable"
                log.error("commit %s: increment unusable (%s)", e.id, err)
        elif m.expected:
            kind = "unavailable"
            log.error("commit %s: config of this commit is no longer readable", e.id)

        if spec_cfg is not None:
            cur = spec_cfg

        label = m.hdr.get("label") or e.label or LABEL_BY_TYPE.get(e.type, e.type or "unknown")
        user = e.user or m.hdr.get("user") or "unknown"
        comment = e.comment or m.hdr.get("comment")
        if comment:
            subject = comment
        elif e.log:
            subject = e.log
        else:
            subject = f"commit {e.id} by {user} via {label}"
        trailers = [(f"{prefix}-Node", node), (f"{prefix}-Commit-Id", str(e.id)), (f"{prefix}-Timestamp", e.ts),
                    (f"{prefix}-User", user), (f"{prefix}-Interface", label)]
        if e.frm:
            trailers.append((f"{prefix}-Client", e.frm))
        trailers += [(f"{prefix}-{k}", v) for k, v in e.extra_trailers]
        trailers.append((f"{prefix}-Tree", kind))
        specs.append(Spec(name=_clean_ident(user), email=f"{_slug(user)}@{_slug(label)}.{node}",
                          when=epoch(e.ts), subject=subject.replace("\n", " "),
                          trailers=trailers, config=spec_cfg, flat=flat))
    return specs, cur


def _selfcheck(cid: int, prev: str, inc_text: str, full_cfg: str, norm) -> None:
    """Debug aid: does the increment applied to the previous tree give the saved config?"""
    try:
        base = norm.tree_of(prev) if prev else Node("", True)
        apply_increment(base, parse_tree(parse_increment(inc_text, norm.redact)))
        got = render_tree(base)
    except ValueError:
        log.debug("commit %s: increment self-check could not run", cid)
        return
    if got == full_cfg:
        log.debug("commit %s: increment reproduces full save exactly", cid)
    elif sorted(got.split("\n")) == sorted(full_cfg.split("\n")):
        log.debug("commit %s: increment reproduces full save (ordering differs only)", cid)
    else:
        log.warning("commit %s: increment does NOT reproduce the full save", cid)


def marker_spec(node: str, prefix: str, marker: tuple, when: int) -> Spec:
    if marker[0] == "gap":
        subj = f"{prefix} history gap: commits {marker[1]}-{marker[2]} no longer on the node"
        tr = [(f"{prefix}-Node", node), (f"{prefix}-Gap", f"{marker[1]}-{marker[2]}")]
    else:
        subj = f"{prefix} commit history restarted on the node (commit-ids are not continuous)"
        tr = [(f"{prefix}-Node", node), (f"{prefix}-Reset", "true")]
    return Spec(PIPELINE_NAME, f"sros-git-sync@{node}", when, subj, tr)


def drift_spec(node: str, prefix: str, cfg: str, flat: Optional[str], when: int, last_id: int) -> Spec:
    return Spec("unknown", f"unattributed@drift.{node}", when,
                "Configuration differs from the last recorded commit (not in commit history)",
                [(f"{prefix}-Node", node), (f"{prefix}-Drift", "true"), (f"{prefix}-After-Commit-Id", str(last_id))],
                cfg, flat)


# ----------------------------------------------------------------- JSON configs (SR Linux)


_KEY_PRIORITY = ("name", "index", "id", "interface-id", "ip-prefix", "address", "afi-safi-name",
                 "network-instance", "peer-address", "group-name", "rule-id", "sequence-id")


def _entry_key(items: list[dict], i: int) -> str:
    """Stable, schema-less list-entry key: priority leaf names first, then alphabetical scalars."""
    scalars = [k for k, v in items[i].items() if not isinstance(v, (dict, list))]
    cands = [k for k in _KEY_PRIORITY if k in scalars] + sorted(k for k in scalars if k not in _KEY_PRIORITY)
    if not cands:
        return f"[{i}]"
    for n in range(1, min(len(cands), 3) + 1):
        keys = [tuple(str(it.get(k)) for k in cands[:n]) for it in items]
        if len(set(keys)) == len(keys) or n == min(len(cands), 3):
            break
    return "[" + ",".join(f"{k}={items[i].get(k)}" for k in cands[:n]) + "]"


def flatten_json(obj, path: str = "") -> list[str]:
    """One line per leaf: /interface[name=ethernet-1/1]/subinterface[index=0]/admin-state "enable"."""
    out: list[str] = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            out += flatten_json(v, f"{path}/{k}")
        if not obj and path:
            out.append(f"{path} {{}}")
    elif isinstance(obj, list):
        if obj and all(isinstance(x, dict) for x in obj):
            for i, it in enumerate(obj):
                p = path + _entry_key(obj, i)
                nested = {k: v for k, v in it.items() if isinstance(v, (dict, list))}
                sub = flatten_json(nested, p) if nested else []
                leaves = [k for k, v in it.items() if not isinstance(v, (dict, list))]
                out += [f"{p}/{k} {json.dumps(it[k], ensure_ascii=False)}" for k in leaves
                        if k not in _entry_key(obj, i)]
                out += sub
                if not sub and all(k in _entry_key(obj, i) for k in leaves):
                    out.append(p)
        else:
            out += [f"{path} {json.dumps(x, ensure_ascii=False)}" for x in obj]
    else:
        out.append(f"{path} {json.dumps(obj, ensure_ascii=False)}")
    return out


class JsonNormalizer:
    """Canonical text of an SR Linux config (checkpoint file or gNMI dump): keys sorted, no preamble."""

    def __init__(self, redact: Optional[list[str]] = None):
        self.patterns = [re.compile(p) for p in (redact or [])]

    def redact(self, text: str) -> str:
        for p in self.patterns:
            def sub(m: re.Match) -> str:
                if p.groups:
                    s, e = m.span(1)
                    return m.group(0)[: s - m.start()] + "<redacted>" + m.group(0)[e - m.start():]
                return "<redacted>"
            text = p.sub(sub, text)
        return text

    def full(self, text: str) -> tuple[str, Optional[str]]:
        try:
            d = json.loads(text)
            if isinstance(d, dict):
                d.pop("_preamble", None)
            cfg = self.redact(json.dumps(d, indent=2, sort_keys=True, ensure_ascii=False)) + "\n"
            return cfg, "\n".join(flatten_json(json.loads(cfg))) + "\n"
        except ValueError as e:
            log.warning("config is not valid JSON (%s); storing verbatim", e)
            return text if text.endswith("\n") else text + "\n", None


# ----------------------------------------------------------------- platform drivers


class Driver(Protocol):
    prefix: str            # trailer prefix: SROS | SRL
    config_path: str       # file name of the config in git
    norm: object

    def read_index(self) -> dict[int, Entry]: ...
    def read_materials(self, entries: list[Entry]) -> dict[int, Material]: ...
    def read_running(self) -> tuple[Optional[int], Optional[str]]: ...


class SrosDriver:
    prefix = "SROS"
    config_path = "config.cfg"

    def __init__(self, fetcher: Fetcher, norm: Normalizer):
        self.fetcher, self.norm = fetcher, norm

    def read_index(self) -> dict[int, Entry]:
        raw = self.fetcher.fetch([INDEX_PATH]).get(INDEX_PATH)
        if raw is None:
            raise RuntimeError(f"cannot read {INDEX_PATH} (gNOI File enabled? commit-history on?)")
        return parse_commit_index(raw.decode(errors="replace"))

    def read_materials(self, entries: list[Entry]) -> dict[int, Material]:
        paths = [p for e in entries for p in (e.location, e.increment) if p]
        files = self.fetcher.fetch(paths) if paths else {}
        out: dict[int, Material] = {}
        for e in entries:
            m = Material(expected=bool(e.location or e.increment))
            if e.location and (raw := files.get(norm_path(e.location))) is not None:
                txt = raw.decode(errors="replace")
                h = parse_header(txt)
                if h.get("id") == e.id and h.get("ts") == e.ts:
                    m.full, m.hdr = txt, h
                else:
                    log.info("commit %s: %s now holds commit %s - not trusting it as a full save",
                             e.id, e.location, h.get("id"))
            if e.increment and (raw := files.get(norm_path(e.increment))) is not None:
                m.inc = raw.decode(errors="replace")
                m.hdr = m.hdr or parse_header(m.inc)
            out[e.id] = m
        return out

    def read_running(self) -> tuple[Optional[int], Optional[str]]:
        raw = self.fetcher.fetch([RUNNING_SAVE]).get(RUNNING_SAVE)
        if raw is None:
            return None, None
        text = raw.decode(errors="replace")
        return parse_header(text).get("id"), text


class GnmicRunner:
    """gNMI Get through the 'gnmic' binary (credentials via environment, never argv)."""

    def __init__(self, node: dict):
        self.node = node

    def get(self, path: str, typ: Optional[str] = None):
        n = self.node
        cmd = [n.get("gnmic", "gnmic"), "-a", f"{n['address']}:{n.get('port', 57400)}",
               "--timeout", str(n.get("timeout", "30s")), "--format", "json"]
        if n.get("insecure"):
            cmd.append("--insecure")
        elif n.get("skip_verify"):
            cmd.append("--skip-verify")
        for flag, key in (("--tls-ca", "tls_ca"), ("--tls-cert", "tls_cert"), ("--tls-key", "tls_key")):
            if n.get(key):
                cmd += [flag, str(n[key])]
        cmd += ["get", "--path", path, "--encoding", "json_ietf"] + (["--type", typ] if typ else [])
        env = GnoicFetcher(n)._env()
        env["GNMIC_USERNAME"], env["GNMIC_PASSWORD"] = env["GNOIC_USERNAME"], env["GNOIC_PASSWORD"]
        r = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=300)
        if r.returncode != 0:
            raise RuntimeError(f"gnmic get {path} failed: {(r.stderr or r.stdout).strip()[-300:]}")
        try:
            return gnmi_value(json.loads(r.stdout))
        except ValueError as e:
            raise RuntimeError(f"gnmic get {path}: unreadable reply ({e})") from e


def gnmi_value(resp):
    """First value of a gnmic JSON reply."""
    for notif in resp or []:
        for upd in notif.get("updates", []):
            for v in (upd.get("values") or {}).values():
                return v
    return None


SRL_CKPT_DIR = "/etc/opt/srlinux/checkpoint"
SRL_INTERNAL_USERS = {"__internal__", "root"}


def srl_label(c: dict) -> str:
    """Interface label from SR Linux's session type/name. NETCONF and CLI share 'shared/default'."""
    typ, name, user = c.get("type", ""), c.get("name", ""), c.get("username", "")
    if user in SRL_INTERNAL_USERS:
        return "system"
    if name.startswith("grpcrpc"):
        return "gNMI"
    if name.startswith("jsonrpc"):
        return "JSON-RPC"
    if typ == "shared" and name == "default":
        return "CLI/NETCONF"
    return re.sub(r"-\d+$", "", name) or typ or "unknown"


class SrlDriver:
    prefix = "SRL"
    config_path = "config.json"

    def __init__(self, gnmi, fetcher: Fetcher, norm: JsonNormalizer):
        self.gnmi, self.fetcher, self.norm = gnmi, fetcher, norm
        self._newest_ckpt: Optional[int] = None

    def read_index(self) -> dict[int, Entry]:
        commits = (self.gnmi.get("/system/configuration/commit") or {}).get("commit")
        if commits is None:
            raise RuntimeError("cannot read /system/configuration/commit over gNMI")
        ckpts = (self.gnmi.get("/system/configuration/checkpoint") or {}).get("checkpoint") or []
        by_commit: dict[int, dict] = {}
        for c in sorted(ckpts, key=lambda c: str(c.get("created", ""))):      # newest wins
            if m := re.search(r"after commit (\d+)\s*$", c.get("comment", "")):
                by_commit[int(m[1])] = c
        # a commit that has not finished must not be skipped over: stop before the first one
        barrier = min((c["id"] for c in commits if c.get("status") not in ("complete", "failed")), default=None)
        out: dict[int, Entry] = {}
        for c in sorted(commits, key=lambda c: c["id"]):
            if c.get("status") != "complete" or (barrier is not None and c["id"] >= barrier):
                continue
            ts = c.get("ended") or c.get("started")
            if not ts:
                continue
            ck = by_commit.get(c["id"])
            e = Entry(id=c["id"], ts=ts, user=c.get("username") or "unknown", label=srl_label(c),
                      comment=c.get("comment") or None,
                      extra_trailers=[("Session", f"{c.get('name', '')} ({c.get('type', '')})")])
            if ck is not None:
                e.ref = {"idx": ck["id"], "name": ck.get("name"), "comment": ck.get("comment")}
            out[e.id] = e
        self._newest_ckpt = max((i for i, e in out.items() if e.ref), default=None)
        self._newest_id = max(out, default=None)
        self._complete = barrier is None          # no unfinished commit hiding newer changes
        return out

    def read_materials(self, entries: list[Entry]) -> dict[int, Material]:
        want = {e.id: f"{SRL_CKPT_DIR}/checkpoint-{e.ref['idx']}.json" for e in entries if e.ref}
        files = self.fetcher.fetch(list(want.values())) if want else {}
        out: dict[int, Material] = {}
        for e in entries:
            # every user commit should have a checkpoint (auto-checkpoint); system ones need none
            m = Material(expected=bool(e.ref) or e.label != "system")
            raw = files.get(want[e.id]) if e.id in want else None
            if raw is not None:
                txt = raw.decode(errors="replace")
                try:
                    hdr = json.loads(txt).get("_preamble", {}).get("header", {})
                except (ValueError, AttributeError):
                    hdr = {}
                if hdr.get("name") == e.ref["name"] and hdr.get("comment") == e.ref["comment"]:
                    m.full = txt
                else:
                    log.info("commit %s: checkpoint-%s now holds %r - not trusting it",
                             e.id, e.ref["idx"], hdr.get("comment"))
            out[e.id] = m
        return out

    def read_running(self) -> tuple[Optional[int], Optional[str]]:
        # The running config corresponds to a commit id only if the newest commit has its checkpoint.
        if not self._complete or self._newest_id is None or self._newest_ckpt != self._newest_id:
            return None, None
        cfg = self.gnmi.get("/", "CONFIG")
        return (self._newest_id, json.dumps(cfg)) if cfg is not None else (None, None)


# ----------------------------------------------------------------- fast-import


def _data(b: bytes) -> bytes:
    return b"data %d\n" % len(b) + b + b"\n"


def build_stream(node: str, branch: str, specs: list[Spec], has_tip: bool, config_path: str = "config.cfg") -> bytes:
    out = bytearray()
    for n, s in enumerate(specs):
        msg = s.subject + "\n\n" + "\n".join(f"{k}: {v}" for k, v in s.trailers) + "\n"
        ident = lambda nm, em: f"{nm} <{em}> {s.when} +0000"
        out += f"commit refs/heads/{branch}\n".encode()
        out += f"author {ident(s.name, s.email)}\n".encode()
        out += f"committer {ident(PIPELINE_NAME, 'sros-git-sync@' + node)}\n".encode()
        out += _data(msg.encode())
        if n == 0 and has_tip:
            out += f"from refs/heads/{branch}^0\n".encode()
        if s.config is not None:
            out += b"M 100644 inline " + f"{config_path}\n".encode() + _data(s.config.encode())
        if s.flat is not None:
            out += b"M 100644 inline " + f"{FLAT_PATH}\n".encode() + _data(s.flat.encode())
        out += b"\n"
    return bytes(out)


# ----------------------------------------------------------------------- driver


@dataclasses.dataclass
class Result:
    node: str
    imported: int = 0
    markers: int = 0
    drift: bool = False
    error: Optional[str] = None


def sync_node(node: dict, repo: Repo, driver: Driver, dry_run: bool = False,
              now: Callable[[], int] = lambda: int(time.time())) -> Result:
    name, pfx = node["name"], driver.prefix
    for attempt in range(1, 5):
        res = Result(name)
        idx = driver.read_index()
        if not idx:
            raise RuntimeError("the node reports no commits")

        last = repo.last_import(name, pfx)
        ids, marker = plan_import(idx, last)
        specs: list[Spec] = []
        if marker:
            specs.append(marker_spec(name, pfx, marker, epoch(idx[ids[0]].ts) if ids else now()))
            res.markers += 1
        entries = [idx[i] for i in ids]
        mats = driver.read_materials(entries)

        # Saved configs rotate with every commit. If the node moved on while we were
        # reading, what we fetched may be stale: start over before writing anything.
        idx2 = driver.read_index()
        newest = max(idx)
        if idx2 and (max(idx2) != newest or idx2[max(idx2)].ts != idx[newest].ts):
            log.info("%s: node committed during sync (now %s), starting over", name, max(idx2))
            continue

        prev_cfg = repo.read(name, driver.config_path) or ""
        cs, cur = build_specs(name, pfx, entries, mats, prev_cfg, driver.norm)
        specs += cs
        res.imported += len(cs)
        last_id = ids[-1] if ids else (last[0] if last else 0)

        # drift check: the node's saved/running config vs. what git believes
        hid, text = driver.read_running()
        idx3 = driver.read_index()                 # a commit between the reads would look like drift
        if idx3 and (max(idx3) != newest or idx3[max(idx3)].ts != idx[newest].ts):
            log.info("%s: node committed during sync (now %s), starting over", name, max(idx3))
            continue
        if text is not None:
            if hid == last_id:
                cfg, flat = driver.norm.full(text)
                if cfg != cur:
                    log.warning("%s: config differs from commit %s - recording drift", name, last_id)
                    specs.append(drift_spec(name, pfx, cfg, flat, now(), last_id))
                    res.drift = True
            else:
                log.info("%s: saved config is at commit %s, last imported %s - drift check skipped",
                         name, hid, last_id)
        _write(repo, name, specs, dry_run, driver.config_path)
        return res
    raise RuntimeError("node kept committing during every sync attempt; nothing written, will retry next run")


def _write(repo: Repo, branch: str, specs: list[Spec], dry_run: bool, config_path: str = "config.cfg") -> None:
    if not specs:
        return
    if dry_run:
        for s in specs:
            log.info("[dry-run] %s %s <%s> %s", time.strftime("%F %T", time.gmtime(s.when)),
                     s.name, s.email, s.subject)
        return
    repo.fast_import(build_stream(branch, branch, specs, repo.tip(branch) is not None, config_path))


PLATFORMS = ("sros", "srlinux")


def load_config(path: Path) -> dict:
    with open(path, "rb") as f:
        cfg = tomllib.load(f)
    if "repo" not in cfg:
        raise ValueError("config: 'repo' is required")
    defaults = cfg.get("defaults", {})
    nodes = []
    for n in cfg.get("node", []):
        merged = {**defaults, **n}
        if not NODE_RE.match(merged.get("name", "")):
            raise ValueError(f"config: bad node name {merged.get('name')!r}")
        if "address" not in merged:
            raise ValueError(f"config: node {merged['name']} has no address")
        if merged.get("platform", "sros") not in PLATFORMS:
            raise ValueError(f"config: node {merged['name']}: platform must be one of {PLATFORMS}")
        nodes.append(merged)
    if not nodes:
        raise ValueError("config: no [[node]] entries")
    cfg["nodes"] = nodes
    return cfg


def make_driver(node: dict, cfg: dict) -> Driver:
    red = cfg.get("redact", {})
    fetcher = GnoicFetcher(node)
    if node.get("platform", "sros") == "srlinux":
        return SrlDriver(GnmicRunner(node), fetcher, JsonNormalizer(red.get("srlinux_patterns", [])))
    return SrosDriver(fetcher, Normalizer(cfg.get("strip_persistent_indices", True), red.get("patterns", [])))


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("-c", "--config", required=True, type=Path)
    ap.add_argument("-n", "--node", action="append", help="only this node (repeatable)")
    ap.add_argument("--dry-run", action="store_true", help="show what would be committed")
    ap.add_argument("--push", action="store_true", help="push imported branches to the 'push_remote'")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s", stream=sys.stderr)
    try:
        cfg = load_config(a.config)
    except (OSError, ValueError, tomllib.TOMLDecodeError) as e:
        log.error("%s", e)
        return 2
    nodes = [n for n in cfg["nodes"] if not a.node or n["name"] in a.node]
    if not nodes:
        log.error("no matching node")
        return 2

    repo = Repo(Path(cfg["repo"]).expanduser())
    if not a.dry_run:
        repo.ensure(nodes[0]["name"])
    lock = open(Path(cfg["repo"]).expanduser() / ".sros-git-sync.lock", "w") if not a.dry_run else None
    if lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            log.error("another sros-git-sync run holds the lock")
            return 3
    failed = 0
    for n in nodes:
        try:
            r = sync_node(n, repo, make_driver(n, cfg), a.dry_run)
            log.info("%s: %d commit(s) imported%s%s", n["name"], r.imported,
                     f", {r.markers} marker" if r.markers else "", ", drift recorded" if r.drift else "")
            if a.push and not a.dry_run and cfg.get("push_remote") and r.imported + r.markers + r.drift:
                repo.git("push", cfg["push_remote"], f"refs/heads/{n['name']}")
        except Exception as e:                                  # noqa: BLE001 - one node must not stop the rest
            failed += 1
            log.error("%s: FAILED: %s", n["name"], e)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
