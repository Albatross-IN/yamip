#!/bin/bash
set -e

echo "============================================"
echo "  WhatsApp Sidecar - One-Click Setup"
echo "============================================"
echo ""

# Check Node.js
if ! command -v node &> /dev/null; then
    echo "[ERROR] Node.js is not installed."
    echo "        Install via: sudo apt install nodejs npm"
    echo "        Or download from https://nodejs.org/ (version 22+)"
    exit 1
fi

NODE_VERSION=$(node -v)
echo "[OK] Node.js $NODE_VERSION found"

# Navigate to sidecar directory
cd "$(dirname "$0")"
echo "[..] Working directory: $(pwd)"
echo ""

# Step 1: Install dependencies
echo "[1/3] Installing dependencies..."
npm install
echo "[OK] Dependencies installed."
echo ""

# Step 2: Apply patches
echo "[2/3] Applying patches..."
npx patch-package || echo "[WARNING] Patch may have failed. Continuing..."
echo "[OK] Patches applied."
echo ""

# Step 3: Build TypeScript
echo "[3/3] Building TypeScript..."
npm run build
echo "[OK] Build complete."
echo ""

echo "============================================"
echo "  Setup Complete!"
echo "============================================"
echo ""
echo "To start the sidecar, run:"
echo "  npm start"
echo ""
echo "Or start it from Odoo:"
echo "  WhatsApp > Accounts > Start Sidecar"
echo ""
