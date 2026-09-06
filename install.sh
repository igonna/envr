#!/bin/sh
set -eu

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)

is_termux=false
case "${PREFIX:-}" in
    *com.termux*) is_termux=true ;;
esac

need_python=false
if ! command -v python3 >/dev/null 2>&1; then
    need_python=true
elif ! python3 -c 'import sys; raise SystemExit(sys.version_info < (3, 10))'; then
    need_python=true
fi

need_ssh=false
command -v ssh >/dev/null 2>&1 || need_ssh=true
command -v scp >/dev/null 2>&1 || need_ssh=true
command -v ssh-keygen >/dev/null 2>&1 || need_ssh=true

need_openssl=false
command -v openssl >/dev/null 2>&1 || need_openssl=true

if [ "$is_termux" = true ]; then
    packages=""
    [ "$need_python" = false ] || packages="$packages python"
    [ "$need_ssh" = false ] || packages="$packages openssh"
    [ "$need_openssl" = false ] || packages="$packages openssl-tool"
    if [ -n "$packages" ]; then
        # shellcheck disable=SC2086
        pkg install -y $packages
    fi
    target_dir=${PREFIX}/bin
elif [ "$(uname -s)" = "Darwin" ]; then
    if [ "$need_python" = true ] || [ "$need_ssh" = true ] || [ "$need_openssl" = true ]; then
        if ! command -v brew >/dev/null 2>&1; then
            echo "envr installer: install Python 3.10+, OpenSSH, and OpenSSL first" >&2
            exit 1
        fi
        [ "$need_python" = false ] || brew install python
        [ "$need_ssh" = false ] || brew install openssh
        [ "$need_openssl" = false ] || brew install openssl
    fi
    target_dir=${HOME}/.local/bin
elif [ "$(uname -s)" = "Linux" ] && command -v apt-get >/dev/null 2>&1; then
    packages=""
    [ "$need_python" = false ] || packages="$packages python3"
    [ "$need_ssh" = false ] || packages="$packages openssh-client"
    [ "$need_openssl" = false ] || packages="$packages openssl"
    if [ -n "$packages" ]; then
        if [ "$(id -u)" -eq 0 ]; then
            # shellcheck disable=SC2086
            apt-get install -y $packages
        else
            # shellcheck disable=SC2086
            sudo apt-get install -y $packages
        fi
    fi
    target_dir=${HOME}/.local/bin
else
    if [ "$need_python" = true ] || [ "$need_ssh" = true ] || [ "$need_openssl" = true ]; then
        echo "envr installer: install Python 3.10+, OpenSSH, and OpenSSL first" >&2
        exit 1
    fi
    target_dir=${HOME}/.local/bin
fi

mkdir -p "$target_dir"
install -m 700 "$script_dir/envr" "$target_dir/envr"

case ":${PATH}:" in
    *:"$target_dir":*) ;;
    *)
        echo "Installed envr to $target_dir/envr"
        echo "Add this directory to PATH:"
        echo "  export PATH=\"$target_dir:\$PATH\""
        exit 0
        ;;
esac

echo "Installed envr to $target_dir/envr"
echo "Run: envr machine"
