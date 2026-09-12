# Changelog

## v3.3.1

- Removed Environment/Relay `Details` rows from the dashboard UI.
- Desktop with 3+ cameras now aligns the two control panels to the two camera rows.
- No change to local live video, G6 Entry package camera, remote snapshot camera,
  relay operation, Network drawer, tablet layout or phone layout.

## v3.3.0

- Optional single remote Protect camera tile through `api.ui.com` cloud connector
- Snapshot-only by design; refreshes about every 10 seconds
- Remote key remains server-side
- `Remote_API_Key` absent -> feature is completely skipped
- Camera selected explicitly by `Remote_Camera_Name` or `Remote_Camera_ID`; no guessing
- Optional `Remote_Console_ID` for disambiguation
- Remote failures never stop local Network, Protect, relays, sensors or live cameras
- Server-side short snapshot cache reduces duplicate cloud requests from multiple dashboard clients

## v3.2.1

Completed dashboard baseline plus G6 Entry dual-camera support.

### Cameras

- Native Protect live video in desktop browsers and iOS WebKit
- Automatic snapshot fallback
- Main-camera tap-to-enlarge
- G6 Entry package-camera detection via `hasPackageCamera`
- Live G6 Entry package-camera inset
- Package inset tap-to-enlarge
- Confirmed G6 Entry package livestream mapping: Protect secondary `lens 2`
- Confirmed tested package stream: 3264×2448, 3 FPS, HEVC/H.265
- G6 Instant AV1 compatibility note: Protect `Advanced` encoding can be unsupported in iPad WebKit; `Standard` resolved the tested case
- No FFmpeg, go2rtc or video transcoding

### Protect / controls

- UP Sense environment/status panel
- Multiple SuperLink Relay support
- Each relay shown by its actual UniFi name
- Relay output names preserved from UniFi
- Touch-friendly immediate relay controls

### Network

- Compact bottom Network bar
- Per-device attached-client drawer
- Client sorting
- Mobile horizontal device selector
- 5G/LTE backup devices use a dedicated single-pane layout

### Responsive UI

- Desktop/laptop dashboard
- iPad landscape layout
- iPad portrait containment polish
- Purpose-built iPhone layout
- iPhone safe-area handling
- WebKit ManagedMediaSource support

### Deployment

- Windows setup/development launchers
- Docker/NAS deployment
- `restart: unless-stopped`
- persistent logs
- external read-only config mount
