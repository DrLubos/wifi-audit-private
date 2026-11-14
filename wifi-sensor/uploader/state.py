"""The uploader's state file (cursor, counters, server anchors, stale windows): JSON, replaced
atomically (temp file, fsync, rename), so a crash leaves the old or the new
state, never half of one. Standard library only."""

import json
import os

last_load_corrupt = None      # where load() moved an unreadable state file to (for the caller's log)


def load(path):
    """The state, or None when there is none: no file, or a file that is not
    a JSON object (an SD card after a power cut) - that one is moved aside
    and the uploader starts as with a lost state, from the server's last
    poll, instead of failing at every start."""
    global last_load_corrupt
    last_load_corrupt = None
    try:
        with open(path) as f:
            state = json.load(f)
        if isinstance(state, dict):
            return state
    except FileNotFoundError:
        return None
    except (ValueError, UnicodeDecodeError):
        pass
    last_load_corrupt = path + ".corrupt"
    try:
        os.replace(path, last_load_corrupt)
    except OSError:
        pass
    return None


def save(path, state):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, separators=(",", ":"), sort_keys=True)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    d = os.open(os.path.dirname(os.path.abspath(path)), os.O_RDONLY)
    try:
        os.fsync(d)
    finally:
        os.close(d)
