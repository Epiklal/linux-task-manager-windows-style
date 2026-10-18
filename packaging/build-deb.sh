#!/usr/bin/env bash
# Build packaging/dist/wintaskmanager_<version>_all.deb from the working tree.
# Usage: packaging/build-deb.sh [version]
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VERSION="${1:-1.0.0}"
PKG=wintaskmanager
BUILD="$(mktemp -d)"
STAGE="$BUILD/${PKG}_${VERSION}_all"
trap 'rm -rf "$BUILD"' EXIT

install -d "$STAGE/DEBIAN" "$STAGE/opt/$PKG" "$STAGE/usr/bin" \
    "$STAGE/usr/share/applications" "$STAGE/lib/systemd/system" "$STAGE/etc/default"

# Application code (no tests, docs, venv or caches).
cp -r "$ROOT"/{app,pages,widgets,scripts,main.py,requirements.txt} "$STAGE/opt/$PKG/"
find "$STAGE/opt/$PKG" -name __pycache__ -prune -exec rm -rf {} +

install -m 0755 "$ROOT/packaging/debian/wintaskmanager" "$STAGE/usr/bin/$PKG"
install -m 0644 "$ROOT/packaging/debian/wintaskmanager.desktop" "$STAGE/usr/share/applications/"
install -m 0644 "$ROOT/packaging/debian/wintaskmanager-server.service" "$STAGE/lib/systemd/system/"
install -m 0644 "$ROOT/packaging/debian/wintaskmanager-server.default" \
    "$STAGE/etc/default/wintaskmanager-server"

sed "s/@VERSION@/$VERSION/" "$ROOT/packaging/debian/control" > "$STAGE/DEBIAN/control"
echo "/etc/default/wintaskmanager-server" > "$STAGE/DEBIAN/conffiles"
for script in postinst prerm postrm; do
    install -m 0755 "$ROOT/packaging/debian/$script" "$STAGE/DEBIAN/$script"
done

mkdir -p "$ROOT/packaging/dist"
dpkg-deb --root-owner-group --build "$STAGE" "$ROOT/packaging/dist/${PKG}_${VERSION}_all.deb"
