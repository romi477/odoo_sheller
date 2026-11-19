"""Where odoo-bin and its config are, asked inside the container.

`LOCATE_SOURCE` is Python that runs in the container's own interpreter, at the
head of every script the daemon sends there: the probe, the test listing and
os_psql's runner. One copy, so the three cannot disagree about which Odoo a
container holds. Stdlib only, conservative syntax — container Pythons go back
to 3.6.

Three steps, cheapest first, and a later one only when the earlier found
nothing:

1. The usual paths: PATH, the official image's, the Debian package's.
2. A running Odoo's command line, from /proc — the binary and its `-c`.
3. A short walk from the working directory and the usual roots of a dev
   checkout (/workspace, /home, /opt …). `docker exec` starts in the
   container's WorkingDir, which is where a compose file that bind-mounts a
   checkout tends to put it. The walk is shallow, keeps out of addons, Odoo's
   own package and the system trees, and gives up after a time budget: it runs
   on every probe of such a container.

A file named odoo-bin counts from the walk only with `odoo/release.py` beside
it. A config counts only with an `[options]` section, and the one nearest the
checkout wins. Walked configs are taken only when the same walk found odoo-bin;
an odoo-bin found otherwise takes a config beside it or one level up, never
one from across the disk, which belongs to another instance.
"""

LOCATE_SOURCE = r'''
import os, re, shutil, time

SEARCH_ROOTS = ["/workspace", "/home", "/opt", "/srv", "/mnt", "/app", "/code", "/odoo"]
SEARCH_DEPTH = 4
SEARCH_BUDGET = 1.5
# Never where odoo-bin is, and large: system trees, caches, databases, and
# the addons a dev checkout mounts by the hundred.
SEARCH_SKIP = set(["proc", "sys", "dev", "run", "tmp", "node_modules", "__pycache__",
                   "site-packages", "dist-packages", "filestore", "postgresql", "addons"])
OPTIONS = re.compile(r"^\s*\[options\]\s*$", re.M)


def _standard_bins():
    return [shutil.which("odoo-bin"), "/opt/odoo/odoo-bin", "/usr/bin/odoo",
            "/odoo/odoo-bin", "/mnt/odoo/odoo-bin", "/usr/lib/python3/dist-packages/odoo-bin"]


def _standard_configs():
    return [os.environ.get("ODOO_RC"), "/etc/odoo/odoo.conf", "/etc/odoo.conf",
            "/opt/odoo.conf"]


def _is_config(path):
    try:
        if os.path.getsize(path) > 1000000:
            return False
        with open(path, encoding="utf-8", errors="replace") as handle:
            return bool(OPTIONS.search(handle.read(65536)))
    except OSError:
        return False


def _skipped(path, name):
    if name.startswith(".") or name in SEARCH_SKIP:
        return True
    # An addon, or Odoo's own package: thousands of files, no odoo-bin.
    for marker in ("__manifest__.py", "__openerp__.py", "release.py"):
        if os.path.exists(os.path.join(path, marker)):
            return True
    return False


def search(roots, depth=SEARCH_DEPTH, budget=SEARCH_BUDGET):
    """The shallowest checkout under the roots, and every config on the way."""
    deadline = time.monotonic() + budget
    found = None
    configs = []
    seen = set()
    for root in roots:
        level = [root]
        for _ in range(depth + 1):
            below = []
            for directory in level:
                real = os.path.realpath(directory)
                if real in seen:
                    continue
                seen.add(real)
                if time.monotonic() > deadline:
                    return found, configs
                try:
                    entries = list(os.scandir(directory))
                except OSError:
                    continue
                for entry in entries:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            if not _skipped(entry.path, entry.name):
                                below.append(entry.path)
                        elif entry.name == "odoo-bin":
                            if found is None and os.path.isfile(
                                    os.path.join(directory, "odoo", "release.py")):
                                found = entry.path
                        elif entry.name.endswith(".conf") or entry.name == ".odoorc":
                            if _is_config(entry.path):
                                configs.append(entry.path)
                    except OSError:
                        continue
            level = below
            if not level:
                break
        # The roots go nearest first: one that held both answers is the
        # container's, and the next could only add a stranger's config.
        if found and configs:
            break
    return found, configs


def running(proc="/proc"):
    """odoo-bin and its config, from the command line of an Odoo running here."""
    try:
        pids = [name for name in os.listdir(proc) if name.isdigit()]
    except OSError:
        return None, None
    for pid in sorted(pids, key=int):
        try:
            with open(os.path.join(proc, pid, "cmdline"), "rb") as handle:
                raw = handle.read()
        except OSError:
            continue
        args = [part.decode("utf-8", "replace") for part in raw.split(b"\0") if part]
        for index, arg in enumerate(args):
            if os.path.basename(arg) not in ("odoo-bin", "odoo"):
                continue
            binary = arg
            if not os.path.isabs(binary):
                try:
                    binary = os.path.join(os.readlink(os.path.join(proc, pid, "cwd")), binary)
                except OSError:
                    continue
            config = None
            rest = args[index + 1:]
            for position, option in enumerate(rest):
                if option in ("-c", "--config") and position + 1 < len(rest):
                    config = rest[position + 1]
                    break
                if option.startswith("--config="):
                    config = option[len("--config="):]
                    break
            return binary, config
    return None, None


def _beside(odoo_bin):
    """Configs in odoo-bin's directory and the one above it."""
    here = os.path.dirname(odoo_bin)
    found = []
    for directory in (here, os.path.dirname(here)):
        try:
            names = sorted(os.listdir(directory))
        except OSError:
            continue
        for name in names:
            path = os.path.join(directory, name)
            if (name.endswith(".conf") or name == ".odoorc") and _is_config(path):
                found.append(path)
    return found


def _roots():
    named = os.environ.get("ODOO_SHELLER_SEARCH_ROOTS")
    candidates = named.split(os.pathsep) if named else [os.getcwd()] + SEARCH_ROOTS
    roots = []
    for root in candidates:
        if root != "/" and root not in roots and os.path.isdir(root):
            roots.append(root)
    return roots


def locate(standard=None, configs=None, roots=None, proc="/proc"):
    """(odoo_bin, config); either may be None."""
    default = standard is None
    standard = _standard_bins() if default else standard
    configs = _standard_configs() if configs is None else configs
    odoo_bin = None
    for path in standard:
        if path and os.path.exists(path):
            odoo_bin = path
            break
    live_bin, live_config = running(proc)
    if odoo_bin is None and live_bin and os.path.isfile(live_bin):
        odoo_bin = live_bin
    found = None
    if odoo_bin is None:
        found = search(_roots() if roots is None else roots)
        odoo_bin = found[0]
    near = [os.path.join(os.path.dirname(odoo_bin), "odoo.conf")] if odoo_bin else []
    home = [os.path.expanduser("~/.odoorc")] if default else []
    for path in configs + [live_config] + near + home:
        if path and os.path.isfile(path):
            return odoo_bin, path
    if odoo_bin is None:
        return None, None
    # Only the walk that found this odoo-bin may offer configs from its tree.
    # An odoo-bin found any other way takes one beside it or a level up, never
    # a .conf from across the disk: that is another instance's.
    candidates = found[1] if found is not None else _beside(odoo_bin)
    if not candidates:
        return odoo_bin, None
    here = os.path.dirname(odoo_bin)
    parent = os.path.dirname(here)

    def nearness(path):
        where = os.path.dirname(path)
        return 0 if where == here else 1 if where == parent else 2

    return odoo_bin, sorted(candidates, key=nearness)[0]
'''
