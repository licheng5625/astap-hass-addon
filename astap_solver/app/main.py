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

app = FastAPI(title="ASTAP Plate Solver", version="0.1.0")

STAR_DB_DIR = os.environ.get("STAR_DB_DIR", "/share/astap_star_db")
SEARCH_RADIUS = os.environ.get("SEARCH_RADIUS", "30")
DEFAULT_FOV = float(os.environ.get("DEFAULT_FOV", "0") or 0)
ASTAP_BIN = "/usr/bin/astap_cli"


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
