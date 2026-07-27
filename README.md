# pia

Un mini agent de codage IA en terminal, pensé pour le **Pocket C.H.I.P** et
autres petites machines ARM. Dans l'esprit de Claude Code / OpenCode / Kimi CLI,
mais réduit à l'essentiel pour tenir sur **512 Mo de RAM, un cœur ARMv7 et un
petit écran**. La commande à taper est **`pia`**.

- **Un seul fichier Python** (`pia.py`), **zéro dépendance** (bibliothèque
  standard uniquement). Pas de `pip`, pas de `node`, pas de compilation.
- Compatible avec **n'importe quelle API au format OpenAI** : OpenCode Zen,
  Kimi/Moonshot, OpenAI, ou un serveur local (`llama.cpp`, Ollama…).
- Agent avec outils : lecture/écriture de fichiers, édition ciblée, recherche
  (`grep`/`glob`), et exécution de commandes shell.
- **Aperçu diff + sauvegarde `.bak`** avant toute écriture/édition, avec
  confirmation `y/N/a` (« a » = ne plus redemander pour cet outil).
- **Sessions persistantes** : reprise après coupure (`--continue`), sauvegarde
  manuelle (`/save`), et suivi des tokens consommés (`/usage`).
- Réponses **streamées** pour la réactivité même sur connexion lente.
- **Historique tronqué automatiquement** pour ne jamais saturer les 512 Mo de RAM.
- **Clé API enregistrée une fois pour toutes** et **auto-update** au démarrage.

Requiert **Python 3.6+**. (Debian 11 « bullseye » du Pocket C.H.I.P fournit
Python 3.9 : parfait, rien à installer.)

---

## Installation sur le Pocket C.H.I.P

```sh
git clone https://github.com/mahtan/chip-ai.git
cd chip-ai
sh install.sh
```

Le script :
- copie `pia` dans `~/.local/bin`,
- crée `~/.config/pia/config.json` (en migrant une éventuelle ancienne config),
- enregistre le chemin de ce clone git pour l'**auto-update**,
- supprime l'ancien binaire `chip` s'il traînait.

> Pas envie d'installer ? `python3 pia.py` marche directement depuis le dossier.

---

## Configuration

### 1. Enregistrer la clé API (une seule fois)

`pia` a besoin d'une **clé d'API** (pas d'un simple login à l'appli de chat).

| Fournisseur     | Où obtenir la clé            | Variable d'environnement   |
|-----------------|------------------------------|----------------------------|
| **OpenCode Zen**| https://opencode.ai/auth     | `OPENCODE_ZEN_API_KEY`     |
| **Kimi/Moonshot**| https://platform.moonshot.ai | `MOONSHOT_API_KEY`        |
| **OpenAI**      | https://platform.openai.com  | `OPENAI_API_KEY`           |

Le plus simple, enregistre-la de façon **permanente** dans la config (fichier en
permissions `600`, jamais versionné) :

```sh
pia -p opencode --set-key TA_CLE      # OpenCode Zen
pia -p kimi     --set-key TA_CLE      # Kimi/Moonshot
```

> Alternative : exporter une variable d'environnement dans `~/.profile`
> (`export OPENCODE_ZEN_API_KEY=...`). La variable a priorité sur la config.

### 2. Choisir le fournisseur

Le fournisseur par défaut est dans `~/.config/pia/config.json` (champ
`"provider"`). À la volée :

```sh
pia -p opencode          # OpenCode Zen
pia -p kimi              # Kimi/Moonshot
pia -p kimi -m kimi-k3   # ... avec un modèle précis
```

---

## Mise à jour automatique

`pia` est lié à ton clone git local (chemin enregistré par `install.sh`). Au
lancement du mode interactif, il fait un `git fetch` discret : s'il y a des
nouveaux commits, il **te propose** de mettre à jour (`git pull` +
réinstallation du binaire + redémarrage). Ça marche aussi avec un dépôt privé,
car ça réutilise ton authentification git.

```sh
pia --update       # vérifier et mettre à jour maintenant, puis quitter
pia --no-update    # démarrer sans vérifier les mises à jour
```

Dans le REPL : la commande `/update`. Silencieux si tu es hors-ligne ou déjà à
jour.

---

## Utilisation

### Mode interactif (REPL)

```sh
pia
```

```
pia> lis le fichier pia.py et résume ce qu'il fait
pia> trouve tous les usages de tool_write_file dans le projet
pia> crée un script hello.sh qui affiche la date
pia> /yolo        (bascule l'auto-approbation des actions)
pia> /help
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
| `/update`           | cherche et propose une mise à jour               |
| `/save [nom]`       | sauvegarde la conversation (défaut : « manual »)  |
| `/load [nom]`       | recharge une conversation sauvegardée             |
| `/sessions`         | liste les conversations sauvegardées              |
| `/usage`            | tokens consommés depuis le début de la session    |
| `/tools`            | liste les outils                                 |
| `/exit`, `/quit`    | quitter (Ctrl-D aussi)                           |

Astuce clavier : termine une ligne par `\` pour continuer à écrire sur la
ligne suivante (pratique pour coller un petit extrait de code).

Quand l'agent te demande une confirmation, réponds `y` (une fois), `n`
(refuser) ou **`a`** pour ne plus te demander pour cet outil pendant le reste
de la session — pratique avec le petit clavier du CHIP.

### Mode « une commande »

```sh
pia "corrige la faute de frappe dans README.md"
pia --yolo "lance les tests et dis-moi ce qui casse"
cat monfichier.py | pia "explique ce fichier et corrige les bugs"
```

> Note : quand l'entrée standard n'est pas un vrai terminal (pipe, script,
> cron…), `pia` ne peut pas demander de confirmation — toute action qui en
> nécessite une (écrire, éditer, exécuter) est **refusée par défaut**. Ajoute
> `--yolo` si le prompt doit réellement modifier des fichiers dans ce contexte.

### Reprendre une session interrompue

Utile si la connexion SSH tombe, si la batterie du CHIP se vide, ou si tu
fermes le terminal par erreur : la conversation est sauvegardée automatiquement
après chaque tour.

```sh
pia --continue     # ou : pia -c
```

### Options en ligne de commande

```
-p, --provider NOM     fournisseur défini dans la config
-m, --model NOM        force le modèle
    --base-url URL      force l'URL de base de l'API
    --no-stream         désactive le streaming
    --yolo              approuve automatiquement toutes les actions
-c, --continue          reprend la dernière session interactive
    --set-key CLE       enregistre la clé API du fournisseur, puis quitte
    --update            vérifie/installe une mise à jour, puis quitte
    --no-update         démarre sans vérifier les mises à jour
    --config            affiche le chemin du fichier de config
    --version
```

---

## Les outils de l'agent

| Outil          | Rôle                                        | Confirmation |
|----------------|---------------------------------------------|--------------|
| `read_file`    | lire un fichier                             | non          |
| `list_dir`     | lister un dossier                           | non          |
| `glob_search`  | trouver des fichiers par motif de nom       | non          |
| `grep_search`  | chercher du texte/code dans le projet       | non          |
| `write_file`   | créer / écraser un fichier                  | **oui**      |
| `str_replace`  | remplacer un passage unique dans un fichier | **oui**      |
| `run_bash`     | exécuter une commande shell                 | **oui**      |

Les actions qui modifient l'état :
- affichent un **aperçu diff** (coloré, limité à ~40 lignes) avant de demander confirmation,
- créent une **sauvegarde `chemin.pia.bak`** du fichier précédent avant de l'écraser,
- demandent une confirmation `y/N/a`, sauf si `auto_approve` est actif (config
  ou `--yolo`), ou si tu as déjà répondu `a` pour cet outil dans la session.

---

## Notes pour les petites machines

- La sortie s'adapte à la largeur du terminal (retour à la ligne automatique).
- La sortie des outils renvoyée au modèle est tronquée (~12 ko), et les
  fichiers de plus de 2 Mo sont ignorés par `grep_search`, pour économiser la
  RAM et le contexte.
- **L'historique de conversation est tronqué automatiquement** au-delà de
  `max_history_messages` (40 par défaut, réglable dans la config) : les tours
  les plus anciens sont supprimés par blocs entiers pour ne jamais couper un
  appel d'outil de sa réponse.
- La bannière affiche le **niveau de batterie** du CHIP quand disponible.
- Tout tient dans un seul fichier : tu peux l'éditer directement sur l'appareil
  avec `nano pia.py`.
- Si ta connexion coupe, `pia` affiche une erreur claire et te rend la main —
  aucun état corrompu. La session est sauvegardée automatiquement, reprends
  avec `pia --continue`.

---

## Dépannage

- **« pas de clé API trouvée »** → `pia -p <fournisseur> --set-key TA_CLE`.
- **HTTP 401 / 403** → clé invalide ou expirée, ou crédits épuisés.
- **HTTP 404 sur le modèle** → le nom de modèle n'existe pas chez ce
  fournisseur ; ajuste avec `-m` ou dans la config.
- **`SyntaxError`** → ton Python est trop ancien (< 3.6). Vérifie
  `python3 --version`.
- **L'auto-update ne trouve rien** → vérifie `repo_dir` dans
  `~/.config/pia/config.json` (doit pointer vers ton clone git).
- **« non-interactif : action refusée »** → normal si stdin n'est pas un
  terminal (pipe/script) ; relance avec `--yolo` si l'action est voulue.
