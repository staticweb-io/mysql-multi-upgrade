#!/usr/bin/env python3
"""Upgrade a MariaDB or MySQL data directory one major version at a time.

Each step starts the next server version on the data directory, lets it
upgrade the system tables, and shuts it down cleanly. Server binaries
come from this project's flake, one nixpkgs revision per version.
"""

import argparse
import fcntl
import json
import os
import pwd
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

# Each flavor's upgrade path. Every step is an upgrade that the
# vendor documents as supported.
CHAINS = {
    "mariadb": ["10.3", "10.4", "10.5", "10.6", "10.11", "11.4", "11.8"],
    "mysql": ["5.7", "8.0", "8.4"],
}

# Replaced with the flake's store path when packaged with Nix
DEFAULT_FLAKE = "@flake@"


class UpgradeError(Exception):
    pass


def parse_version(s):
    return tuple(int(x) for x in s.split("."))


def major(version):
    return ".".join(str(x) for x in parse_version(version)[:2])


def version_str(v):
    return ".".join(str(x) for x in v)


@dataclass(frozen=True)
class Version:
    flavor: str
    version: str

    @property
    def major(self):
        return major(self.version)

    def __str__(self):
        return f"{self.flavor} {self.version}"


# --- Detection ---------------------------------------------------------------

CREATOR_RE = re.compile(rb"(MariaDB|MySQL) (\d+\.\d+\.\d+)")
UPGRADE_INFO_RE = re.compile(r"^(\d+\.\d+\.\d+)(-MariaDB)?")


def redo_log_files(datadir):
    files = [datadir / "ib_logfile0"]
    redo_dir = datadir / "#innodb_redo"
    if redo_dir.is_dir():
        files += sorted(p for p in redo_dir.iterdir() if p.name.startswith("#ib_redo"))
    return [f for f in files if f.is_file()]


def redo_log_evidence(datadir):
    """InnoDB redo logs name the server that created them. It's a lower
    bound, since a newer server keeps a log whose format it shares."""
    found = []
    for f in redo_log_files(datadir):
        with open(f, "rb") as fh:
            header = fh.read(4096)
        m = CREATOR_RE.search(header)
        if m:
            found.append(Version(m.group(1).decode().lower(), m.group(2).decode()))
    return found


def upgrade_info_evidence(datadir):
    """mysql_upgrade and the MySQL 8.4 server record the version that
    last upgraded the system tables."""
    found = []
    for name in ["mariadb_upgrade_info", "mysql_upgrade_info"]:
        f = datadir / name
        if f.is_file():
            m = UPGRADE_INFO_RE.match(f.read_text(errors="replace").strip())
            if m:
                flavor = (
                    "mariadb"
                    if m.group(2) or parse_version(m.group(1))[0] >= 10
                    else "mysql"
                )
                found.append(Version(flavor, m.group(1)))
    history = datadir / "mysql_upgrade_history"
    if history.is_file():
        try:
            entries = json.loads(history.read_text())["upgrade_history"]
            found += [Version("mysql", e["version"]) for e in entries]
        except (ValueError, KeyError, TypeError):
            pass
    return found


def frm_version(path):
    """The version of the server that created or last altered a table,
    from the MYSQL_VERSION_ID stored at offset 0x33 of its .frm file."""
    with open(path, "rb") as fh:
        header = fh.read(0x37)
    if len(header) < 0x37 or header[0] != 0xFE or header[1] != 0x01:
        return None
    n = int.from_bytes(header[0x33:0x37], "little")
    if n < 50000:
        return None
    v = (n // 10000, n // 100 % 100, n % 100)
    return Version("mariadb" if v[0] >= 10 else "mysql", version_str(v))


def frm_evidence(datadir):
    found = []
    for db in datadir.iterdir():
        if db.is_dir() and not db.name.startswith("#"):
            for f in db.glob("*.frm"):
                v = frm_version(f)
                if v:
                    found.append(v)
    return found


def detect_flavor(datadir, evidence):
    if (datadir / "aria_log_control").exists():
        return "mariadb"
    if (datadir / "mysql.ibd").exists() or (datadir / "auto.cnf").exists():
        return "mysql"
    flavors = {v.flavor for v in evidence}
    if len(flavors) == 1:
        return flavors.pop()
    return None


def detect(datadir):
    """The newest version that has written to `datadir`."""
    datadir = Path(datadir)
    if not datadir.is_dir():
        raise UpgradeError(f"{datadir} is not a directory")
    if not (datadir / "mysql").is_dir() and not (datadir / "mysql.ibd").exists():
        raise UpgradeError(
            f"{datadir} doesn't look like a MariaDB or MySQL data directory"
        )
    evidence = (
        redo_log_evidence(datadir)
        + upgrade_info_evidence(datadir)
        + frm_evidence(datadir)
    )
    flavor = detect_flavor(datadir, evidence)
    if not flavor:
        raise UpgradeError(f"Can't tell whether {datadir} is from MariaDB or MySQL")
    versions = [v for v in evidence if v.flavor == flavor]
    if not versions:
        raise UpgradeError(f"Can't find the {flavor} version of {datadir}")
    return max(versions, key=lambda v: parse_version(v.version))


# --- Planning ----------------------------------------------------------------


def parse_target(spec):
    """'11.8', 'mariadb-11.8' or 'mysql-8.4'. The flavors' major
    versions don't overlap, so the flavor is optional."""
    m = re.fullmatch(r"(?:(mariadb|mysql)-)?(\d+\.\d+)", spec)
    if not m:
        raise UpgradeError(f"Invalid target version {spec!r}")
    flavor, ver = m.groups()
    flavors = [flavor] if flavor else [f for f, chain in CHAINS.items() if ver in chain]
    for f in flavors:
        if ver in CHAINS[f]:
            return f, ver
    raise UpgradeError(f"Unsupported target version {spec!r}. Supported: {supported()}")


def supported():
    return ", ".join(f"{f}-{v}" for f, chain in CHAINS.items() for v in chain)


def plan(current, target=None):
    """The major versions to run, oldest first. The current version
    runs first when it's in the chain, to recover from a crash and to
    finish an upgrade that it may not have run."""
    chain = CHAINS[current.flavor]
    if target is None:
        target = (current.flavor, chain[-1])
    flavor, target_major = target
    if flavor != current.flavor:
        raise UpgradeError(f"Can't upgrade {current} to {flavor}-{target_major}")
    cur = parse_version(current.major)
    if cur < parse_version(chain[0]):
        raise UpgradeError(
            f"{current} is older than {flavor}-{chain[0]}, the oldest supported version"
        )
    if cur > parse_version(target_major):
        raise UpgradeError(f"Can't downgrade {current} to {flavor}-{target_major}")
    steps = [v for v in chain if cur <= parse_version(v) <= parse_version(target_major)]
    if not steps:
        raise UpgradeError(f"No supported version can open {current}")
    return steps


# --- Servers -----------------------------------------------------------------


def package_name(flavor, ver):
    return f"{flavor}-{ver}"


def default_flake():
    if not DEFAULT_FLAKE.startswith("@"):
        return DEFAULT_FLAKE
    here = Path(__file__).resolve().parent
    if (here / "flake.nix").exists():
        return f"path:{here}"
    raise UpgradeError(
        "Pass --flake, since this copy of mysql-multi-upgrade doesn't know its flake"
    )


def server_path(name, overrides, flake, gcroots):
    """The store path of server `name`, from `overrides` or by building
    it from `flake`. A built server gets a GC root in `gcroots`, since
    a garbage collection during a later build could otherwise delete
    it."""
    if name in overrides:
        return overrides[name]
    flake = flake or default_flake()
    print(f"Fetching {name} from {flake}", flush=True)
    # Quoted, since the names have dots
    result = subprocess.run(
        [
            "nix",
            "build",
            "--out-link",
            str(gcroots / name),
            "--print-out-paths",
            f'{flake}#"{name}"',
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    if result.returncode != 0:
        raise UpgradeError(f"nix build of {name} failed")
    return result.stdout.strip().splitlines()[-1]


def first_existing(base, names):
    for n in names:
        p = Path(base) / "bin" / n
        if p.exists():
            return str(p)
    return None


def check_unlocked(datadir):
    """InnoDB holds an fcntl lock on ibdata1 while a server runs."""
    ibdata = Path(datadir) / "ibdata1"
    if not ibdata.exists():
        return
    try:
        fd = os.open(ibdata, os.O_RDWR)
    except PermissionError as e:
        raise UpgradeError(f"Can't open {ibdata}: {e}")
    try:
        fcntl.lockf(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.lockf(fd, fcntl.LOCK_UN)
    except OSError:
        raise UpgradeError(f"A server is using {datadir}. Stop it first.")
    finally:
        os.close(fd)


def tail(path, n=30):
    try:
        lines = Path(path).read_text(errors="replace").splitlines()
    except FileNotFoundError:
        return "(no log)"
    return "\n".join(lines[-n:])


class Server:
    def __init__(
        self, base, flavor, ver, datadir, run_dir, log_dir, extra_args, defaults_file
    ):
        self.base = base
        self.flavor = flavor
        self.ver = ver
        self.datadir = datadir
        self.socket = str(Path(run_dir) / "mysqld.sock")
        self.log = str(Path(log_dir) / f"{flavor}-{ver}.err")
        self.run_dir = run_dir
        self.extra_args = extra_args
        self.defaults_file = defaults_file
        self.proc = None

    def args(self):
        mysqld = first_existing(self.base, ["mariadbd", "mysqld"])
        if not mysqld:
            raise UpgradeError(f"No server binary in {self.base}")
        defaults = (
            [f"--defaults-file={self.defaults_file}"]
            if self.defaults_file
            else ["--no-defaults"]
        )
        args = (
            [mysqld]
            + defaults
            + [
                f"--basedir={self.base}",
                f"--datadir={self.datadir}",
                "--innodb-fast-shutdown=0",
                # Native AIO fails on some filesystems, and speed doesn't
                # matter here
                "--innodb-use-native-aio=0",
                f"--log-error={self.log}",
                f"--pid-file={Path(self.run_dir) / 'mysqld.pid'}",
                "--skip-grant-tables",
                "--skip-networking",
                f"--socket={self.socket}",
                # Concurrent servers that shared a tmpdir deleted each
                # other's temporary tables
                f"--tmpdir={self.run_dir}",
            ]
        )
        if self.flavor == "mysql" and parse_version(self.ver) >= (8, 0):
            args += ["--disable-log-bin", "--mysqlx=OFF"]
        if os.geteuid() == 0:
            args.append(f"--user={pwd.getpwuid(os.stat(self.datadir).st_uid).pw_name}")
        return args + self.extra_args

    def client(self):
        return first_existing(self.base, ["mariadb", "mysql"])

    def query(self, sql):
        result = subprocess.run(
            [
                self.client(),
                "--no-defaults",
                f"--socket={self.socket}",
                "--user=root",
                "--batch",
                "--skip-column-names",
                "-e",
                sql,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        if result.returncode != 0:
            raise UpgradeError(f"Query failed: {sql}\n{result.stderr}")
        return result.stdout

    def start(self, timeout):
        try:
            self.proc = subprocess.Popen(self.args(), stdin=subprocess.DEVNULL)
        except OSError as e:
            raise UpgradeError(f"Can't run {self.flavor} {self.ver}: {e}")
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                raise UpgradeError(
                    f"{self.flavor} {self.ver} exited with status {self.proc.returncode}"
                    f" while starting. Its log, {self.log}, ends with:\n{tail(self.log)}"
                )
            if os.path.exists(self.socket):
                try:
                    return self.query("SELECT VERSION()").strip()
                except UpgradeError:
                    pass
            time.sleep(0.5)
        self.kill()
        raise UpgradeError(
            f"{self.flavor} {self.ver} didn't start within {timeout}s. See {self.log}"
        )

    def upgrade_tables(self):
        """MySQL 8.0 and later upgrade themselves when they start."""
        if self.flavor == "mysql" and parse_version(self.ver) >= (8, 0):
            return
        upgrade = first_existing(self.base, ["mariadb-upgrade", "mysql_upgrade"])
        log = Path(self.log).with_suffix(".upgrade.log")
        with open(log, "w") as out:
            status = subprocess.run(
                [upgrade, "--no-defaults", f"--socket={self.socket}", "--user=root"],
                stdout=out,
                stderr=subprocess.STDOUT,
            ).returncode
        if status != 0:
            raise UpgradeError(
                f"{Path(upgrade).name} failed with status {status}:\n{tail(log)}"
            )

    def stop(self, timeout):
        """A slow shutdown, from --innodb-fast-shutdown=0, leaves nothing
        for the next version to recover or merge."""
        self.proc.send_signal(signal.SIGTERM)
        try:
            status = self.proc.wait(timeout)
        except subprocess.TimeoutExpired:
            self.kill()
            raise UpgradeError(
                f"{self.flavor} {self.ver} didn't shut down within {timeout}s"
            )
        if status != 0:
            raise UpgradeError(
                f"{self.flavor} {self.ver} shut down with status {status}."
                f" Its log, {self.log}, ends with:\n{tail(self.log)}"
            )

    def kill(self):
        if self.proc and self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait()


def warnings_after(server):
    """Things that upgrade but stop working."""
    if server.flavor == "mysql" and parse_version(server.ver) >= (8, 4):
        users = server.query(
            "SELECT CONCAT(user, '@', host) FROM mysql.user"
            " WHERE plugin = 'mysql_native_password' ORDER BY 1"
        ).split()
        if users:
            yield (
                "These users authenticate with mysql_native_password, which MySQL 8.4"
                " disables by default: "
                + ", ".join(users)
                + ". Start the server with --mysql-native-password=ON or change"
                " their authentication plugin."
            )


def make_run_dir(datadir):
    """A directory for the socket, whose path must fit in sockaddr_un,
    so it can't be under a deep TMPDIR."""
    base = os.environ.get("TMPDIR", "/tmp")
    if len(base) > 60:
        base = "/tmp"
    d = tempfile.mkdtemp(prefix="mysql-multi-upgrade.", dir=base)
    if os.geteuid() == 0:
        st = os.stat(datadir)
        os.chown(d, st.st_uid, st.st_gid)
    return d


def upgrade(
    datadir, target, overrides, flake, log_dir, extra_args, defaults_file, timeout
):
    datadir = Path(datadir).resolve()
    current = detect(datadir)
    steps = plan(current, target)
    print(f"{datadir} is {current}")
    print("Steps: " + " -> ".join(f"{current.flavor}-{v}" for v in steps), flush=True)
    check_unlocked(datadir)
    run_dir = make_run_dir(datadir)
    try:
        gcroots = Path(run_dir) / "gcroots"
        gcroots.mkdir()
        servers = {
            ver: server_path(
                package_name(current.flavor, ver), overrides, flake, gcroots
            )
            for ver in steps
        }
        # Absolute, since servers resolve --log-error from the data dir
        log_dir = os.path.abspath(
            log_dir or tempfile.mkdtemp(prefix="mysql-multi-upgrade-logs.")
        )
        os.makedirs(log_dir, exist_ok=True)
        print(f"Server logs are in {log_dir}", flush=True)
        warnings = []
        for ver in steps:
            server = Server(
                servers[ver],
                current.flavor,
                ver,
                str(datadir),
                run_dir,
                log_dir,
                extra_args,
                defaults_file,
            )
            print(f"Starting {current.flavor} {ver}", flush=True)
            try:
                running = server.start(timeout)
                print(f"Started {running}", flush=True)
                server.upgrade_tables()
                if ver == steps[-1]:
                    warnings = list(warnings_after(server))
                print(f"Shutting down {running}", flush=True)
                server.stop(timeout)
            finally:
                server.kill()
        for w in warnings:
            print(f"Warning: {w}", file=sys.stderr)
        print(f"Upgraded {datadir} from {current} to {detect(datadir)}")
    finally:
        shutil.rmtree(run_dir, ignore_errors=True)


def parse_overrides(pairs):
    overrides = {}
    servers_file = os.environ.get("MYSQL_MULTI_UPGRADE_SERVERS")
    if servers_file:
        overrides.update(json.loads(Path(servers_file).read_text()))
    for pair in pairs:
        name, sep, path = pair.partition("=")
        if not sep:
            raise UpgradeError(
                f"--server takes NAME=PATH, such as mariadb-10.4=/nix/store/...: {pair!r}"
            )
        overrides[name] = path
    return overrides


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="mysql-multi-upgrade", description=__doc__.splitlines()[0]
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser(
        "detect", help="Print the flavor and version of a data directory"
    )
    p.add_argument("datadir")

    p = sub.add_parser("plan", help="Print the steps to upgrade a data directory")
    p.add_argument("datadir")
    p.add_argument(
        "--to", help="Target version, such as 11.8 or mysql-8.4. Default: the newest"
    )

    p = sub.add_parser("upgrade", help="Upgrade a data directory in place")
    p.add_argument("datadir")
    p.add_argument(
        "--to", help="Target version, such as 11.8 or mysql-8.4. Default: the newest"
    )
    p.add_argument(
        "--defaults-file", help="Option file for each server, instead of --no-defaults"
    )
    p.add_argument("--flake", help="Flake to build the servers from")
    p.add_argument(
        "--log-dir",
        help="Where to write server logs. Default: a new temporary directory",
    )
    p.add_argument(
        "--mysqld-arg",
        action="append",
        default=[],
        metavar="ARG",
        help="Extra argument for each server, such as --lower-case-table-names=1",
    )
    p.add_argument(
        "--server",
        action="append",
        default=[],
        metavar="NAME=PATH",
        help="Use the server at PATH for NAME, such as mariadb-10.4=/nix/store/...",
    )
    p.add_argument(
        "--timeout",
        type=int,
        default=3600,
        help="Seconds to wait for each server to start or stop. Default: 3600",
    )

    sub.add_parser("versions", help="List the supported versions")

    args = parser.parse_args(argv)
    try:
        if args.command == "detect":
            print(detect(args.datadir))
        elif args.command == "plan":
            current = detect(args.datadir)
            target = parse_target(args.to) if args.to else None
            print(" -> ".join(f"{current.flavor}-{v}" for v in plan(current, target)))
        elif args.command == "upgrade":
            target = parse_target(args.to) if args.to else None
            upgrade(
                args.datadir,
                target,
                parse_overrides(args.server),
                args.flake,
                args.log_dir,
                args.mysqld_arg,
                args.defaults_file,
                args.timeout,
            )
        elif args.command == "versions":
            print(supported().replace(", ", "\n"))
    except UpgradeError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
