FROM fedora:43

# Install Python and pip
RUN dnf install -y procps-ng python3 python3-pip python3-wheel && \
    dnf clean all

# Install CarConnectivity with all connectors
RUN pip3 install --no-cache-dir --prefix=/usr/local 'carconnectivity[all]'

# Ensure pip-installed binaries are in PATH
ENV PATH="/usr/local/bin:${PATH}"

WORKDIR /app

# Copy plugin source
COPY pyproject.toml README.md LICENSE ./
COPY src/ ./src/

# setuptools_scm requires a git checkout; provide a fallback version for
# container builds where no git history is available.
ENV SETUPTOOLS_SCM_PRETEND_VERSION=0.1.0.dev0

# Install the CamperMode plugin
RUN pip3 install --no-cache-dir --prefix=/usr/local .

# CamperMode web UI port
EXPOSE 4001

CMD ["carconnectivity", "/config/carconnectivity.json"]
