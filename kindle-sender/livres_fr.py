"""Demandes de livres en français : SeerrNG -> TR4KER/Draupnirr -> ChaptarrNG.

Une demande SeerrNG envoyée vers la destination « Livres en français » (sans recherche automatique)
ajoute le livre dans ChaptarrNG. Ce module cherche alors le titre français de l'édition FR sur les
trackers français via Prowlarr, choisit le meilleur EPUB et le pousse à ChaptarrNG sous le titre
anglais du livre : ChaptarrNG le rattache au bon livre, télécharge, importe, et kindle-sender envoie.
"""
import json, os, re, time, unicodedata, urllib.parse, urllib.request
from datetime import datetime, timezone

CHAPTARR = os.environ.get("CHAPTARR_URL", "http://chaptarrng:8789")
PROWLARR = os.environ.get("PROWLARR_URL", "http://prowlarr:9696")
FR_DESTINATION = "livres en francais"   # nom de la destination SeerrNG (comparé sans accents)
FR_TRACKERS = set(os.environ.get("FR_TRACKERS", "tr4ker,draupnirr").lower().split(","))  # definitionName Prowlarr
FR_LANGS = {"fre", "fra", "fr", "french", "francais"}
PERIOD = 60             # vérification toutes les minutes (approbation des demandes de livres)
RETRY = 3600            # un livre sans résultat est recherché de nouveau au plus 1 fois par heure
MAX_BYTES = 50 * 1024 * 1024
MIN_SEEDERS = 3
BAD = re.compile(r"(?i)\b(mobi|azw3?|pdf|kfx|cbr|cbz|bd|comics?|mp3|m4b|aac|flac|audio ?books?|\d+ ?kbps|"
                 r"pack|int[eé]grale|collection|coffret|english|eng|vo)\b")


def key(name):
    """Clé API lue dans le config.xml de ChaptarrNG / Prowlarr (volume monté en lecture seule)."""
    with open(f"/data/{name}/config.xml", encoding="utf-8") as f:
        return re.search(r"<ApiKey>(\w+)</ApiKey>", f.read()).group(1)


def get(base, path, api_key, timeout=120):
    req = urllib.request.Request(base + path, headers={"X-Api-Key": api_key})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def post(base, path, api_key, body):
    req = urllib.request.Request(base + path, data=json.dumps(body).encode(), method="POST",
                                 headers={"X-Api-Key": api_key, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.load(r)


def delete(base, path, api_key):
    req = urllib.request.Request(base + path, method="DELETE", headers={"X-Api-Key": api_key})
    with urllib.request.urlopen(req, timeout=60):
        pass


STATE = "/config/livres_fr_state.json"   # livre ChaptarrNG -> release poussée


def load_state(path=STATE):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return {}


def save_state(state, path=STATE):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)


def common_prefix(a, b):
    n = 0
    while n < min(len(a), len(b)) and a[n] == b[n]:
        n += 1
    return n


def match_push(item_title, pushes, minimum=12):
    """Livre dont la release poussée ressemble le plus au nom du téléchargement (préfixe commun le plus long).
    TR4KER réécrit les torrents : le hash diffère de celui poussé, seul le nom permet de faire le lien."""
    t = flat(item_title)
    best = max(pushes.items(), key=lambda kv: common_prefix(t, flat(kv[1]["title"])), default=None)
    return best[0] if best and common_prefix(t, flat(best[1]["title"])) >= minimum else None


def flat(text):
    """minuscules, sans accents, lettres et chiffres seulement"""
    t = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode()
    return "".join(c for c in t.lower() if c.isalnum())


def other_book_after(release, want, others):
    """Vrai si le nom de la release contient, APRÈS la dernière occurrence du titre voulu, le titre d'un autre livre
    du même auteur : « Red Rising - Livre 5 - Iron Gold » n'est pas « Red Rising »."""
    t = flat(release)
    end = t.rfind(want) + len(want)
    return any(o and len(o) >= 4 and o not in want and o in t[end:] for o in map(flat, others))


def pick(releases, fr_title, author="", others=()):
    """Meilleur EPUB français pour `fr_title` (et le nom de famille de `author`), ou None.
    `others` : titres (anglais et français) des autres livres de l'auteur."""
    want, surname = flat(fr_title), flat((author or "").split()[-1] if author else "")
    ok = [r for r in releases
          if want and want in flat(r.get("title")) and surname in flat(r.get("title"))
          and not other_book_after(r.get("title"), want, others)
          and "epub" in (r.get("title") or "").lower()
          and not BAD.search(r.get("title") or "")
          and 0 < (r.get("size") or 0) <= MAX_BYTES
          and (r.get("seeders") or 0) >= MIN_SEEDERS
          and (r.get("downloadUrl") or r.get("magnetUrl"))]
    # le nom le plus proche du titre d'abord : « La Guerre du pavot » ne doit pas prendre « La Guerre du pavot 2 - ... »
    return max(ok, key=lambda r: (-len(flat(r.get("title"))), r.get("seeders") or 0), default=None)


def french_titles(editions):
    seen = []
    for e in editions:
        if (e.get("language") or "").lower() in FR_LANGS and e.get("title") and e["title"] not in seen:
            seen.append(e["title"])
    return seen


def extra_titles(title, path="/config/titres_fr.json"):
    """Titres français ajoutés à la main ({"Titre anglais": "Titre français"}) quand les métadonnées n'en ont pas."""
    try:
        with open(path, encoding="utf-8") as f:
            table = {flat(k): v for k, v in json.load(f).items()}
    except FileNotFoundError:
        return []
    return [table[flat(title)]] if flat(title) in table else []


class Worker:
    def __init__(self, seerr, all_requests, log, on_import=None):
        self.seerr, self.all_requests, self.log = seerr, all_requests, log
        self.on_import = on_import  # sender.handle : l'import manuel ne déclenche pas le webhook de ChaptarrNG
        self.next_try = {}

    def import_orphans(self, ck):
        """Téléchargements terminés que ChaptarrNG n'a pas su rattacher (nom français, hash réécrit par le tracker) :
        import dans le livre pour lequel la release a été poussée, envoi Kindle, retrait de la file."""
        state = load_state()
        done = [b for b in state if get(CHAPTARR, f"/api/v1/bookfile?bookId={b}", ck)]  # importé normalement par ChaptarrNG
        for b in done:
            state.pop(b)
        if done:
            save_state(state)
        if not state:
            return
        for item in get(CHAPTARR, "/api/v1/queue?pageSize=500", ck).get("records", []):
            if item.get("bookId") or item.get("status") != "completed" or \
                    item.get("trackedDownloadState") not in ("importFailed", "importBlocked", "importPending"):
                continue
            bid = match_push(item.get("title"), state)
            if not bid:
                continue
            book = get(CHAPTARR, f"/api/v1/book/{bid}", ck)
            files = [f for f in get(CHAPTARR, f"/api/v1/manualimport?downloadId={item['downloadId']}&filterExistingFiles=false", ck)
                     if (f.get("path") or "").lower().endswith(".epub")]
            if not files:
                self.log("livres-fr: aucun EPUB dans", item.get("title"))
                continue
            editions = get(CHAPTARR, f"/api/v1/edition?bookId={bid}", ck)
            # édition suivie par ChaptarrNG : importer dans une édition française non suivie crée un livre en double
            edition = ([e for e in editions if e.get("monitored")] or editions)[0]["id"]
            cmd = post(CHAPTARR, "/api/v1/command", ck, {
                "name": "ManualImport", "importMode": "copy", "replaceExistingFiles": True,  # copy : le torrent continue de seeder
                "files": [{"path": files[0]["path"], "authorId": book["authorId"], "bookId": int(bid), "editionId": edition,
                           "quality": files[0]["quality"], "indexerFlags": 0, "disableReleaseSwitching": False}]})
            for _ in range(60):
                status = get(CHAPTARR, f"/api/v1/command/{cmd['id']}", ck).get("status")
                if status in ("completed", "failed"):
                    break
                time.sleep(2)
            imported = get(CHAPTARR, f"/api/v1/bookfile?bookId={bid}", ck)
            if status != "completed" or not imported:
                self.log("livres-fr: import échoué", book.get("title"), "<-", item.get("title"), status)
                continue
            self.log("livres-fr: importé", book.get("title"), "<-", item.get("title"))
            delete(CHAPTARR, f"/api/v1/queue/{item['id']}?removeFromClient=false&blocklist=false", ck)
            state.pop(bid, None)
            save_state(state)
            if self.on_import:
                self.on_import({"eventType": "Download", "book": {"id": int(bid), "title": book.get("title")},
                                "bookFiles": [{"path": f["path"]} for f in imported]})

    def fr_server_id(self):
        for s in self.seerr("/api/v1/settings/readarr"):
            if flat(s.get("name")) == flat(FR_DESTINATION):
                return s.get("id")
        return None

    def pending_books(self, ck):
        sid = self.fr_server_id()
        if sid is None:
            return []
        busy = {r.get("bookId") for r in get(CHAPTARR, "/api/v1/queue?pageSize=500", ck).get("records", [])}
        # poussé il y a moins de 6 h : le téléchargement peut être dans la file sans bookId (hash réécrit par le tracker)
        busy |= {int(b) for b, v in load_state().items() if time.time() - v.get("at", 0) < 6 * 3600}
        out = []
        for r in self.all_requests():
            m = r.get("media") or {}
            bid = m.get("externalServiceId")
            if r.get("type") == "book" and r.get("serverId") == sid and r.get("status") == 2 and bid and bid not in busy:
                out.append(int(bid))
        return sorted(set(out))

    @staticmethod
    def push_title(bid, title, author, ck):
        """Nom de release que ChaptarrNG relie bien au livre `bid` (« Pierce Brown - Morning Star EPUB » ne se lit pas)."""
        candidates = [f"{title} by {author} EPUB", f"{author} - {title} [EPUB]", f"{author} - {title} EPUB"]
        for c in candidates:
            parsed = get(CHAPTARR, f"/api/v1/parse?title={urllib.parse.quote(c)}", ck)
            if int(bid) in [b.get("id") for b in parsed.get("books") or []]:
                return c
        return candidates[0]

    def handle_book(self, bid, ck, pk):
        if get(CHAPTARR, f"/api/v1/bookfile?bookId={bid}", ck):
            return  # déjà un fichier
        book = get(CHAPTARR, f"/api/v1/book/{bid}", ck)
        author = (book.get("author") or {}).get("authorName") or ""
        titles = french_titles(get(CHAPTARR, f"/api/v1/edition?bookId={bid}", ck)) + extra_titles(book.get("title"))
        if not titles:
            # shortcut: sans édition FR connue, on cherche le titre anglais sur les trackers FR (beaucoup de traductions
            # le gardent, ex. Red Rising) ; les releases marquées english/eng/vo restent exclues par BAD
            titles = [book.get("title")]
        others = []
        for b in get(CHAPTARR, f"/api/v1/book?authorId={book.get('authorId')}", ck):
            if b.get("id") != book.get("id") and flat(b.get("title")) != flat(book.get("title")):
                others.append(b.get("title"))
                if b.get("mediaType", "ebook") == "ebook":
                    others += french_titles(get(CHAPTARR, f"/api/v1/edition?bookId={b['id']}", ck))
        prow = {i["id"]: i["name"] for i in get(PROWLARR, "/api/v1/indexer", pk)
                if i.get("enable") and (i.get("definitionName") or "").lower() in FR_TRACKERS}
        chap = {i["name"]: i["id"] for i in get(CHAPTARR, "/api/v1/indexer", ck)}
        ids = "&".join(f"indexerIds={i}" for i in prow)
        for fr in titles:
            q = urllib.parse.quote(fr)
            rel = pick(get(PROWLARR, f"/api/v1/search?query={q}&type=search&categories=7000&limit=100&{ids}", pk, 180),
                       fr, author, others)
            if not rel:
                continue
            body = {"title": self.push_title(bid, book.get("title"), author, ck),
                    "downloadUrl": rel.get("downloadUrl") or rel.get("magnetUrl"), "protocol": "torrent",
                    "publishDate": datetime.now(timezone.utc).isoformat(), "size": rel.get("size"),
                    "seeders": rel.get("seeders"), "indexer": f"{prow[rel['indexerId']]} (Prowlarr)",
                    "indexerId": chap.get(f"{prow[rel['indexerId']]} (Prowlarr)", 0)}
            res = post(CHAPTARR, "/api/v1/release/push", ck, body)
            res = res[0] if isinstance(res, list) else res
            if res.get("approved"):
                state = load_state()
                state[str(bid)] = {"title": rel.get("title"), "at": time.time()}
                save_state(state)
                self.log("livres-fr: envoyé à ChaptarrNG", book.get("title"), "<-", rel.get("title"))
            else:
                self.log("livres-fr: refusé par ChaptarrNG", book.get("title"), res.get("rejections"))
            return
        self.log("livres-fr: aucun EPUB français pour", book.get("title"), titles, "- réessai dans 1 h")

    def approve_books(self):
        # shortcut: remplace la permission SeerrNG AUTO_APPROVE_BOOK (2^34) qui casse Jellyfin Enhanced
        # (permissions lues en entier 32 bits) ; à retirer si le plugin accepte un jour les grandes valeurs
        for r in self.all_requests():
            if r.get("type") == "book" and r.get("status") == 1:
                self.seerr(f"/api/v1/request/{r['id']}/approve", "POST")
                self.log("livres: demande approuvée", r["id"], (r.get("requestedBy") or {}).get("displayName"))

    def run_once(self):
        try:
            self.approve_books()
        except Exception as e:
            self.log("livres: erreur approbation", repr(e))
        ck, pk = key("chaptarr"), key("prowlarr")
        try:
            self.import_orphans(ck)
        except Exception as e:
            self.log("livres-fr: erreur import", repr(e))
        now = time.time()
        for bid in self.pending_books(ck):
            if self.next_try.get(bid, 0) > now:
                continue
            self.next_try[bid] = now + RETRY
            try:
                self.handle_book(bid, ck, pk)
            except Exception as e:
                self.log("livres-fr: erreur livre", bid, repr(e))

    def loop(self):
        self.log("livres-fr: actif, vérification toutes les", PERIOD, "s")
        while True:
            try:
                self.run_once()
            except Exception as e:
                self.log("livres-fr: erreur", repr(e))
            time.sleep(PERIOD)


def selftest():
    rel = lambda t, s=10, mb=2: {"title": t, "seeders": s, "size": mb * 1024 * 1024, "downloadUrl": "u", "indexerId": 1}
    fr = "La République du Dragon"
    good = rel("Rebecca.F.Kuang.La.Guerre.du.pavot.2.La.République.du.Dragon.epub-NoTag", 19)
    better = rel("La.Guerre.Du.Pavot.T2.La.Republique.Du.Dragon.2025.R.F.Kuang epub FR", 25)
    assert pick([good], fr) is good
    assert pick([good, better], fr) is better                                   # plus de seeders
    assert pick([rel("La République du Dragon epub", 2)], fr) is None          # trop peu de seeders
    assert pick([rel("La République du Dragon mobi")], fr) is None             # pas d'EPUB
    assert pick([rel("La République du Dragon epub", mb=300)], fr) is None     # trop gros
    assert pick([rel("La République du Dragon AUDIOBOOK epub m4b")], fr) is None
    assert pick([rel("Pack 50 ebooks La République du Dragon epub")], fr) is None
    assert pick([rel("Le Dieu Ardent epub")], fr) is None                      # autre livre
    assert pick([good], fr, "R.F. Kuang") is good                              # nom de famille présent
    assert pick([rel("Babel - Jean Dupont epub")], "Babel", "R.F. Kuang") is None  # même titre, autre auteur
    assert pick([rel("R.F.Kuang.Babel.2022.English.Epub-NoTag", 15)], "Babel", "R.F. Kuang") is None  # version anglaise
    t1 = rel("Rebecca.F.Kuang.La.Guerre.du.pavot.1.epub-NoTag", 22)
    t2 = rel("Rebecca.F.Kuang.La.Guerre.du.pavot.2.La.République.du.Dragon.epub-NoTag", 23)
    assert pick([t2, t1], "La Guerre du pavot", "R.F. Kuang") is t1                # pas le tome 2
    assert pick([t1, t2], fr, "R.F. Kuang") is t2
    rr = ["Golden Son", "Morning Star", "Iron Gold", "Dark Age", "Light Bringer"]
    iron = rel("Red Rising - Livre 5 - Iron Gold - Pierce Brown [EPUB]", 8)
    assert pick([iron], "Red Rising", "Pierce Brown", rr) is None                      # autre tome
    assert pick([iron], "Iron Gold", "Pierce Brown", ["Red Rising", "Golden Son"]) is iron
    ms = rel("Red Rising - Livre 3 - Morning Star - Pierce Brown [EPUB]", 8)
    assert pick([ms], "Morning Star", "Pierce Brown", ["Red Rising", "Iron Gold"]) is ms  # série avant le titre : OK
    assert pick([t2], "La Guerre du pavot", "R.F. Kuang", ["La République du Dragon"]) is None
    assert pick([t2], fr, "R.F. Kuang", ["La Guerre du pavot", "Le Dieu Ardent"]) is t2
    pushes = {"2209": {"title": "Rebecca.F.Kuang.La.Guerre.du.pavot.1.epub-NoTag"},
              "2210": {"title": "Rebecca.F.Kuang.La.Guerre.du.pavot.2.La.République.du.Dragon.epub-NoTag"},
              "2212": {"title": "Rebecca.F.Kuang.La.Guerre.du.pavot.3.Le.Dieu.Ardent.epub-NoTag"}}
    assert match_push("Rebecca F. Kuang - La Guerre du pavot 1", pushes) == "2209"   # noms réels des torrents TR4KER
    assert match_push("Rebecca F. Kuang - La Guerre du pavot 2 La République du.Dragon", pushes) == "2210"
    assert match_push("Rebecca F. Kuang - La Guerre du pavot 3 Le Dieu Ardent", pushes) == "2212"
    assert match_push("Brandon Sanderson - Tress", pushes) is None
    assert french_titles([{"language": "eng", "title": "The Dragon Republic"},
                          {"language": "fre", "title": fr}, {"language": "fre", "title": fr}]) == [fr]
    assert flat("Livres en français") == flat(FR_DESTINATION)
    calls = []
    reqs = [{"id": 1, "type": "book", "status": 1}, {"id": 2, "type": "book", "status": 2},
            {"id": 3, "type": "movie", "status": 1}]
    Worker(lambda p, m="GET": calls.append((p, m)), lambda: reqs, lambda *a: None).approve_books()
    assert calls == [("/api/v1/request/1/approve", "POST")]                    # seulement les livres en attente
    print("livres_fr selftest ok")


if __name__ == "__main__":
    selftest()
