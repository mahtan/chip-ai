#!/bin/sh
# Installe chip sur le Pocket C.H.I.P (ou toute machine avec Python 3.6+).
# Usage :  sh install.sh
set -e

BIN_DIR="${HOME}/.local/bin"
CFG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/chip-ai"
SRC_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "chip installer"
echo "--------------"

# 1. verifier python
if ! command -v python3 >/dev/null 2>&1; then
  echo "ERREUR: python3 introuvable. Installe-le : sudo apt-get install python3"
  exit 1
fi
PYV="$(python3 -c 'import sys;print("%d.%d"%sys.version_info[:2])')"
echo "python3 detecte : $PYV (3.6+ requis)"

# 2. installer le binaire
mkdir -p "$BIN_DIR"
cp "$SRC_DIR/chip.py" "$BIN_DIR/chip"
chmod +x "$BIN_DIR/chip"
echo "installe : $BIN_DIR/chip"

# 3. copier la config exemple si absente
mkdir -p "$CFG_DIR"
if [ ! -f "$CFG_DIR/config.json" ]; then
  cp "$SRC_DIR/config.example.json" "$CFG_DIR/config.json"
  echo "config creee : $CFG_DIR/config.json"
else
  echo "config existante conservee : $CFG_DIR/config.json"
fi

# 4. rappel PATH
case ":$PATH:" in
  *":$BIN_DIR:"*) : ;;
  *)
    echo ""
    echo "Ajoute ceci a ton ~/.profile ou ~/.bashrc :"
    echo "  export PATH=\"\$HOME/.local/bin:\$PATH\""
    ;;
esac

echo ""
echo "Termine. Configure ta cle API, par ex :"
echo "  export OPENCODE_ZEN_API_KEY=xxxx    # ou MOONSHOT_API_KEY pour Kimi"
echo "Puis lance :  chip"
