#!/usr/bin/env bash
# Equivalence-spike setup on ai-legion: clone upstream main + pinned toolchain
# from packaging/build-inputs.json into TG_TOOLS layout (go/ node/).
set -euo pipefail
mkdir -p ~/tg-equiv ~/.cache/tollgate-tools ~/tg-equiv/tools
cd ~/tg-equiv
if [ ! -d tollgate ]; then
  git clone --depth 1 https://github.com/OpenTollGate/tollgate-module-basic-go.git tollgate
fi
cd tollgate
echo "CLONE: $(git rev-parse HEAD)"

jqget() { python3 -c "import json,sys;print(json.load(open('packaging/build-inputs.json'))[sys.argv[1]][sys.argv[2]][sys.argv[3]])" "$1" "$2" "$3"; }
GO_URL=$(jqget go tarball_linux_amd64 url);   GO_SHA=$(jqget go tarball_linux_amd64 sha256)
NODE_URL=$(jqget node tarball_linux_x64 url); NODE_SHA=$(jqget node tarball_linux_x64 sha256)

if [ ! -x "$HOME/.cache/tollgate-tools/go/bin/go" ]; then
  curl -fsSL "$GO_URL" -o ~/tg-equiv/tools/go.tgz
  echo "$GO_SHA  $HOME/tg-equiv/tools/go.tgz" | sha256sum -c -
  mkdir -p ~/.cache/tollgate-tools/go
  tar xzf ~/tg-equiv/tools/go.tgz -C ~/.cache/tollgate-tools/go --strip-components=1
fi
if [ ! -x "$HOME/.cache/tollgate-tools/node/bin/node" ]; then
  curl -fsSL "$NODE_URL" -o ~/tg-equiv/tools/node.tgz
  echo "$NODE_SHA  $HOME/tg-equiv/tools/node.tgz" | sha256sum -c -
  mkdir -p ~/.cache/tollgate-tools/node
  tar xzf ~/tg-equiv/tools/node.tgz -C ~/.cache/tollgate-tools/node --strip-components=1
fi
~/.cache/tollgate-tools/go/bin/go version
~/.cache/tollgate-tools/node/bin/node --version
echo SETUP-DONE
