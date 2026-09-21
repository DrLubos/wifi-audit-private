"""python -m detection <detector> [options]  -  run one batch detector over a window.

    python -m detection deauth-flood --from 2026-09-15 --to 2026-09-21 --dry-run --verbose
"""

import argparse
import sys

from . import deauth_flood

DETECTORS = {
    "deauth-flood": deauth_flood,
}


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="python -m detection",
        description="Batch detectors over the server database; writes only `detections`.")
    sub = parser.add_subparsers(dest="detector", metavar="DETECTOR", required=True)
    for name, mod in DETECTORS.items():
        p = sub.add_parser(name, help=mod.__doc__.strip().splitlines()[0],
                           description=mod.__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
        mod.add_arguments(p)
    args = parser.parse_args(argv)
    return DETECTORS[args.detector].run(args)


if __name__ == "__main__":
    sys.exit(main())
