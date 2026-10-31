# Where agent commands run (Hermes connects over SSH). Holds git, gh, gitleaks,
# mise for per-project toolchains, Node with Playwright and a headless Chromium
# (`shoot`), and the pinned Ponytail and Impeccable skills under /opt/skills.
# No model keys ever live here.
# Everything is pinned; bump versions only once a release is 24 h old.
FROM ubuntu:26.04@sha256:c098726dd9b9733f3e885f8de0ef375206a311b0a317eadc40d347b1115fb944

ARG GITLEAKS_VERSION=8.30.1
ARG MISE_VERSION=2026.10.19
ARG NODE_VERSION=24.22.2
ARG NODE_SHA256=48cc0e21e048790e54c0cf5749c2725005313d652651c40b7a21feb9697aaf75
ARG PLAYWRIGHT_VERSION=1.64.1
ARG IMPECCABLE_VERSION=4.8.0
# Ponytail v5.3.3; the build checks the tag still points at this commit.
ARG PONYTAIL_TAG=v5.3.3
ARG PONYTAIL_COMMIT=7801d061243d7b753bad0e0b3c057cd250b11755

RUN apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      openssh-server ca-certificates curl git gh build-essential pkg-config \
      python3 python3-venv unzip xz-utils jq util-linux \
 && rm -rf /var/lib/apt/lists/*

# gitleaks and mise: release binaries, checksum-verified.
RUN set -eu; cd /tmp \
 && curl -fsSLO "https://github.com/gitleaks/gitleaks/releases/download/v${GITLEAKS_VERSION}/gitleaks_${GITLEAKS_VERSION}_linux_x64.tar.gz" \
 && curl -fsSLO "https://github.com/gitleaks/gitleaks/releases/download/v${GITLEAKS_VERSION}/gitleaks_${GITLEAKS_VERSION}_checksums.txt" \
 && grep " gitleaks_${GITLEAKS_VERSION}_linux_x64.tar.gz\$" "gitleaks_${GITLEAKS_VERSION}_checksums.txt" | sha256sum -c - \
 && tar -xzf "gitleaks_${GITLEAKS_VERSION}_linux_x64.tar.gz" -C /usr/local/bin gitleaks \
 && curl -fsSLO "https://github.com/jdx/mise/releases/download/v${MISE_VERSION}/mise-v${MISE_VERSION}-linux-x64" \
 && curl -fsSLO "https://github.com/jdx/mise/releases/download/v${MISE_VERSION}/SHASUMS256.txt" \
 && grep " \./mise-v${MISE_VERSION}-linux-x64\$\| mise-v${MISE_VERSION}-linux-x64\$" SHASUMS256.txt | sed 's# \./# #' | sha256sum -c - \
 && install -m 0755 "mise-v${MISE_VERSION}-linux-x64" /usr/local/bin/mise \
 && rm -f /tmp/gitleaks_* /tmp/mise-* /tmp/SHASUMS256.txt

# Node (LTS, checksum-verified), Playwright with only the headless Chromium
# shell (smaller and lighter than full Chromium) and its system libraries, and
# the Impeccable CLI. `shoot` wraps them; see that file.
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright NODE_PATH=/usr/local/lib/node_modules \
    DO_NOT_TRACK=1 IMPECCABLE_NO_TELEMETRY=1 IMPECCABLE_BROWSER=/usr/local/bin/chromium-headless-shell
RUN set -eu; cd /tmp \
 && curl -fsSLO "https://nodejs.org/dist/v${NODE_VERSION}/node-v${NODE_VERSION}-linux-x64.tar.xz" \
 && echo "${NODE_SHA256}  node-v${NODE_VERSION}-linux-x64.tar.xz" | sha256sum -c - \
 && tar -xJf "node-v${NODE_VERSION}-linux-x64.tar.xz" -C /usr/local --strip-components=1 \
      --exclude='*/doc' --exclude='*/include' --exclude='*/share' --exclude='*/CHANGELOG.md' --exclude='*/README.md' \
 && rm -f "node-v${NODE_VERSION}-linux-x64.tar.xz" \
 && npm install -g --no-fund --no-audit --no-update-notifier \
      "playwright@${PLAYWRIGHT_VERSION}" "impeccable@${IMPECCABLE_VERSION}" \
 && apt-get update \
 && DEBIAN_FRONTEND=noninteractive playwright install --with-deps --only-shell chromium \
 && rm -rf /var/lib/apt/lists/* /root/.npm /tmp/* \
 # For tools that start the browser themselves (Impeccable): the container has no
 # user namespaces for Chromium's own sandbox, and /dev/shm is only 64 MB.
 && printf '#!/bin/sh\nexec %s --no-sandbox --disable-dev-shm-usage --disable-gpu "$@"\n' \
      "$(find /opt/ms-playwright -type f -name chrome-headless-shell | head -1)" > /usr/local/bin/chromium-headless-shell \
 && chmod 0755 /usr/local/bin/chromium-headless-shell \
 && chromium-headless-shell --version \
 # SSH sessions don't get the image's ENV; pam_env gives them these.
 && printf '%s\n' "PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright" "NODE_PATH=/usr/local/lib/node_modules" \
      "DO_NOT_TRACK=1" "IMPECCABLE_NO_TELEMETRY=1" "IMPECCABLE_BROWSER=/usr/local/bin/chromium-headless-shell" >> /etc/environment
COPY --chmod=0755 shoot /usr/local/bin/shoot
COPY --chmod=0755 shoot.cjs /usr/local/lib/shoot/

# Ponytail's skills (MIT), read-only, at a pinned commit. Only the skill files:
# its Hermes plugin would inject its rules into every agent's every turn, and
# its mode switch is gateway-wide (Ishikawa, SEC-19).
RUN set -eu; git clone -q --depth 1 --branch "${PONYTAIL_TAG}" https://github.com/DietrichGebert/ponytail /tmp/ponytail \
 && [ "$(git -C /tmp/ponytail rev-parse HEAD)" = "${PONYTAIL_COMMIT}" ] \
 && mkdir -p /opt/skills \
 && cp -r /tmp/ponytail/skills /opt/skills/ponytail && cp /tmp/ponytail/LICENSE /opt/skills/ponytail/ \
 && echo "ponytail ${PONYTAIL_TAG} ${PONYTAIL_COMMIT}" > /opt/skills/ponytail/VERSION \
 && rm -rf /tmp/ponytail

# Non-root agent user, no sudo. Key-only SSH, no root login, no forwarding.
RUN useradd -m -u 10001 -s /bin/bash agent \
 # '*' = no password but not locked; sshd may refuse key logins to locked ('!') accounts.
 && usermod -p '*' agent \
 && mkdir -p /run/sshd /etc/ssh/authorized_keys \
 && printf '%s\n' \
      'PasswordAuthentication no' 'KbdInteractiveAuthentication no' 'PermitRootLogin no' \
      'AuthorizedKeysFile /etc/ssh/authorized_keys/%u' 'AllowUsers agent' \
      'AllowTcpForwarding no' 'X11Forwarding no' 'PermitTunnel no' \
      'HostKey /etc/ssh/hostkeys/ssh_host_ed25519_key' \
      > /etc/ssh/sshd_config.d/agent.conf

USER agent
RUN echo 'eval "$(mise activate bash)"' >> ~/.bashrc \
 # An address on Petter's own domain with no GitHub account behind it, so no
 # stranger's account is credited (a ...@users.noreply.github.com address is).
 && git config --global user.name "Section 9 agent" \
 && git config --global user.email "agents@sectionnine.dev" \
 && git config --global credential.helper store \
 && git config --global core.hooksPath ~/.config/git/hooks \
 && mkdir -p ~/work ~/.config/git/hooks ~/.config/gh
COPY --chown=agent:agent --chmod=0755 pre-push /home/agent/.config/git/hooks/pre-push
# Impeccable's skill for Hermes, installed for the agent user; the engine it
# downloads is checksum-verified against the package's own list. No hooks, no telemetry.
RUN cd /tmp && impeccable install -y --providers=hermes --scope=global --no-hooks \
 && impeccable telemetry off
USER root
# Read-only for the agents, and next to Ponytail's under /opt/skills.
RUN chown -R root:root /home/agent/.hermes/skills/impeccable \
 && ln -s /home/agent/.hermes/skills/impeccable /opt/skills/impeccable

COPY --chmod=0755 entrypoint.sh /usr/local/bin/entrypoint.sh
# Agents report on their own section9-hq issues with it (uses the GitHub token).
COPY --chmod=0755 s9-task /usr/local/bin/s9-task
EXPOSE 22
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
