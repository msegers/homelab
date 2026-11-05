ARG OPENCODE_IMAGE=ghcr.io/anomalyco/opencode:2.1.4@sha256:6176a71bf7c1352d9aa9e7f112a4ef38443079a2f435789a33dd5ae809acea1a
FROM ${OPENCODE_IMAGE}

USER root
RUN apk add --no-cache \
      bash build-base ca-certificates cargo clang curl git nodejs npm \
      openssh-client openssl-dev pkgconf rust rust-clippy rustfmt \
    && addgroup -g 10001 opencode \
    && adduser -D -u 10001 -G opencode -s /bin/bash opencode \
    && mkdir -p /workspace \
    && chown opencode:opencode /workspace

ENV HOME=/home/opencode \
    XDG_CONFIG_HOME=/home/opencode/.config \
    XDG_DATA_HOME=/home/opencode/.local/share \
    XDG_STATE_HOME=/home/opencode/.local/state \
    XDG_CACHE_HOME=/home/opencode/.cache \
    CARGO_BUILD_JOBS=2
USER 10001:10001
WORKDIR /workspace
ENTRYPOINT ["opencode"]
