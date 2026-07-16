#!/usr/bin/with-contenv bashio
# shellcheck shell=bash
set -e

# Star databases are large (D50 ~ hundreds of MB), so we don't bake them into
# the image. Download the configured one into the persistent /share volume on
# first run and reuse it afterwards.

STAR_DB="$(bashio::config 'star_db')"
STAR_DB_DIR="/share/astap_star_db"
mkdir -p "${STAR_DB_DIR}"

# ASTAP looks for star databases in the directory given by -d. Each catalogue
# is distributed as a set of *.290 / *.1476 files inside a .deb; we grab the
# raw archive from SourceForge and extract the data files.
if ! ls "${STAR_DB_DIR}"/*."${STAR_DB}"* >/dev/null 2>&1 && \
   ! ls "${STAR_DB_DIR}"/*.290 >/dev/null 2>&1; then
  bashio::log.info "Star database '${STAR_DB}' not found in ${STAR_DB_DIR}, downloading..."

  DEB="${STAR_DB}_star_database.deb"
  URL="https://downloads.sourceforge.net/project/astap-program/star_databases/${DEB}"

  cd /tmp
  if curl -fSL "${URL}" -o "${DEB}"; then
    # A .deb is an ar archive containing data.tar.*; extract the star files.
    ar x "${DEB}"
    tar xf data.tar.* -C /tmp
    # Star DB files land under usr/share/astap or similar; move them flat.
    find /tmp -type f \( -name "*.290" -o -name "*.1476" -o -name "*.157" \) \
      -exec mv {} "${STAR_DB_DIR}/" \;
    rm -f "${DEB}" data.tar.* control.tar.* debian-binary
    bashio::log.info "Star database '${STAR_DB}' installed."
  else
    bashio::log.warning "Could not download ${URL}."
    bashio::log.warning "Place star DB files manually in ${STAR_DB_DIR} (see README)."
  fi
else
  bashio::log.info "Star database already present in ${STAR_DB_DIR}."
fi

export STAR_DB_DIR
export SEARCH_RADIUS="$(bashio::config 'search_radius')"
export DEFAULT_FOV="$(bashio::config 'default_fov')"

bashio::log.info "Starting ASTAP solver API on :8000"
exec python3 -m uvicorn main:app --host 0.0.0.0 --port 8000
