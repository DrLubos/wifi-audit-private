"""The uploader's state file (cursor, counters, server anchors): JSON, replaced
atomically (temp file, fsync, rename), so a crash leaves the old or the new
state, never half of one. Standard library only."""

import json
import os


def load(path):
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
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
