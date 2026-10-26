# Where agent commands run (Hermes connects over SSH). Holds git, gh, gitleaks
# and mise for per-project toolchains. No model keys ever live here.
# Everything is pinned; bump versions only once a release is 24 h old.
FROM ubuntu:26.04@sha256:c098726dd9b9733f3e885f8de0ef375206a311b0a317eadc40d347b1115fb944

ARG GITLEAKS_VERSION=8.30.1
ARG MISE_VERSION=2026.10.19

RUN apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      openssh-server ca-certificates curl git gh build-essential pkg-config \
      python3 python3-venv unzip xz-utils jq \
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
 && git config --global user.name "ghost-agent" \
 && git config --global user.email "ghost-agent@users.noreply.github.com" \
 && git config --global credential.helper store \
 && git config --global core.hooksPath ~/.config/git/hooks \
 && mkdir -p ~/work ~/.config/git/hooks ~/.config/gh
COPY --chown=agent:agent --chmod=0755 pre-push /home/agent/.config/git/hooks/pre-push

USER root
COPY --chmod=0755 entrypoint.sh /usr/local/bin/entrypoint.sh
EXPOSE 22
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
