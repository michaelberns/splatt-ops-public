#!/usr/bin/env bash
#
# Splatt Start.command: double click this in Finder to start the stack.
#
# It does the same as typing, in a terminal:
#
#     cd <the folder this file is in>
#     bin/start
#
# Any arguments are passed straight through to bin/start. bin/start is the
# single definition of what the stack is; this file only gets there from
# Finder, fixes up PATH, and keeps the window open at the end so the result
# can be read.
#
# macOS opens a .command file in Terminal on a double click. A plain shell
# script would open in a text editor instead.

set -uo pipefail

# set -e is deliberately off. Under set -e any failing command ends the
# script at once, and a window that closes with no message is
# indistinguishable from one that never ran. Instead, failures are checked
# where they happen, the ERR trap records the line that failed, and die()
# and hold() make sure the window always says how the run ended and waits
# for a key before closing.
FAILED_AT=""
trap 'FAILED_AT="line $LINENO"' ERR

hold() {
    echo ""
    echo "  Press any key to close."
    read -r -n 1 -s || true
    echo ""
}

die() {
    echo ""
    echo "  ERROR    $*"
    [ -n "$FAILED_AT" ] && echo "           (failed at $FAILED_AT)"
    hold
    exit 1
}

# Find the repo. Checked rather than assumed: a cd that fails inside a
# command substitution would otherwise leave REPO empty with no message.
SOURCE_DIR="$(dirname "${BASH_SOURCE[0]}")"
if ! REPO="$(cd "$SOURCE_DIR" 2>/dev/null && pwd)"; then
    die "cannot open the folder this file is in:
           $SOURCE_DIR
           If it is in ~/Documents, macOS may be blocking Terminal from
           reading Documents. Check System Settings, Privacy and Security,
           Files and Folders, and allow Terminal."
fi

cd "$REPO" || die "cannot cd into $REPO"

# PATH. Finder starts this with a minimal environment, so Homebrew's node
# and python3 would be missing and bin/start would refuse to start.
#
# zsh is asked, as a login shell, what PATH it ends up with, and only that
# string is used. Sourcing ~/.zprofile or ~/.zshrc into bash instead would
# make bash parse zsh syntax, and anything in those files that calls exit
# would end this launcher. Nothing from the profile runs in this script.
ZSH_PATH="$(/bin/zsh -lc 'printf %s "$PATH"' 2>/dev/null || true)"
if [ -n "$ZSH_PATH" ]; then
    PATH="$ZSH_PATH"
fi

# The Homebrew folders are added directly as well, so a broken or empty
# profile still leaves a PATH that finds node and python3.
for d in /opt/homebrew/bin /usr/local/bin; do
    case ":$PATH:" in
        *":$d:"*) : ;;
        *) [ -d "$d" ] && PATH="$d:$PATH" ;;
    esac
done
export PATH

# Check bin/start before clearing the screen, so any error stays readable.
if [ ! -e "$REPO/bin/start" ]; then
    die "bin/start is missing at:
           $REPO/bin/start
           This file has to sit at the top of the splatt-ops folder, beside
           the bin directory."
fi

if [ ! -x "$REPO/bin/start" ]; then
    # A lost executable bit (after a copy or a zip) is fixed here.
    chmod +x "$REPO/bin/start" 2>/dev/null || true
    [ -x "$REPO/bin/start" ] || die "bin/start is not executable and chmod failed:
           $REPO/bin/start
           Fix with: chmod +x \"$REPO/bin/start\""
fi

# clear fails when TERM is unset. It is cosmetic, so it may fail.
clear 2>/dev/null || true

echo ""
echo "  Splatt Engineering"
echo "  Starting the stack from $REPO"
echo ""

"$REPO/bin/start" "$@"
STATUS=$?

echo ""
case "$STATUS" in
    0) echo "  The stack has stopped. You can close this window." ;;
    2) echo "  bin/start rejected its arguments (status 2). Scroll up." ;;
    130) echo "  Stopped with Ctrl+C. You can close this window." ;;
    *) echo "  The stack stopped with an error (status $STATUS)."
       echo "  Scroll up to see what it said. The logs are in $REPO/logs." ;;
esac

hold
exit "$STATUS"
