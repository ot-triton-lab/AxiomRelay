#!/usr/bin/env bash
set -euo pipefail

SITE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
THEME_DIR="$SITE_DIR/themes/MATbook"
REPO="https://github.com/srliu3264/MATbook.git"

if [[ -d "$THEME_DIR" ]]; then
  echo "Updating MATbook theme ..."
  cd "$THEME_DIR"
  git pull --ff-only
else
  echo "Installing MATbook theme ..."
  mkdir -p "$SITE_DIR/themes"
  git clone --depth 1 "$REPO" "$THEME_DIR"
fi

echo "MATbook theme ready at $THEME_DIR"
