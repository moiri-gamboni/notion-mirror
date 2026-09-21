"""Where the mirror is — the one answer, for the engine modules and the shell.

The mirror's location is configuration, not something a file can derive from its own
position: the engine lives in its own clone, and the mirror is a git repository on
whatever volume the deployment put it. `$NOTION_MIRROR` names it, either in the
environment or in `~/.config/notion-mirror/env` (a `KEY=VALUE` file; `env.d/*.env` beside
it is how a second deployer ships a value next to the box's own file without editing it).

Refusing matters more here than answering. `refresh.py` and every standalone writer
`makedirs` whatever they are handed, so an off-by-one directory does not raise — it
*materialises* an empty parallel mirror, and every detector stays green. So `mirror_dir()`
asserts a fingerprint (`workspace/_databases`, which only a built mirror carries) and
raises `MirrorError` with the three facts the reader needs: what root was tried, how it was
arrived at, and what to do. There is no best-effort return path.

Nothing is asserted at import: the dead-man reader (`rows_status.py`) imports this module
*because* something may be broken, and resolves on use.

No caching: the environment and the files are re-read on every call. Tests, cron lines and
`refresh.sh` all change `$NOTION_MIRROR` under a live process, and two `isdir` calls are
cheaper than reasoning about when a memo goes stale.
"""
import glob
import os
import sys

ENV = "NOTION_MIRROR"
ENV_FILE = os.path.expanduser("~/.config/notion-mirror/env")
ENV_DIR = os.path.expanduser("~/.config/notion-mirror/env.d")

#: The directory only a built mirror carries: a scratch tree, an empty mount point and a
#: mistyped path all lack it.
FINGERPRINT = "workspace/_databases"

# --- well-known state filenames ---------------------------------------------
# Bare names, joined by the caller onto `state_dir()`. They live beside the resolver rather
# than in `paths.py` (which re-exports them) because the dead-man reader needs them without
# asserting the mirror: importing `paths` is what asserts it.
#: The receiver's append-only comment log.
CAPTURE = "webhook-comments-capture.jsonl"
ROWS_STATUS = "rows-refresh-status.json"
USERS = "users.json"
WEBHOOK_SECRET = "webhook-secret"


class MirrorError(RuntimeError):
    """The configured root is not a mirror, or nothing is configured. Carries the full
    refusal message."""


def config(key, env=None):
    """The value of `key`: the environment, else the last `KEY=VALUE` line for it across
    the env file and then `env.d/*.env` in sorted order; `None` when nowhere.

    Quotes around the value are stripped, `#` comment lines, a leading `export ` and a
    leading `~` in a path are tolerated, so a file a shell could `source` reads the same
    here (a trailing `# comment` on a value line is the one shell form that does not: it
    becomes part of the value). An empty environment value counts as unset.
    """
    return _lookup(key, env)[0]


def _lookup(key, env):
    env = os.environ if env is None else env
    if env.get(key):
        return env[key], f"${key}"
    value, source = None, None
    for path in [ENV_FILE] + sorted(glob.glob(os.path.join(ENV_DIR, "*.env"))):
        found = _read_kv(path, key)
        if found is not None:
            value, source = found, f"{key} in {path}"
    return value, source


def _read_kv(path, key):
    """The last value assigned to `key` in one env file, or `None`. A missing file is an
    ordinary state (a fresh install, no `env.d/`), not an error — but a dangling symlink
    is: a deploy linked the file before its target existed, and reading it as absent
    would silently unset every key it carries."""
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.read().splitlines()
    except FileNotFoundError:
        if os.path.islink(path):
            raise MirrorError(
                f"notion-mirror: {path} is a symlink to {os.readlink(path)}, which does "
                f"not exist\n"
                f"  fix:         re-run the deploy, or remove the link") from None
        return None
    value = None
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        line = line.removeprefix("export ").lstrip()
        name, sep, raw = line.partition("=")
        if not sep or name.strip() != key:
            continue
        raw = raw.strip()
        if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'":
            raw = raw[1:-1]
        value = raw
    return value


def mirror_dir(env=None):
    """The mirror directory: the realpath of `$NOTION_MIRROR`, asserting the fingerprint.

    `realpath` because the mirror is typically reached through a mount point or a symlink,
    and every caller — the shell's `cd`, git's top-level check, the lock's `fstat` — has to
    agree on one spelling. Raises `MirrorError` rather than returning a wrong-but-existing
    path.
    """
    configured, how = _lookup(ENV, env)
    if not configured:
        raise MirrorError(_unconfigured())
    root = os.path.realpath(os.path.expanduser(configured))
    if not os.path.isdir(os.path.join(root, *FINGERPRINT.split("/"))):
        raise MirrorError(_refusal(root, how))
    return root


def state_dir(root=None):
    """`<mirror>/_meta/state` — the engine's run state, the receiver's files, the markers."""
    return os.path.join(root or mirror_dir(), "_meta", "state")


def _unconfigured():
    return (f"notion-mirror: no mirror configured — ${ENV} is unset and {ENV_FILE}"
            f" has no {ENV} line\n"
            f"  resolved as: ${ENV}, then {ENV_FILE} and {ENV_DIR}/*.env\n"
            f"  missing:     {ENV}\n"
            f"  fix:         export {ENV}=/path/to/mirror, or write"
            f" {ENV}=/path/to/mirror to {ENV_FILE}")


def _refusal(root, how):
    """The one refusal message, parameterized on the three facts a reader needs.

    A message that does not name its own cause gets misread, and this one is read by
    someone whose cron job just refused at 03:00: what root was tried, how that root was
    arrived at, what was not there, and what to do about it.

    One case is not a wrong root at all: the directory exists but holds no mirror yet. That
    is a cold start, not a misconfiguration, and its fix is `init` — so an extra line names
    it, rather than leaving the reader to re-point a variable that is already correct.
    """
    init_hint = ("\n  cold start:  the directory exists but holds no mirror yet — run"
                 " refresh.sh init\n"
                 "               to create the skeleton, then refresh.sh daily for the"
                 " first (full, hours-long) refresh") if os.path.isdir(root) else ""
    return (f"notion-mirror: {root} is not a Notion mirror\n"
            f"  resolved as: {how}\n"
            f"  missing:     {FINGERPRINT}\n"
            f"  fix:         export {ENV}=/path/to/mirror, or set {ENV} in {ENV_FILE}"
            f"{init_hint}")


def main(argv):
    """`python3 mirror_root.py [--unchecked]` — how `refresh.sh` gets the Python answer.

    Bare prints the checked mirror; `--unchecked` prints the configured value without the
    fingerprint, which is what `init` needs: the one command that runs before the mirror
    exists. Both refuse with exit 2 and the message on stderr.
    """
    if argv[1:] not in ([], ["--unchecked"]):
        print(f"notion-mirror: usage: {os.path.basename(argv[0])} [--unchecked]",
              file=sys.stderr)
        return 2
    try:
        if argv[1:]:
            configured = config(ENV)
            if not configured:
                raise MirrorError(_unconfigured())
            print(os.path.expanduser(configured))
        else:
            print(mirror_dir())
    except MirrorError as exc:
        print(exc, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
