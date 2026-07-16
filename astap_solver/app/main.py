"""ASTAP plate-solving HTTP API.

Upload an image (FITS/JPG/PNG/TIFF); the service runs the ASTAP command-line
solver and returns the astrometric solution (center RA/Dec, rotation, pixel
scale, field of view) as JSON.

ASTAP writes its result into a sidecar ``.ini`` file next to the input, using
simple ``KEY=VALUE`` lines. ``PLTSOLVD=T`` marks a successful solve.
"""
import math
import os
import subprocess
import tempfile
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import HTMLResponse

app = FastAPI(title="ASTAP Plate Solver", version="0.1.3")

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
    db_files = list(Path(STAR_DB_DIR).glob("*.290")) if Path(STAR_DB_DIR).is_dir() else []
    return {
        "status": "ok" if has_bin and db_files else "degraded",
        "astap": has_bin,
        "star_db_dir": STAR_DB_DIR,
        "star_db_files": len(db_files),
    }


@app.post("/solve")
async def solve(
    file: UploadFile = File(...),
    ra: float | None = Form(None),
    dec: float | None = Form(None),
    fov: float | None = Form(None),
    radius: float | None = Form(None),
):
    """Solve one uploaded image.

    Optional hints massively speed up solving:
    - ``ra`` / ``dec``: approximate center in degrees (0-360 / -90..90).
    - ``fov``: field-of-view height in degrees.
    - ``radius``: search radius in degrees around the ra/dec hint.
    """
    suffix = Path(file.filename or "image.fits").suffix or ".fits"
    with tempfile.TemporaryDirectory() as tmp:
        img_path = Path(tmp) / f"input{suffix}"
        img_path.write_bytes(await file.read())

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

        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        except subprocess.TimeoutExpired:
            raise HTTPException(504, "ASTAP solve timed out after 120s")

        ini_path = img_path.with_suffix(".ini")
        result = _parse_ini(ini_path) if ini_path.exists() else {}

        if result.get("PLTSOLVD") != "T":
            raise HTTPException(
                422,
                {
                    "error": "plate solve failed",
                    "astap_error": result.get("ERROR") or proc.stdout.strip() or proc.stderr.strip(),
                    "exit_code": proc.returncode,
                },
            )

        return _format_solution(result)


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

  solveBtn.onclick = async () => {
    if (!chosen) return;
    solveBtn.disabled = true; solveBtn.textContent = 'Solving…';
    $('out').innerHTML = '';
    const fd = new FormData();
    fd.append('file', chosen);
    for (const k of ['ra','dec','fov','radius']) {
      const v = $(k).value.trim();
      if (v !== '') fd.append(k, v);
    }
    try {
      const r = await fetch('solve', { method: 'POST', body: fd });
      const data = await r.json();
      if (!r.ok) {
        const d = data.detail || data;
        $('out').innerHTML = '<pre class="err">Solve failed:\\n' +
          JSON.stringify(d, null, 2) + '</pre>';
      } else {
        render(data);
      }
    } catch (e) {
      $('out').innerHTML = '<pre class="err">' + e + '</pre>';
    } finally {
      solveBtn.disabled = false; solveBtn.textContent = 'Solve';
    }
  };

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

