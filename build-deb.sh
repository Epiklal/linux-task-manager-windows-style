#!/usr/bin/env bash
# Baut ein Debian-Paket  dist/wintaskmanager_<version>_all.deb , das sich auf
# Debian/Ubuntu/Mint & Co. samt aller Abhängigkeiten installieren lässt:
#     sudo apt install ./dist/wintaskmanager_<version>_all.deb
#
# Aufruf:   ./build-deb.sh [VERSION]        (oder  VERSION=1.2.3 ./build-deb.sh)
# Benötigt nur dpkg-deb (Paket "dpkg"), kein debhelper. Paketquellen: packaging/debian/
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$APP_DIR"

PKG=wintaskmanager
BASE_VERSION=1.0.0
MAINTAINER="${MAINTAINER:-ketchupboy <ketchupboy@ketchuppc.fritz.box>}"
SRC=packaging/debian
STAGE=build/deb
OUT_DIR=dist

if [[ -n "${1:-}" ]]; then
    VERSION="$1"
elif [[ -z "${VERSION:-}" ]]; then
    # Datum/Uhrzeit halten die Versionen aufsteigend, der Commit macht sie nachvollziehbar.
    stamp="$(date -u +%Y%m%d.%H%M)"
    commit="$(git rev-parse --short HEAD 2>/dev/null || true)"
    VERSION="${BASE_VERSION}+${stamp}${commit:+.g$commit}"
fi
DEB="$OUT_DIR/${PKG}_${VERSION}_all.deb"

if ! command -v dpkg-deb >/dev/null 2>&1; then
    echo "dpkg-deb fehlt (Paket dpkg) - das Paket lässt sich nur auf Debian/Ubuntu-Systemen bauen." >&2
    exit 1
fi
if dpkg --help 2>&1 | grep -q -- '--validate-version'; then
    dpkg --validate-version "$VERSION"
fi
for f in main.py app pages widgets scripts/wintaskmanager-smartctl wintaskmanager.desktop.in \
         "$SRC/control.in" "$SRC/postinst" "$SRC/prerm" "$SRC/postrm" "$SRC/wintaskmanager" "$SRC/setup-venv" \
         "$SRC/wintaskmanager-server.service" "$SRC/wintaskmanager-server.default" "$SRC/copyright"; do
    [[ -e "$f" ]] || { echo "Fehlt: $f" >&2; exit 1; }
done

echo "== Baue $DEB =="
rm -rf "$STAGE"
mkdir -p "$STAGE/DEBIAN" "$STAGE/usr/bin" "$STAGE/usr/share/$PKG" "$STAGE/usr/share/applications" \
         "$STAGE/usr/share/doc/$PKG" "$STAGE/usr/libexec/$PKG" "$STAGE/usr/lib/systemd/system" \
         "$STAGE/etc/default"

# Programmdateien - ohne Caches, Tests und Editor-Reste.
cp main.py "$STAGE/usr/share/$PKG/"
cp -r app pages widgets "$STAGE/usr/share/$PKG/"
find "$STAGE/usr/share/$PKG" \( -name __pycache__ -o -name '*.py[cod]' -o -name '*.swp' -o -name '.DS_Store' \) \
    -prune -exec rm -rf {} +

install -m 0755 "$SRC/wintaskmanager" "$STAGE/usr/bin/$PKG"
install -m 0755 scripts/wintaskmanager-smartctl "$STAGE/usr/libexec/$PKG/wintaskmanager-smartctl"
install -m 0755 "$SRC/setup-venv" "$STAGE/usr/libexec/$PKG/setup-venv"
install -m 0644 "$SRC/wintaskmanager-server.service" "$STAGE/usr/lib/systemd/system/"
# Enthält später den Zugriffsschlüssel, daher nur für root lesbar.
install -m 0600 "$SRC/wintaskmanager-server.default" "$STAGE/etc/default/$PKG-server"
echo "/etc/default/$PKG-server" > "$STAGE/DEBIAN/conffiles"

# Menüeintrag: dieselbe Vorlage wie install.sh, nur mit dem Starter aus /usr/bin.
sed -e 's|^Exec=.*|TryExec=wintaskmanager\nExec=wintaskmanager|' wintaskmanager.desktop.in \
    > "$STAGE/usr/share/applications/$PKG.desktop"

# Dokumentation
gzip -9n -c README.md > "$STAGE/usr/share/doc/$PKG/README.md.gz"
install -m 0644 "$SRC/copyright" "$STAGE/usr/share/doc/$PKG/copyright"
{
    echo "$PKG ($VERSION) unstable; urgency=medium"
    echo
    echo "  * Lokal gebaut mit build-deb.sh."
    echo
    echo " -- $MAINTAINER  $(date -R)"
} | gzip -9n > "$STAGE/usr/share/doc/$PKG/changelog.Debian.gz"

install -m 0755 "$SRC/postinst" "$SRC/prerm" "$SRC/postrm" "$STAGE/DEBIAN/"

# Rechte vereinheitlichen - dpkg übernimmt sie 1:1 ins Paket.
find "$STAGE" -type d -exec chmod 0755 {} +
find "$STAGE/usr/share" -type f -exec chmod 0644 {} +

INSTALLED_SIZE="$(du -sk --exclude=DEBIAN "$STAGE" | cut -f1)"
sed -e "s|@VERSION@|$VERSION|" -e "s|@INSTALLED_SIZE@|$INSTALLED_SIZE|" -e "s|@MAINTAINER@|$MAINTAINER|" \
    "$SRC/control.in" > "$STAGE/DEBIAN/control"
( cd "$STAGE" && find . -type f ! -path './DEBIAN/*' -printf '%P\n' | LC_ALL=C sort | xargs -d '\n' md5sum ) \
    > "$STAGE/DEBIAN/md5sums"

mkdir -p "$OUT_DIR"
rm -f "$DEB"
if dpkg-deb --help 2>&1 | grep -q -- '--root-owner-group'; then
    dpkg-deb --build --root-owner-group "$STAGE" "$DEB"
elif command -v fakeroot >/dev/null 2>&1; then
    fakeroot dpkg-deb --build "$STAGE" "$DEB"
else
    echo "Weder 'dpkg-deb --root-owner-group' noch fakeroot verfügbar - bitte dpkg aktualisieren oder fakeroot installieren." >&2
    exit 1
fi

if command -v lintian >/dev/null 2>&1; then
    echo; echo "== lintian =="
    lintian "$DEB" || true
fi

echo
echo "Fertig: $DEB"
echo "Installieren (zieht alle Abhängigkeiten mit):"
echo "    sudo apt install ./$DEB"
