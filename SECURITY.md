# Security

This dashboard is intended for a trusted local network.

## Important

The dashboard exposes control actions for UniFi Protect relay outputs. Treat access to the dashboard as access to those physical controls.

Recommended:

- keep port 8088 LAN-only
- do not direct-port-forward it to the Internet
- keep `config/Unifi.ini` out of Git
- treat `Remote_API_Key` as a secret exactly like the local Integration API key
- use a dedicated local Protect account with minimal permissions
- use a VPN for remote access
- keep the Docker Node helper on loopback only

The project does not currently provide its own user login/session layer.

If you discover a security issue in code you plan to publish, avoid posting credentials, API keys, LAN configuration, or private camera images in a public issue.


## Remote snapshot camera and remote dashboard access

The optional remote snapshot feature does not make the dashboard safe to expose publicly. It only allows the server to retrieve one selected camera image through Ubiquiti's cloud connector.

For viewing the dashboard away from home, a trusted VPN is recommended. Do not directly port-forward port 8088 to the Internet.

An authenticated HTTPS reverse proxy with WebSocket support can be used by experienced administrators, but the dashboard itself does not provide an Internet-facing login/session layer.
