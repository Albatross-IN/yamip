# Sidecar Setup Guide

The sidecar is a small Node.js service that connects Odoo to WhatsApp. Follow these steps to set it up.

## Prerequisites

- **Node.js 22+** — Download from [nodejs.org](https://nodejs.org/)
- Verify installation: `node -v` (should show v22 or higher)

## Quick Setup (Recommended)

### Windows
Double-click `setup.bat` or run in Command Prompt:
```
setup.bat
```

### Linux / macOS
```bash
chmod +x setup.sh
./setup.sh
```

The script will:
1. Install all dependencies (`npm install`)
2. Apply required patches to the WhatsApp library
3. Build the TypeScript source code

## Manual Setup

If the script doesn't work, follow these steps:

### Step 1: Install dependencies
```bash
cd sidecar
npm install
```

### Step 2: Apply patches
```bash
npx patch-package
```

### Step 3: Build
```bash
npm run build
```

## Starting the Sidecar

### Option 1: From Odoo (Recommended)
1. Go to **Settings > Open WhatsApp Connector**
2. **Sidecar Directory** is auto-filled with this folder on install — leave it unless you moved the sidecar
3. Enable **Auto-start Sidecar** (optional)
4. Go to **WhatsApp > Configuration > Accounts** and click **Start Sidecar**

### Option 2: From command line
```bash
npm start
```

### Option 3: With custom port
```bash
PORT=3200 npm start
```

### Option 4: With API key security
```bash
API_KEY=your-secret-key npm start
```

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `PORT` | `3100` | HTTP server port |
| `API_KEY` | (none) | API key for authentication |
| `SESSIONS_DIR` | `./sessions` | Where WhatsApp sessions are stored |
| `LOG_LEVEL` | `info` | Logging level (debug, info, warn, error) |

> **Connection push webhook:** the sidecar emits each session-state transition
> (connecting → qr_pending → connected, plus all disconnect variants) to
> `<callback_url>/open_whatsapp_connector/webhook/connection` using the per-account
> `webhook_secret` configured in Odoo. The `callback_url` and `webhook_secret` are
> per-account, read from Odoo when the session starts — no extra env vars needed.
> If you reverse-proxy the sidecar behind a different hostname, set the
> account's **Callback URL** in Odoo to your public Odoo URL, not localhost.

## Troubleshooting

### `./setup.sh: cannot execute: required file not found` (or `$'\r': command not found`)
The script has Windows (CRLF) line endings — this happens if you copied it from a Windows machine
or a manually built ZIP. Convert it to Unix line endings once, then re-run:
```bash
sed -i 's/\r$//' setup.sh
./setup.sh
```
(Downloads via the Odoo Apps Store / `git clone` are not affected — see `.gitattributes`.)

### "patch-package: No patch files found"
Make sure the `patches/` folder exists with the `.patch` file inside it.

### Sidecar starts but Odoo can't connect
- Check the sidecar URL in Odoo matches the port (default: `http://localhost:3100`)
- If using API key, make sure it matches in both Odoo settings and sidecar
- Check firewall is not blocking the port

### QR code doesn't appear
Click **Refresh Status** in Odoo, or disconnect and reconnect the account.
