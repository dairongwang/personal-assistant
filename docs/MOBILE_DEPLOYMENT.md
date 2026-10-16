# Deploying the Personal Assistant to a smartphone

Context: written for the **single-user, self-hosted** setup (backend on a machine you own).
§1–2 (LAN access, Tailscale) remain the right answer for that setup. For the commercial,
multi-device product, the cross-platform client design (PWA + Capacitor + optional Electron) is in
[`PLATFORM_DESIGN.md`](PLATFORM_DESIGN.md) §2; the PWA/Capacitor findings in §3–5 here still apply.

## TL;DR — recommended path

| Step | What you get | Effort | Code changes |
|---|---|---|---|
| **1. Serve the frontend from the backend + open it on the phone over Wi-Fi** | Works today on any phone browser on the same network | 10 min | **Done in this change** (`main.py` mounts `frontend/`, `index.html` uses a relative API URL) |
| **2. Tailscale (private VPN) for access away from home + free HTTPS** | Works on cellular, no port-forwarding, no domain, no exposed ports | 30 min, no code | none |
| **3. Turn the page into a PWA** (manifest + service worker) | Home-screen icon, full-screen, offline shell, push notifications | ~1 week (Phase 3 in TECH_DESIGN.md) | frontend only |
| 4. *(only if needed)* Capacitor wrapper → App Store / Play Store | Native widgets, share-sheet, background tasks | 2–3 weeks + Apple $99/yr | wrapper project |

A native rewrite (React Native / Flutter) is **not** recommended — see §5.

---

## 1. Same-network access (works now)

The frontend previously had to be opened as a local file with `API = 'http://localhost:8000'`
hard-coded — on a phone, *localhost* is the phone itself, so nothing would load. The backend now
serves `frontend/` at `/`, and the page calls the API with relative URLs.

### Windows PC (your setup)

```powershell
cd personal-assistant\backend
venv\Scripts\activate
uvicorn main:app --host 0.0.0.0 --port 8000      # 0.0.0.0 = listen on all interfaces
ipconfig                                        # note the "IPv4 Address", e.g. 192.168.1.23
```

Allow the port through Windows Defender Firewall once (run as Administrator):

```powershell
netsh advfirewall firewall add rule name="Personal Assistant 8000" dir=in action=allow protocol=TCP localport=8000 profile=private
```

On the phone (same Wi-Fi): open `http://192.168.1.23:8000/`. Use *Add to Home Screen* in
Chrome/Safari for an app-like icon.

### macOS / Linux

Same `uvicorn` command; find the address with `ifconfig` / `ip addr`. No firewall rule is usually
required on a home network.

### Docker

`docker compose up --build` — the compose file now mounts `./frontend` into the container so
`http://<pc-ip>:8000/` works the same way.

**Limitations:** the PC must be on and the phone on the same network. The page has no external
dependencies, so the phone does **not** need internet access. "Install app" and offline caching
only activate over HTTPS or `localhost` (browser rule) — see `DEPLOYMENT.md` for Caddy/Tailscale.

---

## 2. Access from anywhere: Tailscale (recommended, no code)

[Tailscale](https://tailscale.com) creates a private WireGuard network between your devices.
The free "Personal" plan covers this use case.

1. Install Tailscale on the PC and on the phone, sign in with the same account.
2. Each device gets a stable private address (`100.x.y.z`) and a name such as `my-pc`.
3. Run the backend as in §1. On the phone (Wi-Fi *or* cellular): `http://my-pc:8000/`.
4. Optional, and required for PWA install/push in §3 — free HTTPS with a valid certificate:

   ```bash
   tailscale serve --bg 8000          # exposes https://my-pc.<tailnet>.ts.net → localhost:8000
   ```

Why this over alternatives:

| Option | Pros | Cons |
|---|---|---|
| **Tailscale** | Zero exposed ports, free TLS, works behind CGNAT, 30-min setup | Needs the Tailscale app on the phone |
| Cloudflare Tunnel | Public URL, free TLS | App becomes reachable from the internet → **authentication (F12) becomes mandatory first** |
| Router port-forward + Caddy/Let's Encrypt | No third party | Needs a domain, static IP/DDNS, and authentication; exposes the PC |

Because the app has **no authentication**, do not use any option that makes it publicly reachable
until F12 (auth) from the tech design is implemented.

### Keeping the PC "always on" is optional

If the PC is often off, run the same Docker image on a Raspberry Pi / NAS / mini-PC on the home
network and add it to the tailnet. Nothing in the app changes.

---

## 3. Progressive Web App (PWA) — Phase 3 of the tech design

A PWA is the same HTML page plus two files; it is what makes the phone experience feel native:

| Capability | Android (Chrome) | iPhone (Safari, iOS ≥ 16.4) |
|---|---|---|
| Install to home screen, full-screen, splash | Yes (install prompt) | Yes (Share → Add to Home Screen) |
| Offline shell (service worker cache) | Yes | Yes |
| Web Push notifications (reminders, F3) | Yes | Yes, **only when installed to home screen** |
| Background sync | Yes | No (queue writes locally, replay on next open) |
| Widgets, share-sheet target | Limited | No |

Requirements: served over **HTTPS** (Tailscale `serve` provides this) with a
`manifest.json`, an icon set, and a `sw.js` service worker. The queued-writes design
(IndexedDB → replay) is in TECH_DESIGN.md §4.8/F11.

---

## 4. Store-distributed app: Capacitor wrapper (optional, later)

If native features are required (home-screen widgets, Siri/Assistant shortcuts, share-sheet
capture), wrap the *same* PWA with [Capacitor](https://capacitorjs.com): the web code runs in a
WebView with native plugins for push, local notifications, and share targets. The backend and
API are unchanged.

Costs: Node.js + Android Studio (Android) and a Mac + Xcode + Apple Developer Program ($99/yr)
(iOS). Sideloading on your own iPhone is possible with a free Apple ID but requires re-signing
every 7 days — fine for testing, not for daily use. Android APKs can be sideloaded freely.

---

## 5. Options investigated and *not* recommended

**Run the backend on the phone itself (Termux / iSH / Pyto).**
Checked: `pydantic-core` (FastAPI's validation engine) publishes **no Android or iOS wheels**
(verified against PyPI for `android_21_arm64_v8a` and `ios_13_0_arm64_iphoneos`, Python 3.14).
Installing it on Termux means compiling Rust on the phone — the same class of failure as the
Windows 3.14 install error, only slower. Also no background execution guarantees on iOS. Verdict:
**not viable** for this stack.

**Rewrite the frontend in React Native / Expo / Flutter** (the README's original "next step").
A full rewrite of the UI, a second codebase to maintain, and the backend still has to be hosted
somewhere — the rewrite solves none of the hosting/reachability questions above. The PWA +
optional Capacitor route gets >90 % of the benefit for <20 % of the effort, and keeps one codebase.

**Public cloud hosting (VPS, Fly.io, Render).**
Viable and simple, but the moment the app is on the public internet it needs authentication,
HTTPS and backups. Reasonable later; Tailscale gives remote access *today* without any of that.

---

## 6. Checklist

- [x] Frontend served from the backend; API URL relative
- [x] `python dev.py serve` binds to all interfaces and prints the phone URL
- [x] No CDN dependencies — the app works on a LAN without internet (vanilla CSS/JS)
- [x] PWA: `manifest.webmanifest`, icons, `sw.js` (offline read access, update prompt) — needs HTTPS or localhost to activate
- [x] "Today"/"overdue" computed in the phone's timezone (`/today?tz=`)
- [ ] Firewall rule on the PC; test `http://<pc-ip>:8000/` from phone
- [ ] Install Tailscale on PC + phone; `tailscale serve --bg 8000` for HTTPS; test over cellular
- [ ] Push reminders (Phase 3 of TECH_DESIGN.md)
- [ ] Before any public exposure: authentication, backups
