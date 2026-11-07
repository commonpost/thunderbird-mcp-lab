# SPDX-License-Identifier: MIT
# SPDX-FileCopyrightText: 2026 Guillaume Laforest
# thunderbird-mcp lab image: a real Thunderbird (Linux x86_64) + mailpit + Xvfb + Node.js + the harness.
# The network is used ONLY during "docker build"; at run time the container runs with --network none (run-lab.sh).
# Build:  docker build -t thunderbird-mcp-lab:local harness
# Coverage image (the same image plus coverage.py, to measure harness/lab.py; see selftest/coverage.sh):
#         docker build --target coverage -t thunderbird-mcp-lab:coverage harness
#
# Base images are pinned by digest (update them deliberately, e.g. through Dependabot).

# --- Stage 1: the mailpit binary (SMTP + POP3 + HTTP API), copied from the official image.
FROM axllent/mailpit:v1.33.0@sha256:9b3a463be15354c0f0eba91697d80e3e0d8a5623ef0e627cacc836bc17fd5231 AS mailpit

# --- Stage 2: Debian with apt over https://. The slim image fetches packages from http:// sources and has no CA
# certificates for https://; the CA bundle of the mailpit image (pinned above), named to apt (CAInfo), lets apt use
# https:// from the first request. Package integrity still comes from Debian's signed archive metadata.
FROM debian:13-slim@sha256:a99cfc517144bc59b1978475ec53b46ecabec7e43635402ee5b77cc54cd1b20a AS debian-https
COPY --from=mailpit /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-certificates.crt
RUN sed -i 's#^URIs: http://#URIs: https://#' /etc/apt/sources.list.d/debian.sources \
 && echo 'Acquire::https::CAInfo "/etc/ssl/certs/ca-certificates.crt";' > /etc/apt/apt.conf.d/99https-ca \
 && [ ! -e /etc/apt/sources.list ] \
 && ! grep -Rq '^URIs:.*http://' /etc/apt/sources.list.d/ \
 && grep -q '^URIs: https://deb.debian.org/debian$' /etc/apt/sources.list.d/debian.sources

# --- Stage 3: VERIFIED download of Thunderbird (GPG signature of SHA512SUMS, then SHA512 of the archive).
# TB_VERSION names the release directory: 156.0.1, 153.6.0esr or 160.0b5. application.ini of an ESR or a beta says
# Version=153.6.0 or Version=160.0, so the check drops the "esr" and "bN" suffixes.
FROM debian-https AS tbdl
ARG TB_VERSION=156.0.1
ARG TB_BASE=https://archive.mozilla.org/pub/thunderbird/releases
# PRIMARY key fingerprint of "Mozilla Software Releases <release@mozilla.com>" (the KEY file of the release directory;
# SHA512SUMS of 156.0.1 is signed by its subkey 827E658608679618CD349F93678E455D76767AA3). The build fails unless the
# signature is valid AND chains to this primary key. If Mozilla rotates its key, verify the new one out of band first.
ARG MOZ_RELEASE_FPR=14F26682D0916CDD81E37B6D61B7B526D98F0353
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl gnupg gpgv xz-utils \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /dl
SHELL ["/bin/bash", "-o", "pipefail", "-c"]
RUN set -eu; \
    url="$TB_BASE/$TB_VERSION"; f="linux-x86_64/en-US/thunderbird-$TB_VERSION.tar.xz"; \
    get() { curl -fsSL --proto '=https' --tlsv1.2 --retry 3 -o "$1" "$url/$2"; }; \
    get SHA512SUMS SHA512SUMS; get SHA512SUMS.asc SHA512SUMS.asc; get KEY KEY; \
    gpg --batch --dearmor < KEY > moz.gpg; \
    gpgv --status-fd 3 --keyring ./moz.gpg SHA512SUMS.asc SHA512SUMS 3> gpg-status.txt; \
    cat gpg-status.txt; \
    grep -q '^\[GNUPG:\] GOODSIG ' gpg-status.txt; \
    grep -Eq "^\[GNUPG:\] VALIDSIG [0-9A-F]{40} .* $MOZ_RELEASE_FPR\$" gpg-status.txt; \
    if grep -Eq '^\[GNUPG:\] (BADSIG|ERRSIG|EXPKEYSIG|REVKEYSIG|EXPSIG) ' gpg-status.txt; then echo "signature rejected"; exit 1; fi; \
    expected="$(awk -v f="$f" '$2 == f { print $1 }' SHA512SUMS)"; \
    [ "$(printf '%s\n' "$expected" | wc -l)" = 1 ] || { echo "SHA512 sum missing or duplicated"; exit 1; }; \
    [[ "$expected" =~ ^[0-9a-f]{128}$ ]] || { echo "invalid SHA512 sum"; exit 1; }; \
    get tb.tar.xz "$f"; \
    printf '%s  tb.tar.xz\n' "$expected" | sha512sum -c -; \
    mkdir -p /opt/tb; tar -xJf tb.tar.xz -C /opt/tb --no-same-owner; \
    v="${TB_VERSION%esr}"; if [[ "$v" =~ ^([0-9]+(\.[0-9]+)+)b[0-9]+$ ]]; then v="${BASH_REMATCH[1]}"; fi; \
    grep -qxF "Version=$v" /opt/tb/thunderbird/application.ini; \
    echo "Thunderbird $TB_VERSION: GPG signature and SHA512 verified."

# --- Stage 4: runtime image.
FROM debian-https AS lab
LABEL org.opencontainers.image.title="thunderbird-mcp-lab" \
      org.opencontainers.image.description="Offline end-to-end lab for thunderbird-mcp (Thunderbird + mailpit + Xvfb)" \
      org.opencontainers.image.licenses="MIT"
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      libgtk-3-0t64 libasound2t64 libdbus-glib-1-2 libx11-xcb1 libxtst6 libpci3 libegl1 libgl1 \
      xvfb xauth xdotool fonts-dejavu-core fontconfig \
      nodejs python3 jq procps \
 && rm -rf /var/lib/apt/lists/* \
 && rm -f /etc/ssl/certs/ca-certificates.crt /etc/apt/apt.conf.d/99https-ca \
 && rmdir --ignore-fail-on-non-empty /etc/ssl/certs /etc/ssl \
 && { find / -xdev -perm /6000 -type f -exec chmod a-s {} + 2>/dev/null || true; }
# Xvfb compiles its keymap into /var/lib/xkb; the root file system is read-only at run time, so point it at /tmp (tmpfs).
RUN rm -rf /var/lib/xkb && ln -s /tmp /var/lib/xkb \
 && groupadd -g 10001 tblab && useradd -M -u 10001 -g 10001 -d /home/tblab -s /usr/sbin/nologin tblab \
 && mkdir -p /lab /home/tblab /opt/tblab \
 && fc-cache -f >/dev/null
COPY --from=mailpit /mailpit /usr/local/bin/mailpit
COPY --from=tbdl /opt/tb/thunderbird /opt/thunderbird
COPY policies.json /opt/thunderbird/distribution/policies.json
COPY lab.py user.js /opt/tblab/
RUN chown -R root:root /opt/tblab /opt/thunderbird /usr/local/bin/mailpit \
 && chmod 755 /opt/tblab /usr/local/bin/mailpit \
 && chmod 644 /opt/tblab/lab.py /opt/tblab/user.js /opt/thunderbird/distribution/policies.json \
 && rm -f /opt/thunderbird/updater /opt/thunderbird/updater.ini /opt/thunderbird/update-settings.ini \
 && /opt/thunderbird/thunderbird --version
ENV HOME=/home/tblab LANG=C.UTF-8 TZ=UTC
USER 10001:10001
WORKDIR /lab
ENTRYPOINT ["python3", "-I", "/opt/tblab/lab.py"]

# --- Stage 5 (only with --target coverage): the runtime image plus coverage.py, from Debian's signed archive, and
# harness/self_coverage.py, imported by every Python process through a .pth file. It measures harness/lab.py (statements
# and branches) and writes what ran after the result block. The CA bundle is only used by apt and is removed again.
FROM lab AS coverage
USER root
COPY --from=mailpit /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-certificates.crt
RUN apt-get -o Acquire::https::CAInfo=/etc/ssl/certs/ca-certificates.crt update \
 && apt-get -o Acquire::https::CAInfo=/etc/ssl/certs/ca-certificates.crt install -y --no-install-recommends python3-coverage \
 && rm -rf /var/lib/apt/lists/* /etc/ssl
COPY self_coverage.py /usr/lib/python3/dist-packages/lab_self_coverage.py
RUN echo 'import lab_self_coverage' > /usr/lib/python3/dist-packages/lab-self-coverage.pth \
 && chmod 644 /usr/lib/python3/dist-packages/lab_self_coverage.py /usr/lib/python3/dist-packages/lab-self-coverage.pth \
 && LAB_SELF_COVERAGE=off python3 -I -c 'import coverage, lab_self_coverage; print("coverage.py", coverage.__version__)'
USER 10001:10001

# --- The default image: the runtime stage, unchanged (stage 5 is built only when asked for).
FROM lab
