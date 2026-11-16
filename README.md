# livres-kindle

Une demande de livre dans SeerrNG, en anglais ou en français, est téléchargée, rangée dans la bibliothèque puis envoyée par e-mail sur la Kindle de la personne qui l'a demandée. Tout est automatique.

```
SeerrNG (demande) ─► ChaptarrNG (gestion des livres) ─► Prowlarr (indexers) ─► qBittorrent
                                    │ import
                                    ▼
                    kindle-sender ─► Gmail ─► adresse Send to Kindle du demandeur
```

| Service | Adresse | Rôle |
|---|---|---|
| SeerrNG | http://localhost:5055 | demandes (avec un correctif automatique, voir plus bas) |
| ChaptarrNG | http://localhost:8789 | gestion des livres (API Readarr), EPUB uniquement |
| Prowlarr | http://localhost:9696 | indexers |
| qBittorrent | http://localhost:8080 | téléchargements, sans mot de passe depuis cette machine |
| FlareSolverr | interne | indexers protégés par Cloudflare |
| kindle-sender | interne | envoi sur Kindle, demandes en français, approbation automatique |

À utiliser uniquement pour des livres que vous avez le droit de télécharger.

## Prérequis

- Docker avec Docker Compose. Sous Windows, il faut Docker Desktop et au moins 4 Go pour la VM WSL : mettez `memory=4GB` dans `%USERPROFILE%\.wslconfig`.
- Un serveur Jellyfin, Plex ou Emby, dont SeerrNG utilise les comptes.
- Un compte Gmail avec un [mot de passe d'application](https://myaccount.google.com/apppasswords).
- Un compte gratuit sur [hardcover.app](https://hardcover.app), qui fournit les métadonnées des livres.

## Installation

```bash
git clone <ce dépôt> livres-kindle && cd livres-kindle
cp .env.example .env                              # remplir SMTP_USER et SMTP_PASSWORD
cp config/kindle.json.example config/kindle.json  # adresses Kindle (voir plus bas)
docker compose up -d
docker compose run --rm setup
```

`setup` configure tout ce qui passe par les API :

- **ChaptarrNG**
  - profil EPUB de 50 Mo maximum, sans mise à niveau ;
  - formats refusés : résumés, livres audio, BD, scans ;
  - dossier `/ebooks`, client qBittorrent et webhook vers kindle-sender.
- **Prowlarr**
  - lien vers ChaptarrNG ;
  - FlareSolverr ;
  - au moins 3 sources par indexer.
- **SeerrNG** : les deux destinations « Livres en anglais » et « Livres en français ».

Le script ne touche pas à un réglage déjà présent. On peut donc le relancer sans risque, et il faut le relancer après les étapes 1 et 3 ci-dessous.

### Ce qui reste à faire à la main

Ces étapes demandent un compte ou un choix personnel.

1. **SeerrNG** (http://localhost:5055) : suivez l'assistant et connectez votre serveur Jellyfin, Plex ou Emby. Relancez ensuite `docker compose run --rm setup`.
2. **ChaptarrNG** (http://localhost:8789) :
   - créez le compte administrateur ;
   - ouvrez *System → Quickstart → Configure Hardcover API* et collez un jeton Hardcover avec les droits `read:catalog` et `read:me`. Sans ce jeton, la recherche de livres ne marche pas ;
   - si l'ajout d'un livre est lent, coupez dans *Settings → Metadata* les sources en panne et gardez Hardcover.
3. **Prowlarr** (http://localhost:9696) :
   - créez le compte et ajoutez vos indexers ;
   - donnez l'étiquette `flaresolverr` à ceux qui sont protégés par Cloudflare ;
   - pour les demandes en français, ajoutez un tracker français et indiquez son nom Prowlarr (*definitionName*) dans `FR_TRACKERS` du fichier `.env`.

   Relancez ensuite `setup`.
4. **Kindle** :
   - dans `config/kindle.json`, associez chaque utilisateur SeerrNG à son adresse Send to Kindle. La clé est son nom affiché, son nom d'utilisateur, son id SeerrNG ou son e-mail ;
   - chaque lecteur ajoute l'adresse `SMTP_USER` à sa *liste d'adresses e-mail approuvées* sur Amazon (*Contenus et appareils → Préférences → Paramètres des documents personnels*). Sans ça, Amazon jette les e-mails sans prévenir.

Après avoir modifié `.env`, appliquez les changements avec `docker compose up -d kindle-sender`.

## Choisir la langue

- **Anglais** (par défaut) : ChaptarrNG cherche et télécharge le livre lui-même.
- **Français** : il y a deux façons de le demander.
  - Dans la fenêtre de demande, ouvrez *Advanced Options → Destination* et choisissez « Livres en français ».
  - Ou bien, dans le profil de l'utilisateur (*Settings → General → Book Language Override*), réglez « Français ». Toutes ses demandes d'ebooks partent alors en français, y compris depuis l'onglet Livres de Jellyfin, qui n'a pas de choix de destination.

  kindle-sender cherche le titre français sur les trackers de `FR_TRACKERS` et pousse le meilleur EPUB vers ChaptarrNG. Le meilleur EPUB fait au plus 50 Mo, a au moins 3 sources et ne contient ni pack ni version anglaise. Sans résultat, il réessaie toutes les heures.

Les titres français absents des métadonnées se mettent dans `config/titres_fr.json`, sous la forme `{"Titre anglais": "Titre français"}`.

## À savoir

- **Ne mettez pas à jour SeerrNG ni ChaptarrNG.** Leurs images sont épinglées. `seerrng/patches.js` corrige SeerrNG à chaque démarrage pour qu'il fonctionne avec ChaptarrNG v0.9.1000, et vise exactement cette image : avec une autre, SeerrNG refuse de démarrer plutôt que de mal fonctionner.
- **Approbation.** kindle-sender approuve les demandes de livres toutes les minutes. Si vous utilisez le plugin Jellyfin Enhanced, ne donnez à aucun utilisateur SeerrNG une permission supérieure ou égale à 2^31, comme *Auto-approve books* : le plugin plante pour tout le monde.
- **Un fichier par livre.** Les traductions coupées en deux tomes ne sont pas gérées.
- **Tests sans envoi.** Créez le fichier `config/kindle_off` : kindle-sender note alors ce qu'il aurait envoyé sans rien envoyer.
- **Journal.** `docker compose logs -f kindle-sender`
- **Rien n'arrive sur la Kindle ?** Si l'e-mail apparaît dans les messages envoyés de Gmail, vérifiez *Gérer votre contenu → Documents* sur Amazon. Si le livre y est, c'est la synchronisation de la liseuse : redémarrez-la.
- **Vérifier le code :** `docker compose run --rm kindle-sender python /app/sender.py selftest`
