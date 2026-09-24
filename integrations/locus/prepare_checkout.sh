#!/bin/sh
# Create a disposable Locus integration checkout and an isolated runtime venv.
#
#   prepare_checkout.sh LOCUS_REPO SHA DEST WHEEL
#
# The source repository is only read (git archive, no index refresh). DEST
# becomes a new git repository whose first commit is the exported agent/ tree
# at SHA; each patch in patches/ is applied as its own commit. The venv
# installs Locus's hash-locked runtime exactly as the app build does, then the
# langgraph-workflow wheel constrained to those pins.
set -eu
src=$1; sha=$2; dest=$3; wheel=$4
here=$(cd "$(dirname "$0")" && pwd)
[ -e "$dest" ] && { echo "refusing to overwrite $dest" >&2; exit 1; }
mkdir -p "$dest/checkout"
GIT_OPTIONAL_LOCKS=0 git -C "$src" archive "$sha" | tar -x -C "$dest/checkout"
cd "$dest/checkout"
git init -q
git add -A
git -c user.name=integration -c user.email=integration@localhost commit -qm "Locus $sha (tree export)"
for patch in "$here"/patches/*.patch; do
  [ -e "$patch" ] || continue
  git apply "$patch"
  git add -A
  git -c user.name=integration -c user.email=integration@localhost commit -qm "$(basename "$patch")"
done
python3 -m venv "$dest/venv"
py="$dest/venv/bin/python"
"$py" -m pip install -q --upgrade pip
"$py" -m pip install -q --require-hashes --only-binary=:all: -r agent/requirements-runtime.lock
"$py" -m pip install -q --no-deps -e agent
"$py" -m pip install -q "pytest>=8"
# Constrain to the shipped runtime pins so the resolver cannot move them.
grep -E '^[A-Za-z0-9_.-]+==' agent/requirements-runtime.lock | sed 's/ .*//' > "$dest/locus-constraints.txt"
"$py" -m pip install -q --only-binary=:all: -c "$dest/locus-constraints.txt" "$wheel"
"$py" -m pip freeze > "$dest/freeze.txt"
git log --format='%H %s' > "$dest/checkout-log.txt"
echo "prepared $dest"
