"""Tests that need no phone gateway, no signaling server and no network.

Installed and removed separately from the daemon: the code under test does
not live here and is not a package, so this file puts it on the import path
before any test module runs. `BRIDGE_CODE` points at an installed copy;
without it the sibling `bridge/` of a checkout is used.

Run from the repository root, or from wherever this directory was installed:

    python3 -m unittest discover -s tests -t .
    BRIDGE_CODE=/opt/fritzbox-talk-bridge python3 -m unittest discover -s tests -t .

`hardware/` holds the other kind: scripts that place real calls against a
gateway, a signaling server or the Asterisk test peer. They are not
discovered from here and are run by hand - see tests/README.md.
"""
import os
import pathlib
import sys

CODE_DIR = pathlib.Path(os.environ.get("BRIDGE_CODE")
                        or pathlib.Path(__file__).resolve().parent.parent / "bridge")
if str(CODE_DIR) not in sys.path:
    sys.path.insert(0, str(CODE_DIR))
