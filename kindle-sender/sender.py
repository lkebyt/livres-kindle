"""Envoie sur la Kindle du demandeur chaque livre importé par ChaptarrNG.

ChaptarrNG (Connect > Webhook, événement « On Import ») -> POST http://kindle-sender:8000/
-> on retrouve la demande SeerrNG (media.externalServiceId == id du livre ChaptarrNG)
-> on envoie le fichier par e-mail à l'adresse Kindle du demandeur (kindle.json).
"""
import json, mimetypes, os, smtplib, sys, threading, time, urllib.request
from email.message import EmailMessage
from http.server import BaseHTTPRequestHandler, HTTPServer

SEERR = os.environ.get("SEERR_URL", "http://seerrng:5055")
SMTP_USER = os.environ["SMTP_USER"]
KINDLE_MAP = os.environ.get("KINDLE_MAP", "/config/kindle.json")
OK_EXT = {".epub", ".pdf", ".docx", ".txt", ".rtf", ".htm", ".html"}  # formats acceptés par Send to Kindle
MAX_BYTES = 50 * 1024 * 1024  # limite Amazon par e-mail
mimetypes.add_type("application/epub+zip", ".epub")  # absent de l image python slim (pas de /etc/mime.types)


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def seerr_key():
    if os.environ.get("SEERR_API_KEY"):
        return os.environ["SEERR_API_KEY"]
    with open("/data/seerr/settings.json", encoding="utf-8") as f:  # créé par SeerrNG à sa première configuration
        return json.load(f)["main"]["apiKey"]


def seerr(path, method="GET"):
    req = urllib.request.Request(SEERR + path, method=method, headers={"X-Api-Key": seerr_key()})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def norm(title):
    """Titre comparable : sans sous-titre, minuscules, lettres et chiffres seulement."""
    return "".join(c for c in (title or "").split(":")[0].lower() if c.isalnum())


def find_requesters(book_id, requests, title=None, title_of=None):
    """Utilisateurs SeerrNG ayant demandé le livre ChaptarrNG `book_id`."""
    users = {}
    for r in requests:
        m = r.get("media") or {}
        if str(m.get("externalServiceId")) == str(book_id) and r.get("requestedBy"):
            u = r["requestedBy"]
            users[u["id"]] = u
    if users or not (title and title_of):
        return list(users.values())
    # shortcut: repli par titre car SeerrNG n'enregistre pas l'id ChaptarrNG quand ChaptarrNG 0.9.1000
    # répond 500 à PUT /book ; à retirer quand ce bug sera corrigé
    for r in requests:
        m = r.get("media") or {}
        ol = next((i.get("value") for i in m.get("identifiers") or [] if i.get("provider") == "openlibrary"), None)
        if (r.get("type") == "book" and not m.get("externalServiceId") and ol and r.get("requestedBy")
                and norm(title_of(ol)) == norm(title)):
            u = r["requestedBy"]
            users[u["id"]] = u
    return list(users.values())


def book_title(openlibrary_id):
    try:
        return seerr(f"/api/v1/book/{openlibrary_id}").get("title")
    except Exception as e:
        log("titre introuvable pour", openlibrary_id, repr(e))
        return None


def kindle_address(user, kmap):
    for k in (str(user.get("id")), user.get("jellyfinUsername"), user.get("username"),
              user.get("displayName"), user.get("email")):
        if k and k in kmap:
            return kmap[k]
    return None


def all_requests():
    out, skip = [], 0
    while True:
        page = seerr(f"/api/v1/request?take=100&skip={skip}&filter=all&sort=modified")
        out += page.get("results", [])
        skip += 100
        if skip >= page.get("pageInfo", {}).get("results", 0) or skip >= 1000:
            return out


def send(to, path, title):
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = SMTP_USER, to, title
    msg.set_content(f"{title} - envoyé automatiquement par le serveur.")
    with open(path, "rb") as f:
        maintype, subtype = (mimetypes.guess_type(path)[0] or "application/octet-stream").split("/")  # EPUB : application/epub+zip
        msg.add_attachment(f.read(), maintype=maintype, subtype=subtype,
                           filename=os.path.basename(path))
    with smtplib.SMTP("smtp.gmail.com", 587, timeout=60) as s:
        s.starttls()
        s.login(SMTP_USER, os.environ["SMTP_PASSWORD"].replace(" ", ""))  # mot de passe d'application Gmail
        s.send_message(msg)


SENT, SENT_LOCK = {}, threading.Lock()  # (adresse Kindle, id livre) -> heure d'envoi ; perdu au redémarrage


def handle(event):
    etype = event.get("eventType")
    log("event", etype)
    if etype not in ("Download", "BookFileImport", "Import"):
        return
    book = event.get("book") or (event.get("books") or [{}])[0]
    files = [f.get("path") for f in event.get("bookFiles") or [] if f.get("path")]
    if not files and event.get("bookFile"):
        files = [event["bookFile"].get("path")]
    files = [p for p in files if os.path.splitext(p)[1].lower() in OK_EXT and os.path.getsize(p) <= MAX_BYTES]
    if not book.get("id") or not files:
        log("rien à envoyer", book.get("id"), event.get("bookFiles"))
        return
    with open(KINDLE_MAP, encoding="utf-8") as f:
        kmap = json.load(f)
    users = find_requesters(book["id"], all_requests(), book.get("title"), book_title)
    if not users:
        log("aucune demande SeerrNG pour le livre", book["id"], book.get("title"))
    for u in users:
        to = kindle_address(u, kmap)
        if not to:
            log("pas d'adresse Kindle pour", u.get("displayName"), "-> ajouter dans kindle.json")
            continue
        if os.path.exists("/config/kindle_off"):  # interrupteur pour les tests : supprimer le fichier pour réactiver
            log("envoi Kindle désactivé (kindle_off) :", book.get("title"), "->", u.get("displayName"), files[0])
            continue
        with SENT_LOCK:  # le webhook et la reprise de livres_fr peuvent signaler le même import
            if time.time() - SENT.get((to, book["id"]), 0) < 24 * 3600:
                log("déjà envoyé dans les 24 h :", book.get("title"), "->", u.get("displayName"))
                continue
            SENT[(to, book["id"])] = time.time()
        send(to, files[0], book.get("title", "Livre"))
        log("envoyé", book.get("title"), "->", u.get("displayName"))


class H(BaseHTTPRequestHandler):
    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        self.send_response(200)
        self.end_headers()
        try:
            handle(json.loads(body or b"{}"))
        except Exception as e:  # ne jamais faire échouer l'import côté ChaptarrNG
            log("erreur:", repr(e))

    def log_message(self, *a):
        pass


def selftest():
    reqs = [{"media": {"externalServiceId": 7}, "requestedBy": {"id": 3, "displayName": "Alice"}},
            {"media": {"externalServiceId": 8}, "requestedBy": {"id": 1, "displayName": "Bob"}},
            {"media": {"externalServiceId": 7}, "requestedBy": {"id": 3, "displayName": "Alice"}}]
    assert [u["id"] for u in find_requesters(7, reqs)] == [3]
    assert find_requesters(9, reqs) == []
    # repli par titre : demande non liée (externalServiceId vide), sous-titre ignoré
    orphan = [{"type": "book", "requestedBy": {"id": 3, "displayName": "Alice"},
               "media": {"externalServiceId": None, "identifiers": [{"provider": "openlibrary", "value": "OL1W"}]}}]
    titles = {"OL1W": "Babel: Or the Necessity of Violence"}.get
    assert [u["id"] for u in find_requesters(9, orphan, "Babel", titles)] == [3]
    assert find_requesters(9, orphan, "Yellowface", titles) == []
    assert [u["id"] for u in find_requesters(7, reqs + orphan, "Babel", titles)] == [3]  # lien direct prioritaire
    assert kindle_address({"id": 3, "displayName": "Alice"}, {"Alice": "c@kindle.com"}) == "c@kindle.com"
    assert kindle_address({"id": 3}, {"3": "x@kindle.com"}) == "x@kindle.com"
    assert kindle_address({"id": 4}, {}) is None
    print("selftest ok")


if __name__ == "__main__":
    import livres_fr
    if sys.argv[1:] == ["selftest"]:
        selftest()
        livres_fr.selftest()
    else:
        threading.Thread(target=livres_fr.Worker(seerr, all_requests, log, handle).loop, daemon=True).start()
        log("kindle-sender en écoute sur :8000")
        HTTPServer(("", 8000), H).serve_forever()
