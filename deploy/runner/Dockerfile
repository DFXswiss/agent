FROM python:3.12-slim-trixie
SHELL ["/bin/bash", "-o", "pipefail", "-c"]
ARG DEBIAN_FRONTEND=noninteractive

# git and gh provide GitHub and checkout tools; tmux runs each job.
# procps supplies ps for supervise process times and doctor, absent in slim.
# postgresql supplies initdb/pg_ctl for the store cluster under AGENT_HOME.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git tmux procps postgresql ca-certificates curl gnupg \
    && mkdir -p /etc/apt/keyrings \
    && curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg -o /etc/apt/keyrings/githubcli-archive-keyring.gpg \
    && chmod a+r /etc/apt/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" > /etc/apt/sources.list.d/github-cli.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends gh \
    && pg_dirs=(/usr/lib/postgresql/*/bin) \
    && test "${#pg_dirs[@]}" -eq 1 && test -d "${pg_dirs[0]}" \
    && mkdir -p /opt/postgresql \
    && ln -s "${pg_dirs[0]}" /opt/postgresql/bin \
    && rm -rf /var/lib/apt/lists/*
# PostgreSQL's major follows the pinned Debian release (trixie: 17).
# Use AGENT_PG_BIN instead of the store's built-in search for majors 15 and 16.
ENV AGENT_PG_BIN=/opt/postgresql/bin

RUN python -m venv /opt/agent-venv
COPY pyproject.toml README.md LICENSE /src/
COPY src /src/src
RUN /opt/agent-venv/bin/pip install --no-cache-dir /src && rm -rf /src
ENV PATH="/opt/agent-venv/bin:${PATH}"
# No provider CLI: no driver table ships here. Add the executables named by
# drivers.json in a derived image.
RUN groupadd --gid 1000 agent \
    && useradd --uid 1000 --gid 1000 --create-home agent \
    && mkdir /agent-home && chown agent:agent /agent-home
ENV AGENT_HOME=/agent-home
USER agent
WORKDIR /home/agent
ENTRYPOINT ["agent"]
