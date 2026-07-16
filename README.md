# Home Assistant Add-ons by licheng5625

Add this repository to Home Assistant via **Settings → Add-ons → Add-on Store →
⋮ → Repositories** using the URL:

```
https://github.com/licheng5625/astap-hass-addon
```

## Add-ons

### [ASTAP Plate Solver](./astap_solver)

Local astrometric **plate solving** for astrophotos using the
[ASTAP](https://www.hnsky.org/astap.htm) command-line solver. Upload a
FITS/JPG/PNG image over HTTP and get back image-center **RA/Dec**, field
**rotation**, and **pixel scale**. Runs on Raspberry Pi (aarch64) and x86
(amd64).

See the [add-on README](./astap_solver/README.md) for install, configuration,
and API details.

## Remote solving via Nabu Casa

[`remote_solve.py`](./remote_solve.py) solves an image from anywhere through
your Home Assistant Cloud (Nabu Casa) URL — no VPN, no exposed ports, no
reverse proxy. It uses only the Python standard library (no `pip install`).

### Quick start

```bash
export HASS_URL="https://xxxxx.ui.nabu.casa"   # your Nabu Casa remote URL
export HASS_TOKEN="ey..."                       # long-lived access token
python3 remote_solve.py M31.fits --ra 10.68 --dec 41.27 --fov 1.5
```

Output is the JSON solution (center RA/Dec, rotation, pixel scale) on stdout.

### Getting the values you need

- **`HASS_URL`** — your Home Assistant Cloud remote URL. Find it under
  **Settings → Home Assistant Cloud → Remote Control** (looks like
  `https://<random>.ui.nabu.casa`).
- **`HASS_TOKEN`** — a Long-Lived Access Token. Click your **user profile**
  (bottom-left avatar) → scroll to **Long-Lived Access Tokens** → **Create
  Token**. Copy it once; it is not shown again.
- **add-on slug** — `remote_solve.py` defaults to the slug on the author's
  install (`d5d3ad43_astap_solver`). The `d5d3ad43_` prefix is unique per
  Home Assistant instance, so on your install pass `--slug` with your own.
  The script itself resolves the dynamic `ingress_url` from the slug, so you
  only need the slug, not the full URL. To find your slug, open the add-on in
  the UI — it is the last path segment of the add-on page URL
  (`.../hassio/addon/<slug>/info`), e.g. `abcd1234_astap_solver`.

  ```bash
  python3 remote_solve.py M31.fits --slug abcd1234_astap_solver
  ```

### Options

| Flag        | Meaning                                   |
|-------------|-------------------------------------------|
| `--ra`      | RA hint in degrees (0–360)                |
| `--dec`     | Dec hint in degrees (−90..90)             |
| `--fov`     | Field-of-view height in degrees           |
| `--radius`  | Search radius in degrees around the hint  |
| `--slug`    | Add-on slug (see above)                   |
| `--url`     | HA base URL (overrides `$HASS_URL`)       |
| `--token`   | Token (overrides `$HASS_TOKEN`)           |

Hints are optional — without them the solver does a full-sky blind solve
(slower). With `--ra/--dec/--fov` a solve typically finishes in ~1–5 s.

### How it works

The add-on's `ingress_stream: true` lets full-size FITS files (tens of MB)
through ingress, and the script obtains an ingress session over the HA
WebSocket API (which accepts a normal Long-Lived Access Token):

1. Authenticate to `/api/websocket` with the token.
2. Ask HA Core to proxy a Supervisor request for an ingress session
   (`supervisor/api` → `/ingress/session`) — works with a non-admin token
   because HA Core makes the Supervisor call with its own privileges.
3. Resolve the add-on's dynamic `ingress_url` (this is **not** the slug).
4. `POST` the image to `<ingress_url>/solve` with the `ingress_session` cookie.

> Treat the token like a password — it grants full access to your Home
> Assistant. Keep it out of shell history and version control. If it leaks,
> revoke it under your profile's Long-Lived Access Tokens and create a new one.

## Local solving (same network)

On the same LAN you don't need any of the above — hit the add-on's port
directly (no ingress, no size limit):

```bash
# Web UI in a browser
http://<home-assistant-ip>:8000

# API from a script
curl -F "file=@M31.fits" -F "ra=10.68" -F "dec=41.27" -F "fov=1.5" \
     http://<home-assistant-ip>:8000/solve
```


