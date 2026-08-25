# Tale Hermes Connector

Local Hermes platform connector for Tale. Its Hermes
WebSocket remains loopback-only. When the user explicitly starts a phone
transfer, a separate short-lived upload page is opened on the local network.

## Install or update

The installer stops the gateway, installs or updates the Connector in place,
asks for the Browser pairing token, and starts the gateway again. Existing Git
checkouts are updated without deleting the plugin directory.

### Windows (PowerShell)

```powershell
irm https://raw.githubusercontent.com/ThisisPeggy/-Tale-Hermes-Connector/main/install.ps1 | iex
```

### macOS and Linux

```bash
curl -fsSL https://raw.githubusercontent.com/ThisisPeggy/-Tale-Hermes-Connector/main/install.sh | sh
```

Tale generates the pairing token. Paste it into the hidden
prompt when the installer asks. To pair manually, use the actual Hermes data
directory for your platform:

- Windows: `%LOCALAPPDATA%\hermes\plugins\hermes-browser\connect.py`
- macOS/Linux: `${HERMES_HOME:-~/.hermes}/plugins/hermes-browser/connect.py`

The connector listens on `127.0.0.1:8765`. The token is stored in Hermes's
`.env` file with private permissions, is carried in the WebSocket subprotocol (not
the URL), and never needs to appear in shell history or process arguments. No
public port or Hermes API key is needed.

## Send files from a phone

Choose **From phone** in Tale and scan the QR code while the phone and
computer are on the same local network. The Connector opens a separate LAN
listener on port `8766` only while a transfer is active. Each channel has a
random bearer token, accepts the supported attachment formats only, and expires
after five minutes. The token is placed in the QR URL fragment, removed from the
phone address bar after loading, and never appears in HTTP request logs.

Phone files are validated and streamed only to the authenticated Tale
WebSocket that created the transfer. They are staged in the open composer and
are never sent automatically. Set `HERMES_BROWSER_MOBILE_PORT` to change the
temporary port or `HERMES_BROWSER_MOBILE_HOST` to override the advertised LAN
address. A host firewall may ask for permission the first time the feature is
used; only private-network access is needed.

## Attachment boundary

The Connector accepts only the explicit `image.attach_bytes` and `file.attach`
RPCs used by the extension. Attachments are base64 data URLs, are capped at
10 MB each (12 files / 50 MB pending per session), and are written only through
Hermes's image and document cache helpers. Image magic bytes and document
extensions are validated before a staged attachment is passed to the existing
Hermes media pipeline. Pending attachments are isolated to the authenticated
WebSocket that staged them and unused cache files are removed on disconnect.
Unknown RPC methods remain blocked.
