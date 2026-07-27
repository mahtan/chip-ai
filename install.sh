#!/bin/sh
# Installe pia sur le Pocket C.H.I.P (ou toute machine avec Python 3.6+).
# Usage :  sh install.sh
set -e

BIN_DIR="${HOME}/.local/bin"
CFG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/pia"
SRC_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "pia installer"
echo "-------------"

# 1. verifier python
if ! command -v python3 >/dev/null 2>&1; then
  echo "ERREUR: python3 introuvable. Installe-le : sudo apt-get install python3"
  exit 1
fi
PYV="$(python3 -c 'import sys;print("%d.%d"%sys.version_info[:2])')"
echo "python3 detecte : $PYV (3.6+ requis)"

# 2. installer / mettre a jour le binaire (ecrase l'ancienne version)
mkdir -p "$BIN_DIR"
cp "$SRC_DIR/pia.py" "$BIN_DIR/pia"
chmod +x "$BIN_DIR/pia"
echo "installe / mis a jour : $BIN_DIR/pia"

# 2b. nettoyer l'ancien binaire 'chip' s'il existe (renommage vers 'pia')
OLD_CFG="${XDG_CONFIG_HOME:-$HOME/.config}/chip-ai"
if [ -f "$BIN_DIR/chip" ]; then
  rm -f "$BIN_DIR/chip"
  echo "ancien binaire supprime : $BIN_DIR/chip"
fi

# 3. config : migrer l'ancienne, sinon copier l'exemple, sinon conserver
mkdir -p "$CFG_DIR"
if [ -f "$CFG_DIR/config.json" ]; then
  echo "config existante conservee : $CFG_DIR/config.json"
elif [ -f "$OLD_CFG/config.json" ]; then
  cp "$OLD_CFG/config.json" "$CFG_DIR/config.json"
  chmod 600 "$CFG_DIR/config.json" 2>/dev/null || true
  echo "config migree depuis $OLD_CFG : $CFG_DIR/config.json"
else
  cp "$SRC_DIR/config.example.json" "$CFG_DIR/config.json"
  echo "config creee : $CFG_DIR/config.json"
fi

# 3b. enregistrer le chemin du clone git pour l'auto-update au demarrage
python3 - "$CFG_DIR/config.json" "$SRC_DIR" <<'PY'
import json, os, sys
path, src = sys.argv[1], sys.argv[2]
try:
    data = json.load(open(path)) if os.path.exists(path) else {}
except Exception:
    data = {}
upd = data.setdefault("update", {})
upd.setdefault("enabled", True)
# n'enregistre le repo_dir que si c'est bien un clone git
if os.path.isdir(os.path.join(src, ".git")):
    upd["repo_dir"] = src
with open(path, "w") as f:
    json.dump(data, f, indent=2, ensure_ascii=False)
    f.write("\n")
PY
echo "auto-update configure (repo : $SRC_DIR)"

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
echo "Termine. Enregistre ta cle API une fois pour toutes, par ex :"
echo "  pia -p opencode --set-key xxxx      # OpenCode Zen"
echo "  pia -p kimi --set-key xxxx          # Kimi/Moonshot"
echo "Puis lance :  pia"
