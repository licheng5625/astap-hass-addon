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
reverse proxy. It uses only the Python standard library.

The add-on's `ingress_stream: true` lets full-size FITS files (tens of MB)
through ingress, and the script obtains an ingress session over the HA
WebSocket API (which accepts a normal Long-Lived Access Token):

1. Authenticate to `/api/websocket` with the token.
2. Ask HA Core to proxy a Supervisor request for an ingress session
   (`supervisor/api` → `/ingress/session`) — works with a non-admin token
   because HA Core makes the Supervisor call with its own privileges.
3. Resolve the add-on's dynamic `ingress_url` (this is **not** the slug).
4. `POST` the image to `<ingress_url>/solve` with the `ingress_session` cookie.

```bash
export HASS_URL="https://xxxxx.ui.nabu.casa"
export HASS_TOKEN="ey..."         # long-lived access token
python3 remote_solve.py M31.fits --ra 10.68 --dec 41.27 --fov 1.5
```

> Treat the token like a password — it grants full access to your Home
> Assistant. Keep it out of shell history and version control.

