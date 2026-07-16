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
import subprocess
import tempfile
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import HTMLResponse, StreamingResponse

app = FastAPI(title="ASTAP Plate Solver", version="0.2.0")

STAR_DB_DIR = os.environ.get("STAR_DB_DIR", "/share/astap_star_db")
SEARCH_RADIUS = os.environ.get("SEARCH_RADIUS", "30")
DEFAULT_FOV = float(os.environ.get("DEFAULT_FOV", "0") or 0)
ASTAP_BIN = "/usr/bin/astap_cli"


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


def _solution_or_error(img_path: Path, proc_output: str, returncode: int):
    """Read ASTAP's sidecar .ini and return (solution, error). One is None."""
    ini_path = img_path.with_suffix(".ini")
    result = _parse_ini(ini_path) if ini_path.exists() else {}
    if result.get("PLTSOLVD") == "T":
        return _format_solution(result), None
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
):
    """Solve one uploaded image (single request; best for the LAN API).

    Optional hints massively speed up solving:
    - ``ra`` / ``dec``: approximate center in degrees (0-360 / -90..90).
    - ``fov``: field-of-view height in degrees.
    - ``radius``: search radius in degrees around the ra/dec hint.

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
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
        except subprocess.TimeoutExpired:
            raise HTTPException(504, "ASTAP solve timed out after 180s")

        solution, error = _solution_or_error(
            img_path, proc.stdout or proc.stderr, proc.returncode
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
                    raw = await asyncio.wait_for(proc.stdout.readline(), timeout=180)
                    if not raw:
                        break
                    line = raw.decode(errors="ignore").rstrip()
                    if line:
                        captured.append(line)
                        yield json.dumps({"type": "log", "line": line}) + "\n"
                await proc.wait()
            except asyncio.TimeoutError:
                proc.kill()
                yield json.dumps({"type": "error", "message": "solve timed out after 180s"}) + "\n"
                return

            solution, error = _solution_or_error(
                img_path, "\n".join(captured), proc.returncode
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


def _format_solution(ini: dict) -> dict:
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

    return {
        "solved": True,
        "ra_deg": num("CRVAL1"),          # image center right ascension
        "dec_deg": num("CRVAL2"),         # image center declination
        "rotation_deg": num("CROTA2"),    # field rotation
        "pixel_scale_arcsec": pixel_scale,
        "fov_width_deg": num("FOV_W") or num("CDELT1"),
        "fov_height_deg": num("FOV_H") or num("CDELT2"),
        "stars_detected": num("STARS"),
        "raw": ini,                       # full ASTAP output for advanced use
    }


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
      ? `solver ready · ${h.star_db_files} star-db files`
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

  let timer = null, t0 = 0;
  function startTimer(prefix) {
    t0 = Date.now();
    clearInterval(timer);
    timer = setInterval(() => {
      $('progress').textContent = prefix + ' · ' +
        ((Date.now() - t0) / 1000).toFixed(1) + 's';
    }, 100);
  }
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
        $('progress').textContent = `Uploading · ${i + 1}/${total} chunks`;
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
      ['FOV width (deg)', d.fov_width_deg], ['FOV height (deg)', d.fov_height_deg],
      ['Stars detected', d.stars_detected],
    ];
    const fmt = (v) => (v === null || v === undefined) ? '—'
      : (typeof v === 'number' ? v.toFixed(4).replace(/\\.?0+$/, '') : v);
    $('out').innerHTML = rows.map(([k, v]) =>
      `<div class="row"><span>${k}</span><b>${fmt(v)}</b></div>`).join('')
      + '<details style="margin-top:14px"><summary class="muted">raw ASTAP output</summary>'
      + '<pre>' + JSON.stringify(d.raw, null, 2) + '</pre></details>';
  }
</script>
</body>
</html>"""

