#!/bin/sh
# Build the desktop app, and optionally replace the installed copy.
#
#   packaging/app.sh build      the .app  (freeze + rust + bundle)
#   packaging/app.sh dmg        the .dmg to hand someone
#   packaging/app.sh install    build, then replace /Applications/odoo-sheller.app
#
# SKIP_FREEZE=1 reuses packaging/dist/ instead of running PyInstaller. For
# iterating on the Rust side only: nothing checks that those trees match the
# current Python, so a build made that way can ship yesterday's daemon.
#
# APP_DEST overrides where `install` puts it.
set -eu

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
APP_DIR="$ROOT/odoo-sheller-app"
BUNDLE="$APP_DIR/src-tauri/target/release/bundle/macos/odoo-sheller.app"
DEST="${APP_DEST:-/Applications/odoo-sheller.app}"
IDENTIFIER=com.odoo-sheller.desktop

PATH="$HOME/.cargo/bin:$PATH"
export PATH

die() { echo "$@" >&2; exit 1; }

build() {
    command -v cargo >/dev/null 2>&1 || die "cargo not found. Install Rust, or put ~/.cargo/bin on PATH."
    cd "$APP_DIR"
    cargo tauri build --bundles "$1"
}

# Refuse to delete anything that is not our own bundle. `install` removes a
# directory tree; the identifier is what proves it is the one we put there.
assert_ours() {
    [ -e "$1" ] || return 0
    [ -d "$1" ] || die "$1 exists and is not an app bundle. Refusing to replace it."
    found=$(/usr/libexec/PlistBuddy -c "Print :CFBundleIdentifier" "$1/Contents/Info.plist" 2>/dev/null || true)
    [ "$found" = "$IDENTIFIER" ] || die "$1 has identifier '${found:-none}', not $IDENTIFIER. Refusing to replace it."
}

case "${1:-build}" in
    build) build app; echo; echo "built: $BUNDLE" ;;
    dmg)   build dmg; echo; echo "built: $APP_DIR/src-tauri/target/release/bundle/dmg/" ;;
    install)
        build app
        assert_ours "$DEST"
        # A running copy would be replaced underneath itself: the old process
        # keeps the deleted bundle alive and the new one is not what quits.
        osascript -e "quit app id \"$IDENTIFIER\"" >/dev/null 2>&1 || true
        i=0
        while pgrep -f "$DEST/Contents/MacOS/" >/dev/null 2>&1; do
            i=$((i + 1))
            [ "$i" -gt 40 ] && die "the running app did not quit; close it and try again"
            sleep 0.25
        done
        rm -rf "$DEST"
        cp -R "$BUNDLE" "$DEST"
        echo
        echo "installed: $DEST"
        echo "Locally built, so it carries no com.apple.quarantine and opens with"
        echo "no prompt. That says nothing about a copy someone downloads."
        ;;
    *) die "usage: $0 [build|dmg|install]" ;;
esac
