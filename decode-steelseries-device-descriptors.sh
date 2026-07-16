#!/usr/bin/env bash
# Decrypt SteelSeries GG .edevice files for local interoperability research.
set -euo pipefail

readonly DESCRIPTOR_PASSPHRASE='FILL ME IN'
readonly DEFAULT_RESEARCH_ROOT="${HOME}/.local/share/linux-arctis-manager/research"

usage() {
    cat <<'EOF'
Usage:
  decode-steelseries-device-descriptors.sh --installer SETUP.exe --version VERSION [--replace]
  decode-steelseries-device-descriptors.sh --source-dir DEVICE_SPECIFICATIONS --version VERSION [--replace]

Options:
  --installer PATH       SteelSeries GG Windows installer. Requires 7z.
  --source-dir PATH      Existing deviceSpecifications directory. Skips extraction.
  --version VERSION      GG version used to name the research directory.
  --research-root PATH   Parent directory for steelseries-gg-VERSION output.
  --replace              Replace existing descriptor outputs for this version.
  -h, --help             Show this help text.
EOF
}

installer=''
source_dir=''
version=''
research_root="$DEFAULT_RESEARCH_ROOT"
replace=false

while (($#)); do
    case "$1" in
        --installer)
            installer="${2:?--installer requires a path}"
            shift 2
            ;;
        --source-dir)
            source_dir="${2:?--source-dir requires a path}"
            shift 2
            ;;
        --version)
            version="${2:?--version requires a value}"
            shift 2
            ;;
        --research-root)
            research_root="${2:?--research-root requires a path}"
            shift 2
            ;;
        --replace)
            replace=true
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            printf 'Unknown argument: %s\n' "$1" >&2
            usage >&2
            exit 2
            ;;
    esac
done

if [[ -z "$version" ]] || [[ -n "$installer" && -n "$source_dir" ]] || [[ -z "$installer" && -z "$source_dir" ]]; then
    usage >&2
    exit 2
fi

for command in awk base64 find gpg python3 sha256sum sort; do
    command -v "$command" >/dev/null || {
        printf 'Missing required command: %s\n' "$command" >&2
        exit 1
    }
done

temporary_extract=''
gnupg_home=''
stage=''
cleanup() {
    [[ -n "$temporary_extract" ]] && rm -rf "$temporary_extract"
    [[ -n "$gnupg_home" ]] && rm -rf "$gnupg_home"
    [[ -n "$stage" ]] && rm -rf "$stage"
}
trap cleanup EXIT

if [[ -n "$installer" ]]; then
    [[ -f "$installer" ]] || {
        printf 'Installer does not exist: %s\n' "$installer" >&2
        exit 1
    }
    command -v 7z >/dev/null || {
        printf 'Missing required command for --installer: 7z\n' >&2
        exit 1
    }
    temporary_extract="$(mktemp -d)"
    7z x -y "-o$temporary_extract" "$installer" >/dev/null
    source_dir="$temporary_extract/apps/engine/deviceSpecifications"
fi

[[ -d "$source_dir" ]] || {
    printf 'deviceSpecifications directory does not exist: %s\n' "$source_dir" >&2
    exit 1
}

release_dir="$research_root/steelseries-gg-$version"
target_specs="$release_dir/specs/apps/engine/deviceSpecifications"
target_decoded="$release_dir/decoded-device-specifications"

if { [[ -e "$target_specs" ]] || [[ -e "$target_decoded" ]]; } && ! "$replace"; then
    printf 'Descriptor output already exists for GG %s. Re-run with --replace to update it.\n' "$version" >&2
    exit 1
fi

mkdir -p "$release_dir"
stage="$(mktemp -d "$release_dir/.descriptor-stage.XXXXXX")"
stage_specs="$stage/specs/apps/engine/deviceSpecifications"
stage_decoded="$stage/decoded-device-specifications"
mkdir -p "$stage_specs" "$stage_decoded"

mapfile -d '' descriptors < <(find "$source_dir" -maxdepth 1 -type f -name '*.edevice' -print0 | sort -z)
if ((${#descriptors[@]} == 0)); then
    printf 'No .edevice files found in: %s\n' "$source_dir" >&2
    exit 1
fi

gnupg_home="$(mktemp -d)"
results="$stage/decode-results.tsv"
failures="$stage/decode-failures.log"
gpg_log="$stage/gpg.log"
printf 'source\toutput\tbytes\n' > "$results"
: > "$failures"

decode_descriptor() {
    local source_file="$1"
    local output_file="$2"

    # GG uses a custom armor label and checksum. GnuPG accepts the decoded packets.
    awk '/^-----/{next} /^=/{next} NF {print}' "$source_file" \
        | base64 -d \
        | gpg --batch --yes --homedir "$gnupg_home" --pinentry-mode loopback \
            --passphrase-fd 3 --output "$output_file" --decrypt 3<<<"$DESCRIPTOR_PASSPHRASE"
}

for encrypted in "${descriptors[@]}"; do
    filename="$(basename "$encrypted")"
    name="${filename%.edevice}"
    staged_encrypted="$stage_specs/$filename"
    staged_decoded="$stage_decoded/$name.device"
    temporary_output="$(mktemp "$stage_decoded/.$name.XXXXXX")"

    cp -p "$encrypted" "$staged_encrypted"
    : > "$gpg_log"
    if decode_descriptor "$encrypted" "$temporary_output" 2>"$gpg_log"; then
        rm -f "$gpg_log"
        mv "$temporary_output" "$staged_decoded"
        printf '%s\t%s\t%s\n' "$filename" "$name.device" "$(wc -c < "$staged_decoded")" >> "$results"
    else
        cat "$gpg_log" >> "$failures"
        rm -f "$gpg_log"
        rm -f "$temporary_output"
        printf '%s\n' "$filename" >> "$failures"
        printf 'Decryption failed: %s\n' "$filename" >&2
        exit 1
    fi
done

python3 - "$stage_decoded" <<'PY'
from pathlib import Path
import re
import sys

root = Path(sys.argv[1])
files = sorted(root.glob('*.device'))
problems = []
includes = set()
for path in files:
    data = path.read_bytes()
    if not data or b'\0' in data:
        problems.append(path.name)
        continue
    try:
        text = data.decode('utf-8')
    except UnicodeDecodeError:
        problems.append(path.name)
        continue
    includes.update(re.findall(r'\(include\s+"([^"]+)"\)', text))

unresolved = sorted(name for name in includes if not (root / f'{name}.device').is_file())
if problems or unresolved:
    print(f'Invalid descriptor outputs: {problems}', file=sys.stderr)
    print(f'Unresolved includes: {unresolved}', file=sys.stderr)
    raise SystemExit(1)
PY

(
    cd "$stage_specs"
    find . -maxdepth 1 -type f -name '*.edevice' -printf '%f\n' | sort | xargs sha256sum > "$stage/encrypted-device-specifications.sha256"
)
(
    cd "$stage_decoded"
    find . -maxdepth 1 -type f -name '*.device' -printf '%f\n' | sort | xargs sha256sum > "$stage/decoded-device-specifications.sha256"
)

if "$replace"; then
    rm -rf "$target_specs" "$target_decoded"
fi
mkdir -p "$(dirname "$target_specs")"
mv "$stage_specs" "$target_specs"
mv "$stage_decoded" "$target_decoded"
mv "$results" "$release_dir/decode-results.tsv"
mv "$failures" "$release_dir/decode-failures.log"
mv "$stage/encrypted-device-specifications.sha256" "$release_dir/encrypted-device-specifications.sha256"
mv "$stage/decoded-device-specifications.sha256" "$release_dir/decoded-device-specifications.sha256"
rm -rf "$stage"
stage=''

printf 'Decoded %s device descriptors into %s\n' "${#descriptors[@]}" "$target_decoded"
