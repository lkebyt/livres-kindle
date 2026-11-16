// Correctifs SeerrNG v3.72.31 pour ChaptarrNG v0.9.1000, appliqués à chaque démarrage du conteneur
// (idempotents). Si un motif manque (autre version de l'image), le conteneur refuse de démarrer.
const fs = require('fs');
function patch(file, from, to, label) {
  const src = fs.readFileSync(file, 'utf8');
  if (src.includes(to)) return console.log('[patch] déjà fait :', label);
  if (src.split(from).length !== 2) throw new Error(`[patch] motif introuvable ou multiple : ${label}`);
  fs.writeFileSync(file, src.replace(from, to));
  console.log('[patch] OK :', label);
}
const sub = '/app/dist/subscriber/MediaRequestSubscriber.js';

// 1. SeerrNG stocke « hc:123 », ChaptarrNG « hc:edition:123 » : comparer les éditions sans le préfixe.
patch(sub,
  `? editions?.find((edition) => edition.foreignEditionId === preferredEditionId)`,
  `? editions?.find((edition) => require("../lib/editionCompare/availability.js").sameEditionId(edition.foreignEditionId, preferredEditionId))`,
  'comparaison des éditions');

// 2. Titre avant l'ISBN Open Library (un ISBN étranger peut tomber sur une autre fiche Hardcover).
patch(sub,
  `                preferredIsbn ? \`isbn:\${preferredIsbn}\` : undefined,
                isbn,
                isbn ? \`isbn:\${isbn}\` : undefined,
                work?.title,
            ]`,
  `                preferredIsbn ? \`isbn:\${preferredIsbn}\` : undefined,
                work?.title,
                isbn,
                isbn ? \`isbn:\${isbn}\` : undefined,
            ]`,
  'recherche par titre avant ISBN');

// 3. ChaptarrNG 0.9.1000 répond 500 « Self referencing loop » à tout PUT /book alors qu'il l'applique : continuer.
const rd = '/app/dist/api/servarr/readarr.js';
patch(rd, `    async resolveBookMutationResult(result, fallback) {`,
  `    async putBookTolerant(path, body, config) {
        try {
            return await this.request('PUT', path, body, config);
        }
        catch (e) {
            if (!/Self referencing loop/.test(JSON.stringify(e?.response?.data ?? '') + String(e?.message)))
                throw e;
            logger_1.default.warn('Chaptarr PUT /book failed with its serialization bug; continuing.', { label: 'Readarr', path });
            return { data: Number(path.split('/').pop()) };
        }
    }
    async resolveBookMutationResult(result, fallback) {`,
  'méthode putBookTolerant');
{ // remplace tous les appels PUT /book
  const src = fs.readFileSync(rd, 'utf8');
  const from = `await this.request('PUT', \`/book/\${`;
  if (src.includes(from)) { fs.writeFileSync(rd, src.split(from).join(`await this.putBookTolerant(\`/book/\${`)); console.log('[patch] OK : PUT /book tolérant'); }
}

// 4. Respecter « pas de recherche » (destination « Livres en français ») : sinon ChaptarrNG prend une copie anglaise.
patch(sub,
  `                        searchForNewBook: true,`,
  `                        searchForNewBook: !readarrSettings.preventSearch, // patch livres-kindle`,
  'pas de BookSearch si preventSearch');
patch('/app/dist/lib/downloadRecovery.js',
  `                search: (externalId) => readarr.searchBook(externalId),`,
  `                search: (externalId) => server.preventSearch ? Promise.resolve() : readarr.searchBook(externalId), // patch livres-kindle`,
  'pas de recherche de récupération si preventSearch');

// 5. Langue des livres du profil = Français -> destination « Livres en français » (vue Jellyfin sans choix de destination).
patch('/app/dist/entity/MediaRequest.js',
  `    static request(requestBody, user, options = {}) {
        requestBody = { ...requestBody, is4k: requestBody.is4k ?? false };`,
  `    static request(requestBody, user, options = {}) {
        requestBody = { ...requestBody, is4k: requestBody.is4k ?? false };
        // patch livres-kindle : langue des livres du profil -> destination « Livres en français »
        if (requestBody.mediaType === media_1.MediaType.BOOK && requestBody.serverId == null &&
            (requestBody.format ?? 'ebook') === 'ebook' &&
            require('../utils/preferredLanguage.js').getPreferredLanguage(user?.settings?.preferredLanguages, 'book') === 'fr') {
            const frBookshelf = (0, settings_1.getSettings)().readarr.find((s) => !s.isDefault && /fran[cç]ais/i.test(s.name ?? ''));
            if (frBookshelf)
                requestBody.serverId = frBookshelf.id;
        }`,
  'routage par langue des livres');
