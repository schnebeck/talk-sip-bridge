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
