# talk-sip-bridge - test-peer/Dockerfile
# An Asterisk container built from a distribution's own package.
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

# Asterisk from a distribution package rather than a third-party image: the
# point of this peer is to be an ordinary, standards-focused SIP server, so
# what it runs should be a distribution's own build.
#
# Ubuntu, not Debian: Debian no longer ships an asterisk package (neither
# bookworm nor trixie has an installation candidate). Ubuntu 24.04 carries
# Asterisk 20 LTS, whose pjsip stack is the strict, current one - which is
# the whole point of testing against it.
FROM ubuntu:24.04

RUN apt-get update \
    && apt-get install -y --no-install-recommends asterisk \
    && rm -rf /var/lib/apt/lists/*

# Config comes from the compose bind mounts; everything else stays at the
# package defaults. Foreground, so the container's lifetime is Asterisk's.
CMD ["asterisk", "-f", "-vvv"]
