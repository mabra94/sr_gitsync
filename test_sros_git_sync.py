import subprocess
import tempfile
import unittest
from pathlib import Path

import sros_git_sync as s

HDR = ("# TiMOS-B-0.0.B1-1 both/x86_64 Nokia 7250 IXR-s3x\n# Configuration format version 27.3 revision 0\n\n")
PERSIST = ('persistent-indices {\n    description "Persistent indices are maintained by the system."\n'
           '    chassis-mac {\n        client lag index 1 pool 1 offset 1025\n    }\n}\n')


def ts(n: int) -> str:
    return f"2026-11-{10 + n // 100:02d}T10:{n % 60:02d}:00.5Z"


class FakeBox:
    """Imitates what a node serves over gNOI File."""

    def __init__(self, saves_keep=50, index_keep=50):
        self.commits: list[dict] = []
        self.saves_keep, self.index_keep = saves_keep, index_keep
        self.extra_running: str | None = None       # config.cfg content without a commit (drift)

    def commit(self, user="admin", type_="md-cli", label="MD-CLI", body=None, comment=None,
               increment=None, log=None, ts_=None):
        cid = (self.commits[-1]["id"] + 1) if self.commits else 1
        self.commits.append(dict(id=cid, ts=ts_ or ts(cid), user=user, type=type_, label=label,
                                 body=body, comment=comment, increment=increment, log=log, frm="10.0.1.1"))
        return cid

    def _save(self, c) -> str:
        return (HDR + f"# Generated {c['ts']} by system from Console\n# Commit ID {c['id']}\n"
                f"#   Committed {c['ts']} by {c['user']} ({c['label']}) from {c['frm']}\n\n"
                + c["body"] + "\n" + PERSIST + f"\n# Finished {c['ts']}\n")

    def _inc(self, c) -> str:
        return (HDR + f"# Generated {c['ts']} by {c['user']} from {c['frm']}\n# Commit ID {c['id']}\n"
                f"#   Committed {c['ts']} by {c['user']} ({c['label']}) from {c['frm']}\n"
                + (f'#   Comment   "{c["comment"]}"\n' if c["comment"] else "") + "\n"
                + c["increment"] + "\n" + PERSIST + f"\n# Finished {c['ts']}\n")

    def files(self) -> dict[str, bytes]:
        out: dict[str, bytes] = {}
        shown = self.commits[-self.index_keep:]
        newest = self.commits[-1]["id"]
        idx = []
        saved = [c for c in self.commits if c["body"] is not None][-self.saves_keep:]
        for c in shown:
            blk = [f"    commit-id {c['id']} {{", f"        timestamp {c['ts']}", f'        user "{c["user"]}"',
                   f"        type {c['type']}", f"        from {c['frm']}"]
            if c["log"]:
                blk.append(f'        log "{c["log"]}"')
            if c["body"] is not None:
                n = len([x for x in saved if x["id"] > c["id"]])
                name = "config.cfg" if n == 0 else f"config.cfg.{n}"
                blk.append(f'        location "cf3:\\{name}"')
            if c["increment"] is not None:
                name = f"config-{c['ts'].replace(':', '-')}-{c['id']}.is"
                blk.append(f'        increment-location "cf3:\\.commit-history\\{name}"')
                out[f"cf3:/.commit-history/{name}"] = self._inc(c).replace("\n", "\r\n").encode()
            if c["comment"]:
                blk.append(f'        comment "{c["comment"]}"')
            blk.append("    }")
            idx.append("\r\n".join(blk))
        out["cf3:/.commit-history/config.ch"] = ("\r\n".join(idx) + "\r\n").encode()
        for i, c in enumerate(reversed(saved)):
            out["cf3:/config.cfg" if i == 0 else f"cf3:/config.cfg.{i}"] = self._save(c).replace("\n", "\r\n").encode()
        if self.extra_running is not None and saved:
            last = dict(saved[-1], body=self.extra_running)
            out["cf3:/config.cfg"] = self._save(last).encode()
        return out


class BoxFetcher:
    def __init__(self, box: FakeBox, after_first=None):
        self.box, self.calls, self.after_first = box, 0, after_first

    def fetch(self, paths):
        files = self.box.files()
        self.calls += 1
        if self.calls == 1 and self.after_first:
            self.after_first()
        return {s.norm_path(p): files.get(s.norm_path(p)) for p in paths}


def cfg(*leaves):
    return "configure {\n    system {\n" + "".join(f"        {l}\n" for l in leaves) + "    }\n}\n"


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = s.Repo(Path(self.tmp.name) / "r.git")
        self.repo.ensure("r1")
        self.norm = s.Normalizer()
        self.node = {"name": "r1"}

    def tearDown(self):
        self.tmp.cleanup()

    def sync(self, box, **kw):
        return s.sync_node(self.node, self.repo, s.SrosDriver(BoxFetcher(box), self.norm), now=lambda: 2_000_000_000, **kw)

    def log(self, fmt="%an|%ae|%s"):
        r = self.repo.git("log", "refs/heads/r1", "--reverse", f"--format={fmt}", check=False)
        return r.stdout.decode().strip().split("\n") if r.stdout.strip() else []

    def count(self):
        r = self.repo.git("rev-list", "--count", "refs/heads/r1", check=False)
        return int(r.stdout.decode().strip() or 0) if r.returncode == 0 else 0


class Parsing(unittest.TestCase):
    def test_header_uses_first_block_only(self):
        h = s.parse_header("# x\n\n# Generated t by system from Console\n# Commit ID 2\n"
                           "#   Committed 2026-11-25T11:17:17.2Z by admin (MD-CLI) from 10.0.1.1\n"
                           "# Commit ID 1\n#   Committed 2026-11-20T08:52:05.6Z by system (MD-CLI) from Console\n"
                           "configure {\n")
        self.assertEqual((h["id"], h["user"], h["label"]), (2, "admin", "MD-CLI"))

    def test_index(self):
        e = s.parse_commit_index('commit-id 7 {\r\n timestamp 2026-01-01T00:00:00.1Z\r\n user "bob"\r\n type grpc\r\n'
                                 ' from 1.2.3.4\r\n comment "a \\"quoted\\" word"\r\n}\r\n')[7]
        self.assertEqual((e.user, e.type, e.comment), ("bob", "grpc", 'a "quoted" word'))

    def test_normalize_strips_comments_and_persistent(self):
        t = HDR + "# Commit ID 1\n\nconfigure {\r\n    a 1   \r\n}\r\n\n" + PERSIST + "# Finished\n"
        self.assertEqual(s.Normalizer().full(t)[0], "configure {\n    a 1\n}\n")

    def test_flat(self):
        c, flat = s.Normalizer().full('configure {\n    port 1/1/c1 {\n        description "x"\n    }\n'
                                      '    lag "l" {\n    }\n}\n')
        self.assertEqual(flat, 'configure port 1/1/c1 description "x"\nconfigure lag "l"\n')

    def test_redact(self):
        n = s.Normalizer(redact=[r'password\s+"([^"]*)"'])
        self.assertIn('password "<redacted>"', n.full('configure {\n    password "$2y$abc"\n}\n')[0])

    def apply(self, base, inc):
        n = s.Normalizer()
        t = n.tree_of(base)
        s.apply_increment(t, s.parse_tree(s.parse_increment(inc, n.redact)))
        return s.render_tree(t)

    def test_increment_add_modify_remove_sorted(self):
        base = ("configure {\n    port 1/1/c1 {\n    }\n    port 1/1/c3 {\n    }\n"
                "    system {\n        name \"a\"\n        location \"x\"\n    }\n}\n")
        inc = ("    configure {\n+       port 1/1/c2 {\n+       }\n        system {\n"
               '-           name "a"\n+           name "b"\n-           location "x"\n        }\n    }\n')
        out = self.apply(base, inc)
        self.assertEqual(out, 'configure {\n    port 1/1/c1 {\n    }\n    port 1/1/c2 {\n    }\n    port 1/1/c3 {\n'
                              '    }\n    system {\n        name "b"\n    }\n}\n')


class Importing(Base):
    def test_first_and_incremental_runs(self):
        box = FakeBox()
        box.commit("system", log="System booted", type_="md-cli")
        box.commits[-1]["body"] = None
        box.commit(body=cfg('name "a"'))
        box.commit("alice", "netconf", "NETCONF", body=cfg('name "b"'), comment="rename")
        r = self.sync(box)
        self.assertEqual((r.imported, r.drift), (3, False))
        self.assertEqual(self.log()[1:], ["admin|admin@md-cli.r1|commit 2 by admin via MD-CLI", "alice|alice@netconf.r1|rename"])
        box.commit("bob", "grpc", "gNMI", body=cfg('name "c"'))
        box.commit(body=cfg('name "c"', 'location "L"'))
        r = self.sync(box)
        self.assertEqual(r.imported, 2)
        self.assertEqual(self.count(), 5)
        self.assertIn("bob|bob@gnmi.r1", self.log()[-2])
        tr = s.parse_trailers(self.repo.git("log", "-1", "--skip=1", "refs/heads/r1", "--format=%B").stdout.decode())
        self.assertEqual((tr["SROS-Commit-Id"], tr["SROS-Interface"], tr["SROS-Tree"]), ("4", "gNMI", "full-save"))

    def test_rerun_is_noop_and_deterministic(self):
        box = FakeBox()
        for i in range(4):
            box.commit(body=cfg(f'name "{i}"'))
        self.sync(box)
        tip = self.repo.tip("r1")
        self.assertEqual(self.sync(box).imported, 0)
        self.assertEqual(self.repo.tip("r1"), tip)
        other = s.Repo(Path(self.tmp.name) / "o.git")
        other.ensure("r1")
        s.sync_node(self.node, other, s.SrosDriver(BoxFetcher(box), self.norm), now=lambda: 2_000_000_000)
        self.assertEqual(other.tip("r1"), tip)

    def test_blame_shows_user_and_interface(self):
        box = FakeBox()
        box.commit(body=cfg('name "a"'))
        box.commit("alice", "netconf", "NETCONF", body=cfg('name "a"', 'location "x"'))
        self.sync(box)
        out = self.repo.git("blame", "-e", "refs/heads/r1", "--", "config.flat").stdout.decode()
        self.assertRegex(out, r"<admin@md-cli\.r1>.*name")
        self.assertRegex(out, r"<alice@netconf\.r1>.*location")

    def test_gap_when_commits_fell_off_the_node(self):
        box = FakeBox(index_keep=3)
        for i in range(3):
            box.commit(body=cfg(f'name "{i}"'))
        self.sync(box)
        for i in range(3, 9):
            box.commit(body=cfg(f'name "{i}"'))      # ids 4..9 -> index keeps 7,8,9
        r = self.sync(box)
        self.assertEqual((r.imported, r.markers), (3, 1))
        msgs = self.log("%s")
        self.assertIn("SROS history gap: commits 4-6 no longer on the node", msgs)

    def test_reset_when_ids_restart(self):
        box = FakeBox()
        for i in range(3):
            box.commit(body=cfg(f'name "{i}"'))
        self.sync(box)
        box2 = FakeBox()
        box2.commit(body=cfg('name "fresh"'))
        r = self.sync(box2)
        self.assertEqual((r.imported, r.markers), (1, 1))
        self.assertTrue(any("restarted" in m for m in self.log("%s")))

    def test_fallback_to_increment_when_full_save_gone(self):
        box = FakeBox(saves_keep=1)
        box.commit(body=cfg('name "a"'))
        box.commit(body=cfg('name "b"'), increment='    configure {\n        system {\n-           name "a"\n+           name "b"\n        }\n    }\n')
        box.commit(body=cfg('name "c"'), increment='    configure {\n        system {\n-           name "b"\n+           name "c"\n        }\n    }\n')
        self.sync_prefix_then(box)

    def sync_prefix_then(self, box):
        full = box.commits[:]
        box.commits = full[:1]
        self.sync(box)                                 # night 1: only commit 1
        box.commits = full                              # night 2: 2 and 3, only 3 still has its save
        r = self.sync(box)
        self.assertEqual(r.imported, 2)
        trees = [s.parse_trailers(m)["SROS-Tree"] for m in
                 self.repo.git("log", "refs/heads/r1", "--reverse", "--format=%B%x00").stdout.decode().split("\x00") if m.strip()]
        self.assertEqual(trees, ["full-save", "reconstructed", "full-save"])
        self.assertEqual(self.repo.read("r1", "config.cfg"), cfg('name "c"'))
        self.assertFalse(r.drift)

    def test_drift_is_recorded_once(self):
        box = FakeBox()
        box.commit(body=cfg('name "a"'))
        self.sync(box)
        box.extra_running = cfg('name "sneaky"')
        r = self.sync(box)
        self.assertTrue(r.drift)
        self.assertEqual(self.log()[-1].split("|")[:2], ["unknown", "unattributed@drift.r1"])
        self.assertEqual(self.repo.read("r1", "config.cfg"), cfg('name "sneaky"'))
        self.assertFalse(self.sync(box).drift)           # the same drift is not recorded twice
        self.assertEqual(self.count(), 2)

    def test_commit_arriving_during_sync_is_picked_up(self):
        box = FakeBox()
        box.commit(body=cfg('name "a"'))
        f = BoxFetcher(box, after_first=lambda: box.commit(body=cfg('name "b"')))
        r = s.sync_node(self.node, self.repo, s.SrosDriver(f, self.norm), now=lambda: 2_000_000_000)
        self.assertEqual(self.count(), 2)
        self.assertEqual(self.repo.read("r1", "config.cfg"), cfg('name "b"'))

    def test_unreadable_index_fails_loudly(self):
        class Dead:
            def fetch(self, paths):
                return {p: None for p in paths}
        with self.assertRaises(RuntimeError):
            s.sync_node(self.node, self.repo, s.SrosDriver(Dead(), self.norm))
        self.assertEqual(self.count(), 0)



# ----------------------------------------------------------------------- SR Linux


import json


class FakeSrl:
    """Imitates the gNMI + gNOI-file view of an SR Linux node with auto-checkpoint."""

    def __init__(self, ckpt_keep=10):
        self.commits, self.ckpts, self.cfg = [], [], {}
        self.ckpt_keep = ckpt_keep
        self.corrupt: set[int] = set()            # checkpoint idx whose header does not match

    def commit(self, user="admin", type_="shared", name="default", cfg=None, comment=None,
               checkpoint=True, status="complete"):
        cid = (self.commits[-1]["id"] + 1) if self.commits else 1
        t = f"2026-11-26T10:{cid:02d}:00.250Z"
        c = dict(id=cid, type=type_, name=name, status=status, username=user, started=t, ended=t)
        if comment:
            c["comment"] = comment
        self.commits.append(c)
        if cfg is not None:
            self.cfg = cfg
        if checkpoint and status == "complete":
            self.ckpts.insert(0, dict(name=f"checkpoint-{t}", comment=f"automatic checkpoint after commit {cid}",
                                      created=t, cfg=json.loads(json.dumps(self.cfg))))
            del self.ckpts[self.ckpt_keep:]
        return cid

    def complete(self, cid):
        c = next(c for c in self.commits if c["id"] == cid)
        c["status"] = "complete"
        self.ckpts.insert(0, dict(name=f"checkpoint-{c['ended']}", comment=f"automatic checkpoint after commit {cid}",
                                  created=c["ended"], cfg=json.loads(json.dumps(self.cfg))))

    def get(self, path, typ=None):
        if path == "/system/configuration/commit":
            return {"commit": self.commits}
        if path == "/system/configuration/checkpoint":
            return {"checkpoint": [dict(id=i, name=c["name"], comment=c["comment"], created=c["created"])
                                   for i, c in enumerate(self.ckpts)]}
        if path == "/":
            return self.cfg
        raise AssertionError(path)

    def fetch(self, paths):
        out = {}
        for p in paths:
            i = int(re.search(r"checkpoint-(\d+)\.json", p)[1])
            if i < len(self.ckpts):
                c = self.ckpts[i]
                hdr = {"name": c["name"] + ("x" if i in self.corrupt else ""), "comment": c["comment"]}
                out[p] = json.dumps({"_preamble": {"header": hdr}, **c["cfg"]}).encode()
            else:
                out[p] = None
        return out


import re


def sc(**kw):
    base = {"srl_nokia-system:system": {"srl_nokia-system-info:information": {"location": "x"}},
            "srl_nokia-interfaces:interface": [{"name": "ethernet-1/1", "admin-state": "enable"}]}
    base["srl_nokia-system:system"]["srl_nokia-system-info:information"].update(kw)
    return base


class SrlImporting(Base):
    def setUp(self):
        super().setUp()
        self.node = {"name": "leaf"}
        self.repo.ensure("leaf")

    def sync(self, box):
        return s.sync_node(self.node, self.repo, s.SrlDriver(box, box, s.JsonNormalizer(['"community":\\s*"([^"]*)"'])),
                           now=lambda: 2_000_000_000)

    def log(self, fmt="%an|%ae|%s"):
        r = self.repo.git("log", "refs/heads/leaf", "--reverse", f"--format={fmt}", check=False)
        return r.stdout.decode().strip().split("\n") if r.stdout.strip() else []

    def test_labels_and_trees(self):
        box = FakeSrl()
        box.commit("admin", "private", "grpcrpc-1", sc(location="a"))
        box.commit("alice", cfg=sc(location="b"), comment="by hand")
        r = self.sync(box)
        self.assertEqual(r.imported, 2)
        self.assertEqual(self.log(), ["admin|admin@gnmi.leaf|commit 1 by admin via gNMI", "alice|alice@cli-netconf.leaf|by hand"])
        self.assertEqual(self.repo.git("ls-tree", "-r", "--name-only", "refs/heads/leaf").stdout.decode().split(), ["config.flat", "config.json"])
        flat = self.repo.read("leaf", "config.flat")
        self.assertIn('/srl_nokia-interfaces:interface[name=ethernet-1/1]/admin-state "enable"', flat)
        self.assertIn('information/location "b"', flat)
        tr = s.parse_trailers(self.repo.git("log", "-1", "refs/heads/leaf", "--format=%B").stdout.decode())
        self.assertEqual((tr["SRL-Commit-Id"], tr["SRL-Interface"], tr["SRL-Session"]), ("2", "CLI/NETCONF", "default (shared)"))

    def test_jsonrpc_label(self):
        box = FakeSrl()
        box.commit("carol", "private", "jsonrpc-3", sc(location="j"))
        self.assertEqual(self.sync(box).imported, 1)
        self.assertEqual(self.log(), ["carol|carol@json-rpc.leaf|commit 1 by carol via JSON-RPC"])

    def test_incremental_idempotent_and_deterministic(self):
        box = FakeSrl()
        box.commit(cfg=sc(location="a"))
        self.assertEqual(self.sync(box).imported, 1)
        box.commit(cfg=sc(location="b"))
        box.commit("bob", "private", "grpcrpc-7", sc(location="c"))
        self.assertEqual(self.sync(box).imported, 2)
        tip = self.repo.tip("leaf")
        self.assertEqual(self.sync(box).imported, 0)
        other = s.Repo(Path(self.tmp.name) / "o.git")
        other.ensure("leaf")
        s.sync_node(self.node, other, s.SrlDriver(box, box, s.JsonNormalizer()), now=lambda: 2_000_000_000)
        # the rebuilt repo only knows 3 commits too -> same history, same hashes
        self.assertEqual(other.tip("leaf"), tip)

    def test_commit_without_checkpoint_is_flagged_not_guessed(self):
        box = FakeSrl()
        box.commit(cfg=sc(location="a"))
        box.commit(cfg=sc(location="b"), checkpoint=False)
        box.commit(cfg=sc(location="c"))
        self.sync(box)
        trees = [s.parse_trailers(m)["SRL-Tree"] for m in
                 self.repo.git("log", "refs/heads/leaf", "--reverse", "--format=%B%x00").stdout.decode().split("\x00") if m.strip()]
        self.assertEqual(trees, ["full-save", "unavailable", "full-save"])
        self.assertIn('"location": "c"', self.repo.read("leaf", "config.json"))

    def test_rotated_checkpoint_is_not_trusted(self):
        box = FakeSrl()
        box.commit(cfg=sc(location="a"))
        box.commit(cfg=sc(location="b"))
        box.corrupt = {1}                          # checkpoint-1 now holds some other checkpoint
        self.sync(box)
        trees = [s.parse_trailers(m)["SRL-Tree"] for m in
                 self.repo.git("log", "refs/heads/leaf", "--reverse", "--format=%B%x00").stdout.decode().split("\x00") if m.strip()]
        self.assertEqual(trees, ["unavailable", "full-save"])

    def test_unfinished_commit_is_a_barrier(self):
        box = FakeSrl()
        box.commit(cfg=sc(location="a"))
        box.commit("bob", "private", "grpcrpc-1", sc(location="b"), status="in-progress")
        box.commit(cfg=sc(location="c"))
        self.assertEqual(self.sync(box).imported, 1)           # 3 must wait for 2
        box.complete(2)
        self.assertEqual(self.sync(box).imported, 2)
        self.assertEqual([x.split("|")[0] for x in self.log()], ["admin", "bob", "admin"])

    def test_drift_against_running_config(self):
        box = FakeSrl()
        box.commit(cfg=sc(location="a"))
        self.assertFalse(self.sync(box).drift)
        box.cfg = sc(location="sneaky")                         # changed without a commit entry
        r = self.sync(box)
        self.assertTrue(r.drift)
        self.assertEqual(self.log()[-1].split("|")[:2], ["unknown", "unattributed@drift.leaf"])
        self.assertFalse(self.sync(box).drift)

    def test_flatten_and_redact(self):
        n = s.JsonNormalizer(['"community":\\s*"([^"]*)"'])
        cfg, flat = n.full(json.dumps({"_preamble": {"x": 1}, "a": {"community": "secret", "list": [
            {"name": "n1", "v": 1, "sub": {"z": True}}, {"name": "n2"}], "empty": {}, "ll": ["x", "y"]}}))
        self.assertNotIn("secret", cfg + flat)
        self.assertNotIn("_preamble", cfg)
        self.assertEqual(flat.split("\n")[:-1], ['/a/community "<redacted>"', '/a/empty {}', '/a/list[name=n1]/v 1',
                                                 '/a/list[name=n1]/sub/z true', '/a/list[name=n2]',
                                                 '/a/ll "x"', '/a/ll "y"'])
        self.assertIn('/a/list[name=n1]/v 1', flat)
        self.assertIn('/a/list[name=n2]', flat.split("\n"))
        self.assertIn('/a/empty {}', flat)


if __name__ == "__main__":
    unittest.main()
