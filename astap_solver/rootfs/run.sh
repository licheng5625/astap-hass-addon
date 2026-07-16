#!/usr/bin/with-contenv bashio
# shellcheck shell=bash
set -e

# Star databases are large (D50 ~ 826 MB), so we don't bake them into the
# image. Download the configured one into the persistent /share volume on
# first run and reuse it afterwards.
#
# We stage the download + extraction inside /share (real disk) rather than
# /tmp, because /tmp is often a tmpfs (RAM) and extracting ~800 MB there can
# OOM a Raspberry Pi. Files are only moved into place after a full successful
# extraction, so a failed/interrupted run never leaves a half-populated DB.

STAR_DB="$(bashio::config 'star_db')"
STAR_DB_DIR="/share/astap_star_db"
STAGE_DIR="${STAR_DB_DIR}/.stage"
mkdir -p "${STAR_DB_DIR}"

install_star_db() {
  local deb url
  deb="${STAR_DB}_star_database.deb"
  url="https://downloads.sourceforge.net/project/astap-program/star_databases/${deb}"

  rm -rf "${STAGE_DIR}"
  mkdir -p "${STAGE_DIR}"

  bashio::log.info "Downloading star database '${STAR_DB}' (~hundreds of MB)..."
  if ! curl -fSL "${url}" -o "${STAGE_DIR}/${deb}"; then
    bashio::log.warning "Download failed: ${url}"
    rm -rf "${STAGE_DIR}"
    return 1
  fi

  bashio::log.info "Extracting ${deb} (no progress shown, please wait)..."
  # A .deb is an ar archive containing data.tar.{xz,gz,zst}.
  ( cd "${STAGE_DIR}" && ar x "${deb}" ) || { rm -rf "${STAGE_DIR}"; return 1; }
  if ! tar xf "${STAGE_DIR}"/data.tar.* -C "${STAGE_DIR}"; then
    bashio::log.warning "Extraction of data.tar failed."
    rm -rf "${STAGE_DIR}"
    return 1
  fi

  # Star DB files land under usr/share/astap or similar; move them flat.
  local moved=0
  while IFS= read -r -d '' f; do
    mv "$f" "${STAR_DB_DIR}/"
    moved=$((moved + 1))
  done < <(find "${STAGE_DIR}" -type f \( "${STAR_DB_GLOBS[@]}" \) -print0)

  rm -rf "${STAGE_DIR}"

  if [ "${moved}" -eq 0 ]; then
    bashio::log.warning "No star database files found inside ${deb}."
    return 1
  fi
  bashio::log.info "Star database '${STAR_DB}' installed (${moved} files)."
  return 0
}

# Star database file extensions vary per catalogue (.290 / .1476 / .157).
STAR_DB_GLOBS=(-name "*.290" -o -name "*.1476" -o -name "*.157")

star_db_present() {
  [ -n "$(find "${STAR_DB_DIR}" -maxdepth 1 -type f \( "${STAR_DB_GLOBS[@]}" \) -print -quit 2>/dev/null)" ]
}

# Only download if this catalogue isn't already present.
if star_db_present; then
  bashio::log.info "Star database already present in ${STAR_DB_DIR}."
else
  if ! install_star_db; then
    bashio::log.warning "Could not install star database automatically."
    bashio::log.warning "The API will start but solving will fail until a DB is present."
    bashio::log.warning "Place star DB files manually in ${STAR_DB_DIR} (see README)."
  fi
fi

export STAR_DB_DIR
export SEARCH_RADIUS="$(bashio::config 'search_radius')"
export DEFAULT_FOV="$(bashio::config 'default_fov')"

bashio::log.info "Starting ASTAP solver API on :8000"
cd /app
exec python3 -m uvicorn main:app --app-dir /app --host 0.0.0.0 --port 8000
