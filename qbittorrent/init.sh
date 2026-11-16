#!/bin/sh
# qBittorrent : pas de mot de passe pour les conteneurs du réseau Docker (ChaptarrNG). L'interface web
# n'est publiée que sur 127.0.0.1 : on y entre donc sans mot de passe depuis cette machine uniquement.
conf=/config/qBittorrent/qBittorrent.conf
grep -q 'AuthSubnetWhitelistEnabled=true' "$conf" 2>/dev/null && exit 0
mkdir -p "$(dirname "$conf")"
touch "$conf"
ADD=$(cat <<'X'
WebUI\AuthSubnetWhitelistEnabled=true
WebUI\AuthSubnetWhitelist=172.16.0.0/12, 192.168.0.0/16, 10.0.0.0/8
X
)
export ADD
# ENVIRON : awk n'interprète pas les « \ » des clés qBittorrent
awk '!/AuthSubnetWhitelist/ { print } /^\[Preferences\]$/ { print ENVIRON["ADD"]; done = 1 }
     END { if (!done) print "[Preferences]\n" ENVIRON["ADD"] }' "$conf" > "$conf.tmp" && mv "$conf.tmp" "$conf"
echo "[livres-kindle] qBittorrent : accès sans mot de passe depuis le réseau Docker"
