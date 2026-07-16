ARG BUILD_FROM
FROM ${BUILD_FROM}

# ASTAP downloads differ per CPU architecture. HA passes BUILD_ARCH
# (aarch64 / amd64) into the build so we can pick the right binary.
ARG BUILD_ARCH

ENV LANG=C.UTF-8

# Runtime deps: python for the API + libs ASTAP's GUI-linked binary needs.
RUN apk add --no-cache \
      python3 \
      py3-pip \
      curl \
      unzip \
      binutils \
      tar \
      xz \
      zstd \
      libgcc \
      libstdc++

# --- Install ASTAP command-line solver -------------------------------------
# The CLI build is a single self-contained binary (astap_cli).
RUN set -eux; \
    case "${BUILD_ARCH}" in \
      aarch64) ASTAP_ZIP="astap_command-line_version_Linux_aarch64.zip" ;; \
      amd64)   ASTAP_ZIP="astap_command-line_version_Linux_amd64.zip" ;; \
      *) echo "Unsupported arch: ${BUILD_ARCH}" && exit 1 ;; \
    esac; \
    curl -fSL "https://downloads.sourceforge.net/project/astap-program/linux_installer/${ASTAP_ZIP}" \
      -o /tmp/astap.zip; \
    unzip /tmp/astap.zip -d /tmp/astap; \
    install -m 0755 /tmp/astap/astap_cli /usr/bin/astap_cli; \
    rm -rf /tmp/astap /tmp/astap.zip

# --- Deep sky annotation database ------------------------------------------
# The CLI's -annotate option only exists in the GUI build, so we do our own
# in-field object lookup instead (see app/main.py). ASTAP's object catalogue
# is a plain 1.1 MB CSV (30k objects) bundled only in the GUI .deb — the CLI
# zip does not ship it. It's pure data (architecture-independent), so we pull
# it from the amd64 package on every build and bake it into the image.
RUN set -eux; \
    mkdir -p /opt/astap /tmp/dsdb; \
    curl -fSL "https://downloads.sourceforge.net/project/astap-program/linux_installer/astap_amd64.deb" \
      -o /tmp/dsdb/astap.deb; \
    ( cd /tmp/dsdb && ar x astap.deb && tar xf data.tar.* ./opt/astap/deep_sky.csv ); \
    install -m 0644 /tmp/dsdb/opt/astap/deep_sky.csv /opt/astap/deep_sky.csv; \
    rm -rf /tmp/dsdb

# --- Python API ------------------------------------------------------------
COPY requirements.txt /tmp/requirements.txt
RUN pip3 install --no-cache-dir --break-system-packages -r /tmp/requirements.txt

COPY rootfs /
COPY app /app

WORKDIR /app

# run.sh (via S6 or direct) downloads the star DB then starts the API.
CMD ["/run.sh"]
