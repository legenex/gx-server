#!/usr/bin/env bash
# User-private, pinned source build. No system package or policy changes.
set -euo pipefail
[[ $# == 0 && $(uname -m) == aarch64 ]] || { echo 'Requires ARM64 and no arguments'; exit 1; }
source_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
work_dir=$(mktemp -d /tmp/legenex-speech-landrun.XXXXXXXX)
trap 'rm -rf -- "$work_dir"' EXIT
umask 077
python3 - "$work_dir" <<'PY'
import hashlib,pathlib,sys,tarfile,urllib.request
p=pathlib.Path(sys.argv[1]);f=p/'go.tar.gz'
urllib.request.urlretrieve('https://go.dev/dl/go1.27.2.linux-arm64.tar.gz',f)
assert hashlib.file_digest(f.open('rb'),'sha256').hexdigest()=='94f3e30b8e374bc285e7dadc11e0865726b9bc6e85b841ccceaabc0214c6b7c8'
with tarfile.open(f) as t:t.extractall(p,filter='data')
PY
git clone --quiet https://github.com/zouuup/landrun.git "$work_dir/source"
git -C "$work_dir/source" checkout --quiet --detach 62823c05e58ec22c1f91b4c8468318c1f97f2d32
git -C "$work_dir/source" apply --check "$source_dir/landrun-abi7.patch"
git -C "$work_dir/source" apply "$source_dir/landrun-abi7.patch"
output_dir="$HOME/.cache/legenex-team-speech/landrun-62823c0-abi7"
mkdir -p "$output_dir"
cd "$work_dir/source"
GOTOOLCHAIN=local GOMAXPROCS=2 "$work_dir/go/bin/go" mod verify
GOTOOLCHAIN=local GOMAXPROCS=2 "$work_dir/go/bin/go" build -trimpath -o "$output_dir/landrun-abi7" ./cmd/landrun
cp LICENSE "$output_dir/LICENSE"
sha256sum "$output_dir/landrun-abi7"
