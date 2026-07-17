# ASTAP Plate Solver — Home Assistant Add-on

Local astrometric **plate solving** for astrophotos, running as a Home Assistant
add-on. Upload a FITS/JPG/PNG image over HTTP and get back the astrometric
solution: image-center **RA/Dec**, field **rotation**, and **pixel scale**.

It wraps the [ASTAP](https://www.hnsky.org/astap.htm) command-line solver
(`astap_cli`). Runs on Raspberry Pi (aarch64) and x86 (amd64).

## Why an add-on

There is no official HA add-on for plate solving — this fills that gap so you can
solve images from HA automations / scripts / Node-RED without a separate host.

## Install

1. **Settings → Add-ons → Add-on Store → ⋮ → Repositories**, add the Git URL of
   this repo.
2. Install **ASTAP Plate Solver**, then **Start** it.
3. On first start it downloads the configured **star database** into
   `/share/astap_star_db` (this can take a while — D50 is a few hundred MB).

## Configuration

```yaml
star_db: d50          # d05 | d20 | d50 | d80 | w08 | h17 | h18
search_radius: 30     # degrees, for hinted solves
default_fov: 0         # field-of-view height in degrees; 0 = let ASTAP guess
solve_timeout: 600     # seconds before a solve is aborted (504)
```

Pick the star DB by your **field of view**:

| Catalogue | Field of view       | Approx. use            |
|-----------|---------------------|------------------------|
| `d80`     | 6° > FOV > 0.15°    | long focal length      |
| `d50`     | 6° > FOV > 0.2°     | **good default**       |
| `d20`     | 6° > FOV > 0.3°     |                        |
| `d05`     | 6° > FOV > 0.6°     | small / fast download  |
| `w08`     | FOV > 20°           | wide field / lens      |

## API

The add-on listens on port **8000**.

### `GET /health`
Returns whether ASTAP and a star database are available, plus the deep sky
annotation catalogue status (`deepsky_db`, `deepsky_objects`).

### `POST /solve`
Multipart form upload.

| Field      | Required | Description                                        |
|------------|----------|----------------------------------------------------|
| `file`     | yes      | The image (FITS/JPG/PNG/TIFF)                      |
| `ra`       | no       | Approx center RA in **degrees** (0–360)            |
| `dec`      | no       | Approx center Dec in **degrees** (−90..90)         |
| `fov`      | no       | Field-of-view height in degrees                    |
| `radius`   | no       | Search radius in degrees around the ra/dec         |
| `annotate` | no       | `true` to include known deep sky objects in field  |

Giving `ra`/`dec`/`fov` hints turns a slow blind solve into a ~1–3 s solve.

**Example**

```bash
# Blind solve
curl -F "file=@M31.fits" http://homeassistant.local:8000/solve

# Hinted solve (much faster)
curl -F "file=@M31.fits" -F "ra=10.68" -F "dec=41.27" -F "fov=1.5" \
     http://homeassistant.local:8000/solve
```

**Response**

```json
{
  "solved": true,
  "ra_deg": 10.6847,
  "dec_deg": 41.269,
  "rotation_deg": 179.2,
  "pixel_scale_arcsec": 1.83,
  "pixel_size_um": 3.76,
  "focal_length_mm": 423.5,
  "fov_width_deg": 1.51,
  "fov_height_deg": 1.02,
  "fov_width_dms": "01d 30m 36.00s",
  "fov_height_dms": "01d 01m 12.00s",
  "raw": { "PLTSOLVD": "T", "CRVAL1": "10.6847", ... }
}
```

`pixel_size_um` is read from the FITS header (`XPIXSZ`); `focal_length_mm`
is derived from it and the solved pixel scale
(`f = 206.265 × pixel_size_µm / pixel_scale_arcsec`). Both are omitted
(`null`) for non-FITS input or when the header lacks the pixel size. `fov_*_dms`
give the field of view in degrees-minutes-seconds.

A failed solve returns HTTP **422** with the ASTAP error message.

### Deep sky annotation

With `annotate=true`, the response gains an `objects` array listing catalogued
deep sky objects whose centre falls within the solved field, ordered by
distance from the field centre (so `objects[0]` is the best "primary target"
candidate):

```json
{
  "solved": true,
  "ra_deg": 85.24, "dec_deg": -2.46, "fov_width_deg": 1.5,
  "objects": [
    { "name": "IC434", "ra_deg": 85.25, "dec_deg": -2.453, "size_arcmin": 60 },
    { "name": "NGC2024/Tank_Track_Nebula/Sh2-277", "ra_deg": 85.425, "dec_deg": -1.857, "size_arcmin": 30 }
  ]
}
```

Notes:

- `name` carries the catalogue's aliases joined by `/` (e.g.
  `M42/NGC1976/Orion_Nebula`); take the part before the first `/` for a short
  label.
- `size_arcmin` is the object's major-axis size; omitted for point-like entries.
- There is **no `type` field** — ASTAP's `deep_sky.csv` does not classify object
  types, so it cannot be reported reliably.
- `objects` is only present when `annotate=true`, and is `[]` when nothing is in
  field. Annotation never fails a solve: a missing catalogue just yields `[]`.
- Matching is done by the add-on itself (gnomonic projection about the field
  centre) from ASTAP's `deep_sky.csv` (~30 000 objects, baked into the image).
  ASTAP's own `-annotate` flag is **not** used — it exists only in the GUI build,
  not `astap_cli`, and only renders a labelled JPEG.

A failed solve returns HTTP **422** with the ASTAP error message.

## Manual star database install

If the automatic download fails (e.g. no internet on the HA host), download the
catalogue `.deb` from the
[ASTAP star database area](https://sourceforge.net/projects/astap-program/files/star_databases/)
on another machine, extract the data files, and copy them into
`/share/astap_star_db/` (accessible via the Samba/`share` add-on).

## Notes

- FITS headers with `FOCALLEN`, `XPIXSZ`, `RA`/`DEC` let ASTAP self-configure —
  hints become optional for those files.
- The solver only reads the uploaded copy in a temp dir; your originals are
  never touched.
