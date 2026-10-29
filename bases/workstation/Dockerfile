# syntax=docker/dockerfile:1.4@sha256:9ba7531bd80fb0a858632727cf7a112fbfd19b17e94c4e84ced81e24ef1a0dbc
# ZeroPath's platform-neutral model workstation. Platform adapters may
# build on this image; platform SDKs, controllers, graders, and MCP servers do
# not belong here.
FROM docker.io/ubuntu:24.04@sha256:534baea6a22c03a63003dbc8dbe78fe34bc0d7e595d9a9dc9834884ff530eb55

# Each `|| true` is scoped to its own command: the leading deletes are absent on
# some base tags, but a failed groupadd/useradd must fail the build. (The ghl
# shim base this was extracted from let one trailing `|| true` swallow the whole
# chain.)
RUN (userdel -r ubuntu 2>/dev/null || true) && \
    (groupdel ubuntu 2>/dev/null || true) && \
    groupadd -g 1000 model && \
    useradd -m -u 1000 -g 1000 model && \
    (gpasswd -d model root || true)

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y \
    ca-certificates \
    curl \
    git \
    sudo \
    python3 \
    python3-venv \
    python3-dev \
    python3-pip \
    && apt-get clean && rm -rf /var/lib/apt/lists/* \
    && ln -sf /usr/bin/python3 /usr/bin/python \
    && rm -f /usr/lib/python3.12/EXTERNALLY-MANAGED \
    && pip install --no-cache-dir uv==0.10.9

ENV WORKDIR=/workdir
RUN mkdir -p ${WORKDIR} && chown -R model:model ${WORKDIR}
WORKDIR ${WORKDIR}
