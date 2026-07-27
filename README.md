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
- Réponses **streamées** pour la réactivité même sur connexion lente, avec
  **reprise automatique** quand le wifi du CHIP décroche.
- **Contexte projet** (`PIA.md`), **commandes personnalisées**, mentions
  `@fichier` et raccourci shell `!` — pour taper le moins possible.
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
- installe deux commandes personnalisées d'exemple dans `~/.config/pia/commands/`,
- enregistre le chemin de ce clone git pour l'**auto-update**,
- supprime l'ancien binaire `chip` s'il traînait.

Relancer `sh install.sh` plus tard **met à jour le programme** sans jamais
écraser ta config ni tes commandes personnalisées.

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

## Contexte projet (`PIA.md`)

Comme le `CLAUDE.md` de Claude Code ou le `AGENTS.md` d'OpenCode, `pia` cherche
au démarrage un fichier de contexte dans le dossier courant, puis en remontant
jusqu'à la racine du dépôt git (il ne sort jamais du projet) :

1. `PIA.md`
2. `AGENTS.md`
3. `CLAUDE.md`

Son contenu (limité à 8 ko) est ajouté aux instructions du modèle à chaque
requête. Sers-t'en pour les règles du projet : comment lancer les tests, les
conventions de code, ce qu'il ne faut pas toucher…

```markdown
# Mon projet

- Lancer les tests : `python3 -m pytest`
- Toujours répondre en français.
- Ne jamais modifier `legacy/`.
```

Tu n'en as pas encore ? La commande `/init` demande au modèle d'explorer le
projet et d'en écrire un pour toi.

---

## Commandes personnalisées

Crée un fichier `.md` dans `~/.config/pia/commands/` : son nom devient une
commande. `$ARGUMENTS` est remplacé par ce que tu tapes derrière.

`~/.config/pia/commands/revue.md` :

```markdown
Relis $ARGUMENTS et liste les bugs, du plus grave au moins grave.
Une ligne par problème.
```

S'utilise ensuite ainsi :

```
pia> /revue @pia.py
```

`install.sh` en installe deux en exemple (`/revue`, `/explique`) sans jamais
écraser les tiennes. `/commands` liste celles dont tu disposes.

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
| `/models`           | liste les modèles proposés par le fournisseur     |
| `/compact`          | remplace l'historique par un résumé (libère la RAM) |
| `/undo`             | annule la dernière modification de fichier        |
| `/diff`             | affiche `git diff`                                |
| `/commit`           | rédige un message pour le diff **indexé** (`git add`) puis committe |
| `/init`             | génère un `PIA.md` décrivant le projet            |
| `/commands`         | liste tes commandes personnalisées                |
| `/exit`, `/quit`    | quitter (Ctrl-D aussi)                           |

### Raccourcis pour taper moins (petit clavier)

| Raccourci        | Effet                                                    |
|------------------|----------------------------------------------------------|
| `!commande`      | lance une commande shell directement — **aucun token consommé** ; la sortie est ajoutée au contexte |
| `@chemin/fichier`| joint le contenu du fichier à ton message                 |
| `\` en fin de ligne | continue la saisie sur la ligne suivante              |

Exemples :

```
pia> !ls -la
pia> corrige le bug dans @pia.py
pia> /revue @tools.py
```

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
                        (nécessaire aussi pour --update non interactif)
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

Tu peux revenir en arrière avec **`/undo`** : il restaure le dernier fichier
modifié depuis sa sauvegarde, ou supprime le fichier si `pia` venait de le créer.

---

## Notes pour les petites machines

- La sortie s'adapte à la largeur du terminal (retour à la ligne automatique).
- La sortie des outils renvoyée au modèle est tronquée (~12 ko), et les
  fichiers de plus de 2 Mo sont ignorés par `grep_search`, pour économiser la
  RAM et le contexte.
- **L'historique de conversation est tronqué automatiquement** au-delà de
  `max_history_messages` (40 par défaut, réglable dans la config) : les tours
  les plus anciens sont supprimés par blocs entiers pour ne jamais couper un
  appel d'outil de sa réponse. `/compact` fait le ménage plus agressivement en
  remplaçant tout l'historique par un résumé.
- **Reprise réseau automatique** : une coupure wifi, un timeout ou une réponse
  429/5xx sont réessayés jusqu'à 4 fois (1s, 2s, 4s). Une clé invalide (401/403)
  échoue immédiatement, sans réessai inutile.
- `!commande` évite un aller-retour avec le modèle : **zéro token, zéro
  attente réseau** — idéal quand la batterie ou le forfait sont comptés.
- La bannière affiche le **niveau de batterie** du CHIP quand disponible.
- Tout tient dans un seul fichier : tu peux l'éditer directement sur l'appareil
  avec `nano pia.py`.
- Si ta connexion coupe pour de bon, `pia` affiche une erreur claire et te rend
  la main — aucun état corrompu. La session est sauvegardée automatiquement,
  reprends avec `pia --continue`.

---

## Aide-mémoire

| Je veux…                                   | Je tape                          |
|--------------------------------------------|----------------------------------|
| démarrer                                    | `pia`                            |
| reprendre après une coupure                 | `pia -c`                         |
| poser une question sur un fichier           | `pia> explique @fichier.py`      |
| lancer une commande sans consommer de token | `pia> !make test`                |
| annuler une modification                    | `pia> /undo`                     |
| committer proprement                        | `pia> /commit`                   |
| libérer de la RAM en pleine session         | `pia> /compact`                  |
| voir les modèles disponibles                | `pia> /models`                   |
| documenter le projet pour l'agent           | `pia> /init`                     |

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
- **Ça réessaie en boucle** → le serveur répond 429/5xx ou le wifi lâche ;
  `pia` retente 4 fois puis abandonne avec un message. Vérifie la connexion
  du CHIP (`ping 1.1.1.1`) ou tes crédits chez le fournisseur.
- **`/models` renvoie une erreur** → tous les fournisseurs n'exposent pas
  `/v1/models` ; règle le modèle à la main avec `-m` ou dans la config.
- **Mon `PIA.md` est ignoré** → il doit être dans le dossier courant ou un
  dossier parent **à l'intérieur** du dépôt git. Vérifie avec `/cwd`, et
  relance `/reset` après l'avoir créé.

---

## Ce que `pia` ne fait pas

Par choix, pour rester léger sur cette machine : pas d'interface graphique,
pas de coloration syntaxique, pas de MCP, pas de sous-agents, pas d'images.
Si tu as besoin de tout ça, utilise Claude Code ou OpenCode sur une machine
plus puissante — `pia` vise le cas « je suis sur mon Pocket C.H.I.P et je veux
coder ».
