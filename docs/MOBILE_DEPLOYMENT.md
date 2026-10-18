# Phones and tablets

The PWA **is** the mobile app: same code on Android, iPhone/iPad, and desktop browsers.

| Capability | Android (Chrome/Edge) | iPhone/iPad (Safari ≥ 16.4) | Desktop (Chrome/Edge/Safari) |
|---|---|---|---|
| Install to home screen / windowed app | Install prompt | Share → *Add to Home Screen* | Install button in the address bar |
| Offline | Full (service worker + IndexedDB) | Full | Full |
| Automatic code updates on reopen | Yes | Yes | Yes |
| Storage eviction | Installed apps are exempt | Installed apps are exempt; browser tabs can be evicted after 7 days unused — install the app or enable sync + vault | Persistent |
| Touch help | press-and-hold any control, or the `?` help mode | same | hover / keyboard focus |

Requirements: the app must be served over **HTTPS** (Azure Static Web Apps does this) or from
`localhost`. On a LAN, `python dev.py serve` prints the phone URL; plain `http://192.168…` works
as a web page but without install/offline — put HTTPS in front (Caddy `tls internal`, Tailscale
`serve`) for the full experience.

Store distribution (optional, later): wrap the same build with Capacitor for native
notifications, share-sheet capture and in-app purchases — see `PLATFORM_DESIGN.md` §2.
