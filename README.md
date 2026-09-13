# Myuu Multiusage Bot

Bot Telegram (Pyrofork) pour le leech/hardsub d'anime, pensé pour tourner sur
Google Colab. Récupère un magnet via Seedr, en extrait le meilleur flux de
sous-titres français, applique un style ASS maison, et brûle le résultat via
CloudConvert ou FreeConvert.

## Démarrage sur Colab

Le point d'entrée est **`main.py`** à la racine du repo (pas de fichier
`colab_launcher.py` séparé). Ouvre-le dans Google Colab, renseigne les champs
`@param` en haut du fichier, puis lance la cellule :

```text
API_ID          identifiants Telegram (my.telegram.org)
API_HASH
BOT_TOKEN       token du bot (@BotFather)
USER_ID         ton ID Telegram (devient OWNER côté bot)
DUMP_ID         chat/canal de dump pour l'auto-forward (optionnel)
CC_API_KEY      clé(s) CloudConvert — plusieurs clés séparées par des virgules
FC_API_KEY      clé(s) FreeConvert — idem, séparées par des virgules
SEEDR_USERNAME  compte Seedr (nécessaire pour les workflows magnet/hardsub)
SEEDR_PASSWORD
SEEDR_PROXY     proxy optionnel pour Seedr
MAX_RESTARTS    nombre max de redémarrages auto en cas de crash (défaut 50)
```

`main.py` clone/rafraîchit le repo, écrit ces valeurs, puis lance
`colab_leecher/__main__.py` avec redémarrage automatique en cas de crash.

## Styles de hardsub

Le bot propose 3 styles au moment de brûler les sous-titres, gérés par
`colab_leecher/house_style.py` :

| Style | Police | Mécanisme |
|---|---|---|
| **A** | Trebuchet MS | Profil visuel unique appliqué à tous les noms de style trouvés dans la source ; seul l'alignement change selon le nom (ex. signs routés en haut). |
| **B (CR)** | Trebuchet MS | Profils par nom de style Crunchyroll/Erai-Raws précis (`Default`, `Italique`, `Sign`, `TiretsDefault`, etc.). |
| **C (Asakura)** | Gandhi Sans | Même mécanisme que le Style A (profil unique, alignement variable), mais avec la police Gandhi Sans embarquée directement dans le `.ass` produit (section `[Fonts]`) puisqu'elle n'est pas installée côté CloudConvert/FreeConvert. |

Dans les trois cas, les valeurs (taille de police, outline, shadow, marges)
sont redimensionnées proportionnellement à la `PlayResY` du script source —
aucun style ne réécrit `PlayResX`/`PlayResY`.

Les 4 graisses de Gandhi Sans (Regular/Bold/Italic/BoldItalic, police
gratuite et redistribuable, éditée par Librerías Gandhi) vivent dans
`colab_leecher/fonts/`.

## Agent de veille (Claude)

- `/Relève` — réveille un agent qui surveille nyaa.si/Erai-Raws en continu
  (toutes les 20s) et liste les épisodes détectés dans les 15 dernières
  minutes, avec un choix d'encodage (360p/480p/720p/qualité d'origine, Style
  B) via boutons.
- `/Arise` — rendort l'agent.
- `/search_claude` — recherche ponctuelle sans laisser l'agent actif.

## Commandes

```text
/start            Menu principal
/help             Aide
/settings         Réglages (upload, conversion, sous-titres, split, etc.)
/setname, /rename, /removerename   Nom personnalisé pour le prochain envoi
/zipaswd, /unzipaswd               Mot de passe pour zip/dézip
/add, /dumps      Gestion des chats/canaux de dump
/addcc, /addfc, /apikeys           Gestion des clés CloudConvert/FreeConvert
/status, /stats, /ping             État du bot / machine
/cancel, /stop    Annuler la tâche en cours / arrêter le bot
/logs             Logs (owner)
/Relève, /Arise, /search_claude    Agent de veille nyaa.si/Erai-Raws
/Naut_Anime, /Mal_Anime            Recherche anime (Nautiljan / MyAnimeList)
/nyaa_search, /nyaa_add, /nyaa_list, /nyaa_remove, /nyaa_check
                  Tracker Nyaa (suivi de sorties)
/allow, /deny, /allowed, /ban, /unban, /banned, /broadcast
                  Gestion des accès (owner only)
```

L'envoi d'une vidéo ou d'un lien/magnet fait apparaître les menus d'action
(hardsub, resize, compress, extraction de flux, miniatures, etc.) — ce sont
des boutons inline, pas des commandes.

## Comment le hardsub fonctionne réellement

1. Seedr récupère le magnet et expose chaque vidéo via une URL CDN directe.
2. `ffprobe` interroge cette URL pour lister ses flux, et
   `pick_french_text_subtitle()` note chaque flux sous-titre texte (tag de
   langue `fr`/`fra`/`fre`, ou indices "vostfr"/"french"/"français" dans le
   titre) pour garder le meilleur candidat FR.
3. Le flux retenu est extrait via `ffmpeg -map`, puis passé dans
   `apply_hardsub_style()` (Style A/B/C au choix).
4. Le `.ass` stylé est envoyé à CloudConvert (`subtitle_add: upload`,
   `subtitle_mode: hard`) ou FreeConvert selon le moteur choisi.
5. Le job terminé est renvoyé sur Telegram et le dossier Seedr est nettoyé.

Aucun lookup Erai-Raws par CRC, aucune extraction "en parallèle" ni rotation
automatique de clé sur quota dépassé — si une clé CloudConvert/FreeConvert
manque ou est invalide, le job échoue avec un message d'erreur explicite.

## Développement

```bash
pip install -r requirements.txt
python -m compileall colab_leecher main.py
```

Il n'y a pas de dossier `tests/` dans ce repo pour l'instant — la vérification
se limite à la compilation syntaxique.

## Notes

- Colab est la cible principale ; le repo ne contient que `main.py` +
  `colab_leecher/`.
- `LICENSE` et `requirements.txt` sont à la racine avec `main.py`.
