# Slim repackaging of the official Paperclip image.
#
# The official image (ghcr.io/paperclipai/paperclip, 3.4 GB compressed) is
# mostly things our setup never runs. This image keeps the application and the
# runtime exactly as upstream's `production` stage builds them, and drops:
#
#   - the global npm install of the agent CLIs (claude-code, codex, opencode,
#     gemini-cli, kimi-code), ~986 MB compressed. Our agents use the
#     hermes_gateway adapter, so Paperclip never starts them, and with them
#     gone nothing in the container that holds DATABASE_URL can run them.
#   - /opt/paperclip-runner/provider-pack (+ its chmod/verify step and the
#     PAPERCLIP_RUNNER_REMOTE_PROVIDER_PACK_PATH variable), ~1.2 GB compressed.
#     It serves remote OpenCode/ACPX runs, which we do not use.
#
# Everything else is reproduced from upstream's Dockerfile at the tag in the
# `upstream` line below (stages `base` and `production`): the same apt
# packages (Paperclip may shell out to git/gh/ssh), the node user and UID/GID
# handling, /paperclip ownership, the entrypoint script, ENV, EXPOSE, tini as
# PID 1, and the default command. The application itself is copied from the
# upstream image, not rebuilt.
#
# Built and pushed by .github/workflows/paperclip-image.yml to
# ghcr.io/petterfranzen/paperclip-slim. That workflow starts the image against
# Postgres and compares its config with the upstream image's before pushing.
#
# To bump Paperclip:
#   1. Read upstream's Dockerfile and scripts/docker-entrypoint.sh at the new
#      tag and port any change to its `base` / `production` stages here.
#   2. Change the digest (and tag) on the `upstream` line, plus the two
#      PAPERCLIP_BUILD_* defaults below (upstream's image config shows them).
#   3. Open a PR: the workflow builds, smoke-tests and compares the config.
#      After the merge it pushes the image and prints its digest in the run
#      summary.
#   4. Pin that digest in compose.yaml in a follow-up PR (no earlier than 24 h
#      after the upstream release).

FROM ghcr.io/paperclipai/paperclip:2026.1023.0@sha256:40f46940c4612ead131aebe6079cd4651f8cd1434af49f7c621887f4a452a62e AS upstream

# Same Node base as upstream's `base` stage (node:24-trixie-slim), by digest.
FROM node:26-trixie-slim@sha256:e4db4c914bf8ad241bd24804fc25d272d89bad97e68cdc8a1414862790a02c39

ARG USER_UID=1000
ARG USER_GID=1000

# Upstream `base`: OS packages and corepack.
RUN apt-get update \
  && apt-get install -y --no-install-recommends ca-certificates gosu curl gh git wget ripgrep python3 tini \
  && rm -rf /var/lib/apt/lists/* \
  && corepack enable

# Modify the existing node user/group to have the specified UID/GID to match host user
RUN usermod -u $USER_UID --non-unique node \
  && groupmod -g $USER_GID --non-unique node \
  && usermod -g $USER_GID -d /paperclip node

# Upstream `production`, minus the global npm CLI tools.
WORKDIR /app
RUN apt-get update \
  && apt-get install -y --no-install-recommends openssh-client jq \
  && rm -rf /var/lib/apt/lists/* \
  && mkdir -p /paperclip \
  && chown node:node /paperclip

COPY --from=upstream --chmod=755 /usr/local/bin/docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh

COPY --chown=node:node --from=upstream /app /app

# Upstream's values for this release (see its image config).
ARG PAPERCLIP_BUILD_VERSION=v2026.1023.0-0-g1b4c80381
ARG PAPERCLIP_BUILD_COMMIT=1b4c803816e82b0265c8422e1a804327a7ed8c54
ENV NODE_ENV=production \
  HOME=/paperclip \
  HOST=0.0.0.0 \
  PORT=3100 \
  SERVE_UI=true \
  PAPERCLIP_HOME=/paperclip \
  PAPERCLIP_INSTANCE_ID=default \
  PAPERCLIP_BUILD_VERSION=${PAPERCLIP_BUILD_VERSION} \
  PAPERCLIP_BUILD_COMMIT=${PAPERCLIP_BUILD_COMMIT} \
  USER_UID=${USER_UID} \
  USER_GID=${USER_GID} \
  PAPERCLIP_CONFIG=/paperclip/instances/default/config.json \
  PAPERCLIP_DEPLOYMENT_MODE=authenticated \
  PAPERCLIP_DEPLOYMENT_EXPOSURE=private \
  OPENCODE_ALLOW_ALL_MODELS=true \
  GEMINI_SANDBOX=false

# Links the ghcr.io package to this repository, so this repo's workflows may
# push it with GITHUB_TOKEN.
LABEL org.opencontainers.image.source="https://github.com/petterfranzen/agent-manager" \
  org.opencontainers.image.description="Slim repackaging of the official Paperclip image: no agent CLIs, no provider pack" \
  org.opencontainers.image.licenses="MIT"

EXPOSE 3100

# tini, not node, is PID 1 (upstream's reasoning: reaps orphaned descendants).
ENTRYPOINT ["/usr/bin/tini", "--", "docker-entrypoint.sh"]
CMD ["node", "--import", "./server/node_modules/tsx/dist/loader.mjs", "server/dist/index.js"]
