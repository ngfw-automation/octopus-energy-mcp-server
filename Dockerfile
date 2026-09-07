# Octopus Energy MCP server — streamable-HTTP backend.
#
# Base: Chainguard Python (wolfi-based, minimal, NO SHELL — every RUN uses
# the exec form). Pinned by digest, which is the version lock: this is
# Python 3.14.7 as of 2026-09-04. To bump, pull the new image and paste its
# digest (docker pull prints it).
FROM cgr.dev/chainguard/python:latest@sha256:1f37785e5cdb70151f36aaa15e1e3cef4571424dbefbf4b0d8a9222535cb13ff

WORKDIR /app

# Package metadata first for layer caching, then the code.
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
COPY scripts/healthcheck.py /opt/healthcheck.py

# The base image runs as `nonroot` and its system Python is PEP 668
# "externally managed" (pip refuses system installs), so build as root into
# a dedicated venv, then drop back to nonroot for the runtime.
USER root
RUN ["python3", "-m", "venv", "/opt/venv"]
ENV PATH="/opt/venv/bin:$PATH"
RUN ["/opt/venv/bin/pip", "install", "--no-cache-dir", "."]

USER nonroot

# Streamable HTTP MCP endpoint: http://<host>:8000/mcp
# (proxied by NGINX in compose.yaml; MCP_HOST defaults to 127.0.0.1, so the
# container sets MCP_HOST=0.0.0.0)
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
    CMD ["/opt/venv/bin/python", "/opt/healthcheck.py"]

# The base image sets ENTRYPOINT to its own python (and runs as nonroot by
# default) — take over the entrypoint so the venv interpreter runs the app.
ENTRYPOINT ["/opt/venv/bin/python"]
CMD ["-m", "octopus_mcp"]
