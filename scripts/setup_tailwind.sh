#!/usr/bin/env bash
# Downloads the pinned Tailwind standalone CLI for this OS/arch into bin/tailwind (no Node needed).
set -euo pipefail

TAILWIND_VERSION="${TAILWIND_VERSION:-v4.1.11}"
cd "$(dirname "$0")/.."

os="$(uname -s | tr '[:upper:]' '[:lower:]')"
arch="$(uname -m)"
ext=""
case "$os" in
  linux) os="linux" ;;
  darwin) os="macos" ;;
  mingw*|msys*|cygwin*) os="windows"; ext=".exe" ;;
  *) echo "Unsupported OS: $os" >&2; exit 1 ;;
esac
case "$arch" in
  x86_64|amd64) arch="x64" ;;
  aarch64|arm64) arch="arm64" ;;
  *) echo "Unsupported architecture: $arch" >&2; exit 1 ;;
esac

mkdir -p bin
url="https://github.com/tailwindlabs/tailwindcss/releases/download/${TAILWIND_VERSION}/tailwindcss-${os}-${arch}${ext}"
echo "Downloading ${url}"
curl -fsSL "$url" -o "bin/tailwind${ext}"
chmod +x "bin/tailwind${ext}"
echo "Installed bin/tailwind${ext} (${TAILWIND_VERSION})"
