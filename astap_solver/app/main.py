"""ASTAP plate-solving HTTP API.

Upload an image (FITS/JPG/PNG/TIFF); the service runs the ASTAP command-line
solver and returns the astrometric solution (center RA/Dec, rotation, pixel
scale, field of view) as JSON.

ASTAP writes its result into a sidecar ``.ini`` file next to the input, using
simple ``KEY=VALUE`` lines. ``PLTSOLVD=T`` marks a successful solve.
"""
import asyncio
import json
import math
import os
import tempfile
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import HTMLResponse, StreamingResponse

app = FastAPI(title="ASTAP Plate Solver", version="0.4.1")

STAR_DB_DIR = os.environ.get("STAR_DB_DIR", "/share/astap_star_db")
SEARCH_RADIUS = os.environ.get("SEARCH_RADIUS", "30")
DEFAULT_FOV = float(os.environ.get("DEFAULT_FOV", "0") or 0)
ASTAP_BIN = "/usr/bin/astap_cli"
# ASTAP's deep sky object catalogue, baked into the image at build time.
DEEPSKY_CSV = os.environ.get("DEEPSKY_CSV", "/opt/astap/deep_sky.csv")

# A solve on a slow host (Raspberry Pi) or a blind solve of a large image can
# take several minutes, so allow a generous timeout. Configurable via the
# add-on options (solve_timeout) through this env var.
SOLVE_TIMEOUT = int(os.environ.get("SOLVE_TIMEOUT", "600") or 600)


async def _run_astap(cmd: list, timeout: int = SOLVE_TIMEOUT) -> tuple:
    """Run astap_cli without blocking the event loop. Returns (output, rc).

    Using an async subprocess (not the blocking subprocess.run) is essential:
    the endpoints are ``async def``, so a blocking call would stall the whole
    event loop and make concurrent requests pile up and time out.

    Raises asyncio.TimeoutError if the solve exceeds ``timeout`` seconds.
    """
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise
    return stdout.decode(errors="ignore"), proc.returncode


# --- Deep sky object annotation --------------------------------------------
# ASTAP's own -annotate flag exists only in the GUI build (not astap_cli) and
# merely renders a labelled JPEG, so we do the in-field lookup ourselves from
# ASTAP's catalogue file. The CSV holds ~30k objects, one per line:
#
#   RA[0..864000], DEC[-324000..324000], name(s), length[0.1'], width[0.1'], orientation[deg]
#
# RA is in 1/2400 degree units (864000/360), DEC in arc-seconds offset such
# that 324000 == +90 deg. Names carry aliases separated by "/". The size and
# orientation columns are optional (point-like objects omit them).

# Loaded once at startup: list of dicts {name, ra_deg, dec_deg, size_arcmin}.
_DEEPSKY_OBJECTS: list = []


def _load_deepsky(path: str = DEEPSKY_CSV) -> list:
    """Parse ASTAP's deep_sky.csv into a list of catalogue objects.

    Returns an empty list if the file is missing or unreadable so that a
    missing database only disables annotation, never breaks solving.
    """
    objects = []
    try:
        lines = Path(path).read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        return objects

    for line in lines:
        parts = line.split(",")
        if len(parts) < 3:
            continue
        try:
            ra_raw = int(parts[0])
            dec_raw = int(parts[1])
        except ValueError:
            # Header lines and any malformed rows are skipped.
            continue
        name = parts[2].strip()
        if not name:
            continue
        # length/width are in 0.1-arcmin units; use the larger as the object
        # size (major axis). Absent for point-like entries.
        size_arcmin = None
        dims = []
        for col in parts[3:5]:
            try:
                dims.append(int(col) / 10.0)
            except ValueError:
                pass
        if dims:
            size_arcmin = max(dims)

        objects.append({
            "name": name,
            "ra_deg": ra_raw / 2400.0,       # 864000 units == 360 deg
            "dec_deg": dec_raw / 3600.0,     # arc-seconds; 324000 == 90 deg
            "size_arcmin": size_arcmin,
        })
    return objects


def _find_objects_in_field(ra_deg, dec_deg, fov_w_deg, fov_h_deg) -> list:
    """Return catalogue objects whose centre falls within the solved field.

    Uses a gnomonic (tangent-plane) projection about the field centre and keeps
    objects landing inside the half-width/half-height rectangle, plus a small
    margin so large objects straddling the edge are still reported.
    """
    if ra_deg is None or dec_deg is None or not _DEEPSKY_OBJECTS:
        return []

    fov_w = fov_w_deg or fov_h_deg
    fov_h = fov_h_deg or fov_w_deg
    if not fov_w or not fov_h:
        return []

    half_w = fov_w / 2.0
    half_h = fov_h / 2.0
    dec0 = math.radians(dec_deg)
    sin_dec0, cos_dec0 = math.sin(dec0), math.cos(dec0)

    found = []
    for obj in _DEEPSKY_OBJECTS:
        d = math.radians(obj["dec_deg"])
        dra = math.radians(obj["ra_deg"] - ra_deg)
        # Normalise RA difference into [-180, 180] to handle the 0/360 wrap.
        while dra > math.pi:
            dra -= 2 * math.pi
        while dra < -math.pi:
            dra += 2 * math.pi

        sin_d, cos_d = math.sin(d), math.cos(d)
        cos_c = sin_dec0 * sin_d + cos_dec0 * cos_d * math.cos(dra)
        if cos_c <= 0:
            continue  # more than 90 deg away — behind the tangent plane
        # Standard coordinates (degrees) on the tangent plane.
        xi = math.degrees(cos_d * math.sin(dra) / cos_c)
        eta = math.degrees(
            (cos_dec0 * sin_d - sin_dec0 * cos_d * math.cos(dra)) / cos_c
        )

        # Allow half the object's own size as edge margin so big nebulae that
        # overlap the frame edge still count as "in field".
        margin = (obj["size_arcmin"] or 0) / 60.0 / 2.0
        if abs(xi) <= half_w + margin and abs(eta) <= half_h + margin:
            entry = {
                "name": obj["name"],
                "ra_deg": round(obj["ra_deg"], 5),
                "dec_deg": round(obj["dec_deg"], 5),
            }
            if obj["size_arcmin"] is not None:
                entry["size_arcmin"] = obj["size_arcmin"]
            # Distance from centre (deg) drives ordering / "primary" selection.
            entry["_sep_deg"] = math.hypot(xi, eta)
            found.append(entry)

    # Closest to centre first; strip the internal sort key before returning.
    found.sort(key=lambda e: e["_sep_deg"])
    for e in found:
        e.pop("_sep_deg", None)
    return found


# Load the catalogue once, at import time.
_DEEPSKY_OBJECTS = _load_deepsky()


@app.get("/", response_class=HTMLResponse)
def index():
    """Minimal web UI for uploading an image and viewing the solution.

    All requests use relative paths so the page works both on the direct
    LAN port and behind Home Assistant Ingress (which adds a path prefix).
    """
    return INDEX_HTML


@app.get("/health")
def health():
    """Report whether ASTAP and a star database are available."""
    has_bin = Path(ASTAP_BIN).exists()
    # Star DB file extensions vary per catalogue (.290 / .1476 / .157).
    db_dir = Path(STAR_DB_DIR)
    db_files = []
    if db_dir.is_dir():
        for pattern in ("*.290", "*.1476", "*.157"):
            db_files += list(db_dir.glob(pattern))
    return {
        "status": "ok" if has_bin and db_files else "degraded",
        "astap": has_bin,
        "star_db_dir": STAR_DB_DIR,
        "star_db_files": len(db_files),
        # Deep sky annotation catalogue (baked into the image).
        "deepsky_db": Path(DEEPSKY_CSV).exists(),
        "deepsky_objects": len(_DEEPSKY_OBJECTS),
    }


def _build_cmd(img_path: Path, ra, dec, fov, radius) -> list:
    """Assemble the astap_cli command line, applying optional hints."""
    cmd = [ASTAP_BIN, "-f", str(img_path), "-d", STAR_DB_DIR]

    effective_fov = fov if fov is not None else (DEFAULT_FOV or None)
    if effective_fov:
        cmd += ["-fov", str(effective_fov)]

    # ra hint is given to ASTAP in hours; spd = dec + 90 in degrees.
    if ra is not None and dec is not None:
        cmd += ["-ra", str(ra / 15.0), "-spd", str(dec + 90.0)]
        cmd += ["-r", str(radius if radius is not None else SEARCH_RADIUS)]
    else:
        # Blind solve: search the whole sky.
        cmd += ["-r", "180"]
    return cmd


def _solution_or_error(img_path: Path, proc_output: str, returncode: int,
                       annotate: bool = False):
    """Read ASTAP's sidecar .ini and return (solution, error). One is None."""
    ini_path = img_path.with_suffix(".ini")
    result = _parse_ini(ini_path) if ini_path.exists() else {}
    if result.get("PLTSOLVD") == "T":
        return _format_solution(result, img_path, annotate), None
    return None, {
        "error": "plate solve failed",
        "astap_error": result.get("ERROR") or proc_output.strip(),
        "exit_code": returncode,
    }


@app.post("/solve")
async def solve(
    file: UploadFile = File(...),
    ra: float | None = Form(None),
    dec: float | None = Form(None),
    fov: float | None = Form(None),
    radius: float | None = Form(None),
    annotate: bool = Form(False),
):
    """Solve one uploaded image (single request; best for the LAN API).

    Optional hints massively speed up solving:
    - ``ra`` / ``dec``: approximate center in degrees (0-360 / -90..90).
    - ``fov``: field-of-view height in degrees.
    - ``radius``: search radius in degrees around the ra/dec hint.
    - ``annotate``: when true, include known deep sky objects that fall within
      the solved field under an ``objects`` array.

    Note: through Home Assistant Ingress the request body is capped at ~1 MB,
    so large FITS files must use the chunked ``/upload`` + ``/solve_stream``
    flow (used by the web UI) or the direct LAN port.
    """
    suffix = Path(file.filename or "image.fits").suffix or ".fits"
    with tempfile.TemporaryDirectory() as tmp:
        img_path = Path(tmp) / f"input{suffix}"
        img_path.write_bytes(await file.read())

        cmd = _build_cmd(img_path, ra, dec, fov, radius)
        try:
            output, returncode = await _run_astap(cmd)
        except asyncio.TimeoutError:
            raise HTTPException(504, f"ASTAP solve timed out after {SOLVE_TIMEOUT}s")

        solution, error = _solution_or_error(
            img_path, output, returncode, annotate
        )
        if error:
            raise HTTPException(422, error)
        return solution


# ---- chunked upload + streaming solve (used by the web UI) ----------------
# Ingress caps a single request body at ~1 MB, so the browser slices the file
# into sub-1 MB chunks and appends them into a staging file keyed by upload_id.

UPLOAD_ROOT = Path(tempfile.gettempdir()) / "astap_uploads"


def _upload_dir(upload_id: str) -> Path:
    # Guard against path traversal: only allow safe id characters.
    if not upload_id or not all(c.isalnum() or c in "-_" for c in upload_id):
        raise HTTPException(400, "invalid upload_id")
    return UPLOAD_ROOT / upload_id


@app.post("/upload")
async def upload_chunk(
    upload_id: str = Form(...),
    index: int = Form(...),
    filename: str = Form("image.fits"),
    chunk: UploadFile = File(...),
):
    """Append one chunk to the staging file. index 0 (re)starts the upload."""
    d = _upload_dir(upload_id)
    d.mkdir(parents=True, exist_ok=True)
    suffix = Path(filename).suffix or ".fits"
    staging = d / f"input{suffix}"

    mode = "wb" if index == 0 else "ab"
    with open(staging, mode) as f:
        f.write(await chunk.read())
    return {"upload_id": upload_id, "index": index, "size": staging.stat().st_size}


@app.post("/solve_stream")
async def solve_stream(
    upload_id: str = Form(...),
    filename: str = Form("image.fits"),
    ra: float | None = Form(None),
    dec: float | None = Form(None),
    fov: float | None = Form(None),
    radius: float | None = Form(None),
    annotate: bool = Form(False),
):
    """Solve a previously uploaded (chunked) file, streaming ASTAP's log.

    Returns newline-delimited JSON: ``{"type":"log","line":...}`` per output
    line, then a final ``{"type":"result",...}`` or ``{"type":"error",...}``.
    """
    d = _upload_dir(upload_id)
    suffix = Path(filename).suffix or ".fits"
    img_path = d / f"input{suffix}"
    if not img_path.exists():
        raise HTTPException(404, "no uploaded file for this upload_id")

    async def stream():
        cmd = _build_cmd(img_path, ra, dec, fov, radius)
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            captured = []
            try:
                while True:
                    raw = await asyncio.wait_for(
                        proc.stdout.readline(), timeout=SOLVE_TIMEOUT
                    )
                    if not raw:
                        break
                    line = raw.decode(errors="ignore").rstrip()
                    if line:
                        captured.append(line)
                        yield json.dumps({"type": "log", "line": line}) + "\n"
                await proc.wait()
            except asyncio.TimeoutError:
                proc.kill()
                yield json.dumps({"type": "error", "message": f"solve timed out after {SOLVE_TIMEOUT}s"}) + "\n"
                return

            solution, error = _solution_or_error(
                img_path, "\n".join(captured), proc.returncode, annotate
            )
            if error:
                yield json.dumps({"type": "error", **error}) + "\n"
            else:
                yield json.dumps({"type": "result", "data": solution}) + "\n"
        finally:
            # Clean up the staging directory regardless of outcome.
            try:
                for p in d.iterdir():
                    p.unlink()
                d.rmdir()
            except OSError:
                pass

    return StreamingResponse(stream(), media_type="application/x-ndjson")


def _parse_ini(path: Path) -> dict:
    data = {}
    for line in path.read_text(errors="ignore").splitlines():
        if "=" in line:
            key, _, val = line.partition("=")
            data[key.strip()] = val.strip()
    return data


def _fits_pixel_size_um(img_path: Path):
    """Read the pixel size (µm) from a FITS primary header, if available.

    FITS headers are 80-char cards in 2880-byte blocks, ending at an END card.
    We only scan the first blocks looking for XPIXSZ (physical pixel size in
    microns, already accounting for binning as written by most capture tools).
    Returns None for non-FITS input or when the keyword is absent.
    """
    try:
        with open(img_path, "rb") as f:
            head = f.read(2880 * 4)  # a few blocks is plenty for the primary header
    except OSError:
        return None
    if not head.startswith(b"SIMPLE"):
        return None
    for i in range(0, len(head), 80):
        card = head[i:i + 80].decode("ascii", errors="ignore")
        kw = card[:8].strip()
        if kw == "END":
            break
        if kw in ("XPIXSZ", "PIXSIZE1") and card[8:9] == "=":
            try:
                return float(card[9:].split("/")[0].strip())
            except ValueError:
                return None
    return None


def _deg_to_dms(deg):
    """Format a positive angle in degrees as 'DDd MMm SS.Ss'."""
    if deg is None:
        return None
    d = int(deg)
    m_full = (deg - d) * 60
    m = int(m_full)
    s = (m_full - m) * 60
    return f"{d:02d}d {m:02d}m {s:05.2f}s"


def _format_solution(ini: dict, img_path: Path = None, annotate: bool = False) -> dict:
    """Turn ASTAP's raw WCS keys into a friendly JSON solution."""

    def num(key):
        try:
            return float(ini[key])
        except (KeyError, ValueError):
            return None

    cd1_1, cd1_2 = num("CD1_1"), num("CD1_2")
    # Pixel scale (arcsec/pixel) from the CD matrix, if present.
    pixel_scale = None
    if cd1_1 is not None and cd1_2 is not None:
        pixel_scale = math.hypot(cd1_1, cd1_2) * 3600.0

    # ASTAP's .ini has no explicit FOV/size keys, so derive the field of view
    # from the plate scale (CDELT, deg/pixel) times the image dimensions. The
    # reference pixel CRPIX sits at the image centre, so width/height ≈ 2*CRPIX.
    def fov(cdelt_key, crpix_key):
        cdelt, crpix = num(cdelt_key), num(crpix_key)
        if cdelt is None or crpix is None:
            return None
        return abs(cdelt) * 2 * crpix

    fov_w = fov("CDELT1", "CRPIX1")
    fov_h = fov("CDELT2", "CRPIX2")

    # Pixel size comes from the FITS header (µm). With the solved pixel scale
    # the focal length follows: f(mm) = 206.265 * pixel_size(µm) / scale(arcsec).
    pixel_size_um = _fits_pixel_size_um(img_path) if img_path else None
    focal_length_mm = None
    if pixel_size_um and pixel_scale:
        focal_length_mm = 206.265 * pixel_size_um / pixel_scale

    ra_center, dec_center = num("CRVAL1"), num("CRVAL2")

    solution = {
        "solved": True,
        "ra_deg": ra_center,              # image center right ascension
        "dec_deg": dec_center,            # image center declination
        "rotation_deg": num("CROTA2"),    # field rotation
        "pixel_scale_arcsec": pixel_scale,
        "pixel_size_um": pixel_size_um,
        "focal_length_mm": focal_length_mm,
        "fov_width_deg": fov_w,
        "fov_height_deg": fov_h,
        "fov_width_dms": _deg_to_dms(fov_w),
        "fov_height_dms": _deg_to_dms(fov_h),
        "raw": ini,                       # full ASTAP output for advanced use
    }
    # Deep sky objects falling inside the solved field. Best-effort: a missing
    # catalogue or absent FOV just yields an empty list, never a solve failure.
    if annotate:
        solution["objects"] = _find_objects_in_field(
            ra_center, dec_center, fov_w, fov_h
        )
    return solution


INDEX_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ASTAP Plate Solver</title>
<style>
  :root { color-scheme: dark; }
  body { font-family: system-ui, sans-serif; margin: 0; background: #0b132b;
         color: #e8eefc; display: flex; justify-content: center; }
  main { width: 100%; max-width: 620px; padding: 24px 20px 60px; }
  h1 { font-size: 1.3rem; display: flex; align-items: center; gap: 10px; }
  .drop { border: 2px dashed #4a6; border-radius: 14px; padding: 34px 16px;
          text-align: center; cursor: pointer; transition: .15s; background: #111a3a; }
  .drop.hover { border-color: #ff9529; background: #16224a; }
  .drop small { color: #9fb0d8; display: block; margin-top: 6px; }
  .hints { display: grid; grid-template-columns: repeat(2, 1fr); gap: 10px; margin: 16px 0; }
  label { font-size: .8rem; color: #9fb0d8; display: block; margin-bottom: 4px; }
  input[type=number] { width: 100%; box-sizing: border-box; padding: 8px;
         border-radius: 8px; border: 1px solid #2b3a66; background: #0e1738; color: #e8eefc; }
  button { width: 100%; padding: 13px; font-size: 1rem; font-weight: 600; border: 0;
           border-radius: 10px; background: #ff9529; color: #1a1000; cursor: pointer; }
  button:disabled { opacity: .5; cursor: default; }
  pre { background: #0e1738; border: 1px solid #2b3a66; border-radius: 10px;
        padding: 14px; overflow-x: auto; white-space: pre-wrap; word-break: break-word; }
  .row { display: flex; justify-content: space-between; padding: 6px 0;
         border-bottom: 1px solid #1d2a55; }
  .row b { color: #ff9529; }
  .err { color: #ff6b6b; }
  .muted { color: #9fb0d8; font-size: .85rem; }
</style>
</head>
<body>
<main>
  <h1>🔭 ASTAP Plate Solver</h1>
  <p class="muted" id="status">checking solver…</p>

  <div class="drop" id="drop">
    <div id="dropLabel">Drop an image here, or click to choose</div>
    <small>FITS / JPG / PNG / TIFF</small>
    <input type="file" id="file" accept=".fits,.fit,.fts,.jpg,.jpeg,.png,.tif,.tiff" hidden>
  </div>

  <div class="hints">
    <div><label>RA hint (deg)</label><input type="number" id="ra" step="any" placeholder="optional"></div>
    <div><label>Dec hint (deg)</label><input type="number" id="dec" step="any" placeholder="optional"></div>
    <div><label>FOV height (deg)</label><input type="number" id="fov" step="any" placeholder="optional"></div>
    <div><label>Search radius (deg)</label><input type="number" id="radius" step="any" placeholder="optional"></div>
  </div>

  <label style="display:flex; align-items:center; gap:8px; margin:0 0 16px; cursor:pointer">
    <input type="checkbox" id="annotate" checked style="width:auto">
    <span>Identify deep sky objects in field</span>
  </label>

  <button id="solve" disabled>Solve</button>
  <p class="muted" id="progress"></p>
  <pre id="log" style="display:none; max-height:220px; overflow-y:auto; font-size:.8rem"></pre>
  <div id="out"></div>
</main>

<script>
  // Relative paths only — works behind Home Assistant Ingress path prefix.
  const $ = (id) => document.getElementById(id);
  const drop = $('drop'), fileInput = $('file'), solveBtn = $('solve');
  let chosen = null;

  fetch('health').then(r => r.json()).then(h => {
    $('status').textContent = h.status === 'ok'
      ? `solver ready · ${h.star_db_files} star-db files · ${h.deepsky_objects || 0} DSO catalogue`
      : `⚠ degraded · astap=${h.astap} · star-db files=${h.star_db_files}`;
    $('status').className = h.status === 'ok' ? 'muted' : 'err';
  }).catch(() => { $('status').textContent = 'cannot reach API'; });

  drop.onclick = () => fileInput.click();
  ['dragover','dragenter'].forEach(e => drop.addEventListener(e, ev => {
    ev.preventDefault(); drop.classList.add('hover');
  }));
  ['dragleave','drop'].forEach(e => drop.addEventListener(e, ev => {
    ev.preventDefault(); drop.classList.remove('hover');
  }));
  drop.addEventListener('drop', ev => { pick(ev.dataTransfer.files[0]); });
  fileInput.onchange = () => pick(fileInput.files[0]);

  function pick(f) {
    if (!f) return;
    chosen = f;
    $('dropLabel').textContent = f.name;
    solveBtn.disabled = false;
  }

  // Ingress caps a request body at ~1 MB, so we upload the file in sub-1 MB
  // chunks, then stream the solve log. Works both behind ingress and on LAN.
  const CHUNK = 512 * 1024;   // 512 KB, safely under the ingress limit

  // The timer ticks every 100ms; `phase` holds the current detail (e.g.
  // "Uploading 20/98 chunks") so the tick appends elapsed time without the
  // two writers overwriting each other.
  let timer = null, t0 = 0, phase = '';
  function paintProgress() {
    $('progress').textContent = phase + ' · ' + ((Date.now() - t0) / 1000).toFixed(1) + 's';
  }
  function startTimer(label) {
    t0 = Date.now();
    phase = label;
    clearInterval(timer);
    paintProgress();
    timer = setInterval(paintProgress, 100);
  }
  function setPhase(label) { phase = label; paintProgress(); }
  function stopTimer(msg) { clearInterval(timer); $('progress').textContent = msg || ''; }

  function newUploadId() {
    return 'u' + Date.now().toString(36) + Math.random().toString(36).slice(2, 8);
  }

  solveBtn.onclick = async () => {
    if (!chosen) return;
    solveBtn.disabled = true; solveBtn.textContent = 'Solving…';
    $('out').innerHTML = '';
    const logEl = $('log');
    logEl.style.display = 'block';
    logEl.textContent = '';

    const uploadId = newUploadId();
    const total = Math.ceil(chosen.size / CHUNK);
    try {
      // --- 1. chunked upload ---
      startTimer('Uploading');
      for (let i = 0; i < total; i++) {
        const blob = chosen.slice(i * CHUNK, (i + 1) * CHUNK);
        const fd = new FormData();
        fd.append('upload_id', uploadId);
        fd.append('index', i);
        fd.append('filename', chosen.name);
        fd.append('chunk', blob, 'chunk');
        const r = await fetch('upload', { method: 'POST', body: fd });
        if (!r.ok) throw new Error('upload failed at chunk ' + i);
        setPhase(`Uploading ${i + 1}/${total} chunks`);
      }

      // --- 2. streaming solve ---
      startTimer('Solving');
      const fd = new FormData();
      fd.append('upload_id', uploadId);
      fd.append('filename', chosen.name);
      for (const k of ['ra','dec','fov','radius']) {
        const v = $(k).value.trim();
        if (v !== '') fd.append(k, v);
      }
      if ($('annotate').checked) fd.append('annotate', 'true');
      const resp = await fetch('solve_stream', { method: 'POST', body: fd });
      if (!resp.ok && !resp.body) {
        const txt = await resp.text();
        throw new Error(txt);
      }

      const reader = resp.body.getReader();
      const dec = new TextDecoder();
      let buf = '';
      let done = false;
      while (!done) {
        const { value, done: d } = await reader.read();
        done = d;
        buf += dec.decode(value || new Uint8Array(), { stream: !done });
        let nl;
        while ((nl = buf.indexOf('\\n')) >= 0) {
          const line = buf.slice(0, nl).trim();
          buf = buf.slice(nl + 1);
          if (!line) continue;
          handleEvent(JSON.parse(line));
        }
      }
    } catch (e) {
      stopTimer('');
      $('out').innerHTML = '<pre class="err">' + e + '</pre>';
    } finally {
      solveBtn.disabled = false; solveBtn.textContent = 'Solve';
    }
  };

  function handleEvent(ev) {
    const logEl = $('log');
    if (ev.type === 'log') {
      logEl.textContent += ev.line + '\\n';
      logEl.scrollTop = logEl.scrollHeight;
    } else if (ev.type === 'result') {
      stopTimer('Solved in ' + ((Date.now() - t0) / 1000).toFixed(1) + 's');
      render(ev.data);
    } else if (ev.type === 'error') {
      stopTimer('');
      const { type, ...rest } = ev;
      $('out').innerHTML = '<pre class="err">Solve failed:\\n' +
        JSON.stringify(rest, null, 2) + '</pre>';
    }
  }

  function render(d) {
    const rows = [
      ['RA (deg)', d.ra_deg], ['Dec (deg)', d.dec_deg],
      ['Rotation (deg)', d.rotation_deg],
      ['Pixel scale (arcsec/px)', d.pixel_scale_arcsec],
      ['Pixel size (µm)', d.pixel_size_um],
      ['Focal length (mm)', d.focal_length_mm],
      ['FOV', (d.fov_width_dms && d.fov_height_dms)
        ? d.fov_width_dms + ' × ' + d.fov_height_dms : null],
    ];
    const fmt = (v) => (v === null || v === undefined) ? '—'
      : (typeof v === 'number' ? v.toFixed(4).replace(/\\.?0+$/, '') : v);
    let html = rows.map(([k, v]) =>
      `<div class="row"><span>${k}</span><b>${fmt(v)}</b></div>`).join('');

    // Deep sky objects in the field (when annotation was requested).
    if (Array.isArray(d.objects)) {
      if (d.objects.length) {
        const names = d.objects.map(o => o.name.split('/')[0]).join(' · ');
        html += '<div class="row"><span>Objects in field</span><b>'
          + d.objects.length + '</b></div>'
          + '<div class="muted" style="padding:8px 0">' + names + '</div>';
      } else {
        html += '<div class="row"><span>Objects in field</span><b>none</b></div>';
      }
    }

    $('out').innerHTML = html
      + '<details style="margin-top:14px"><summary class="muted">raw ASTAP output</summary>'
      + '<pre>' + JSON.stringify(d.raw, null, 2) + '</pre></details>';
  }
</script>
</body>
</html>"""

