"""Configure ChaptarrNG, Prowlarr et SeerrNG pour la chaîne livres -> Kindle.

docker compose run --rm setup
Idempotent : chaque réglage déjà présent est laissé tel quel. À relancer après la configuration
initiale de SeerrNG et après l'ajout d'indexers dans Prowlarr.
"""
import json, os, re, sys, time, urllib.error, urllib.request

CHAPTARR, PROWLARR, SEERR = "http://chaptarrng:8789", "http://prowlarr:9696", "http://seerrng:5055"
WEBHOOK = "http://kindle-sender:8000/"
CUSTOM_FORMATS = {  # nom -> (score, expression) ; score < 0 = refusé (score minimal 0)
    "Ebook - Retail": (100, r"\bretail\b"),
    "Ebook - EPUB tagged": (20, r"\bepub\b"),
    "Ebook - Summary/Guide": (-10000, r"\b(summary|summaries|study guide|sparknotes|cliff ?notes|workbook|conversation starters|analysis of|quiz(zes)?|trivia)\b"),
    "Ebook - Audio": (-10000, r"\b(\d{2,3} ?kbps|m4b|mp3|aac|flac|audio ?books?|unabridged|narrated|read by)\b"),
    "Ebook - Comic": (-10000, r"\b(cbr|cbz|comics?|graphic novel|manga)\b"),
    "Ebook - Scan/OCR": (-10000, r"\b(ocr|scann?ed|scan)\b"),
    "Ebook - Suspicious": (-10000, r"\.(exe|scr|bat|lnk)\b|\bpassword\b|\bkeygen\b"),
}


def call(base, key, path, method="GET", body=None):
    req = urllib.request.Request(base + path, method=method, data=None if body is None else json.dumps(body).encode(),
                                 headers={"X-Api-Key": key, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            raw = r.read()
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"{method} {base}{path} -> {e.code} {e.read()[:500]!r}") from None


def xml_key(name):
    with open(f"/data/{name}/config.xml", encoding="utf-8") as f:
        return re.search(r"<ApiKey>(\w+)</ApiKey>", f.read()).group(1)


def wait(name, fn):
    for _ in range(60):
        try:
            return fn()
        except Exception as e:
            last = e
            time.sleep(5)
    sys.exit(f"{name} ne répond pas : {last}")


def fill(schema, values):
    """Copie d'un modèle du schéma de l'API avec les champs `values` remplis."""
    for f in schema["fields"]:
        if f["name"] in values:
            f["value"] = values[f["name"]]
    return schema


def chaptarr(ck):
    c = lambda *a, **k: call(CHAPTARR, ck, *a, **k)
    # formats personnalisés + profil : EPUB seulement (Send to Kindle refuse MOBI/AZW3), pas de mise à niveau
    # (elle renverrait le livre sur la Kindle), 50 Mo maximum (limite d'un e-mail Send to Kindle)
    existing = {f["name"] for f in c("/api/v1/customformat")}
    for name, (score, rx) in CUSTOM_FORMATS.items():
        if name not in existing:
            c("/api/v1/customformat", "POST", {
                "name": name, "includeCustomFormatWhenRenaming": False, "appliesTo": "ebook",
                "specifications": [{"name": name, "implementation": "ReleaseTitleSpecification", "negate": False,
                                    "required": True, "fields": [{"name": "value", "value": rx}]}]})
    profiles = c("/api/v1/qualityprofile")  # relu après la création des formats (formatItems à jour)
    profile = next((p for p in profiles if re.search(r"e-?book", p["name"], re.I)), profiles[0])
    meta = c("/api/v1/metadataprofile")
    meta_id = next((m["id"] for m in meta if re.search(r"e-?book|standard", m["name"], re.I)), meta[0]["id"])
    keep = ["Unknown Text", "EPUB"]  # dernier = préféré
    items = [i for i in profile["items"] if i.get("quality", {}).get("name") not in keep]
    for i in items:
        i["allowed"] = False
    wanted = {i["quality"]["name"]: i for i in profile["items"] if i.get("quality", {}).get("name") in keep}
    if len(wanted) != 2:
        sys.exit(f"profil {profile['name']} : qualités EPUB / Unknown Text introuvables")
    for n in keep:
        wanted[n]["allowed"] = True
        items.append(wanted[n])
    for fi in profile["formatItems"]:
        fi["score"] = CUSTOM_FORMATS.get(fi["name"], (fi["score"],))[0]
    profile.update(items=items, cutoff=wanted["EPUB"]["quality"]["id"], upgradeAllowed=False,
                   minFormatScore=0, cutoffFormatScore=0)
    c(f"/api/v1/qualityprofile/{profile['id']}", "PUT", profile)
    defs = c("/api/v1/qualitydefinition")
    for d in defs:
        if d["quality"]["name"] in keep:
            d["minSize"], d["maxSize"] = 0, 50
    c("/api/v1/qualitydefinition/update", "PUT", defs)
    print(f"ChaptarrNG : profil « {profile['name']} » = EPUB, ≤ 50 Mo, sans mise à niveau")

    if not any(r["path"].rstrip("/") == "/ebooks" for r in c("/api/v1/rootfolder")):
        c("/api/v1/rootfolder", "POST", {
            "name": "Livres", "path": "/ebooks", "defaultQualityProfileId": profile["id"],
            "defaultMetadataProfileId": meta_id, "defaultMonitorOption": "all", "defaultNewItemMonitorOption": "all",
            "defaultTags": [], "ebookQualityProfileId": profile["id"], "ebookMetadataProfileId": meta_id,
            "isCalibreLibrary": False})
        print("ChaptarrNG : dossier racine /ebooks ajouté")

    if not any(d["implementation"] == "QBittorrent" for d in c("/api/v1/downloadclient")):
        schema = next(s for s in c("/api/v1/downloadclient/schema") if s["implementation"] == "QBittorrent")
        schema.update(name="qBittorrent", enable=True, priority=1, removeCompletedDownloads=True,
                      removeFailedDownloads=True)
        c("/api/v1/downloadclient", "POST", fill(schema, {
            "host": "qbittorrent", "port": 8080, "ebookCategory": "chaptarr", "audiobookCategory": "chaptarr",
            "musicCategory": "chaptarr"}))
        print("ChaptarrNG : client qBittorrent ajouté")

    if not any(n["implementation"] == "Webhook" and n["name"] == "Kindle" for n in c("/api/v1/notification")):
        schema = next(s for s in c("/api/v1/notification/schema") if s["implementation"] == "Webhook")
        schema.update(name="Kindle", onReleaseImport=True, onUpgrade=True)
        c("/api/v1/notification?forceSave=true", "POST", fill(schema, {"url": WEBHOOK, "method": 1}))
        print("ChaptarrNG : webhook Kindle ajouté")
    return profile["id"], meta_id


def prowlarr(pk, ck):
    p = lambda *a, **k: call(PROWLARR, pk, *a, **k)
    if not any(a["implementation"] == "Readarr" for a in p("/api/v1/applications")):
        schema = next(s for s in p("/api/v1/applications/schema") if s["implementation"] == "Readarr")
        schema.update(name="ChaptarrNG", syncLevel="fullSync")
        p("/api/v1/applications", "POST", fill(schema, {
            "prowlarrUrl": PROWLARR, "baseUrl": CHAPTARR, "apiKey": ck}))
        print("Prowlarr : application ChaptarrNG ajoutée")

    tags = {t["label"]: t["id"] for t in p("/api/v1/tag")}
    if "flaresolverr" not in tags:
        tags["flaresolverr"] = p("/api/v1/tag", "POST", {"label": "flaresolverr"})["id"]
    if not any(x["implementation"] == "FlareSolverr" for x in p("/api/v1/indexerProxy")):
        schema = next(s for s in p("/api/v1/indexerProxy/schema") if s["implementation"] == "FlareSolverr")
        schema.update(name="FlareSolverr", tags=[tags["flaresolverr"]])
        p("/api/v1/indexerProxy", "POST", fill(schema, {"host": "http://flaresolverr:8191/"}))
        print("Prowlarr : FlareSolverr ajouté (étiquette « flaresolverr » sur les indexers protégés par Cloudflare)")

    changed = 0
    for i in p("/api/v1/indexer"):
        f = next((f for f in i["fields"] if f["name"] == "torrentBaseSettings.appMinimumSeeders"), None)
        if f is not None and f.get("value") != 3:
            f["value"] = 3  # moins de 3 sources : le téléchargement d'un EPUB reste souvent bloqué
            p(f"/api/v1/indexer/{i['id']}?forceSave=true", "PUT", i)
            changed += 1
    if changed:
        p("/api/v1/command", "POST", {"name": "ApplicationIndexerSync"})
    print(f"Prowlarr : {len(p('/api/v1/indexer'))} indexer(s), minimum 3 sources ({changed} modifié(s))")


def seerr(ck, profile_id, meta_id):
    try:
        with open("/data/seerr/settings.json", encoding="utf-8") as f:
            sk = json.load(f)["main"]["apiKey"]
        initialized = call(SEERR, sk, "/api/v1/settings/public")["initialized"]
    except Exception:
        initialized = False
    if not initialized:
        print("SeerrNG : pas encore configuré -> ouvrir http://localhost:5055, puis relancer ce script")
        return
    s = lambda *a, **k: call(SEERR, sk, *a, **k)
    names = {d["name"] for d in s("/api/v1/settings/readarr")}
    profile_name = next(q["name"] for q in call(CHAPTARR, ck, "/api/v1/qualityprofile") if q["id"] == profile_id)
    for name, default, no_search in (("Livres en anglais", True, False), ("Livres en français", False, True)):
        if name in names:
            continue
        s("/api/v1/settings/readarr", "POST", {
            "name": name, "hostname": "chaptarrng", "port": 8789, "apiKey": ck, "useSsl": False,
            "activeProfileId": profile_id, "activeProfileName": profile_name, "activeDirectory": "/ebooks",
            "activeMetadataProfileId": meta_id, "tags": [], "is4k": False, "isDefault": default,
            # français : kindle-sender cherche lui-même sur les trackers FR ; une recherche de ChaptarrNG
            # sur le titre anglais prendrait une copie anglaise
            "syncEnabled": not no_search, "preventSearch": no_search, "tagRequests": False,
            "backend": "readarr", "serviceType": "ebook"})
        print(f"SeerrNG : destination « {name} » ajoutée")
    print("SeerrNG : destinations livres prêtes")


if __name__ == "__main__":
    ck = wait("ChaptarrNG", lambda: (call(CHAPTARR, xml_key("chaptarr"), "/api/v1/system/status"), xml_key("chaptarr"))[1])
    pk = wait("Prowlarr", lambda: (call(PROWLARR, xml_key("prowlarr"), "/api/v1/system/status"), xml_key("prowlarr"))[1])
    profile_id, meta_id = chaptarr(ck)
    prowlarr(pk, ck)
    seerr(ck, profile_id, meta_id)
    print("Terminé.")
