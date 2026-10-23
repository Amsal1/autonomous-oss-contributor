#!/usr/bin/env bash
# probe entrypoint (DESIGN §10.2 snapshot post-check, §10.9 probes, §22.5 smoke tests).
# Never receives the Kiro key (filter Env row). Mode from OSSBOT_PROBE:
#   snapshot_check   print every entry of /work/repo that is not a regular file or directory
#                    (sandboxd requires empty output; the ro snapshot is mounted at /work/repo)
#   extract_fixture  untar /opt/ossbot/fixtures/probe-repo.tar into /work/repo, symlinks preserved
#   settings         `kiro-cli settings list` with the baked cli.json on the tmpfs HOME (sandbox-llm)
#   smoke            write /io/output/probe.json with the isolation facts of this container (§22.5)
#   hostile          a hostile job on the task volume: /work/input and /work/output symlinks, a FIFO
#                    and a symlinked directory in /work/repo (M3, H1 checks of the next jobs)
#   execd            agent-llm side of the coder pair: requests to execd, answers in execd.json
#   execd_run        the same, with the run requests of /io/input/execd-run.json (sandbox tests)
# OSSBOT_WORK / OSSBOT_IO only relocate the fixed paths for tests. OSSBOT_PROBE_PEER (smoke) is an
# address on another task network that must be unreachable.
set -euo pipefail
WORK="${OSSBOT_WORK:-/work}"
IO="${OSSBOT_IO:-/io}"
MODE="${OSSBOT_PROBE:-}"
case "$MODE" in
  snapshot_check)
    find "$WORK/repo" -mindepth 1 ! -type f ! -type d -print
    ;;
  extract_fixture)
    mkdir -p "$WORK/repo" "$WORK/.home" "$WORK/cache"
    tar -xpf /opt/ossbot/fixtures/probe-repo.tar -C "$WORK/repo" --no-same-owner
    ;;
  settings)
    export HOME=/home/agent
    mkdir -p "$HOME/.kiro/settings" "$HOME/cwd" "$IO/output"
    cp /opt/ossbot/kiro/settings/cli.json "$HOME/.kiro/settings/cli.json"
    cd "$HOME/cwd"
    kiro-cli settings list >"$IO/output/settings.txt" 2>"$IO/output/settings.stderr"
    ;;
  hostile)
    mkdir -p "$WORK/repo"
    ln -sfn /etc "$WORK/input"
    ln -sfn /etc "$WORK/output"
    rm -f "$WORK/repo/fifo"
    mkfifo "$WORK/repo/fifo"
    ln -sfn /etc "$WORK/repo/linkdir"
    echo hostile >"$IO/output/hostile.txt"
    ;;
  execd)
    # agent-llm side of the coder pair, key-free: talk to agent-exec's execd over /run/exec/exec.sock
    # (S-symlink-1 ELINK/EPATH, S-key-1 facts from inside agent-exec via the run tool).
    mkdir -p "$IO/output"
    IO="$IO" python3 -I - <<'PY'
import json, os, socket, time
io = os.environ["IO"]
reqs = [
    {"id": 1, "op": "read_file", "args": {"path": "docs/env"}},
    {"id": 2, "op": "read_file", "args": {"path": "linkdir/hostname"}},
    {"id": 3, "op": "read_file", "args": {"path": "README.md"}},
    {"id": 4, "op": "run", "args": {"cmd": "echo hi"}},
    {"id": 5, "op": "run", "args": {"cmd": "c=0; for f in /proc/[0-9]*/environ; do n=$(grep -c KIRO_API_KEY \"$f\" 2>/dev/null) && c=$((c+n)); done; echo key_hits=$c; echo ifaces=$(ls /sys/class/net | tr '\\n' ' '); (timeout 3 bash -c '</dev/tcp/1.1.1.1/443') 2>/dev/null && echo direct=open || echo direct=closed"}},
]
out = {}
for _ in range(60):
    if os.path.exists("/run/exec/exec.sock"):
        break
    time.sleep(0.5)
s = socket.socket(socket.AF_UNIX)
s.settimeout(30)
s.connect("/run/exec/exec.sock")
f = s.makefile("rwb")
for r in reqs:
    f.write((json.dumps(r) + "\n").encode())
    f.flush()
    out[str(r["id"])] = json.loads(f.readline())
with open(f"{io}/output/execd.json", "w") as fh:
    json.dump(out, fh, sort_keys=True)
PY
    ;;
  execd_run)
    # Sandbox tests only (tests/sandbox/test_profiles.py, key-free): the run requests listed in
    # /io/input/execd-run.json ([{"id", "cmd", "timeout_s"}], at most 10) to agent-exec's execd.
    mkdir -p "$IO/output"
    IO="$IO" python3 -I - <<'PY'
import json, os, socket, time
io = os.environ["IO"]
with open(f"{io}/input/execd-run.json") as fh:
    runs = json.load(fh)[:10]
out = {}
for _ in range(60):
    if os.path.exists("/run/exec/exec.sock"):
        break
    time.sleep(0.5)
s = socket.socket(socket.AF_UNIX)
s.settimeout(1300)
s.connect("/run/exec/exec.sock")
f = s.makefile("rwb")
for r in runs:
    req = {"id": int(r["id"]), "op": "run", "args": {"cmd": str(r["cmd"]), "timeout_s": int(r.get("timeout_s", 600))}}
    f.write((json.dumps(req) + "\n").encode())
    f.flush()
    out[str(req["id"])] = json.loads(f.readline())
with open(f"{io}/output/execd.json", "w") as fh:
    json.dump(out, fh, sort_keys=True)
PY
    ;;
  smoke)
    mkdir -p "$IO/output"
    WORK="$WORK" IO="$IO" python3 -I - <<'PY'
import hashlib, json, os, socket, stat, subprocess

work, io = os.environ["WORK"], os.environ["IO"]
facts = {}

def run(argv, timeout=20):
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout.strip(), p.stderr.strip()[-200:]
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 124, "", str(exc)[-200:]

def can_write(path):
    try:
        with open(path, "w") as fh:
            fh.write("x")
        os.unlink(path)
        return True
    except OSError:
        return False

def tcp(host, port, timeout=3.0):
    try:
        socket.create_connection((host, port), timeout).close()
        return "open"
    except socket.timeout:
        return "timeout"
    except OSError as exc:
        return f"errno{exc.errno}"

def connect_code(url, proxy=None, timeout=15):
    argv = ["curl", "-s", "-o", "/dev/null", "--max-time", str(timeout), "-w", "%{http_connect} %{http_code}"]
    argv += ["-x", proxy] if proxy else ["--noproxy", "*"]
    code, out, _ = run(argv + [url], timeout + 5)
    return {"curl_exit": code, "connect": (out.split() or ["000"])[0], "http": (out.split() + ["000", "000"])[1]}

def dns_txt(server, name="x.example.com"):
    q = b"\x12\x34\x01\x00\x00\x01\x00\x00\x00\x00\x00\x00"
    q += b"".join(bytes([len(p)]) + p.encode() for p in name.split(".")) + b"\x00\x00\x10\x00\x01"
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(3)
    try:
        s.sendto(q, (server, 53))
        data = s.recv(512)
        return "answers" if len(data) > 12 and int.from_bytes(data[6:8], "big") > 0 else "empty"
    except OSError:
        return "no_reply"
    finally:
        s.close()

def mode_of(path):
    try:
        st = os.stat(path)
        return f"{st.st_uid}:{stat.S_IMODE(st.st_mode):o}"
    except OSError:
        return None

status = open("/proc/self/status").read()
facts["uid"] = os.getuid()
facts["cap_eff"] = next(l.split()[1] for l in status.splitlines() if l.startswith("CapEff"))
facts["root_writable"] = can_write("/.ossbot-probe")
facts["input_writable"] = can_write(f"{io}/input/.ossbot-probe")
facts["output_writable"] = can_write(f"{io}/output/.ossbot-probe")
open(f"{io}/output/x", "w").write("x")
facts["docker_sock"] = os.path.exists("/var/run/docker.sock")
facts["run_ossbot"] = os.path.exists("/run/ossbot")
facts["mnt_entries"] = sorted(os.listdir("/mnt")) if os.path.isdir("/mnt") else []
facts["resolves_example"] = run(["getent", "hosts", "example.com"])[0] == 0
facts["dns_txt_embedded"] = dns_txt("127.0.0.11")
facts["env_names"] = sorted(os.environ)
facts["interfaces"] = sorted(os.listdir("/sys/class/net"))
proxy = os.environ.get("HTTPS_PROXY", "")
networked = facts["interfaces"] != ["lo"]
if networked:
    facts["proxy_example"] = connect_code("https://example.com/", proxy)
    facts["noproxy_example"] = connect_code("https://example.com/", None, 8)
    facts["github"] = connect_code("https://github.com/", proxy)
    # github_user_content (CR-002): research in every egress mode, never fetch/validate (§11.3)
    facts["gist"] = connect_code("https://gist.github.com/", proxy)
    facts["gist_raw"] = connect_code("https://gist.githubusercontent.com/", proxy)
    facts["ghio"] = connect_code("https://github.github.io/", proxy)
    # always denied for every caller (CR-003 U11)
    facts["telegraph"] = connect_code("https://telegra.ph/", proxy)
    facts["host_ports"] = {str(p): tcp("10.212.0.2", p) for p in (3140, 3141, 3150)}
    facts["alias_ports"] = {str(p): connect_code("https://example.com/", f"http://proxy:{p}", 8) for p in (3140, 3141, 3150)}
    peer = os.environ.get("OSSBOT_PROBE_PEER")
    if peer:
        facts["peer"] = tcp(peer, 3128)
facts["kiro_direct"] = tcp("1.1.1.1", 443, 3)
mountinfo = open("/proc/self/mountinfo").read()
facts["mounts_task"] = "ossbot-task-" in mountinfo
facts["mounts_ro"] = "ossbot-ro-" in mountinfo
facts["work_entries"] = run(["find", work, "-mindepth", "1"])[1].splitlines()[:50] if os.path.isdir(work) else None
facts["work_nonregular"] = run(["find", work, "-mindepth", "1", "!", "-type", "f", "!", "-type", "d"])[1].splitlines()[:50] if os.path.isdir(work) else None
facts["work_top"] = sorted(os.listdir(work)) if os.path.isdir(work) else None
facts["repo_top"] = sorted(os.listdir(f"{work}/repo")) if os.path.isdir(f"{work}/repo") else None
facts["repo_writable"] = can_write(f"{work}/repo/.ossbot-probe") if os.path.isdir(f"{work}/repo") else None
home = os.environ.get("HOME", "/")
facts["home"] = home
facts["home_writable"] = can_write(f"{home}/.ossbot-probe")
try:
    os.makedirs("/tmp/bin", exist_ok=True)
    with open("/bin/true", "rb") as src, open("/tmp/bin/x", "wb") as dst:
        dst.write(src.read())
    os.chmod("/tmp/bin/x", 0o755)
    facts["tmp_exec"] = run(["/tmp/bin/x"])[0]
except OSError as exc:
    facts["tmp_exec"] = f"errno{exc.errno}"
facts["volume_modes"] = {p: mode_of(p) for p in ("/work", "/io/output", "/run/exec", "/snap")}
inputs = {}
for name in sorted(os.listdir(f"{io}/input")) if os.path.isdir(f"{io}/input") else []:
    path = f"{io}/input/{name}"
    st = os.lstat(path)
    digest = hashlib.sha256(open(path, "rb").read()).hexdigest() if stat.S_ISREG(st.st_mode) else None
    inputs[name] = {"regular": stat.S_ISREG(st.st_mode), "mode": f"{stat.S_IMODE(st.st_mode):o}", "sha256": digest}
facts["inputs"] = inputs
key_hits = 0
for pid in os.listdir("/proc"):
    if pid.isdigit():
        try:
            key_hits += open(f"/proc/{pid}/environ", "rb").read().count(b"KIRO_API_KEY")
        except OSError:
            pass
facts["kiro_key_in_environ"] = key_hits
link = f"{work}/repo/docs/env.symlink.txt"
facts["symlink_stub"] = open(link).read().strip() if os.path.isfile(link) else None
facts["docs_env_present"] = os.path.lexists(f"{work}/repo/docs/env")
with open(f"{io}/output/probe.json", "w") as fh:
    json.dump(facts, fh, sort_keys=True)
PY
    ;;
  *)
    echo "probe: unknown OSSBOT_PROBE mode" >&2
    exit 2
    ;;
esac
