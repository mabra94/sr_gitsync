# sros-git-sync

Mirrors the commit history of Nokia **SR OS** and **SR Linux** nodes into git, so `git log`,
`git blame` and `git diff` show what the node shows: who changed what, when, through which interface.

Transport is gRPC only - **no SSH**: gNOI File (SR OS and SR Linux) and gNMI (SR Linux).
Requires `python3` (3.11+), `git`, [`gnoic`](https://gnoic.kmrd.dev/) and, for SR Linux, [`gnmic`](https://gnmic.openconfig.net/).
Select the platform per node with `platform = "sros"` (default) or `"srlinux"`.

## What you get

One branch per node, one git commit per SR OS commit:

| SR OS | git |
|---|---|
| user | author name |
| interface (MD-CLI / NETCONF / gNMI) + node | author email: `admin@netconf.<node>` (`git blame -e`) |
| commit time | author + committer date |
| commit comment | subject (otherwise `commit N by user via IF`) |
| commit-id, client IP, interface, tree source | trailers `SROS-Commit-Id`, `SROS-Client`, ... |
| config after the commit | `config.cfg` (as on the box) and `config.flat` (one leaf per line, best for `blame`) |

```
git clone <repo> && cd <repo> && git checkout ixrs3x
git log --format='%h %an <%ae> %s'
git blame -e config.flat | grep 'port 1/1/c1 '
git log -p -1 --grep='SROS-Commit-Id: 12'
git log --format='%(trailers:key=SROS-Interface,valueonly)' | sort | uniq -c
```

Boots are imported as empty commits (author `system`). Hashes are deterministic: rebuilding
the repo from the same node history reproduces identical commit ids.

## SR Linux

Differences from SR OS, found on SR Linux 26.7 (leaf2):

- **Commit list** comes from gNMI (`/system/configuration/commit`): id, user, times, comment, session name.
- **A config per commit needs `auto-checkpoint`.** Enable it once on every node:
  `set /system configuration auto-checkpoint true` (and raise `max-checkpoints`, default 10, max 100).
  Each commit then leaves a checkpoint "automatic checkpoint after commit N", which the importer fetches over gNOI File.
  Commits made before it was enabled, or whose checkpoint has rotated out, are imported with
  `SRL-Tree: unavailable` (author and time are still right); the next commit with a checkpoint carries their changes.
- **Interface is only partly known.** gNMI commits are `private` sessions named `grpcrpc-N` -> label `gNMI`.
  CLI and NETCONF both appear as `shared default` -> label `CLI/NETCONF`; the node does not record the difference.
  JSON-RPC commits are `private` sessions named `jsonrpc-N` -> label `JSON-RPC` (verified on leaf2, HTTP and HTTPS).
  Other session names are used as the label as is.
- **No client IP** is recorded, so there is no `SRL-Client` trailer; `SRL-Session` shows the node's session name/type.
- Files in git: `config.json` (canonical: keys sorted, `_preamble` removed) and `config.flat`.
  The flat key syntax (`list[name=x]`) is derived heuristically because the JSON carries no schema.
- **Drift** (config changed with no commit entry) is checked against the gNMI running config when the newest commit has its checkpoint.
- Redaction for SR Linux uses `[redact] srlinux_patterns` (regexes on the JSON text).
- gNMI/gNOI on SR Linux use TLS: configure `tls_ca`, or `skip_verify = true` in a lab.

```toml
[[node]]
name = "leaf2"
platform = "srlinux"
address = "leaf2"
skip_verify = true             # lab; use tls_ca otherwise
username_env = "SRL_USERNAME"  # different credentials than the SR OS nodes
password_env = "SRL_PASSWORD"
```

## Running it

```
export SROS_USERNAME=... SROS_PASSWORD=...        # never in the config file
python3 sros_git_sync.py -c config.toml --dry-run  # show what would be committed
python3 sros_git_sync.py -c config.toml            # import
```

`sros-git-sync.service` + `.timer` run it nightly (copy to `/etc/systemd/system/`, adjust paths).
Exit codes: 0 ok, 1 a node failed (others still imported), 2 bad config, 3 another run active.

## Why it is safe to run every night

State lives in git itself: a run imports only commits newer than the branch's last
`SROS-Commit-Id` trailer, so re-runs do nothing. Edge cases are handled explicitly:

| Situation | Result |
|---|---|
| Commits pushed off the node's history before the run | commit `SROS history gap: commits X-Y ...` (`SROS-Gap` trailer); no guessing |
| Node reinstalled / commit-ids restarted | commit `SROS commit history restarted ...` (`SROS-Reset`) |
| Numbered saves (`config.cfg.N`) rotated | each save is trusted only if its header commit-id **and** timestamp match; otherwise falls back to the increment file |
| Full save gone, increment still there | tree rebuilt from the increment, trailer `SROS-Tree: reconstructed` (content exact; ordering of new containers is best effort, so the next real save may show a harmless move) |
| Neither available | commit without tree change, `SROS-Tree: unavailable`, error logged |
| Config changed without a commit-history entry (file edited, classic CLI) | author `unknown <unattributed@drift.<node>>`, trailer `SROS-Drift: true` |
| Node commits while the run reads | the pass is discarded and restarted before anything is written |
| Node unreachable / auth failure | nothing written, exit 1, other nodes continue |
| Two runs overlap | the second exits with code 3 |

Drift is only checked when `cf3:/config.cfg` is at the newest commit, so a node with
`auto-config-save` off does not produce false drift every night.

## Limits you should know

- **Retention is on the node.** Only commits still in `cf3:/.commit-history/config.ch` can be
  imported. If a night produces more commits than the node keeps, the gap commit records
  what is missing. Run more often (or on change) if automation commits in bursts.
- **"Who" is the SR OS user.** Changes pushed by NSP or a shared automation account appear as that account.
- **Client IP** is what the node saw; behind NAT or a mediation layer it is the proxy.
- **Secrets.** Saved configs contain password hashes and SNMP communities. Use `[redact]` in
  the config, and treat the repo as sensitive anyway. Redaction hides that a secret *changed*.
- **gNOI over plain-text gRPC** (`insecure = true`) sends credentials unencrypted. Use a
  TLS server profile on the node and `tls_ca`/`skip_verify` for anything but a lab.
- Tested against SR OS `B-0.0.B1-8673` (7250 IXR-s3x simulator) and SR Linux `v26.7.1` (7220 IXR-D2L simulator). The file formats
  (`config.ch`, `.is`, save headers) come from that release; run `--dry-run` first after upgrades.

## Tests

```
PYTHONPATH=. python3 -m unittest test_sros_git_sync -v
```
