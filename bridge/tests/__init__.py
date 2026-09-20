"""Tests that need no phone gateway, no signaling server and no network.

Run from the bridge directory:

    python3 -m unittest discover -s tests -t .

See tests/support.py for the three tiers of dependency and which tests skip
when the media stack is not installed.
"""
