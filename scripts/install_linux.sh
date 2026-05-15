#!/usr/bin/env bash
set -euo pipefail

# Bundle you extracted or cloned (e.g. ~/Downloads/atak-imagery). Used only as a source copy.
# After a successful install, this folder is removed automatically when it is
# ~/Downloads/atak-imagery (name must be atak-imagery). Set ATAK_INSTALL_KEEP_SOURCE=1 to keep it.
# Other locations: zenity asks whether to remove; without zenity, a hint is printed.
SOURCE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Persistent install location (not Downloads). Override: export ATAK_PIPELINE_HOME=/path
DEFAULT_INSTALL="${XDG_DATA_HOME:-$HOME/.local/share}/atak-imagery"
INSTALL_ROOT="${ATAK_PIPELINE_HOME:-$DEFAULT_INSTALL}"

VENV_DIR="$INSTALL_ROOT/.venv"
LAUNCHER="$INSTALL_ROOT/run_atak_pipeline.sh"
DEVICE_LAUNCHER="$INSTALL_ROOT/run_atak_pipeline_with_device.sh"
DESKTOP_FILE_NAME="ATAK Imagery Downloader.desktop"
DESKTOP_FILE_NAME_DEVICE="ATAK Device Installer.desktop"
APP_NAME="ATAK Imagery Downloader"
APP_NAME_DEVICE="ATAK Device Installer"

# Default sources for ATAK Device Installer (USB). Forks can override before running:
#   ATAK_BUNDLE_MANIFEST_URL=... ATAK_BUNDLE_PLUGIN_REPO=... bash install_linux.sh
ATAK_BUNDLE_MANIFEST_URL="${ATAK_BUNDLE_MANIFEST_URL:-http://31.220.30.74/atak/manifest.json}"
ATAK_BUNDLE_PLUGIN_REPO="${ATAK_BUNDLE_PLUGIN_REPO:-atakmaps/TAK-UV-PRO}"

deb_installed() {
    local pkg="$1"
    local st
    st="$(dpkg-query -W -f='${Status}' "$pkg" 2>/dev/null || true)"
    [[ "$st" == *"ok installed"* ]]
}

rpm_installed() {
    rpm -q "$1" &>/dev/null
}

pacman_installed() {
    pacman -Qi "$1" &>/dev/null
}

# Copy application tree into INSTALL_ROOT (excludes venv and junk). Preserves existing deploy.env.
sync_bundle_into_install_dir() {
    local src dest
    src="$(cd "$SOURCE_ROOT" && pwd -P)"
    dest="$(mkdir -p "$INSTALL_ROOT" && cd "$INSTALL_ROOT" && pwd -P)"
    if [ "$src" = "$dest" ]; then
        echo "  Source and install directory are the same; skipping file copy."
        return 0
    fi

    local deploy_bak=""
    if [ -f "$dest/deploy.env" ]; then
        deploy_bak="$(mktemp)"
        cp -a "$dest/deploy.env" "$deploy_bak"
    fi

    echo "  From: $src"
    echo "  To:   $dest"
    mkdir -p "$dest"

    if command -v rsync >/dev/null 2>&1; then
        rsync -a "$src/" "$dest/" \
            --exclude '.venv' \
            --exclude '__pycache__' \
            --exclude '.git' \
            --exclude 'logs' \
            --exclude 'scripts/logs' \
            --exclude '.mypy_cache' \
            --exclude '.pytest_cache' \
            --exclude 'dist' \
            --exclude 'build' \
            --exclude 'output' \
            --exclude '.cursor'
    else
        # tar if rsync is missing (should be rare once packaged)
        tar -C "$src" \
            --exclude='.venv' \
            --exclude='__pycache__' \
            --exclude='.git' \
            --exclude='logs' \
            --exclude='scripts/logs' \
            --exclude='.mypy_cache' \
            --exclude='.pytest_cache' \
            --exclude='dist' \
            --exclude='build' \
            --exclude='output' \
            --exclude='.cursor' \
            -cf - . | tar -C "$dest" -xf -
    fi

    if [ -n "$deploy_bak" ] && [ -f "$deploy_bak" ]; then
        mv "$deploy_bak" "$dest/deploy.env"
    fi
}

# Ensure deploy.env lists where to download ATAK (manifest) and the plugin (GitHub repo).
append_deploy_env_defaults() {
    local f="$ROOT/deploy.env"
    mkdir -p "$(dirname "$f")"
    touch "$f"
    if ! grep -qE "^[[:space:]]*ATAK_DEPLOY_MANIFEST_URL=" "$f" 2>/dev/null; then
        {
            echo ""
            echo "# Default ATAK release JSON (install_linux.sh). Change only if you host your own."
            echo "ATAK_DEPLOY_MANIFEST_URL=$ATAK_BUNDLE_MANIFEST_URL"
        } >> "$f"
    fi
    if ! grep -qE "^[[:space:]]*ATAK_PLUGIN_GITHUB_REPO=" "$f" 2>/dev/null; then
        echo "ATAK_PLUGIN_GITHUB_REPO=$ATAK_BUNDLE_PLUGIN_REPO" >> "$f"
    fi
}

echo "[1/7] Install location: $INSTALL_ROOT"
echo "      (Override with ATAK_PIPELINE_HOME if needed.)"
sync_bundle_into_install_dir

ROOT="$INSTALL_ROOT"

echo "[2/7] Checking required system packages..."

if command -v apt >/dev/null 2>&1; then
    if ! command -v python3 >/dev/null 2>&1; then
        echo "  Installing python3 (required to continue)..."
        sudo apt-get update -qq
        sudo apt-get install -y python3
    fi

    PY_VER="$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
    apt_pkgs=(python3 python3-pip python3-tk zenity adb rsync "python${PY_VER}-venv")
    apt_missing=()
    for pkg in "${apt_pkgs[@]}"; do
        if ! deb_installed "$pkg"; then
            apt_missing+=("$pkg")
        fi
    done

    if [ ${#apt_missing[@]} -gt 0 ]; then
        echo "  Installing: ${apt_missing[*]}"
        sudo apt-get update -qq
        sudo apt-get install -y "${apt_missing[@]}"
    else
        echo "  All apt packages already present; skipping."
    fi

elif command -v dnf >/dev/null 2>&1; then
    dnf_pkgs=(python3 python3-pip python3-virtualenv python3-tkinter zenity android-tools rsync)
    dnf_missing=()
    for pkg in "${dnf_pkgs[@]}"; do
        if ! rpm_installed "$pkg"; then
            dnf_missing+=("$pkg")
        fi
    done

    if [ ${#dnf_missing[@]} -gt 0 ]; then
        echo "  Installing: ${dnf_missing[*]}"
        sudo dnf install -y "${dnf_missing[@]}"
    else
        echo "  All dnf packages already present; skipping."
    fi

elif command -v pacman >/dev/null 2>&1; then
    pac_pkgs=(python python-pip python-virtualenv tk zenity android-tools rsync)
    pac_missing=()
    for pkg in "${pac_pkgs[@]}"; do
        if ! pacman_installed "$pkg"; then
            pac_missing+=("$pkg")
        fi
    done

    if [ ${#pac_missing[@]} -gt 0 ]; then
        echo "  Installing: ${pac_missing[*]}"
        sudo pacman -Sy --noconfirm "${pac_missing[@]}"
    else
        echo "  All pacman packages already present; skipping."
    fi

else
    echo "Unsupported distro."
    echo "Please install these manually, then run this installer again:"
    echo "  python3 / pip / venv / tkinter / zenity / adb (Android platform tools)"
    exit 1
fi

venv_python="$VENV_DIR/bin/python"
venv_created=0

echo "[3/7] Virtual environment in install directory..."

if [ -x "$venv_python" ]; then
    if "$venv_python" -c "import geopandas, shapely, rasterio, numpy, requests" &>/dev/null; then
        echo "  Reusing existing .venv (imports OK)."
    else
        echo "  Existing .venv is incomplete; recreating..."
        rm -rf "$VENV_DIR"
        python3 -m venv "$VENV_DIR"
        venv_created=1
    fi
else
    echo "  Creating new .venv..."
    rm -rf "$VENV_DIR"
    python3 -m venv "$VENV_DIR"
    venv_created=1
fi

echo "[4/7] Python dependencies (requirements.txt)..."
if [ "$venv_created" -eq 1 ]; then
    "$VENV_DIR/bin/python" -m pip install --upgrade pip
    "$VENV_DIR/bin/pip" install -r "$ROOT/requirements.txt"
else
    if ! "$venv_python" -m pip check &>/dev/null; then
        echo "  pip check reported issues; syncing requirements.txt..."
        "$VENV_DIR/bin/pip" install -r "$ROOT/requirements.txt"
    else
        echo "  Already satisfied; skipping pip."
    fi
fi

if [ ! -f "$ROOT/deploy.env" ] && [ -f "$ROOT/deploy.env.example" ]; then
    cp "$ROOT/deploy.env.example" "$ROOT/deploy.env"
fi
append_deploy_env_defaults
echo "  deploy.env: default ATAK manifest + plugin GitHub repo (edit or override env if you fork)."

tile_plan_dir="$ROOT/scripts/data/tile_plans/v1"
mkdir -p "$tile_plan_dir"
tile_plan_count="$(find "$tile_plan_dir" -maxdepth 1 -type f -name '*.tiles.gz' | wc -l | tr -d '[:space:]')"
if [ "${tile_plan_count:-0}" -gt 0 ]; then
    echo "  Tile-plan caches bundled: $tile_plan_count file(s) in $tile_plan_dir"
else
    echo "  WARNING: no tile-plan caches found in $tile_plan_dir"
    echo "           Downloader will still work but may recompute tile plans."
fi

echo "[5/7] Creating runtime launchers..."
cat > "$LAUNCHER" <<LAUNCHER_EOF
#!/usr/bin/env bash
set -euo pipefail
ROOT="$ROOT"
cd "\$ROOT"
exec "\$ROOT/.venv/bin/python" "\$ROOT/scripts/atak_downloader_finalbuild.py"
LAUNCHER_EOF

chmod +x "$LAUNCHER"

cat > "$DEVICE_LAUNCHER" <<DEVICE_LAUNCHER_EOF
#!/usr/bin/env bash
set -euo pipefail
ROOT="$ROOT"
cd "\$ROOT"
export PATH="/usr/local/bin:/usr/bin:/bin\${PATH:+:\$PATH}"
if [ -d "\$HOME/Android/Sdk/platform-tools" ]; then
  export PATH="\$HOME/Android/Sdk/platform-tools:\$PATH"
fi
if [ -f "\$ROOT/deploy.env" ]; then
  set -a
  # shellcheck disable=SC1090
  . "\$ROOT/deploy.env"
  set +a
fi
exec "\$ROOT/.venv/bin/python" "\$ROOT/scripts/atak_adb_deploy.py"
DEVICE_LAUNCHER_EOF

chmod +x "$DEVICE_LAUNCHER"

write_desktop_shortcut() {
    local file_name="$1"
    local name="$2"
    local comment="$3"
    local exec_cmd="$4"

    local content="[Desktop Entry]
Version=1.0
Type=Application
Name=$name
Comment=$comment
Exec=$exec_cmd
Terminal=false
Categories=Utility;
StartupNotify=true
"
    mkdir -p "$HOME/.local/share/applications"
    printf '%s\n' "$content" > "$HOME/.local/share/applications/$file_name"
    chmod +x "$HOME/.local/share/applications/$file_name"

    if [ -d "$HOME/Desktop" ]; then
        printf '%s\n' "$content" > "$HOME/Desktop/$file_name"
        chmod +x "$HOME/Desktop/$file_name"
    fi
}

cleanup_legacy_shortcuts() {
    local app_dir desktop_dir dir f base
    app_dir="$HOME/.local/share/applications"
    desktop_dir="$HOME/Desktop"

    for dir in "$app_dir" "$desktop_dir"; do
        [ -d "$dir" ] || continue
        for f in "$dir"/*.desktop; do
            [ -f "$f" ] || continue
            base="$(basename "$f")"
            case "$base" in
                "$DESKTOP_FILE_NAME"|"$DESKTOP_FILE_NAME_DEVICE")
                    continue
                    ;;
            esac

            # Remove old ATAK pipeline shortcuts from prior installer naming.
            if grep -q "run_atak_pipeline" "$f" 2>/dev/null; then
                rm -f "$f"
            fi
        done
    done
}

# Remove the extracted bundle after install (app lives in INSTALL_ROOT). Safe: cd out first.
maybe_remove_source_extract() {
    if [ "$SOURCE_ROOT" = "$INSTALL_ROOT" ]; then
        return 0
    fi
    local src_p inst_p downloads_p
    src_p="$(cd "$SOURCE_ROOT" && pwd -P 2>/dev/null)" || return 0
    inst_p="$(cd "$INSTALL_ROOT" && pwd -P 2>/dev/null)" || return 0
    if [ "$src_p" = "$inst_p" ]; then
        return 0
    fi

    downloads_p=""
    if [ -d "$HOME/Downloads" ]; then
        downloads_p="$(cd "$HOME/Downloads" && pwd -P 2>/dev/null)" || true
    fi

    local do_remove=0
    if [ -n "$downloads_p" ] && [[ "$src_p" == "$downloads_p"/* ]]; then
        case "$(basename "$src_p")" in
            atak-imagery)
                do_remove=1
                ;;
        esac
    fi

    if [ "${ATAK_INSTALL_KEEP_SOURCE:-}" = "1" ]; then
        echo "  Setup folder kept (ATAK_INSTALL_KEEP_SOURCE=1): $src_p"
        return 0
    fi

    if [ "$do_remove" -eq 0 ] && command -v zenity >/dev/null 2>&1; then
        if zenity --question \
            --title="ATAK Imagery installer" \
            --text="Remove the folder you ran the installer from?\n\n$src_p\n\nThe application is installed in:\n$inst_p" \
            --width=480 2>/dev/null; then
            do_remove=1
        else
            echo "  Kept setup folder: $src_p"
            return 0
        fi
    elif [ "$do_remove" -eq 0 ]; then
        echo "  You may delete the setup folder when finished:"
        echo "    rm -rf '$src_p'"
        return 0
    fi

    if [ "$do_remove" -eq 1 ]; then
        cd "$HOME" 2>/dev/null || cd / || true
        rm -rf "$src_p"
        echo "  Removed setup folder: $src_p"
        REMOVED_SOURCE_EXTRACT=1
    fi
}

REMOVED_SOURCE_EXTRACT=0

echo "[6/7] Creating desktop shortcuts..."
cleanup_legacy_shortcuts
write_desktop_shortcut "$DESKTOP_FILE_NAME" "$APP_NAME" "Download maps and build packages for ATAK" \
    "/bin/bash -lc 'cd \"$ROOT\" && nohup \"$ROOT/run_atak_pipeline.sh\" >/tmp/atak_pipeline_launcher.log 2>&1 &'"
write_desktop_shortcut "$DESKTOP_FILE_NAME_DEVICE" "$APP_NAME_DEVICE" "Install ATAK and plugin on Android, then run the map pipeline" \
    "/bin/bash -lc 'cd \"$ROOT\" && nohup \"$ROOT/run_atak_pipeline_with_device.sh\" >/tmp/atak_pipeline_device_launcher.log 2>&1 &'"

if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database "$HOME/.local/share/applications" >/dev/null 2>&1 || true
fi

maybe_remove_source_extract

echo "[7/7] Installation complete."
echo "  Programs installed under: $ROOT"
if [ "${REMOVED_SOURCE_EXTRACT:-0}" -eq 1 ]; then
    echo "  Extracted setup folder under Downloads was removed."
else
    echo "  Re-run this installer from a new release zip to update."
fi
echo "  Desktop shortcuts: \"$DESKTOP_FILE_NAME\" and \"$DESKTOP_FILE_NAME_DEVICE\"."

ZENITY_EXTRA=""
if [ "${REMOVED_SOURCE_EXTRACT:-0}" -eq 1 ]; then
    ZENITY_EXTRA="\n\nThe extracted setup folder was removed (the app stays in the install location above)."
fi

if command -v zenity >/dev/null 2>&1; then
    zenity --info \
        --title="ATAK Imagery installer" \
        --text="Installation complete.\n\nApplications are installed in:\n$ROOT\n\nThis folder is separate from where you extracted the zip.${ZENITY_EXTRA}\n\nThe first time, open ATAK Device Installer (USB install of ATAK and your plugin, then the map tools).\n\nLater, for more imagery: ATAK Imagery Downloader." \
        --width=520 >/dev/null 2>&1 || true
fi
