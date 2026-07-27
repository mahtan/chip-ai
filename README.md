# chip

Un mini agent de codage IA en terminal, pensé pour le **Pocket C.H.I.P** et
autres petites machines ARM. Dans l'esprit de Claude Code / OpenCode / Kimi CLI,
mais réduit à l'essentiel pour tenir sur **512 Mo de RAM, un cœur ARMv7 et un
petit écran**.

- **Un seul fichier Python** (`chip.py`), **zéro dépendance** (bibliothèque
  standard uniquement). Pas de `pip`, pas de `node`, pas de compilation.
- Compatible avec **n'importe quelle API au format OpenAI** : OpenCode Zen,
  Kimi/Moonshot, OpenAI, ou un serveur local (`llama.cpp`, Ollama…).
- Agent avec outils : lecture/écriture de fichiers, édition ciblée, `ls`, et
  exécution de commandes shell — avec confirmation avant toute action qui
  modifie quelque chose.
- Réponses **streamées** pour la réactivité même sur connexion lente.

Requiert **Python 3.6+**. (Debian 11 « bullseye » du Pocket C.H.I.P fournit
Python 3.9 : parfait, rien à installer.)

---

## Installation sur le Pocket C.H.I.P

```sh
git clone https://github.com/mahtan/chip-ai.git
cd chip-ai
sh install.sh
```

Le script copie `chip` dans `~/.local/bin`, crée `~/.config/chip-ai/config.json`
et te rappelle d'ajouter `~/.local/bin` à ton `PATH`.

> Pas envie d'installer ? `python3 chip.py` marche directement depuis le dossier.

---

## Configuration

### 1. La clé API

`chip` a besoin d'une **clé d'API** (pas d'un simple login à l'appli de chat).

| Fournisseur     | Où obtenir la clé            | Variable d'environnement   |
|-----------------|------------------------------|----------------------------|
| **OpenCode Zen**| https://opencode.ai/auth     | `OPENCODE_ZEN_API_KEY`     |
| **Kimi/Moonshot**| https://platform.moonshot.ai | `MOONSHOT_API_KEY`        |
| **OpenAI**      | https://platform.openai.com  | `OPENAI_API_KEY`           |

Exporte-la (ajoute la ligne à ton `~/.profile` pour la garder) :

```sh
export OPENCODE_ZEN_API_KEY=xxxxxxxx     # ton abonnement OpenCode
# ou
export MOONSHOT_API_KEY=xxxxxxxx         # ton abonnement Kimi
```

### 2. Choisir le fournisseur

Le fournisseur par défaut est défini dans `~/.config/chip-ai/config.json`
(champ `"provider"`). Tu peux aussi le changer à la volée :

```sh
chip -p opencode          # utilise OpenCode Zen
chip -p kimi              # utilise Kimi/Moonshot
chip -p kimi -m kimi-k3   # ... avec un modèle précis
```

Le fichier de config permet de définir/renommer des fournisseurs, changer les
modèles, activer `auto_approve`, etc. Vois `config.example.json`.

---

## Utilisation

### Mode interactif (REPL)

```sh
chip
```

```
chip> lis le fichier chip.py et résume ce qu'il fait
chip> crée un script hello.sh qui affiche la date
chip> /yolo        (bascule l'auto-approbation des actions)
chip> /help
```

Commandes du REPL :

| Commande            | Effet                                            |
|---------------------|--------------------------------------------------|
| `/help`             | aide                                             |
| `/reset`            | efface la conversation                           |
| `/model [nom]`      | affiche / change le modèle                       |
| `/provider [nom]`   | affiche / change le fournisseur                  |
| `/yolo`             | bascule l'auto-approbation (write/edit/run)      |
| `/cwd [dossier]`    | affiche / change le dossier de travail           |
| `/tools`            | liste les outils                                 |
| `/exit`, `/quit`    | quitter (Ctrl-D aussi)                           |

### Mode « une commande »

```sh
chip "corrige la faute de frappe dans README.md"
chip --yolo "lance les tests et dis-moi ce qui casse"
```

### Options en ligne de commande

```
-p, --provider NOM     fournisseur défini dans la config
-m, --model NOM        force le modèle
    --base-url URL      force l'URL de base de l'API
    --no-stream         désactive le streaming
    --yolo              approuve automatiquement toutes les actions
    --config            affiche le chemin du fichier de config
    --version
```

---

## Les outils de l'agent

| Outil         | Rôle                                        | Confirmation |
|---------------|---------------------------------------------|--------------|
| `read_file`   | lire un fichier                             | non          |
| `list_dir`    | lister un dossier                           | non          |
| `write_file`  | créer / écraser un fichier                  | **oui**      |
| `str_replace` | remplacer un passage unique dans un fichier | **oui**      |
| `run_bash`    | exécuter une commande shell                 | **oui**      |

Les actions qui modifient l'état demandent une confirmation `y/N`, sauf si
`auto_approve` est actif (config ou `--yolo`). Sur un petit clavier, `--yolo`
est pratique une fois que tu as confiance.

---

## Notes pour les petites machines

- La sortie s'adapte à la largeur du terminal (retour à la ligne automatique).
- La sortie des outils renvoyée au modèle est tronquée (~12 ko) pour économiser
  la RAM et le contexte.
- Tout tient dans un seul fichier : tu peux l'éditer directement sur l'appareil
  avec `nano chip.py`.
- Si ta connexion coupe, `chip` affiche une erreur claire et te rend la main —
  aucun état corrompu.

---

## Dépannage

- **« no API key found »** → exporte la variable d'environnement du fournisseur
  choisi (voir tableau plus haut).
- **HTTP 401 / 403** → clé invalide ou expirée, ou crédits épuisés.
- **HTTP 404 sur le modèle** → le nom de modèle n'existe pas chez ce
  fournisseur ; ajuste avec `-m` ou dans la config.
- **`SyntaxError`** → ton Python est trop ancien (< 3.6). Vérifie
  `python3 --version`.
