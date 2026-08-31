# reelscribe — Documentation technique

Outil personnel, **100 % local et gratuit**, qui enregistre des liens de vidéos
(surtout des reels Instagram), en télécharge la piste audio avec `yt-dlp`, la
transcrit avec `faster-whisper` et écrit un transcript Markdown par clip.

Principes directeurs (par ordre de priorité) :

1. **Local et gratuit** — aucune clé d'API, aucun compte, aucune télémétrie.
   L'audio ne quitte jamais la machine.
2. **Reprenable** — un `run` interrompu reprend là où il s'était arrêté.
3. **Isolation des échecs** — un clip qui échoue ne bloque pas les suivants.
4. **« Boring »** — pas de démon, pas d'UI web, pas de base de données. Le seul
   composant qui écoute le réseau (`serve`) est optionnel et ne tourne que si on
   le lance explicitement.

Contenu attendu : mixte français / anglais, langue auto-détectée.

---

## 1. Vue d'ensemble

```
                  ┌───────────────┐
  reelscribe add  │   state.json  │   file d'attente persistante
  reelscribe serve│  (version 1)  │   clé = URL normalisée
                  └───────┬───────┘
                          │ to_process()  (pending + failed)
                          ▼
              ┌───────────────────────┐
              │   pipeline.process_item │
              ├───────────────────────┤
              │ 1. fetch_metadata      │  yt-dlp --dump-single-json
              │ 2. download_audio      │  yt-dlp -x --audio-format mp3
              │ 3. transcriber.run     │  faster-whisper (VAD)
              │ 4. translate (option)  │  faster-whisper task=translate
              │ 5. build_markdown      │
              │ 6. write_transcript    │  <home>/transcripts/<stem>.md
              └───────────────────────┘
```

`reelscribe run` (CLI) et `reelscribe serve` (HTTP) partagent tous les deux
`pipeline.process_item`. La seule différence : `run` traite la file une fois puis
sort ; `serve` garde le process (et le modèle Whisper) chargé et draine la file à
chaque nouvelle URL reçue.

---

## 2. Arborescence du code

| Fichier | Rôle |
|---|---|
| `src/reelscribe/cli.py` | Parsing argparse, dispatch des sous-commandes, codes de sortie |
| `src/reelscribe/config.py` | Résolution des variables d'environnement, valeurs par défaut, vérif. des dépendances système |
| `src/reelscribe/state.py` | Normalisation d'URL, persistance atomique de `state.json` |
| `src/reelscribe/fetch.py` | Wrappers `yt-dlp` (métadonnées + audio) |
| `src/reelscribe/transcribe.py` | Wrapper `faster-whisper`, fallback CPU |
| `src/reelscribe/render.py` | Génération du Markdown, `slugify`, nom de fichier |
| `src/reelscribe/pipeline.py` | Orchestration d'un item (partagé CLI + serve) |
| `src/reelscribe/serve.py` | Endpoint HTTP d'ingestion (optionnel) |
| `src/reelscribe/__main__.py` | `python -m reelscribe` |
| `scripts/reelscribe-serve.service` | Unité systemd **user** pour `serve` |
| `scripts/checkpoint_fetch.py`, `scripts/smoke_test.sh` | Aides de test manuel |

Layout `src/`, packaging `setuptools` via `pyproject.toml`, point d'entrée
console `reelscribe = reelscribe.cli:main`. Python ≥ 3.10.

Dépendances Python : `yt-dlp`, `faster-whisper`.
Dépendances système : `ffmpeg`, `yt-dlp` (sur le PATH).

---

## 3. Configuration (`config.py`)

Toute la config passe par des variables d'environnement. `Config()` les lit une
fois au démarrage ; une valeur invalide lève `ConfigError` → **code de sortie 2**.

| Variable | Défaut | Description |
|---|---|---|
| `REELSCRIBE_HOME` | `~/reelscribe` | Racine des données (`state.json`, `audio/`, `transcripts/`) |
| `REELSCRIBE_MODEL` | `medium` | Modèle Whisper : `tiny`, `base`, `small`, `medium`, `large-v3` |
| `REELSCRIBE_DEVICE` | `auto` | `auto`, `cpu`, `cuda` |
| `REELSCRIBE_LANG` | *(vide)* | Force une langue ; vide = auto-détection |
| `REELSCRIBE_TRANSLATE` | *(vide)* | `1` pour ajouter une passe de traduction vers l'anglais |
| `REELSCRIBE_COOKIES` | *(vide)* | Navigateur pour `yt-dlp --cookies-from-browser` (`chrome`, `firefox`, …) |
| `REELSCRIBE_DELAY` | `3.0` | Pause en secondes entre deux items d'un même `run` |
| `REELSCRIBE_TOKEN` | *(vide)* | **Requis** pour `serve` — jeton d'authentification |
| `REELSCRIBE_SERVE_ADDR` | `0.0.0.0:8765` | Adresse d'écoute de `serve` (`HOST:PORT` ou `PORT`) |

Chemins dérivés : `state_file`, `audio_dir`, `transcript_dir`, `log_file`.

`check_dependencies()` vérifie `ffmpeg` et `yt-dlp` sur le PATH et lève
`ConfigError` avec une commande d'installation adaptée à l'OS.

---

## 4. File d'attente et état (`state.py`)

### Normalisation d'URL

`normalize_url()` produit la **clé canonique** d'un lien :

- force le schéma `https://` ;
- met l'hôte en minuscules, retire un `www.` de tête ;
- supprime la query string et le fragment ;
- supprime le slash final ;
- **conserve la casse du path** (les IDs de reels sont sensibles à la casse).

Exemple : `instagram.com/reel/ABC/?igsh=xyz` et
`https://www.instagram.com/reel/ABC` → `https://instagram.com/reel/ABC`.

### Schéma `state.json` (version 1)

```json
{
  "version": 1,
  "items": {
    "https://instagram.com/reel/ABC": {
      "status": "pending | done | failed",
      "added": "2026-08-31T14:13:24",
      "attempts": 0,
      "completed": "2026-08-31T14:13:59",
      "transcript": "/…/transcripts/ABC-video-by-x.md",
      "audio": "/…/audio/ABC-video-by-x.mp3",
      "title": "Video by x",
      "language": "en",
      "error": "…(présent uniquement si failed)"
    }
  }
}
```

### Opérations

| Méthode | Effet |
|---|---|
| `add(url)` | Ajoute `{status: pending, attempts: 0}` ; renvoie la clé, ou `None` si déjà présente |
| `to_process()` | Liste des clés `pending` **ou** `failed` |
| `mark_done(key, **fields)` | `status=done`, `completed=now`, efface `error` |
| `mark_failed(key, error)` | `status=failed`, stocke `error`, `attempts += 1` |
| `retry_failed()` | Repasse tous les `failed` en `pending` ; renvoie le compte |

### Écriture atomique

`save()` écrit dans `state.json.tmp` puis `os.replace()` → jamais de fichier
d'état tronqué, même si le process est tué en plein écrit. L'état est persisté
**après chaque item**, ce qui rend `run` reprenable.

---

## 5. Récupération (`fetch.py`)

Une seule exception, `FetchError`, dont le message porte les **600 derniers
caractères** de la stderr de `yt-dlp`.

- `fetch_metadata(url, cookies=None)` → `yt-dlp --no-warnings --dump-single-json`,
  renvoie le blob JSON parsé.
- `download_audio(url, out_stem, audio_dir, cookies=None)` :
  - si `<audio_dir>/<out_stem>.mp3` **existe déjà**, renvoie ce chemin sans
    invoquer `yt-dlp` (reprenabilité) ;
  - sinon : `yt-dlp -x --audio-format mp3 --audio-quality 5 -o …`.

La **classification fine des erreurs** (auth / géo-bloc / supprimé /
rate-limit) n'est délibérément *pas* implémentée : d'après BUILD.md §9, les
motifs de stderr doivent être écrits contre des erreurs réellement observées, pas
devinées. Comportement actuel : on remonte la stderr brute.

### Faits `yt-dlp` observés (intégrés au code)

- Les reels ont `duration: null` → le rendu affiche « unknown ».
- `webpage_url` conserve le tracker `?igsi=` → le transcript utilise l'URL
  normalisée comme `Source`.
- Un post Instagram **supprimé** renvoie exactement la même erreur (« empty media
  response… use --cookies-from-browser ») qu'un post **privé** : `yt-dlp` ne les
  distingue pas.

---

## 6. Transcription (`transcribe.py`)

`Transcriber(model_size, device, language, compute_type="int8", model_factory=None)`

- Le modèle est **coûteux à charger** : il est instancié une seule fois (lazy,
  via la property `model`) et réutilisé pour tous les clips et toutes les passes
  du process.
- `run(audio_path, task="transcribe")` appelle
  `model.transcribe(…, vad_filter=True, language=…, task=…)` et renvoie un
  `TranscriptResult(text, rows, language)`. `rows` = liste de
  `(start, end, texte)`.
- `translate_to_english(audio_path)` = `run(task="translate").text or None`.
- `model_factory` est le point d'injection pour les tests (aucune dépendance
  réelle nécessaire).

### Fallback CPU

Sur cette machine, le device CUDA est détecté mais `libcublas.so.12` est absent —
et l'erreur ne surgit qu'**au moment de l'inférence**, pas au chargement. Donc
`run()` attrape toute exception, et si `device ∈ {auto, cuda}` et qu'on n'a pas
déjà rétrogradé, il repasse en `device="cpu"`, jette le modèle et réessaie **une
fois**. Toute erreur suivante se propage.

`_load_real_model()` : `compute_type` forcé à `int8` en CPU.

---

## 7. Rendu (`render.py`)

- `slugify(text, limit=60)` — unicode, réduit aux `[\w\s-]`, minuscules, tirets ;
  fallback `"untitled"`.
- `make_stem(meta)` → `f"{meta['id'] or 'clip'}-{slugify(meta['title'])}"` — sert
  de nom de base pour le `.mp3` **et** le `.md`.
- `build_markdown(*, source, meta, result, english=None, transcribed_at=None, note=None)`

Ordre des sections (BUILD.md §7) :

1. `# <titre>` (fallback : l'URL source)
2. Puces méta : `Source` (URL normalisée), `Uploader`, `Duration`, `Language`,
   `Transcribed`, `Note` (optionnelle)
3. `## Caption` — seulement si `meta.description` est non vide
4. `## Transcript` — texte, ou `_(no speech detected)_`
5. `## English translation` — seulement si `REELSCRIBE_TRANSLATE=1` et langue ≠ en
6. `## Timestamped` — une ligne `[MM:SS] texte` par segment

`write_transcript(transcript_dir, stem, markdown)` → `<transcript_dir>/<stem>.md`.

---

## 8. Pipeline (`pipeline.py`)

```python
def process_item(cfg, transcriber, url) -> dict:
    meta   = fetch_metadata(url, cookies=cfg.cookies)
    stem   = make_stem(meta)
    audio  = download_audio(url, stem, cfg.audio_dir, cookies=cfg.cookies)
    result = transcriber.run(audio)
    note   = "no speech detected in audio" if result.is_empty else None
    english = None
    if cfg.translate and result.language != "en":
        english = transcriber.translate_to_english(audio)
    markdown = build_markdown(source=url, meta=meta, result=result,
                              english=english, transcribed_at=now_iso(), note=note)
    path = write_transcript(cfg.transcript_dir, stem, markdown)
    return {"transcript": str(path), "audio": str(audio),
            "title": meta.get("title"), "language": result.language}
```

Le dict renvoyé est passé tel quel à `state.mark_done(url, **fields)`.

---

## 9. CLI (`cli.py`)

| Commande | Effet |
|---|---|
| `reelscribe add <url> [<url>…]` | Enregistre des liens (`pending`) |
| `reelscribe run [--only URL]` | Traite la file (`pending` + `failed`) ; `--only` force un seul lien |
| `reelscribe list [--failed]` | Affiche l'état ; `--failed` = échecs seulement, erreur non tronquée |
| `reelscribe retry` | Repasse les `failed` en `pending` |
| `reelscribe clean` | Supprime les `.mp3` des items `done` (transcripts conservés) |
| `reelscribe serve` | Lance l'endpoint HTTP (voir §10) |

### Codes de sortie (BUILD.md §5)

| Code | Sens |
|---|---|
| `0` | Tout a réussi |
| `1` | Au moins un item a échoué (ou rate-limit rencontré) |
| `2` | Mauvaise invocation, config invalide, ou dépendance système manquante |

### Comportements particuliers de `run`

- **Rate-limit** : si la stderr contient `429` ou `too many requests`, le `run`
  s'arrête immédiatement (les items restants ne sont pas touchés), code 1.
- **Indice d'auth** : si la stderr matche `login` / `log in` / `cookies` /
  `private`, un indice `REELSCRIBE_COOKIES=chrome` est affiché.
- Pause `REELSCRIBE_DELAY` entre deux items (pas après le dernier).
- `state.save()` après **chaque** item.

---

## 10. Endpoint HTTP (`serve.py`) — optionnel

But : partager un reel depuis le téléphone (Share sheet) et le voir transcrit
automatiquement, via Tailscale. C'est la **seule** partie « démon » de reelscribe
et un écart assumé au « no daemon » de BUILD.md, confiné à ce fichier.

Bibliothèque : `http.server` de la stdlib (`ThreadingHTTPServer`), **aucune
dépendance supplémentaire**.

### Routes

| Route | Méthode | Réponse |
|---|---|---|
| `/health` | GET | `200 {"status": "ok"}` — pas de token requis |
| `/add` | GET ou POST | voir ci-dessous |
| *(autre)* | — | `404 {"error": "not found"}` |

`/add` :

| Cas | Code | Corps |
|---|---|---|
| Pas de token / mauvais token | `401` | `{"error": "unauthorized"}` |
| `url` absente | `400` | `{"error": "missing url"}` |
| Méthode ≠ GET/POST | `405` | `{"error": "method not allowed"}` |
| Nouvelle URL | `202` | `{"queued": "<url normalisée>"}` |
| URL déjà connue | `200` | `{"skipped": "<url normalisée>"}` |

### Authentification

- Comparaison du token en temps constant (`hmac.compare_digest`).
- `serve` refuse de démarrer si `REELSCRIBE_TOKEN` n'est pas défini → code 2.
- Le token est accepté depuis : la query string, le corps de formulaire (toute
  méthode), ou un header `Authorization: Bearer <token>`.
- `log_message` retire la query string des logs → le token ne fuite **pas** dans
  le journal systemd.

### Worker (`Ingest`)

- File d'attente protégée par un `Lock`, un thread worker unique.
- Synchronisation via `threading.Condition` + compteurs `_wakes` / `_processed`.
  `enqueue()` incrémente `_wakes` et `notify_all()` ; le worker traite puis pose
  `_processed = target`.
- `wait_idle(timeout)` bloque jusqu'à `_processed >= _wakes` (utilisé par les
  tests).
- Le worker **n'attrape jamais** de mort : toute exception dans `_run_once` est
  loggée, le thread continue.
- `_default_drain` : pour chaque URL de `to_process()`, `process_item` →
  `mark_done` / `mark_failed` + `save()`. Le `Transcriber` reste chargé pour
  toute la vie du process.

### Handler HTTP

- Lit un corps de requête sur **n'importe quelle** méthode (certains clients
  mettent les paramètres GET dans le corps).
- Corps JSON (`Content-Type: …json`) ou `application/x-www-form-urlencoded`.

### Limite connue

Re-partager un reel **en échec** renvoie `{"skipped"}` : la déduplication ignore
le `status`. Pas de retry via l'endpoint — il faut lancer `reelscribe retry` sur
la machine.

---

## 11. Déploiement (état actuel sur cette machine)

| Élément | Valeur |
|---|---|
| Racine des données | `~/reelscribe/` (`state.json`, `audio/`, `transcripts/`, `serve.env`) |
| `REELSCRIBE_HOME` | exporté dans `~/.bashrc` |
| `serve.env` (chmod 600) | `REELSCRIBE_TOKEN`, `REELSCRIBE_MODEL=medium`, `REELSCRIBE_HOME` |
| Service | systemd **user** `reelscribe-serve`, activé + running, linger activé |
| Tailscale | la box et le téléphone sur le même tailnet ; MagicDNS actif |
| Langue | auto-détection |

### Unité systemd (`scripts/reelscribe-serve.service`)

```ini
[Unit]
Description=reelscribe HTTP ingestion endpoint
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
EnvironmentFile=%h/reelscribe/serve.env
ExecStart=%h/.local/bin/reelscribe serve
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
```

Installation :

```sh
cp scripts/reelscribe-serve.service ~/.config/systemd/user/
loginctl enable-linger "$USER"
systemctl --user daemon-reload
systemctl --user enable --now reelscribe-serve
systemctl --user status reelscribe-serve
journalctl --user -u reelscribe-serve -f
```

### Raccourci téléphone (Android, app « HTTP Shortcuts »)

- Méthode : GET
- URL :
  `http://homelab:8765/add?token=<TOKEN>&url={shared_url}`
- `shared_url` = variable globale **Static** avec « Allow Receiving Value from
  Share Dialog » = Text.
- **`token` et `url` doivent être dans l'URL** (ou dans la section « Request Body
  / Parameters »), **pas** en headers custom.
- Latence observée : ~35 s du tap au transcript avec `medium` en CPU.

---

## 12. Tests

`pytest`, `testpaths = ["tests"]`. **Aucun accès réseau** dans la suite.

| Fichier | Couvre |
|---|---|
| `test_config.py` | Résolution env, valeurs par défaut, parsing `SERVE_ADDR`, validations |
| `test_state.py` | Normalisation d'URL, `add`/`to_process`/`mark_*`, écriture atomique |
| `test_fetch.py` | Wrappers `yt-dlp` (subprocess mocké), header Bearer, corps de requête |
| `test_transcribe.py` | `FakeModel` via `model_factory`, modèle instancié une seule fois |
| `test_render.py` | Sections Markdown, `slugify`, format `MM:SS` |
| `test_cli.py` | Sous-commandes, codes de sortie (fixture `home`, pipeline mocké) |
| `test_serve.py` | Routage `handle()`, worker `Ingest`, smoke test socket réel, Bearer, corps GET |

Points d'injection pour les tests :

- `reelscribe.pipeline.fetch_metadata` / `download_audio` (monkeypatch — **pas**
  dans `cli`)
- `reelscribe.cli.Transcriber`
- `Transcriber(model_factory=…)`
- `Ingest(cfg, run_once=…)`

Lancer :

```sh
python -m pytest -q
```

---

## 13. Hors périmètre (contraintes BUILD.md §16)

Outil personnel pour lire du contenu auquel l'utilisateur a déjà accès. Le
contenu téléchargé appartient à ses créateurs — usage privé, pas de
redistribution. Comptes privés et contenu payant : hors périmètre. Pas de
gestion de cookies au-delà de la session navigateur déjà connectée de
l'utilisateur pour du contenu qu'il peut déjà voir.

Non implémenté (délibérément) : classification fine auth/géo/supprimé (manque
d'échantillons d'erreurs réels).
