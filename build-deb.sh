#!/usr/bin/env bash
# Builds a Debian package  dist/lintask_<version>_all.deb  that can be
# installed on Debian/Ubuntu/Mint etc. together with all its dependencies:
#     sudo apt install ./dist/lintask_<version>_all.deb
#
# Usage:    ./build-deb.sh [VERSION]        (or  VERSION=1.2.3 ./build-deb.sh)
# Only needs dpkg-deb (package "dpkg"), no debhelper. Package sources: packaging/debian/
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$APP_DIR"

PKG=lintask
BASE_VERSION=1.0.0
MAINTAINER="${MAINTAINER:-ketchupboy <ketchupboy@ketchuppc.fritz.box>}"
SRC=packaging/debian
STAGE=build/deb
OUT_DIR=dist

if [[ -n "${1:-}" ]]; then
    VERSION="$1"
elif [[ -z "${VERSION:-}" ]]; then
    # Date and time keep versions ascending, the commit makes them traceable.
    stamp="$(date -u +%Y%m%d.%H%M)"
    commit="$(git rev-parse --short HEAD 2>/dev/null || true)"
    VERSION="${BASE_VERSION}+${stamp}${commit:+.g$commit}"
fi
DEB="$OUT_DIR/${PKG}_${VERSION}_all.deb"

if ! command -v dpkg-deb >/dev/null 2>&1; then
    echo "dpkg-deb is missing (package dpkg) - the package can only be built on Debian/Ubuntu systems." >&2
    exit 1
fi
if dpkg --help 2>&1 | grep -q -- '--validate-version'; then
    dpkg --validate-version "$VERSION"
fi
for f in main.py app pages widgets scripts/lintask-smartctl lintask.desktop.in \
         "$SRC/control.in" "$SRC/postinst" "$SRC/prerm" "$SRC/postrm" "$SRC/lintask" "$SRC/setup-venv" "$SRC/migrate-wintaskmanager" \
         "$SRC/lintask-server.service" "$SRC/lintask-server.default" "$SRC/copyright"; do
    [[ -e "$f" ]] || { echo "Missing: $f" >&2; exit 1; }
done

echo "== Building $DEB =="
rm -rf "$STAGE"
mkdir -p "$STAGE/DEBIAN" "$STAGE/usr/bin" "$STAGE/usr/share/$PKG" "$STAGE/usr/share/applications" \
         "$STAGE/usr/share/doc/$PKG" "$STAGE/usr/libexec/$PKG" "$STAGE/usr/lib/systemd/system" \
         "$STAGE/etc/default"

# Program files - without caches, tests and editor leftovers.
cp main.py "$STAGE/usr/share/$PKG/"
cp -r app pages widgets "$STAGE/usr/share/$PKG/"
find "$STAGE/usr/share/$PKG" \( -name __pycache__ -o -name '*.py[cod]' -o -name '*.swp' -o -name '.DS_Store' \) \
    -prune -exec rm -rf {} +

install -m 0755 "$SRC/lintask" "$STAGE/usr/bin/$PKG"
# The old command keeps working after the rename.
ln -s "$PKG" "$STAGE/usr/bin/wintaskmanager"
install -m 0755 scripts/lintask-smartctl "$STAGE/usr/libexec/$PKG/lintask-smartctl"
install -m 0755 "$SRC/setup-venv" "$STAGE/usr/libexec/$PKG/setup-venv"
install -m 0755 "$SRC/migrate-wintaskmanager" "$STAGE/usr/libexec/$PKG/migrate-wintaskmanager"
install -m 0644 "$SRC/lintask-server.service" "$STAGE/usr/lib/systemd/system/"
# Will hold the access token, so only root can read it.
install -m 0600 "$SRC/lintask-server.default" "$STAGE/etc/default/$PKG-server"
echo "/etc/default/$PKG-server" > "$STAGE/DEBIAN/conffiles"

# Menu entry: same template as install.sh, only with the launcher from /usr/bin.
sed -e 's|^Exec=.*|TryExec=lintask\nExec=lintask|' lintask.desktop.in \
    > "$STAGE/usr/share/applications/$PKG.desktop"

# Documentation
gzip -9n -c README.md > "$STAGE/usr/share/doc/$PKG/README.md.gz"
install -m 0644 "$SRC/copyright" "$STAGE/usr/share/doc/$PKG/copyright"
{
    echo "$PKG ($VERSION) unstable; urgency=medium"
    echo
    echo "  * Built locally with build-deb.sh."
    echo
    echo " -- $MAINTAINER  $(date -R)"
} | gzip -9n > "$STAGE/usr/share/doc/$PKG/changelog.Debian.gz"

install -m 0755 "$SRC/postinst" "$SRC/prerm" "$SRC/postrm" "$STAGE/DEBIAN/"

# Normalize permissions - dpkg copies them into the package as they are.
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
    echo "Neither 'dpkg-deb --root-owner-group' nor fakeroot is available - please update dpkg or install fakeroot." >&2
    exit 1
fi

if command -v lintian >/dev/null 2>&1; then
    echo; echo "== lintian =="
    lintian "$DEB" || true
fi

echo
echo "Done: $DEB"
echo "Install (pulls in all dependencies):"
echo "    sudo apt install ./$DEB"
