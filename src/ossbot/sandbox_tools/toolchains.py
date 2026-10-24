#!/usr/bin/env python3
"""ossbot-toolchains: the per-repo toolchain resolver (build-env design §6, BE-4). Stdlib only; baked into
``sandbox-full`` at /opt/ossbot/bin/ossbot-toolchains and run by /usr/bin/python3 in fetch, validate and
agent-exec, inside the repo-code container (no credential exists there).

    ossbot-toolchains --repo /work/repo --state /work/.home/.ossbot --mode install|select
                      [--report /io/output/toolchains.json] [--budget 0..3600] [--log FILE]

It reads the repo's toolchain declarations (untrusted data: size, count and depth limits; XML through
ElementTree, TOML through tomllib; nothing from the repo is executed by the resolver itself), decides the exact
versions (ecosystem indexes for majors and ranges, so mise only ever gets exact versions), installs what the
image lacks into the task volume's cache (``install``: argv lists, a total time budget) or picks only what is
installed (``select``), and writes three fresh files under ``--state``: ``toolchain.env`` (a fixed key set with
validated values, read line by line by lib/tool_env.sh, lib/jvm_env.sh and execd, never sourced),
``mise.toml`` (the resolved global mise config) and ``toolchains.json`` (the report). Exit code 0 always, 2
only for bad arguments: every failure is a report entry (a gap), never a job failure.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import tomllib
import urllib.request
import xml.etree.ElementTree as ET  # expat; capped input, no DOCTYPE, no external entities (Repo.xml)
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# ------------------------------------------------------------------------------------------- limits (§6.1)
MAX_FILE = 1024 * 1024
MAX_FILES = 400
MAX_DEPTH = 3
INDEX_MAX = 32 * 1024 * 1024
INDEX_TIMEOUT = 30
INSTALL_TIMEOUT = 900
DEFAULT_BUDGET = 600
MAX_BUDGET = 3600
MAX_ENTRIES = 64
MAX_SKIPPED = 32
TAIL = 400

TOOL_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,39}")
VERSION_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,63}")
JAVA_MIN, JAVA_MAX = 8, 30

# ----------------------------------------------------------------------------------- image facts (§6.4)
IMAGE_JDKS = (8, 11, 17, 21, 25)
DEFAULT_JDK = 21
JDK_HOME = "/usr/lib/jvm/java-{}-openjdk-amd64"
IMAGE_NODE = "24.22.1"
IMAGE_NODE_BIN = f"/opt/mise/installs/node/{IMAGE_NODE}/bin"
IMAGE_GO = "1.27.3"
IMAGE_TERRAFORM = "1.17.0"
IMAGE_TOFU = "1.13.1"
IMAGE_RUST = "1.99.1"
IMAGE_MAVEN = "3.9.16"
RUST_TRIPLE = "x86_64-unknown-linux-gnu"
RUST_LINK = f"ossbot-{IMAGE_RUST}"
RUST_IMAGE_DIR = f"/opt/rust/rustup/toolchains/{IMAGE_RUST}-{RUST_TRIPLE}"
RUST_BAKED = frozenset({"rustc", "cargo", "rust-std", "rustfmt", "clippy"})
NODE_MAJORS = (26, 24, 22, 20, 18)
NODE_NEWEST_LTS = 24
NODE_CODENAMES = {"argon": 4, "boron": 6, "carbon": 8, "dubnium": 10, "erbium": 12, "fermium": 14,
                  "gallium": 16, "hydrogen": 18, "iron": 20, "jod": 22, "krypton": 24}  # fmt: skip
PYTHON_MIRROR = "https://github.com/astral-sh/python-build-standalone/releases/download"
COREPACK_FALLBACK = "/opt/ossbot/corepack/corepack-0.36.0.tgz"
MISE = "/usr/local/bin/mise"
UV = "/usr/local/bin/uv"
RUSTUP = "/opt/rust/cargo/bin/rustup"

INDEXES = {
    "node": "https://nodejs.org/dist/index.json",
    "go": "https://go.dev/dl/?mode=json&include=all",
    "java": "https://mise-java.jdx.dev/jvm/ga/linux/x86_64.json",
    "terraform": "https://releases.hashicorp.com/terraform/index.json",
}

# The Java versions each Gradle version can run on (V-G1, docs.gradle.org compatibility matrix): Java N needs at
# least this Gradle; Gradle >= 9 itself needs Java >= 17.
GRADLE_RUN: dict[int, tuple[int, ...]] = {
    8: (2, 0), 9: (4, 3), 10: (4, 7), 11: (5, 0), 12: (5, 4), 13: (6, 0), 14: (6, 3), 15: (6, 7), 16: (7, 0),
    17: (7, 3), 18: (7, 5), 19: (7, 6), 20: (8, 3), 21: (8, 5), 22: (8, 8), 23: (8, 10), 24: (8, 14),
    25: (9, 1, 0), 26: (9, 4, 0), 27: (9, 8, 0),
}  # fmt: skip

SDKMAN_VENDORS = {"tem": "temurin", "zulu": "zulu", "amzn": "corretto", "graalce": "graalvm-community",
                  "graal": "oracle-graalvm", "librca": "liberica", "sapmchn": "sapmachine", "sem": "semeru",
                  "ms": "microsoft", "oracle": "oracle", "open": "openjdk"}  # fmt: skip
JAVA_VENDORS = frozenset({"corretto", "dragonwell", "graalvm", "graalvm-community", "jetbrains", "kona", "liberica",
                          "liberica-nik", "mandrel", "microsoft", "openjdk", "oracle", "oracle-graalvm", "redhat",
                          "sapmachine", "semeru", "temurin", "trava", "zulu"})  # fmt: skip

# gradle/*.versions.toml [versions] keys read as a Gradle toolchain (integer values only)
CATALOG_TOOLCHAIN_KEYS = frozenset({"java-toolchain", "jvm-toolchain", "javaToolchain", "jvmToolchain",
                                    "jdk-toolchain", "toolchain", "java", "jdk"})  # fmt: skip
SKIP_DIRS = frozenset({".git", "node_modules", "target", "build", ".gradle", "dist", "vendor", ".venv", "venv",
                       "__pycache__", ".idea", "out", ".terraform"})  # fmt: skip

# --------------------------------------------------------------------------------- toolchain.env (§6.3)
ENV_KEYS = ("JAVA_HOME", "OSSBOT_GRADLE_JAVA_HOME", "OSSBOT_JDKS", "OSSBOT_PATH_PREPEND", "GOTOOLCHAIN",
            "RUSTUP_TOOLCHAIN", "RUSTUP_HOME", "CARGO_HOME", "COREPACK_HOME", "UV_PYTHON_INSTALL_DIR",
            "UV_PYTHON_INSTALL_MIRROR", "MISE_GLOBAL_CONFIG_FILE")  # fmt: skip
VALUE_RE = re.compile(r"[A-Za-z0-9_./:@+,=-]{1,1024}")
JVM_RE = re.compile(r"/usr/lib/jvm/java-(8|11|17|21|25)-openjdk-amd64")
OPT_RE = re.compile(r"/opt/(mise|rust|maven|uv-python)(/[A-Za-z0-9._+-]{1,128}){0,6}")
TOOLCHAIN_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,63}")


def _cache_re(work: str) -> re.Pattern[str]:
    return re.compile(rf"{re.escape(work)}/cache/(mise|rustup|cargo|corepack|corepack-bin|uv-python)"
                      r"(/[A-Za-z0-9._+-]{1,128}){0,6}")  # fmt: skip


def path_ok(value: str, work: str = "/work") -> bool:
    """A toolchain path: an image JDK, a directory under the task cache or under the image's /opt toolchains;
    never a ``.`` or ``..`` component."""
    if any(part in (".", "..") for part in value.split("/")[1:]):
        return False
    return bool(JVM_RE.fullmatch(value) or _cache_re(work).fullmatch(value) or OPT_RE.fullmatch(value))


def valid_env(key: str, value: str, work: str = "/work") -> bool:
    """The §6.3 rule for one ``toolchain.env`` line (the same rule as lib/tool_env.sh and execd)."""
    if key not in ENV_KEYS or not VALUE_RE.fullmatch(value):
        return False
    fixed = {"RUSTUP_HOME": f"{work}/cache/rustup", "CARGO_HOME": f"{work}/cache/cargo",
             "COREPACK_HOME": f"{work}/cache/corepack", "UV_PYTHON_INSTALL_DIR": f"{work}/cache/uv-python",
             "UV_PYTHON_INSTALL_MIRROR": PYTHON_MIRROR, "MISE_GLOBAL_CONFIG_FILE": f"{work}/.home/.ossbot/mise.toml",
             "GOTOOLCHAIN": "local"}  # fmt: skip
    if key in fixed:
        return value == fixed[key]
    if key == "RUSTUP_TOOLCHAIN":
        return TOOLCHAIN_NAME_RE.fullmatch(value) is not None
    if key in ("JAVA_HOME", "OSSBOT_GRADLE_JAVA_HOME"):
        return path_ok(value, work)
    if key == "OSSBOT_JDKS":
        return all(path_ok(p, work) for p in value.split(","))
    return all(path_ok(p, work) for p in value.split(":"))  # OSSBOT_PATH_PREPEND


def parse_env(text: str, work: str = "/work") -> dict[str, str]:
    """``toolchain.env`` read line by line: ``KEY=VALUE`` lines that pass ``valid_env``; the first occurrence of
    a key wins, anything else is ignored (a malformed line drops that key and the image default applies)."""
    out: dict[str, str] = {}
    for line in text.splitlines()[:64]:
        key, sep, value = line.partition("=")
        if sep and key not in out and valid_env(key, value, work):
            out[key] = value
    return out


# ------------------------------------------------------------------------------------------ versions
def vkey(version: str) -> tuple[int, ...]:
    """Numeric sort key of a version string (non-numeric parts ignored)."""
    return tuple(int(p) for p in re.findall(r"\d+", version)[:6])


def java_feature(value: str) -> int | None:
    """``1.8`` -> 8, ``17.0.2`` -> 17, ``21`` -> 21; 8..30 only."""
    m = re.match(r"(\d+)(?:\.(\d+))?", value)
    if m is None:
        return None
    feature = int(m.group(1))
    if feature == 1 and m.group(2):
        feature = int(m.group(2))
    return feature if JAVA_MIN <= feature <= JAVA_MAX else None


def _semver(text: str) -> tuple[list[int], int] | None:
    """A partial version ``1``, ``1.2``, ``1.2.3`` (``x``/``*`` wildcards) -> (numbers, how many were given)."""
    m = re.fullmatch(r"v?(\d+|[xX*])(?:\.(\d+|[xX*]))?(?:\.(\d+|[xX*]))?(?:[-+][0-9A-Za-z.-]*)?", text)
    if m is None:
        return None
    nums: list[int] = []
    for part in m.groups():
        if part is None or part in "xX*":
            break
        nums.append(int(part))
    return nums, len(nums)


def _bump(nums: list[int], at: int) -> tuple[int, int, int]:
    padded = [*nums, 0, 0, 0][:3]
    padded[at] += 1
    for i in range(at + 1, 3):
        padded[i] = 0
    return padded[0], padded[1], padded[2]


def _pad(nums: list[int]) -> tuple[int, int, int]:
    padded = [*nums, 0, 0, 0][:3]
    return padded[0], padded[1], padded[2]


def _comparator(token: str) -> list[tuple[str, tuple[int, int, int]]] | None:
    """One npm range comparator -> ANDed (op, version) bounds."""
    m = re.fullmatch(r"(>=|<=|>|<|=|\^|~)?(.*)", token)
    assert m is not None
    op, rest = m.group(1) or "", m.group(2)
    parsed = _semver(rest)
    if parsed is None:
        return None
    nums, given = parsed
    if given == 0:
        return [] if op in ("", "=", ">=", "<=", "^", "~") else None
    if op in ("", "="):
        return [(">=", _pad(nums))] + ([("<", _bump(nums, given - 1))] if given < 3 else [("<=", _pad(nums))])
    if op == ">=":
        return [(">=", _pad(nums))]
    if op == ">":
        return [(">", _pad(nums))] if given == 3 else [(">=", _bump(nums, given - 1))]
    if op == "<":
        return [("<", _pad(nums))]
    if op == "<=":
        return [("<=", _pad(nums))] if given == 3 else [("<", _bump(nums, given - 1))]
    if op == "~":
        return [(">=", _pad(nums)), ("<", _bump(nums, 0 if given == 1 else 1))]
    first = next((i for i, n in enumerate(nums) if n != 0), given - 1)  # ^: the first non-zero part
    return [(">=", _pad(nums)), ("<", _bump(nums, min(first, given - 1)))]


def _cmp(version: tuple[int, int, int], op: str, bound: tuple[int, int, int]) -> bool:
    return {">=": version >= bound, ">": version > bound, "<": version < bound, "<=": version <= bound}[op]


def npm_satisfies(version: str, rng: str) -> bool | None:
    """``version`` against an npm range (``>= > < <= = ^ ~ x || -``); None when the range can't be parsed."""
    ver = _semver(version)
    if ver is None or len(rng) > 256:
        return None
    v = _pad(ver[0])
    text = re.sub(r"(>=|<=|>|<|=|\^|~)\s+", r"\1", rng.strip())
    for raw_alt in text.split("||"):
        alt = raw_alt.strip()
        bounds: list[tuple[str, tuple[int, int, int]]] = []
        hyphen = re.fullmatch(r"(\S+)\s+-\s+(\S+)", alt)
        if hyphen:
            lo, hi = _semver(hyphen.group(1)), _semver(hyphen.group(2))
            if lo is None or hi is None:
                return None
            bounds.append((">=", _pad(lo[0])))
            bounds.append(("<=", _pad(hi[0])) if hi[1] == 3 else ("<", _bump(hi[0], max(hi[1] - 1, 0))))
        else:
            for token in alt.split() or ["*"]:
                got = _comparator(token)
                if got is None:
                    return None
                bounds += got
        if all(_cmp(v, op, b) for op, b in bounds):
            return True
    return False


def pep440_satisfies(version: tuple[int, ...], spec: str) -> bool | None:
    """``version`` against a PEP 440 specifier set (``>= > < <= == != ~= ===``, ``.*``); None when unparsable."""
    if len(spec) > 256:
        return None
    for clause in (c.strip() for c in spec.split(",")):
        m = re.fullmatch(r"(===|==|!=|~=|>=|<=|>|<)\s*(\d+(?:\.\d+)*)(\.\*)?", clause)
        if m is None:
            return None
        op, nums, star = m.group(1), tuple(int(p) for p in m.group(2).split(".")), bool(m.group(3))
        width = max(len(nums), len(version))
        v, n = version + (0,) * (width - len(version)), nums + (0,) * (width - len(nums))
        if op in ("==", "===") and star:
            ok = version[: len(nums)] == nums
        elif op == "!=" and star:
            ok = version[: len(nums)] != nums
        elif op == "~=":
            ok = v >= n and version[: len(nums) - 1] == nums[:-1]
        else:
            ok = {"==": v == n, "===": v == n, "!=": v != n, ">=": v >= n, "<=": v <= n, ">": v > n, "<": v < n}[op]
        if not ok:
            return False
    return True


def tf_satisfies(version: str, constraint: str) -> bool | None:
    """``version`` against Terraform version constraints (``= != > >= < <= ~>``, comma-separated)."""
    v = vkey(version)
    if len(constraint) > 256 or not v:
        return None
    for clause in (c.strip() for c in constraint.split(",")):
        m = re.fullmatch(r"(=|!=|>=|<=|>|<|~>)?\s*v?(\d+(?:\.\d+){0,2})", clause)
        if m is None:
            return None
        op, nums = m.group(1) or "=", tuple(int(p) for p in m.group(2).split("."))
        n = nums + (0,) * (3 - len(nums))
        vv = (*v, 0, 0, 0)[:3]
        if op == "~>":
            ok = vv >= n and (vv[: len(nums) - 1] == nums[:-1] if len(nums) > 1 else True)
        else:
            ok = {"=": vv == n, "!=": vv != n, ">=": vv >= n, "<=": vv <= n, ">": vv > n, "<": vv < n}[op]
        if not ok:
            return False
    return True


def go_version(text: str) -> tuple[int, int, int, int] | None:
    """``1.26``, ``1.26.10``, ``1.22rc1`` -> comparable (major, minor, patch, pre); a release sorts after its
    pre-releases (Go's own order)."""
    m = re.fullmatch(r"(?:go)?(\d+)\.(\d+)(?:\.(\d+))?((?:rc|beta)\d+)?", text)
    if m is None:
        return None
    return int(m.group(1)), int(m.group(2)), int(m.group(3) or 0), -1 if m.group(4) else 0


def _clean(text: object, limit: int = 64) -> str:
    """Untrusted text for the report: printable ASCII only, bounded."""
    return re.sub(r"[^\x20-\x7e]", "?", str(text))[:limit]


# ------------------------------------------------------------------------------------------- the report
@dataclass
class Report:
    mode: str
    tools: list[dict[str, Any]] = field(default_factory=list)
    skipped: list[dict[str, str]] = field(default_factory=list)
    budget_s: int = 0
    spent_s: float = 0.0
    error: str | None = None

    def add(self, tool: str, **fields: Any) -> dict[str, Any]:
        entry = {"tool": tool, **{k: v for k, v in fields.items() if v is not None}}
        if len(self.tools) < MAX_ENTRIES:
            self.tools.append(entry)
        return entry

    def skip(self, file: str, reason: str) -> None:
        if len(self.skipped) < MAX_SKIPPED:
            self.skipped.append({"file": _clean(file, 200), "reason": reason})

    def gaps(self) -> list[str]:
        out = []
        for entry in self.tools:
            if entry.get("gap") and entry["gap"] != "invalid":
                want = entry.get("declared") or entry.get("chosen") or "?"
                out.append(f"{entry['tool']} {want}: {entry['gap']}")
        return out

    def to_json(self) -> dict[str, Any]:
        doc: dict[str, Any] = {
            "version": 1,
            "mode": self.mode,
            "tools": self.tools,
            "skipped": self.skipped,
            "gaps": self.gaps(),
            "budget_s": self.budget_s,
            "spent_s": round(self.spent_s, 1),
        }
        if self.error:
            doc["error"] = self.error
        return doc


# --------------------------------------------------------------------------------------- reading the repo
class Repo:
    """Read-only, bounded access to the untrusted repo: no symlink is followed, files <= 1 MiB, <= 400 reads,
    walks <= 3 directories deep."""

    def __init__(self, root: Path, report: Report) -> None:
        self.root = root
        self.report = report
        self.reads = 0
        self.real_root = os.path.realpath(root)

    def _clean_path(self, rel: str) -> str | None:
        full = os.path.join(self.root, rel)
        if os.path.realpath(full) != os.path.join(self.real_root, rel):  # a symlink somewhere on the way
            return None
        return full

    def exists(self, rel: str) -> bool:
        full = self._clean_path(rel)
        return full is not None and os.path.isfile(full) and not os.path.islink(full)

    def read(self, rel: str) -> str | None:
        full = self._clean_path(rel)
        if full is None or not os.path.lexists(full):
            return None
        if self.reads >= MAX_FILES:
            self.report.skip(rel, "file_limit")
            return None
        self.reads += 1
        try:
            fd = os.open(full, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
        except OSError:
            self.report.skip(rel, "unreadable")
            return None
        with os.fdopen(fd, "rb") as fh:
            st = os.fstat(fh.fileno())
            if st.st_mode & 0o170000 != 0o100000:  # not S_ISREG
                self.report.skip(rel, "not_a_file")
                return None
            if st.st_size > MAX_FILE:
                self.report.skip(rel, "oversize")
                return None
            data = fh.read(MAX_FILE + 1)
        if len(data) > MAX_FILE:
            self.report.skip(rel, "oversize")
            return None
        return data.decode("utf-8", "replace")

    def walk(self, match: Callable[[str], bool], depth: int = MAX_DEPTH) -> list[str]:
        """Relative paths of regular files whose name matches, at most ``depth`` directories below the root."""
        found: list[str] = []
        for top, dirs, files in os.walk(self.root, followlinks=False):
            rel_top = os.path.relpath(top, self.root)
            level = 0 if rel_top == "." else rel_top.count(os.sep) + 1
            dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not os.path.islink(os.path.join(top, d)))
            if level >= depth:
                dirs[:] = []
            for name in sorted(files):
                path = os.path.join(top, name)
                if match(name) and not os.path.islink(path):
                    found.append(os.path.normpath(os.path.join(rel_top, name)))
                    if len(found) >= MAX_FILES:
                        return found
        return found

    def listdir(self, rel: str, suffixes: tuple[str, ...]) -> list[str]:
        """Regular files with one of ``suffixes`` directly in the real directory ``rel`` (sorted, <= 50)."""
        full = self._clean_path(rel)
        if full is None or os.path.islink(full) or not os.path.isdir(full):
            return []
        with os.scandir(full) as entries:
            names = sorted(e.name for e in entries if e.is_file(follow_symlinks=False) and e.name.endswith(suffixes))
        return [os.path.join(rel, n) for n in names[:50]]

    def toml(self, rel: str) -> dict[str, Any] | None:
        text = self.read(rel)
        if text is None:
            return None
        try:
            return tomllib.loads(text)
        except (tomllib.TOMLDecodeError, ValueError):
            self.report.skip(rel, "unparsable")
            return None

    def json(self, rel: str) -> Any:
        text = self.read(rel)
        if text is None:
            return None
        try:
            return json.loads(text)
        except ValueError:
            self.report.skip(rel, "unparsable")
            return None

    def xml(self, rel: str) -> ET.Element | None:
        text = self.read(rel)
        if text is None:
            return None
        if "<!ENTITY" in text or "<!DOCTYPE" in text:  # no DTDs at all: nothing to expand
            self.report.skip(rel, "doctype")
            return None
        try:
            return ET.fromstring(text)  # noqa: S314 - <= 1 MiB, no DOCTYPE, expat never fetches entities
        except ET.ParseError:
            self.report.skip(rel, "unparsable")
            return None


def _pom_value(raw: str | None, scopes: tuple[dict[str, str], ...]) -> str | None:
    """A pom value; one ``${prop}`` level resolved from the same pom, then the root pom."""
    if raw is None:
        return None
    m = re.fullmatch(r"\$\{([A-Za-z0-9_.-]{1,100})\}", raw.strip())
    if m is None:
        return raw.strip()
    return next((s[m.group(1)] for s in scopes if m.group(1) in s), None)


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _children(node: ET.Element, name: str) -> list[ET.Element]:
    return [c for c in node if _local(c.tag) == name]


def _child_text(node: ET.Element, name: str) -> str | None:
    for c in node:
        if _local(c.tag) == name and c.text:
            return c.text.strip()
    return None


# ------------------------------------------------------------------------------------------- declarations
@dataclass(frozen=True)
class JavaSpec:
    feature: int
    vendor: str | None = None  # None = any vendor (the image's Ubuntu OpenJDK satisfies it)
    exact: str | None = None  # a full vendor version for mise ("22.0.2+9")
    source: str = "image"
    declared: str | None = None


def parse_java(value: str, source: str, sdkman: bool = False) -> JavaSpec | None:
    """A Java declaration: mise/.java-version style ``[vendor-]version`` or sdkman ``version-suffix``."""
    value = value.strip()
    if not VERSION_RE.fullmatch(value):
        return None
    vendor: str | None = None
    version = value
    if sdkman:
        head, _, suffix = value.rpartition("-")
        if head and suffix in SDKMAN_VENDORS:
            vendor, version = SDKMAN_VENDORS[suffix], head
        elif head:
            version = head
        exact = None  # sdkman's version names are not mise's: only the feature and the vendor carry over
    else:
        m = re.fullmatch(r"([a-z][a-z-]*?)-(\d.*)", value)
        if m:
            vendor = m.group(1) if m.group(1) in JAVA_VENDORS else None
            version = m.group(2)
        exact = version if vendor and re.search(r"[.+]", version) else None
    feature = java_feature(version)
    if feature is None:
        return None
    if vendor == "openjdk" and exact is None:
        vendor = None
    return JavaSpec(feature, vendor, exact, source, _clean(value))


@dataclass
class Ctx:
    repo: Path
    state: Path
    cache: Path
    work: str
    mode: str
    budget: int
    log: Path | None = None
    run: Callable[[list[str], dict[str, str], str, int], tuple[int, str]] | None = None
    fetch: Callable[[str], bytes | None] | None = None
    system_python: tuple[int, ...] | None = None
    image_pythons: Path = Path("/opt/uv-python")
    clock: Callable[[], float] = time.monotonic


@dataclass
class Got:
    path: str | None
    action: str
    exit_code: int | None = None
    gap: str | None = None
    tail: str | None = None


class Resolver:
    def __init__(self, ctx: Ctx) -> None:
        self.ctx = ctx
        self.report = Report(ctx.mode, budget_s=ctx.budget)
        self.repo = Repo(ctx.repo, self.report)
        self.started = ctx.clock()
        self.done: dict[str, Got] = {}
        self.indexes: dict[str, Any] = {}
        self.env: dict[str, str] = {}
        self.path: list[str] = []
        self.jdks: list[str] = []
        self.mise_tools: dict[str, str] = {}
        self.mise_decl: dict[str, tuple[str, str]] = {}

    # ----------------------------------------------------------------------------------- installers
    def _remaining(self) -> float:
        return self.ctx.budget - (self.ctx.clock() - self.started)

    def _run(self, argv: list[str], extra: dict[str, str], cwd: str, timeout: int) -> tuple[int, str]:
        if self.ctx.run is not None:
            return self.ctx.run(argv, extra, cwd, timeout)
        env = {**os.environ, **extra}
        log = open(self.ctx.log, "ab") if self.ctx.log else None  # noqa: SIM115 - closed below
        try:
            proc = subprocess.run(argv, env=env, cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                  stderr=subprocess.STDOUT, timeout=timeout, check=False)  # fmt: skip
            out = proc.stdout or b""
            code = proc.returncode
        except subprocess.TimeoutExpired as exc:
            out, code = (exc.stdout or b"") if isinstance(exc.stdout, bytes) else b"", 124
        except OSError as exc:
            out, code = str(exc).encode(), 127
        if log is not None:
            with log:
                log.write(f"$ {' '.join(argv)}\n".encode() + out[-200_000:] + f"\n[exit {code}]\n".encode())
        return code, out[-TAIL:].decode("utf-8", "replace")

    def ensure(self, key: str, argv: list[str], check: Callable[[], str | None], *, cwd: str | None = None,
               env: dict[str, str] | None = None, after: Callable[[int], None] | None = None) -> Got:  # fmt: skip
        """One installer: already there -> ``cached``; select -> ``not_installed``; an exhausted budget ->
        ``budget_exceeded``; else run it (timeout min(900, remaining)) and check again."""
        if key in self.done:
            return self.done[key]
        found = check()
        if found:
            got = Got(found, "cached")
        elif self.ctx.mode != "install":
            got = Got(None, "select", gap="not_installed")
        elif self._remaining() <= 0:
            got = Got(None, "skipped", gap="budget_exceeded")
        else:
            timeout = max(1, int(min(INSTALL_TIMEOUT, self._remaining())))
            code, tail = self._run(argv, env or {}, cwd or str(self.ctx.repo), timeout)
            if after is not None:
                after(code)
            found = check()
            if code == 0 and found:
                got = Got(found, "install", 0)
            else:
                got = Got(None, "install", code, gap="timeout" if code == 124 else "install_failed",
                          tail=_clean(tail, TAIL))  # fmt: skip
        self.done[key] = got
        return got

    def _installed(self, tool: str, name: str, exact: bool = True) -> str | None:
        """The install dir of mise ``tool`` ``name`` under the task's MISE_DATA_DIR (or with ``exact=False``
        the newest real directory whose name starts with it)."""
        base = self.ctx.cache / "mise" / "installs" / tool
        if exact:
            path = base / name
            return str(path) if path.is_dir() and not path.is_symlink() else None
        if not base.is_dir():
            return None
        cands = [p for p in base.iterdir() if p.is_dir() and not p.is_symlink()
                 and (p.name == name or p.name.startswith((f"{name}.", f"{name}+")))]  # fmt: skip
        return str(max(cands, key=lambda p: vkey(p.name))) if cands else None

    def _mise_install(self, tool: str, version: str, by_mise: bool, check_bin: str | None = None,
                      env: dict[str, str] | None = None) -> Got:  # fmt: skip
        def check() -> str | None:
            found = self._installed(tool, version, exact=not by_mise)
            if found and check_bin and not (Path(found) / check_bin).exists():
                return None
            return found

        extra = {"MISE_DATA_DIR": str(self.ctx.cache / "mise"), **(env or {})}
        return self.ensure(f"{tool}@{version}", [MISE, "install", f"{tool}@{version}"], check, env=extra)

    # --------------------------------------------------------------------------------------- indexes
    def index(self, name: str) -> Any:
        """An ecosystem index, fetched at most once per task through the proxy (``urllib`` honours HTTPS_PROXY)
        and cached under the task cache; offline (``select``) only the cache is read."""
        if name in self.indexes:
            return self.indexes[name]
        cache = self.ctx.cache / "indexes" / f"{name}.json"
        data: bytes | None = None
        with contextlib.suppress(OSError):
            if cache.is_file() and not cache.is_symlink() and cache.stat().st_size <= INDEX_MAX:
                data = cache.read_bytes()
        if data is None and self.ctx.mode == "install":
            data = (self.ctx.fetch or fetch_url)(INDEXES[name])
            if data is not None:
                with contextlib.suppress(OSError):
                    cache.parent.mkdir(parents=True, exist_ok=True)
                    tmp = cache.with_suffix(".tmp")
                    tmp.unlink(missing_ok=True)
                    tmp.write_bytes(data)
                    tmp.replace(cache)
        parsed: Any = None
        if data is not None:
            try:
                parsed = json.loads(data)
            except ValueError:
                parsed = None
        self.indexes[name] = parsed
        return parsed

    def java_version(self, vendor: str, feature: int) -> str | None:
        """The newest GA ``vendor`` JDK ``feature`` of the mise-java metadata (plain tar.gz, no musl/fx)."""
        idx = self.index("java")
        if not isinstance(idx, list):
            return None
        best: str | None = None
        for e in idx:
            if not isinstance(e, dict) or e.get("vendor") != vendor or e.get("image_type") != "jdk":
                continue
            version = str(e.get("version", ""))
            if e.get("file_type") != "tar.gz" or e.get("features") or not VERSION_RE.fullmatch(version):
                continue
            if java_feature(version) == feature and (best is None or vkey(version) > vkey(best)):
                best = version
        return best

    def node_versions(self) -> list[str] | None:
        idx = self.index("node")
        if not isinstance(idx, list):
            return None
        out = []
        for e in idx:
            v = str(e.get("version", "")).lstrip("v") if isinstance(e, dict) else ""
            if re.fullmatch(r"\d+\.\d+\.\d+", v):
                out.append(v)
        return out

    def node_lts_major(self) -> int:
        idx = self.index("node")
        majors = []
        for e in idx if isinstance(idx, list) else []:
            if isinstance(e, dict) and e.get("lts") and re.fullmatch(r"v\d+\.\d+\.\d+", str(e.get("version"))):
                majors.append(int(str(e["version"]).lstrip("v").split(".")[0]))
        return max(majors) if majors else NODE_NEWEST_LTS

    def go_newest(self, prefix: tuple[int, int, int, int], whole: int) -> str | None:
        """The newest stable Go release matching the first ``whole`` parts of ``prefix``."""
        idx = self.index("go")
        if not isinstance(idx, list):
            return None
        best: tuple[tuple[int, int, int, int], str] | None = None
        for e in idx:
            if not isinstance(e, dict) or not e.get("stable"):
                continue
            v = go_version(str(e.get("version", "")))
            if v is None or v[:whole] != prefix[:whole]:
                continue
            if best is None or v > best[0]:
                best = (v, str(e["version"]).removeprefix("go"))
        return best[1] if best else None

    def tf_newest(self, pred: Callable[[str], bool]) -> str | None:
        idx = self.index("terraform")
        versions = idx.get("versions") if isinstance(idx, dict) else None
        if not isinstance(versions, dict):
            return None
        cands = [v for v in versions if re.fullmatch(r"\d+\.\d+\.\d+", str(v)) and pred(str(v))]
        return max(cands, key=vkey) if cands else None

    # ---------------------------------------------------------------------------------- declarations
    def read_mise(self) -> None:
        """``mise.toml``/``.mise.toml`` ``[tools]``, then ``.tool-versions`` (first declaration of a tool wins)."""
        aliases = {"nodejs": "node", "golang": "go", "core:node": "node", "core:go": "go", "core:java": "java",
                   "core:python": "python", "core:ruby": "ruby", "core:rust": "rust"}  # fmt: skip
        for rel in ("mise.toml", ".mise.toml"):
            doc = self.repo.toml(rel)
            tools = doc.get("tools") if isinstance(doc, dict) else None
            if not isinstance(tools, dict):
                continue
            for name, raw in tools.items():
                value = raw[0] if isinstance(raw, list) and raw else raw
                if isinstance(value, dict):
                    value = value.get("version")
                tool = aliases.get(str(name), str(name))
                if isinstance(value, str) and TOOL_RE.fullmatch(tool) and tool not in self.mise_decl:
                    self.mise_decl[tool] = (value.strip(), rel)
        text = self.repo.read(".tool-versions")
        for line in (text or "").splitlines():
            parts = line.split("#", 1)[0].split()
            if len(parts) >= 2:
                tool = aliases.get(parts[0], parts[0])
                if TOOL_RE.fullmatch(tool) and tool not in self.mise_decl:
                    self.mise_decl[tool] = (parts[1], ".tool-versions")

    def sdkmanrc(self) -> dict[str, str]:
        out: dict[str, str] = {}
        for line in (self.repo.read(".sdkmanrc") or "").splitlines():
            key, sep, value = line.split("#", 1)[0].strip().partition("=")
            if sep and key.strip() in ("java", "maven", "gradle"):
                out.setdefault(key.strip(), value.strip())
        return out

    def first_line(self, rel: str) -> str | None:
        text = self.repo.read(rel)
        if text is None:
            return None
        for raw in text.splitlines():
            line = raw.split("#", 1)[0].strip()
            if line:
                return line
        return None

    def workflow_ints(self, key: str) -> list[int]:
        """Integer ``key:`` values in .github/workflows/*.y*ml (a hint only, §6.2)."""
        out: set[int] = set()
        for rel in self.repo.listdir(os.path.join(".github", "workflows"), (".yml", ".yaml")):
            for m in re.finditer(rf"(?m)^\s*-?\s*{key}:\s*(\[[^\]\n]*\]|\S+)", self.repo.read(rel) or ""):
                for n in re.findall(r"(?<![\d.])(\d{1,2})(?![\d.])", m.group(1)):
                    out.add(int(n))
        return sorted(out)

    # -------------------------------------------------------------------------------------------- Java
    def jdk(self, spec: JavaSpec, role: str) -> tuple[str, int]:
        """A JDK home for ``spec`` (image, cached or installed); a missing one falls back to the smallest image JDK
        >= the feature (else 21) with a gap. One report entry per role."""
        feature = spec.feature
        if spec.vendor is None and feature in IMAGE_JDKS:
            self.report.add(role, declared=spec.declared, source=spec.source, chosen=str(feature),
                            path=JDK_HOME.format(feature), action="image")  # fmt: skip
            return JDK_HOME.format(feature), feature
        vendor = spec.vendor or "temurin"
        version, resolved_by = spec.exact, "declared"
        if version is None:
            version = self.java_version(vendor, feature)
            resolved_by = "index"
        by_mise = version is None
        name = f"{vendor}-{version}" if version else f"{vendor}-{feature}"
        if by_mise:
            resolved_by = "mise"
        got = self._mise_install("java", name, by_mise, check_bin="bin/java")
        if got.path and path_ok(got.path, self.ctx.work):
            self.report.add(role, declared=spec.declared, source=spec.source, chosen=name, path=got.path,
                            action=got.action, exit_code=got.exit_code, resolved_by=resolved_by)  # fmt: skip
            if got.path not in self.jdks:
                self.jdks.append(got.path)
            return got.path, feature
        fallback = next((j for j in IMAGE_JDKS if j >= feature), DEFAULT_JDK)
        self.report.add(role, declared=spec.declared, source=spec.source, chosen=f"{name} -> image {fallback}",
                        path=JDK_HOME.format(fallback), action=got.action, exit_code=got.exit_code,
                        gap=got.gap or "install_failed", resolved_by=resolved_by, tail=got.tail)  # fmt: skip
        return JDK_HOME.format(fallback), fallback

    def maven_needs(self) -> tuple[list[JavaSpec], list[JavaSpec]]:
        """(compile needs of every pom <= 3 deep, toolchain JDKs: the toolchains plugin and the repo's own
        toolchains.xml). One ``${prop}`` level is resolved from the same pom or the root pom."""
        poms = self.repo.walk(lambda n: n == "pom.xml")
        props_root: dict[str, str] = {}
        needs: list[JavaSpec] = []
        tcs: list[JavaSpec] = []
        for rel in poms:
            root = self.repo.xml(rel)
            if root is None:
                continue
            props: dict[str, str] = {}
            for p in _children(root, "properties"):
                for c in p:
                    if c.text:
                        props[_local(c.tag)] = c.text.strip()
            if rel == "pom.xml":
                props_root = props
            scopes = (props, props_root)
            found: list[tuple[str, str | None]] = [
                (k, _pom_value(props.get(k), scopes))
                for k in ("maven.compiler.release", "maven.compiler.target", "maven.compiler.source", "java.version")
            ]
            for plugin in root.iter():
                if _local(plugin.tag) != "plugin":
                    continue
                artifact = _child_text(plugin, "artifactId")
                for conf in _children(plugin, "configuration"):
                    if artifact == "maven-compiler-plugin":
                        found += [(f"compiler {k}", _pom_value(_child_text(conf, k), scopes))
                                  for k in ("release", "target", "source")]  # fmt: skip
                    elif artifact == "maven-toolchains-plugin":
                        versions = [_pom_value(v.text, scopes) for v in conf.iter() if _local(v.tag) == "version"]
                        for v in versions:
                            f = java_feature(re.sub(r"^[\[(]", "", v or ""))
                            if f is not None:
                                tcs.append(JavaSpec(f, None, None, rel, _clean(v)))
            for label, raw in found:
                f = java_feature(raw or "") if raw and VERSION_RE.fullmatch(raw) else None
                if f is not None:
                    needs.append(JavaSpec(f, None, None, f"{rel} {label}", _clean(raw)))
        for rel in (os.path.join(".mvn", "toolchains.xml"), "toolchains.xml"):
            root = self.repo.xml(rel)
            if root is None:
                continue
            for tc in root.iter():
                if _local(tc.tag) != "toolchain" or _child_text(tc, "type") != "jdk":
                    continue
                for provides in _children(tc, "provides"):
                    raw = _child_text(provides, "version")
                    f = java_feature(re.sub(r"^[\[(]", "", raw or ""))
                    if f is not None:
                        tcs.append(JavaSpec(f, None, None, rel, _clean(raw)))
        return needs, tcs

    def gradle_facts(self) -> dict[str, Any]:
        facts: dict[str, Any] = {"wrapper": None, "daemon": None, "toolchains": [], "declared": False, "source": []}
        props = self.repo.read(os.path.join("gradle", "wrapper", "gradle-wrapper.properties"))
        if props is not None:
            m = re.search(r"gradle-(\d+(?:\.\d+){0,2})(?:-[0-9A-Za-z.-]+)?-(?:bin|all)\.zip", props)
            facts["wrapper"] = m.group(1) if m else "unknown"
        daemon = self.repo.read(os.path.join("gradle", "gradle-daemon-jvm.properties"))
        m = re.search(r"(?m)^\s*toolchainVersion\s*=\s*(\d+)\s*$", daemon or "")
        if m and java_feature(m.group(1)):
            facts["daemon"] = int(m.group(1))
        scripts = self.repo.walk(lambda n: n.endswith((".gradle", ".gradle.kts")))
        for rel in scripts:
            text = self.repo.read(rel) or ""
            if re.search(r"JavaLanguageVersion\s*\.\s*of\s*\(|jvmToolchain\s*[({]|\btoolchain\s*\{", text):
                facts["declared"] = True
            for m in re.finditer(r"JavaLanguageVersion\s*\.\s*of\s*\(\s*(\d+)\s*\)|jvmToolchain\s*\(\s*(\d+)\s*\)",
                                 text):  # fmt: skip
                f = java_feature(m.group(1) or m.group(2))
                if f is not None:
                    facts["toolchains"].append((f, rel))
            pat = (r"(?:source|target)Compatibility\s*=\s*(?:JavaVersion\s*\.\s*(?:VERSION_(?:1_)?(\d+)|toVersion\s*"
                   r"\(\s*['\"]?(?:1\.)?(\d+))|['\"]?(?:1\.)?(\d+)['\"]?)")  # fmt: skip
            for m in re.finditer(pat, text):
                f = java_feature(m.group(1) or m.group(2) or m.group(3))
                if f is not None:
                    facts["source"].append((f, rel))
        for rel in self.repo.walk(lambda n: n.endswith(".versions.toml"), depth=1):
            doc = self.repo.toml(rel)
            versions = doc.get("versions") if isinstance(doc, dict) else None
            for key, raw in versions.items() if isinstance(versions, dict) else ():
                if key in CATALOG_TOOLCHAIN_KEYS and isinstance(raw, (str, int)) and re.fullmatch(r"\d+", str(raw)):
                    f = java_feature(str(raw))
                    if f is not None:
                        facts["toolchains"].append((f, rel))
                        facts["declared"] = True
        facts["present"] = bool(props is not None or scripts)
        return facts

    def resolve_java(self) -> None:
        sdk = self.sdkmanrc()
        explicit: JavaSpec | None = None
        mise_java = self.mise_decl.get("java")
        candidates: list[tuple[str | None, str, bool]] = [
            (mise_java[0] if mise_java else None, mise_java[1] if mise_java else "", False),
            (sdk.get("java"), ".sdkmanrc", True),
            (self.first_line(".java-version"), ".java-version", False),
        ]
        for value, source, sdkman in candidates:
            if value is None or explicit is not None:
                continue
            explicit = parse_java(value, source, sdkman)
            if explicit is None:
                self.report.add("java", declared=_clean(value), source=source, gap="invalid")
        needs, maven_tcs = self.maven_needs()
        maven = self.repo.exists("pom.xml") or bool(needs)
        mw = self.repo.read(os.path.join(".mvn", "wrapper", "maven-wrapper.properties"))
        mwv = re.search(r"apache-maven-(\d+(?:\.\d+){1,2}(?:-[A-Za-z0-9.-]+)?)-bin\.zip", mw or "")
        maven_version = mwv.group(1) if mwv else sdk.get("maven") or self.mise_decl.get("maven", (None, ""))[0]
        need = max((s.feature for s in [*needs, *maven_tcs]), default=None)
        if maven_version and vkey(maven_version)[:1] >= (4,):
            need = max(need or 0, 17)  # Maven 4 runs on Java >= 17
        jvm_config = self.repo.read(os.path.join(".mvn", "jvm.config"))
        if jvm_config and "--enable-preview" in jvm_config:
            self.report.add("java", source=".mvn/jvm.config", hint="preview")
        gradle = self.gradle_facts()
        if not (explicit or maven or gradle["present"]):
            return
        # JAVA_HOME (the Maven JVM rule, §6.2)
        if explicit is not None and (need is None or explicit.feature >= need):
            home, home_f = self.jdk(explicit, "java")
        elif need is not None:
            src = max([*needs, *maven_tcs], key=lambda s: s.feature, default=None)
            if src is None or src.feature < need:  # raised by Maven 4 alone
                src = JavaSpec(need, None, None, "Maven 4 runs on Java >= 17", maven_version)
            home, home_f = self.jdk(JavaSpec(need, None, None, src.source, src.declared), "java")
        else:
            hints = self.workflow_ints("java-version") if not explicit else []
            hint = max((h for h in hints if JAVA_MIN <= h <= JAVA_MAX), default=None)
            f = next((j for j in IMAGE_JDKS if hint is not None and j >= hint), DEFAULT_JDK)
            source = ".github/workflows java-version (hint)" if hint else "image"
            home, home_f = self.jdk(JavaSpec(f, None, None, source, str(hint) if hint else None), "java")
        for spec in maven_tcs:
            self.jdk(spec, "java-toolchain")
        if maven:
            self.report.add("maven", declared=_clean(maven_version) if maven_version else None,
                            chosen="mvnw" if self.repo.exists("mvnw") else f"/opt/maven {IMAGE_MAVEN}",
                            action="report")  # fmt: skip
        if gradle["present"]:
            home, home_f = self.resolve_gradle(gradle, explicit, home, home_f, maven)
        self.env["JAVA_HOME"] = home
        self.resolve_sdkman_gradle(sdk, gradle)

    def resolve_gradle(self, g: dict[str, Any], explicit: JavaSpec | None, home: str, home_f: int,
                       maven: bool) -> tuple[str, int]:  # fmt: skip
        wrapper = g["wrapper"]
        gv = vkey(wrapper) if wrapper and wrapper != "unknown" else None

        def runnable(f: int) -> bool:
            if gv is None:
                return f == DEFAULT_JDK
            if gv >= (9,) and f < 17:
                return False
            least = GRADLE_RUN.get(f)
            return least is not None and gv >= least

        self.report.add("gradle", declared=_clean(wrapper) if wrapper else None, action="report",
                        source="gradle/wrapper/gradle-wrapper.properties" if wrapper else None)  # fmt: skip
        if g["daemon"] is not None:
            daemon, daemon_f = self.jdk(JavaSpec(g["daemon"], None, None, "gradle/gradle-daemon-jvm.properties",
                                                 str(g["daemon"])), "java-gradle")  # fmt: skip
        elif explicit is not None and runnable(explicit.feature):
            daemon, daemon_f = self.jdk(explicit, "java-gradle")
        else:
            best = max((j for j in IMAGE_JDKS if runnable(j)), default=DEFAULT_JDK)
            daemon, daemon_f = self.jdk(JavaSpec(best, None, None, f"Gradle {wrapper or 'unknown'} run matrix"),
                                        "java-gradle")  # fmt: skip
        src = max((f for f, _ in g["source"]), default=None) if not g["declared"] else None
        if src is not None and daemon_f < src and runnable(src):
            daemon, daemon_f = self.jdk(JavaSpec(src, None, None, "sourceCompatibility", str(src)), "java-gradle")
        compile_set: dict[int, str] = dict(g["toolchains"])
        if src is not None:
            compile_set.setdefault(src, "sourceCompatibility")
        for f, rel in sorted(compile_set.items()):
            self.jdk(JavaSpec(f, None, None, rel, str(f)), "java-toolchain")
        if g["declared"] and not g["toolchains"]:
            self.report.add(
                "java-toolchain",
                declared="computed",
                source="Gradle scripts",
                hint="computed toolchain version: Gradle provisions it (auto-download with a network)",
            )
        self.env["OSSBOT_GRADLE_JAVA_HOME"] = daemon
        if wrapper and not maven and not runnable(home_f):
            return daemon, daemon_f  # the wrapper's own client JVM must be one this Gradle can run on
        return home, home_f

    def resolve_sdkman_gradle(self, sdk: dict[str, str], g: dict[str, Any]) -> None:
        """C-N4: ``.sdkmanrc gradle=<v>`` -> ``mise install gradle@<v>`` only without a Gradle wrapper."""
        value = sdk.get("gradle")
        if value is None:
            return
        if not re.fullmatch(r"\d+\.\d+(?:\.\d+)?", value):
            self.report.add("gradle", declared=_clean(value), source=".sdkmanrc", gap="invalid")
            return
        if g["wrapper"] is not None:
            self.report.add("gradle", declared=value, source=".sdkmanrc", action="report",
                            hint="gradle/wrapper/gradle-wrapper.properties decides the Gradle version")  # fmt: skip
            return
        got = self._mise_install("gradle", value, False, check_bin="bin/gradle")
        self._record("gradle", value, ".sdkmanrc", value, got, "bin")

    def _record(self, tool: str, declared: str | None, source: str | None, chosen: str, got: Got,
                bindir: str | None, resolved_by: str | None = None) -> None:  # fmt: skip
        if got.path and path_ok(got.path, self.ctx.work):
            path = str(Path(got.path) / bindir) if bindir and (Path(got.path) / bindir).is_dir() else got.path
            if path_ok(path, self.ctx.work) and path not in self.path:
                self.path.append(path)
            self.report.add(tool, declared=declared, source=source, chosen=chosen, path=got.path, action=got.action,
                            exit_code=got.exit_code, resolved_by=resolved_by)  # fmt: skip
        else:
            self.report.add(tool, declared=declared, source=source, chosen=chosen, action=got.action,
                            exit_code=got.exit_code, gap=got.gap or "install_failed", resolved_by=resolved_by,
                            tail=got.tail)  # fmt: skip

    # -------------------------------------------------------------------------------------------- Node
    def resolve_node(self) -> str:
        """The Node bin dir in use (the image's when nothing is declared or it satisfies the declaration)."""
        raw = self.repo.json("package.json")
        pkg: dict[str, Any] = raw if isinstance(raw, dict) else {}
        engines: dict[str, Any] = pkg["engines"] if isinstance(pkg.get("engines"), dict) else {}
        decl: tuple[str, str] | None = None
        mise_node = self.mise_decl.get("node")
        candidates: list[tuple[str | None, str]] = [
            (mise_node[0] if mise_node else None, mise_node[1] if mise_node else ""),
            (self.first_line(".nvmrc"), ".nvmrc"),
            (self.first_line(".node-version"), ".node-version"),
            (engines.get("node") if isinstance(engines.get("node"), str) else None, "package.json engines.node"),
        ]
        for value, source in candidates:
            if value:
                decl = (value.strip(), source)
                break
        if decl is None and (pkg or self.repo.exists(".nvmrc")):
            hints = self.workflow_ints("node-version")
            if len(hints) == 1:
                decl = (str(hints[0]), ".github/workflows node-version (hint)")
        bindir = IMAGE_NODE_BIN
        if decl is not None:
            bindir = self._node(decl[0], decl[1])
        self.resolve_package_manager(pkg, bindir)
        return bindir

    def _node(self, value: str, source: str) -> str:
        declared = _clean(value)
        target: str | None = None  # an exact version or a prefix
        if source == "package.json engines.node":
            if npm_satisfies(IMAGE_NODE, value):
                self.report.add("node", declared=declared, source=source, chosen=IMAGE_NODE, path=IMAGE_NODE_BIN,
                                action="image")  # fmt: skip
                return IMAGE_NODE_BIN
            versions = self.node_versions()
            for major in NODE_MAJORS:
                if versions is not None:
                    cands = [v for v in versions if vkey(v)[0] == major and npm_satisfies(v, value)]
                    if cands:
                        target = max(cands, key=vkey)
                        break
                elif npm_satisfies(f"{major}.0.0", value) or npm_satisfies(f"{major}.99.99", value):
                    target = str(major)
                    break
            if target is None:
                self.report.add("node", declared=declared, source=source, gap="unsatisfiable")
                return IMAGE_NODE_BIN
        else:
            low = value.lower()
            if low in ("node", "stable"):
                target = IMAGE_NODE
            elif low in ("lts/*", "lts", "lts/latest"):
                target = str(self.node_lts_major())
            elif low.startswith("lts/") and low[4:] in NODE_CODENAMES:
                target = str(NODE_CODENAMES[low[4:]])
            elif low == "latest":
                versions = self.node_versions()
                target = max(versions, key=vkey) if versions else None
            elif re.fullmatch(r"v?\d+(?:\.\d+){0,2}", low):
                target = low.lstrip("v")
            if target is None:
                self.report.add("node", declared=declared, source=source, gap="invalid")
                return IMAGE_NODE_BIN
        if target == IMAGE_NODE or IMAGE_NODE.startswith(f"{target}."):
            self.report.add("node", declared=declared, source=source, chosen=IMAGE_NODE, path=IMAGE_NODE_BIN,
                            action="image")  # fmt: skip
            return IMAGE_NODE_BIN
        exact, resolved_by = target, "declared"
        if target.count(".") < 2:
            versions = self.node_versions()
            cands = [v for v in versions or [] if v.startswith(f"{target}.")]
            exact, resolved_by = (max(cands, key=vkey), "index") if cands else (target, "mise")
        got = self._mise_install("node", exact, resolved_by == "mise", check_bin="bin/node")
        self._record("node", declared, source, exact, got, "bin", resolved_by)
        if got.path and path_ok(got.path, self.ctx.work):
            self.mise_tools["node"] = Path(got.path).name
            return str(Path(got.path) / "bin")
        return IMAGE_NODE_BIN

    def resolve_package_manager(self, pkg: dict[str, Any], node_bin: str) -> None:
        raw = pkg.get("packageManager")
        if not isinstance(raw, str):
            return
        m = re.fullmatch(r"(pnpm|yarn|npm)@(\d+\.\d+\.\d+(?:-[0-9A-Za-z.]+)?)(?:\+[A-Za-z0-9.=/+-]{1,200})?", raw)
        if m is None:
            self.report.add("package-manager", declared=_clean(raw), source="package.json packageManager",
                            gap="invalid")  # fmt: skip
            return
        name, version = m.group(1), m.group(2)
        if name == "npm":
            self.report.add("npm", declared=version, source="package.json packageManager", action="report",
                            hint="npm comes with Node")  # fmt: skip
            return
        cache = self.ctx.cache
        corepack = f"{node_bin}/corepack"
        if not Path(corepack).exists():
            fallback = cache / "corepack-bin" / "bin" / "corepack"
            got = self.ensure("corepack-fallback", [f"{node_bin}/npm", "i", "-g", "--offline", "--prefix",
                                                    str(cache / "corepack-bin"), COREPACK_FALLBACK],
                              lambda: str(fallback) if fallback.exists() else None)  # fmt: skip
            if got.path is None:
                self.report.add(name, declared=version, source="package.json packageManager", action=got.action,
                                exit_code=got.exit_code, gap=got.gap or "install_failed",
                                hint="no corepack")  # fmt: skip
                return
            corepack = got.path
        home = cache / "corepack"
        shims = cache / "corepack-bin" / "bin"

        def in_cache() -> str | None:
            base = home / "v1" / name
            return str(base / version) if (base / version).is_dir() else None

        # corepack and the first run of the package manager work in a scratch project that names only the
        # packageManager, never in the repo (a package manager may write a lockfile where it runs)
        scratch = self.ctx.state / "pm"
        if self.ctx.mode == "install":
            with contextlib.suppress(OSError):
                scratch.mkdir(parents=True, exist_ok=True)
                shims.mkdir(parents=True, exist_ok=True)  # corepack enable needs an existing directory
                _write(scratch / "package.json", json.dumps({"name": "ossbot-pm", "private": True,
                                                             "packageManager": f"{name}@{version}"}) + "\n")  # fmt: skip
        env = {"COREPACK_HOME": str(home), "COREPACK_ENABLE_DOWNLOAD_PROMPT": "0",
               "PATH": f"{node_bin}:{os.environ.get('PATH', '/usr/bin:/bin')}"}  # fmt: skip
        key = f"corepack-{name}@{version}"
        got = self.ensure(key, [corepack, "install"], in_cache, cwd=str(scratch), env=env)
        if got.path:
            shim = self.ensure(f"corepack-enable-{name}",
                               [corepack, "enable", "--install-directory", str(shims), name],
                               lambda: str(shims) if (shims / name).exists() else None, cwd=str(scratch),
                               env=env)  # fmt: skip
            if shim.path and str(shims) not in self.path:
                self.path.insert(0, str(shims))
            if shim.path:  # the first run of a package manager may fetch more (pnpm >= 11: its platform binary)
                warm = self.ctx.state / f"{name}-{version}.warm"

                def warmed(code: int) -> None:
                    if code == 0:
                        with contextlib.suppress(OSError):
                            _write(warm, "ok\n")

                self.ensure(f"warm-{name}@{version}", [str(shims / name), "--version"],
                            lambda: str(warm) if warm.is_file() else None, cwd=str(scratch), env=env,
                            after=warmed)  # fmt: skip
        self.report.add(name, declared=version, source="package.json packageManager", chosen=f"{name}@{version}",
                        path=got.path, action=got.action, exit_code=got.exit_code, gap=got.gap,
                        tail=got.tail)  # fmt: skip

    # ------------------------------------------------------------------------------------------ Python
    def resolve_python(self) -> None:
        pyproject = self.repo.toml("pyproject.toml")
        project = pyproject.get("project") if isinstance(pyproject, dict) else None
        requires = project.get("requires-python") if isinstance(project, dict) else None
        mise_py = self.mise_decl.get("python")
        decl: tuple[str, str, bool] | None = None
        candidates: list[tuple[str | None, str, bool]] = [
            (mise_py[0] if mise_py else None, mise_py[1] if mise_py else "", False),
            (self.first_line(".python-version"), ".python-version", False),
            (requires if isinstance(requires, str) else None, "pyproject.toml requires-python", True),
        ]
        for value_, source_, flag in candidates:
            if value_:
                decl = (value_.strip(), source_, flag)
                break
        if decl is None:
            return
        value, source, is_spec = decl
        if is_spec:
            if not re.fullmatch(r"[0-9.,<>=!~* ]{1,128}", value):
                self.report.add("python", declared=_clean(value), source=source, gap="invalid")
                return
            spec = value.replace(" ", "")
        else:
            m = re.fullmatch(r"(?:cpython[-@]?|python)?(\d+\.\d+(?:\.\d+)?)", value)
            if m is None:
                self.report.add("python", declared=_clean(value), source=source, gap="invalid")
                return
            spec = m.group(1)
        sysver = self.ctx.system_python

        def fits(version: tuple[int, ...]) -> bool:
            return bool(pep440_satisfies(version, spec)) if is_spec else version[: len(vkey(spec))] == vkey(spec)

        image = [(sysver, "/usr/bin/python3")] if sysver else []
        image += _managed_pythons(self.ctx.image_pythons)  # the image's uv-managed 3.13
        for version, path in image:
            if fits(version):
                self.report.add("python", declared=_clean(value), source=source, chosen=".".join(map(str, version)),
                                path=path, action="image")  # fmt: skip
                return
        base = self.ctx.cache / "uv-python"

        def check() -> str | None:
            if not base.is_dir():
                return None
            for p in sorted(base.iterdir(), key=lambda p: vkey(p.name), reverse=True):
                m = re.fullmatch(r"cpython-(\d+\.\d+\.\d+)-linux-x86_64-gnu", p.name)
                if m and p.is_dir() and not p.is_symlink() and fits(vkey(m.group(1))):
                    return str(p)
            return None

        got = self.ensure(f"python{spec}", [UV, "python", "install", spec], check,
                          env={"UV_PYTHON_INSTALL_DIR": str(base), "UV_PYTHON_INSTALL_MIRROR": PYTHON_MIRROR,
                               "UV_PYTHON_DOWNLOADS": "automatic"})  # fmt: skip
        self.report.add("python", declared=_clean(value), source=source, chosen=spec, path=got.path,
                        action=got.action, exit_code=got.exit_code, gap=got.gap, tail=got.tail)  # fmt: skip

    # ---------------------------------------------------------------------------------------------- Go
    def resolve_go(self) -> None:
        exact = self.mise_decl.get("go")
        gomod = self.repo.read("go.mod")
        if exact is None and gomod is None:
            return
        image = go_version(IMAGE_GO)
        assert image is not None
        if exact is not None:
            value, source = exact
            v = go_version(value)
            if v is None:
                self.report.add("go", declared=_clean(value), source=source, gap="invalid")
                return
            target, resolved_by = value.removeprefix("go"), "declared"
            if value.count(".") < 2:  # mise semantics: "1.26" = the newest 1.26.x
                newest = self.go_newest(v, 2)
                target, resolved_by = (newest, "index") if newest else (target, "mise")
            self._go(target, _clean(value), source, resolved_by)
            return
        go_line = re.search(r"(?m)^go\s+(\d+\.\d+(?:\.\d+)?(?:(?:rc|beta)\d+)?)\s*$", gomod or "")
        tc_line = re.search(r"(?m)^toolchain\s+go(\d+\.\d+(?:\.\d+)?(?:(?:rc|beta)\d+)?)\s*$", gomod or "")
        lines = [(m.group(1), label) for m, label in ((tc_line, "toolchain"), (go_line, "go")) if m]
        if not lines:
            return
        want = max(lines, key=lambda p: go_version(p[0]) or (0, 0, 0, 0))
        declared = ", ".join(f"{label} {v}" for v, label in lines)
        wv = go_version(want[0])
        assert wv is not None
        if image >= wv:
            self.report.add("go", declared=declared, source="go.mod", chosen=IMAGE_GO, action="image")
            return
        target, resolved_by = want[0], "declared"
        if want[0].count(".") < 2:
            newest = self.go_newest(wv, 2)
            target, resolved_by = (newest, "index") if newest else (want[0], "mise")
        self._go(target, declared, "go.mod", resolved_by)

    def _go(self, target: str, declared: str, source: str, resolved_by: str) -> None:
        if target == IMAGE_GO:
            self.report.add("go", declared=declared, source=source, chosen=IMAGE_GO, action="image")
            return
        got = self._mise_install("go", target, resolved_by == "mise", check_bin="bin/go")
        self._record("go", declared, source, target, got, "bin", resolved_by)
        if got.path and path_ok(got.path, self.ctx.work):
            self.mise_tools["go"] = Path(got.path).name

    # -------------------------------------------------------------------------------------------- Rust
    def resolve_rust(self) -> None:
        channel: str | None = None
        components: list[str] = []
        targets: list[str] = []
        profile = "minimal"
        source = None
        doc = self.repo.toml("rust-toolchain.toml")
        legacy = None if doc is not None else self.repo.read("rust-toolchain")
        if doc is None and legacy is not None:
            with contextlib.suppress(tomllib.TOMLDecodeError, ValueError):
                doc = tomllib.loads(legacy)
        if isinstance(doc, dict) and isinstance(doc.get("toolchain"), dict):
            tc = doc["toolchain"]
            channel = tc.get("channel") if isinstance(tc.get("channel"), str) else None
            components = [c for c in tc.get("components", []) if isinstance(c, str)][:16]
            targets = [t for t in tc.get("targets", []) if isinstance(t, str)][:16]
            profile = tc.get("profile") if tc.get("profile") in ("minimal", "default", "complete") else "minimal"
            source = "rust-toolchain.toml" if self.repo.exists("rust-toolchain.toml") else "rust-toolchain"
        elif legacy is not None:
            channel, source = legacy.strip().splitlines()[0].strip() if legacy.strip() else None, "rust-toolchain"
        elif "rust" in self.mise_decl:
            channel, source = self.mise_decl["rust"]
        if source is None and not self.repo.exists("Cargo.toml"):
            return
        names_ok = all(re.fullmatch(r"[a-z0-9][a-z0-9_.-]{0,63}", n) for n in [*components, *targets])
        if channel is not None and (not TOOLCHAIN_NAME_RE.fullmatch(channel) or not names_ok):
            self.report.add("rust", declared=_clean(channel), source=source, gap="invalid")
            channel = None
        env = {"RUSTUP_HOME": str(self.ctx.cache / "rustup"), "CARGO_HOME": str(self.ctx.cache / "cargo")}
        link_dir = self.ctx.cache / "rustup" / "toolchains" / RUST_LINK
        linked = self.ensure("rust-link", [RUSTUP, "toolchain", "link", RUST_LINK, RUST_IMAGE_DIR],
                             lambda: str(link_dir) if link_dir.exists() else None, env=env)  # fmt: skip
        if linked.path:  # the task's default toolchain is the image's (local, no download)
            settings = self.ctx.cache / "rustup" / "settings.toml"
            self.ensure("rust-default", [RUSTUP, "default", RUST_LINK],
                        lambda: str(settings) if RUST_LINK in _read_small(settings) else None, env=env)  # fmt: skip
        extra = [c for c in components if c not in RUST_BAKED] + targets
        image_ok = channel in (None, "stable", IMAGE_RUST, RUST_LINK) and not extra
        if image_ok:
            self.env["RUSTUP_TOOLCHAIN"] = RUST_LINK
            self.report.add("rust", declared=_clean(channel) if channel else None, source=source, chosen=IMAGE_RUST,
                            path=RUST_IMAGE_DIR, action="image" if linked.path else linked.action,
                            gap=None if linked.path else linked.gap or "link_failed")  # fmt: skip
            return
        assert channel is not None or extra
        chan = channel or "stable"
        tdir = self.ctx.cache / "rustup" / "toolchains" / f"{chan}-{RUST_TRIPLE}"
        argv = [RUSTUP, "toolchain", "install", chan, "--profile", profile, "--no-self-update"]
        for c in components:
            argv += ["-c", c]
        for t in targets:
            argv += ["-t", t]
        got = self.ensure(f"rust@{chan}", argv, lambda: str(tdir) if (tdir / "bin" / "rustc").exists() else None,
                          env=env)  # fmt: skip
        self.env["RUSTUP_TOOLCHAIN"] = chan if got.path else RUST_LINK
        self.report.add("rust", declared=_clean(chan), source=source, chosen=chan if got.path else IMAGE_RUST,
                        path=got.path, action=got.action, exit_code=got.exit_code, gap=got.gap,
                        tail=got.tail)  # fmt: skip

    # ------------------------------------------------------------------------------- Terraform, OpenTofu
    def resolve_terraform(self) -> None:
        decl = self.mise_decl.get("terraform")
        exact_src = decl[1] if decl else ".terraform-version"
        exact = decl[0] if decl else self.first_line(".terraform-version")
        constraints: list[str] = []
        if exact is None:
            for rel in self.repo.walk(lambda n: n.endswith(".tf")):
                constraints += re.findall(r"required_version\s*=\s*\"([^\"\n]{1,128})\"", self.repo.read(rel) or "")
        if exact is not None:
            if not re.fullmatch(r"\d+\.\d+(?:\.\d+)?", exact):
                self.report.add("terraform", declared=_clean(exact), source=exact_src, gap="invalid")
                return
            if exact.count(".") == 1:
                newest = self.tf_newest(lambda v: v.startswith(f"{exact}."))
                target, by = (newest, "index") if newest else (exact, "mise")
            else:
                target, by = exact, "declared"
            self._tf("terraform", IMAGE_TERRAFORM, target, exact, exact_src, by)
            return
        if not constraints:
            return
        joined = ", ".join(sorted(set(constraints)))[:200]
        if all(tf_satisfies(IMAGE_TERRAFORM, c) for c in constraints):
            self.report.add("terraform", declared=_clean(joined, 200), source="*.tf required_version",
                            chosen=IMAGE_TERRAFORM, action="image")  # fmt: skip
            return
        if any(tf_satisfies(IMAGE_TERRAFORM, c) is None for c in constraints):
            self.report.add("terraform", declared=_clean(joined, 200), source="*.tf required_version", gap="invalid")
            return
        newest = self.tf_newest(lambda v: all(tf_satisfies(v, c) for c in constraints))
        if newest:
            self._tf("terraform", IMAGE_TERRAFORM, newest, joined, "*.tf required_version", "index")
            return
        m = re.fullmatch(r"(?:~>|=)?\s*(\d+\.\d+)(?:\.0)?|(\d+\.\d+\.\d+)", constraints[0].strip())
        if len(constraints) == 1 and m:
            self._tf("terraform", IMAGE_TERRAFORM, m.group(1) or m.group(2), joined, "*.tf required_version", "mise")
            return
        self.report.add("terraform", declared=_clean(joined, 200), source="*.tf required_version",
                        gap="unsatisfiable_by_image")  # fmt: skip

    def _tf(self, tool: str, image: str, target: str, declared: str, source: str, by: str) -> None:
        if target == image:
            self.report.add(tool, declared=_clean(declared, 200), source=source, chosen=image, action="image")
            return
        got = self._mise_install(tool, target, by == "mise")
        self._record(tool, _clean(declared, 200), source, target, got, "bin", by)
        if got.path and path_ok(got.path, self.ctx.work):
            self.mise_tools[tool] = Path(got.path).name

    def resolve_opentofu(self) -> None:
        decl = self.mise_decl.get("opentofu")
        if decl is None:
            return
        if not re.fullmatch(r"\d+\.\d+\.\d+", decl[0]):
            self.report.add("opentofu", declared=_clean(decl[0]), source=decl[1], gap="invalid")
            return
        self._tf("opentofu", IMAGE_TOFU, decl[0], decl[0], decl[1], "declared")

    # -------------------------------------------------------------------------------------------- Ruby
    def resolve_ruby(self) -> None:
        decl = self.mise_decl.get("ruby")
        value, source = decl if decl else (self.first_line(".ruby-version"), ".ruby-version")
        if value is None:
            m = re.search(r"(?m)^\s*ruby\s+['\"](\d+\.\d+\.\d+)['\"]", self.repo.read("Gemfile") or "")
            value, source = (m.group(1), "Gemfile") if m else (None, "")
        if value is None:
            return
        version = value.removeprefix("ruby-")
        if not re.fullmatch(r"\d+\.\d+\.\d+", version):
            self.report.add("ruby", declared=_clean(value), source=source, gap="invalid")
            return
        got = self._mise_install("ruby", version, False, check_bin="bin/ruby", env={"MISE_RUBY_COMPILE": "false"})
        self._record("ruby", version, source, version, got, "bin", "declared")
        if got.path and path_ok(got.path, self.ctx.work):
            self.mise_tools["ruby"] = version

    def resolve_repo_mise(self) -> None:
        """Last (§6.1): the repo's own ``mise install`` for whatever else its mise config names."""
        files = [f for f in ("mise.toml", ".mise.toml", ".tool-versions") if self.repo.exists(f)]
        if not files:
            return
        digest = hashlib.sha256()
        for rel in files:  # a changed config (a refetch after the coder's edit) installs again
            digest.update(rel.encode() + b"\0" + (self.repo.read(rel) or "").encode() + b"\0")
        marker = self.ctx.state / f"mise-install-{digest.hexdigest()[:16]}.done"

        def done(code: int) -> None:  # the marker: the same config never installs twice in a task
            if code == 0:
                with contextlib.suppress(OSError):
                    self.ctx.state.mkdir(parents=True, exist_ok=True)
                    _write(marker, "ok\n")

        got = self.ensure("mise-install", [MISE, "install"], lambda: str(marker) if marker.is_file() else None,
                          after=done)  # fmt: skip
        self.report.add("mise", source="mise config of the repo", action=got.action, exit_code=got.exit_code,
                        gap=got.gap if got.gap != "not_installed" else None, tail=got.tail)  # fmt: skip

    # --------------------------------------------------------------------------------------------- run
    def resolve(self) -> None:
        self.read_mise()
        self.resolve_java()
        node = "node" in self.mise_decl or any(self.repo.exists(f) for f in ("package.json", ".nvmrc", ".node-version"))
        node_bin = self.resolve_node() if node else None
        if node_bin and node_bin != IMAGE_NODE_BIN and node_bin not in self.path:
            self.path.insert(0, node_bin)
        self.resolve_python()
        self.resolve_go()
        self.resolve_rust()
        self.resolve_terraform()
        self.resolve_opentofu()
        self.resolve_ruby()
        self.resolve_repo_mise()

    def env_lines(self) -> list[str]:
        work = self.ctx.work
        env = dict(self.env)
        if self.jdks:
            env["OSSBOT_JDKS"] = ",".join(self.jdks)
        if self.path:
            env["OSSBOT_PATH_PREPEND"] = ":".join(self.path)
        env.update({"GOTOOLCHAIN": "local", "RUSTUP_HOME": f"{work}/cache/rustup", "CARGO_HOME": f"{work}/cache/cargo",
                    "COREPACK_HOME": f"{work}/cache/corepack", "UV_PYTHON_INSTALL_DIR": f"{work}/cache/uv-python",
                    "UV_PYTHON_INSTALL_MIRROR": PYTHON_MIRROR,
                    "MISE_GLOBAL_CONFIG_FILE": f"{work}/.home/.ossbot/mise.toml"})  # fmt: skip
        return [f"{k}={env[k]}" for k in ENV_KEYS if k in env and valid_env(k, env[k], work)]

    def mise_toml(self) -> str:
        lines = ["# written by ossbot-toolchains at every job start (the resolved global mise config)", "[tools]"]
        lines += [f'{tool} = "{version}"' for tool, version in sorted(self.mise_tools.items())
                  if TOOL_RE.fullmatch(tool) and VERSION_RE.fullmatch(version)]  # fmt: skip
        return "\n".join(lines) + "\n"


def _read_small(path: Path) -> str:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            return fh.read(65536)
    except OSError:
        return ""


def fetch_url(url: str) -> bytes | None:
    """GET ``url`` (one of the fixed INDEXES) through the job's proxy: 30 s, <= 32 MiB, else None."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "ossbot-toolchains"})  # noqa: S310 - fixed https URLs
        with urllib.request.urlopen(req, timeout=INDEX_TIMEOUT) as resp:  # noqa: S310
            data: bytes = resp.read(INDEX_MAX + 1)
    except (OSError, ValueError):
        return None
    return data if len(data) <= INDEX_MAX else None


def _write(path: Path, text: str) -> None:
    """A fresh file (an existing file or symlink is removed first, never followed), 0644."""
    with contextlib.suppress(FileNotFoundError):
        path.unlink()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o644)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.chmod(path, 0o644)


def _managed_pythons(root: Path) -> list[tuple[tuple[int, ...], str]]:
    """(version, bin dir) of the uv-managed CPythons under ``root`` (the image's /opt/uv-python)."""
    out = []
    with contextlib.suppress(OSError):
        for p in sorted(root.iterdir()):
            m = re.fullmatch(r"cpython-(\d+\.\d+\.\d+)-linux-x86_64-gnu", p.name)
            if m and (p / "bin").is_dir() and not p.is_symlink():
                out.append((vkey(m.group(1)), str(p / "bin")))
    return out


def _system_python() -> tuple[int, ...] | None:
    """The image's /usr/bin/python3 version when this resolver runs on it (the entrypoints call it that way)."""
    if os.path.realpath(sys.executable).startswith("/usr/bin/python3"):
        return tuple(sys.version_info[:3])
    return None


def run(ctx: Ctx, report_path: Path | None = None) -> Report:
    """Resolve, install or select, then write toolchain.env, mise.toml and toolchains.json (always)."""
    resolver = Resolver(ctx)
    try:
        resolver.resolve()
    except Exception as exc:  # noqa: BLE001 - never a job failure: the report says what broke
        resolver.report.error = _clean(f"{type(exc).__name__}: {exc}", 300)
    resolver.report.spent_s = ctx.clock() - resolver.started
    ctx.state.mkdir(parents=True, exist_ok=True)
    doc = json.dumps(resolver.report.to_json(), indent=1, sort_keys=True) + "\n"
    _write(ctx.state / "toolchain.env", "\n".join(resolver.env_lines()) + "\n")
    _write(ctx.state / "mise.toml", resolver.mise_toml())
    _write(ctx.state / "toolchains.json", doc)
    if report_path is not None:
        with contextlib.suppress(OSError):
            _write(report_path, doc)
    return resolver.report


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ossbot-toolchains")
    parser.add_argument("--repo", required=True)
    parser.add_argument("--state", required=True)
    parser.add_argument("--mode", required=True, choices=("install", "select"))
    parser.add_argument("--report")
    parser.add_argument("--budget", type=int, default=DEFAULT_BUDGET)
    parser.add_argument("--work", default="/work")
    parser.add_argument("--log")
    try:
        args = parser.parse_args(list(argv) if argv is not None else None)
    except SystemExit:
        return 2
    if not 0 <= args.budget <= MAX_BUDGET or not os.path.isabs(args.work):
        print("ossbot-toolchains: --budget must be 0..3600 and --work absolute", file=sys.stderr)
        return 2
    ctx = Ctx(repo=Path(args.repo), state=Path(args.state), cache=Path(args.work) / "cache", work=args.work,
              mode=args.mode, budget=args.budget, log=Path(args.log) if args.log else None,
              system_python=_system_python())  # fmt: skip
    report = run(ctx, Path(args.report) if args.report else None)
    for gap in report.gaps()[:20]:
        print(f"ossbot-toolchains: gap: {gap}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
