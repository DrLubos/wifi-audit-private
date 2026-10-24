"""python3 -m evaluation.dataset DIR --sensor S --from F --to T

Renders DIR/dataset.md from the CSVs export.sh wrote into DIR. Standard
library only; touches no database."""

import argparse
import os
import sys

from . import render


def main(argv=None):
    p = argparse.ArgumentParser(prog="python3 -m evaluation.dataset", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("dir")
    p.add_argument("--sensor", default="pi-fri")
    p.add_argument("--from", dest="frm", default="")
    p.add_argument("--to", default="")
    a = p.parse_args(argv)
    md = render.render(a.dir, a.sensor, a.frm, a.to)
    with open(os.path.join(a.dir, "dataset.md"), "w", encoding="utf-8") as f:
        f.write(md + "\n")
    print("-> %s" % os.path.join(a.dir, "dataset.md"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
