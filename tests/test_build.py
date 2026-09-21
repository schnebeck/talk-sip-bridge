# talk-sip-bridge - tests/test_build.py
# Does it build: every module imports, and the cheap ones stay cheap.
#
#   Copyright (C) 2026 Thorsten Schnebeck <thorsten.schnebeck@gmx.net>
#   Produced by Thorsten Schnebeck - the idea, the decisions, the testing.
#   Written by Anthropic Claude Opus 5 - AI generated content.
#
#   Free software under the GNU General Public License, version 3 or later.
#   There is no warranty, to the extent permitted by law. The full text is
#   in LICENSES/GPL-3.0-or-later.txt.
#
# SPDX-FileCopyrightText: (C) 2026 Thorsten Schnebeck <thorsten.schnebeck@gmx.net>
# SPDX-FileContributor: Anthropic Claude Opus 5 (AI generated content)
# SPDX-License-Identifier: GPL-3.0-or-later

"""Does it build: every module imports, and the cheap ones stay cheap."""
import os
import subprocess
import sys
import unittest

from tests import CODE_DIR
from tests.support import bridge_env, env, needs_media_stack

PURE_MODULES = ["payload_types", "sip_messages", "sip_sdp", "sip_requests", "room_state", "call"]
CONFIG_MODULES = ["config", "sip_transport", "sip_registrar", "talk_ocs"]
# Pure like the SIP message builders: dicts in, dicts out.
PURE_MODULES.append("talk_messages")
MEDIA_MODULES = ["g711", "g722", "agc", "rtp", "media", "call_media", "sip_call",
                 "talk_client", "control_api", "daemon"]


def import_in_subprocess(module: str, environment: dict = None) -> subprocess.CompletedProcess:
    """A fresh interpreter per module: an import that only succeeds because
    something else already imported its dependency is not an import that
    works."""
    return subprocess.run(
        [sys.executable, "-c", f"import {module}"],
        capture_output=True, text=True, env=environment or {"PATH": "/usr/bin:/bin"})


class BuildTest(unittest.TestCase):
    def test_pure_modules_import_without_anything(self):
        for module in PURE_MODULES:
            with self.subTest(module=module):
                result = import_in_subprocess(module, {"PATH": "/usr/bin:/bin", "PYTHONPATH": str(CODE_DIR)})
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_config_modules_import_with_environment(self):
        environment = {"PATH": "/usr/bin:/bin", "PYTHONPATH": str(CODE_DIR)}
        environment.update(bridge_env())
        for module in CONFIG_MODULES:
            with self.subTest(module=module):
                result = import_in_subprocess(module, environment)
                self.assertEqual(result.returncode, 0, result.stderr)

    @needs_media_stack
    def test_media_modules_import(self):
        environment = {"PATH": "/usr/bin:/bin", "PYTHONPATH": str(CODE_DIR)}
        environment.update(bridge_env())
        for module in MEDIA_MODULES:
            with self.subTest(module=module):
                result = import_in_subprocess(module, environment)
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_message_building_does_not_pull_in_the_media_stack(self):
        """SDP and message assembly are string work. Importing the codecs
        for them would put a media library between these tests and the
        machine they run on, which is what payload_types.py exists to
        avoid."""
        probe = (
            "import sys, sip_requests, sip_sdp, sip_messages, room_state;"
            "heavy = [m for m in ('av', 'numpy', 'aiortc', 'rtp', 'g722') if m in sys.modules];"
            "print(','.join(heavy))"
        )
        result = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True,
                                env={"PATH": "/usr/bin:/bin", "PYTHONPATH": str(CODE_DIR)})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "", "unexpectedly imported: " + result.stdout)


class ConfigBuildTest(unittest.TestCase):
    def test_single_line_configuration(self):
        with env() as config:
            self.assertEqual(len(config.lines), 1)
            self.assertEqual(config.lines[0].id, "default")
            self.assertEqual(config.lines[0].sip_user, "sip-phone")

    def test_multiple_lines_get_their_own_settings(self):
        with env(BRIDGE_LINES="a,b",
                 BRIDGE_LINE_a_SIP_USER="user-a", BRIDGE_LINE_a_SIP_PASS="pw-a",
                 BRIDGE_LINE_a_GATEWAY_HOST="192.0.2.1",
                 BRIDGE_LINE_a_LOCAL_SIP_PORT="5101", BRIDGE_LINE_a_LOCAL_RTP_PORT="41000",
                 BRIDGE_LINE_b_SIP_USER="user-b", BRIDGE_LINE_b_SIP_PASS="pw-b",
                 BRIDGE_LINE_b_GATEWAY_HOST="192.0.2.2",
                 BRIDGE_LINE_b_LOCAL_SIP_PORT="5102", BRIDGE_LINE_b_LOCAL_RTP_PORT="41010") as config:
            self.assertEqual([line.id for line in config.lines], ["a", "b"])
            self.assertEqual(config.lines[0].sip_user, "user-a")
            self.assertEqual(config.lines[1].gateway_host, "192.0.2.2")
            self.assertNotEqual(config.lines[0].local_sip_port, config.lines[1].local_sip_port)
            self.assertNotEqual(config.lines[0].local_rtp_port, config.lines[1].local_rtp_port)

    def test_the_environment_a_test_sees_is_only_the_one_it_asked_for(self):
        """Run from a shell that sourced a deployment's env file, the suite
        would otherwise read that deployment's settings and answer
        differently - which is how a relay address once turned a passing
        test red on one machine and green on another."""
        os.environ["BRIDGE_RELAY_LAN_HOST"] = "203.0.113.99"
        os.environ["BRIDGE_LINES"] = "stray"
        try:
            with env() as config:
                self.assertFalse(config.lines[0].media_relay_enabled)
                self.assertEqual([line.id for line in config.lines], ["default"])
        finally:
            os.environ.pop("BRIDGE_RELAY_LAN_HOST", None)
            os.environ.pop("BRIDGE_LINES", None)

    def test_media_relay_is_off_until_all_four_settings_are_present(self):
        with env() as config:
            self.assertFalse(config.lines[0].media_relay_enabled)
        with env(BRIDGE_RELAY_LAN_HOST="203.0.113.10", BRIDGE_RELAY_LAN_PORT="40000",
                 BRIDGE_RELAY_OVERLAY_HOST="198.51.100.2", BRIDGE_RELAY_OVERLAY_PORT="40001") as config:
            self.assertTrue(config.lines[0].media_relay_enabled)


if __name__ == "__main__":
    unittest.main()
