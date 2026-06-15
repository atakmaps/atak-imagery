#!/usr/bin/env bash
# Collect ATAK Device Installer / ATAK update debug info for support.
#
# Usage (send this file to the user, or they run from an installed copy):
#   chmod +x collect_atak_install_debug.sh
#   ./collect_atak_install_debug.sh
#
# Writes: ~/Desktop/atak-device-installer-debug-YYYYMMDD_HHMMSS.log
#         (falls back to ~/ if Desktop is missing)
#
# Safe to run while Device Installer is open or stuck. No sudo required.
# Does NOT install or change ATAK on the phone (read-only adb queries only).

set -u

DEFAULT_INSTALL="${XDG_DATA_HOME:-$HOME/.local/share}/atak-imagery"
INSTALL_ROOT="${ATAK_PIPELINE_HOME:-$DEFAULT_INSTALL}"

DESKTOP_DIR="${XDG_DESKTOP_DIR:-$HOME/Desktop}"
if [ ! -d "$DESKTOP_DIR" ]; then
    DESKTOP_DIR="$HOME"
fi

TS="$(date +%Y%m%d_%H%M%S)"
OUT="${DESKTOP_DIR}/atak-device-installer-debug-${TS}.log"

exec > >(tee "$OUT") 2>&1

section() {
    echo
    echo "======================================================================"
    echo "== $1"
    echo "======================================================================"
}

run_timeout() {
    local secs="$1"
    local label="$2"
    shift 2
    echo "--- $label (timeout ${secs}s) ---"
    local start end rc
    start=$(date +%s)
    if timeout "$secs" "$@" 2>&1; then
        rc=0
    else
        rc=$?
    fi
    end=$(date +%s)
    if [ "$rc" -eq 124 ]; then
        echo ">>> TIMED OUT after ${secs}s (this often explains a UI lock-up)"
    elif [ "$rc" -ne 0 ]; then
        echo ">>> exit code: $rc"
    fi
    echo ">>> elapsed: $((end - start))s"
}

redact_env_file() {
    local f="$1"
    if [ ! -f "$f" ]; then
        echo "(missing: $f)"
        return
    fi
    sed -E \
        -e 's/^(ATAK_DEPLOY_API_TOKEN=).*/\1(redacted)/' \
        -e 's/^(GITHUB_TOKEN=).*/\1(redacted)/' \
        -e 's/^(ATAK_GITHUB_TOKEN=).*/\1(redacted)/' \
        "$f"
}

tail_file() {
    local f="$1"
    local n="${2:-200}"
    if [ -f "$f" ]; then
        echo "--- tail -${n} $f ---"
        tail -n "$n" "$f"
    else
        echo "(missing: $f)"
    fi
}

section "ATAK Device Installer debug bundle"
echo "Generated: $(date -Is 2>/dev/null || date)"
echo "Output file: $OUT"
echo "User: $(whoami)"
echo "Host: $(hostname 2>/dev/null || echo unknown)"
echo "Install root: $INSTALL_ROOT"

section "System"
run_timeout 5 "uname" uname -a
if [ -r /etc/os-release ]; then
    echo "--- /etc/os-release ---"
    cat /etc/os-release
fi
run_timeout 5 "lsb_release" bash -lc 'command -v lsb_release >/dev/null && lsb_release -a'
echo "--- disk free ---"
df -h "$HOME" "$INSTALL_ROOT" /tmp 2>/dev/null || df -h
echo "--- memory ---"
free -h 2>/dev/null || true
echo "--- groups (need plugdev for USB adb on some distros) ---"
id
groups 2>/dev/null || true

section "Pipeline install tree"
for f in VERSION deploy.env deploy.env.example run_atak_pipeline.sh run_atak_pipeline_with_device.sh; do
    p="$INSTALL_ROOT/$f"
    if [ -e "$p" ]; then
        echo "--- $p ---"
        ls -la "$p"
    else
        echo "(missing: $p)"
    fi
done
if [ -f "$INSTALL_ROOT/VERSION" ]; then
    echo "Installed VERSION: $(tr -d '\r\n' < "$INSTALL_ROOT/VERSION")"
fi

section "deploy.env (secrets redacted)"
if [ -f "$INSTALL_ROOT/deploy.env" ]; then
    redact_env_file "$INSTALL_ROOT/deploy.env"
else
    echo "(no deploy.env — installer may use deploy.env.example defaults only)"
    redact_env_file "$INSTALL_ROOT/deploy.env.example"
fi

# Load deploy.env for downstream checks (same as Device Installer launcher).
if [ -f "$INSTALL_ROOT/deploy.env" ]; then
    set -a
    # shellcheck disable=SC1090
    . "$INSTALL_ROOT/deploy.env"
    set +a
fi

MANIFEST_URL="${ATAK_DEPLOY_MANIFEST_URL:-http://31.220.30.74/atak/manifest.json}"
ATAK_PKG="${ATAK_PACKAGE_NAME:-com.atakmap.app.civ}"

section "Desktop shortcuts"
for d in "$HOME/.local/share/applications" "$HOME/Desktop"; do
    for name in "ATAK Device Installer.desktop" "ATAK Imagery Downloader.desktop"; do
        f="$d/$name"
        if [ -f "$f" ]; then
            echo "--- $f ---"
            cat "$f"
        fi
    done
done

section "Python / venv"
run_timeout 10 "python3" bash -lc 'command -v python3 && python3 --version'
if [ -x "$INSTALL_ROOT/.venv/bin/python" ]; then
    run_timeout 10 "install venv python" "$INSTALL_ROOT/.venv/bin/python" --version
    run_timeout 15 "tkinter import" "$INSTALL_ROOT/.venv/bin/python" -c "import tkinter; print('tkinter OK')"
    run_timeout 15 "requests import" "$INSTALL_ROOT/.venv/bin/python" -c "import requests; print('requests', requests.__version__)"
else
    echo "(no venv at $INSTALL_ROOT/.venv)"
fi

section "adb"
export PATH="/usr/local/bin:/usr/bin:/bin${PATH:+:$PATH}"
if [ -d "$HOME/Android/Sdk/platform-tools" ]; then
    export PATH="$HOME/Android/Sdk/platform-tools:$PATH"
fi
run_timeout 10 "adb version" bash -lc 'command -v adb && adb version'
run_timeout 15 "adb kill-server" adb kill-server
run_timeout 30 "adb start-server" adb start-server
run_timeout 20 "adb devices -l" adb devices -l

DEVICES=()
while IFS= read -r line; do
    case "$line" in
        ????????????[[:space:]]device*)
            DEVICES+=("${line%%[[:space:]]*}") ;;
    esac
done < <(adb devices 2>/dev/null || true)

if [ "${#DEVICES[@]}" -eq 0 ]; then
    echo ">>> No adb device in 'device' state. Lock-ups during ATAK install often mean:"
    echo ">>>   - phone not authorized (check USB debugging prompt on phone)"
    echo ">>>   - cable/port issue"
    echo ">>>   - wrong USB mode (use File transfer / MTP, not charge-only)"
else
    for ser in "${DEVICES[@]}"; do
        section "Device $ser"
        run_timeout 15 "get-state" adb -s "$ser" get-state
        run_timeout 20 "model" adb -s "$ser" shell getprop ro.product.model
        run_timeout 20 "android version" adb -s "$ser" shell getprop ro.build.version.release
        run_timeout 30 "installed ATAK path" adb -s "$ser" shell pm path "$ATAK_PKG"
        run_timeout 45 "ATAK package summary" bash -c "adb -s \"$ser\" shell dumpsys package \"$ATAK_PKG\" 2>/dev/null | grep -E 'versionName|versionCode|firstInstallTime|lastUpdateTime|signatures' | head -20"
        run_timeout 30 "pm list packages" adb -s "$ser" shell pm list packages -f "$ATAK_PKG"
        run_timeout 20 "usb dumpsys (head)" bash -c "adb -s \"$ser\" shell dumpsys usb 2>/dev/null | head -40"
    done
fi

section "Deploy manifest (network)"
echo "Manifest URL: $MANIFEST_URL"
run_timeout 30 "curl manifest" curl -fsSL -H 'User-Agent: ATAK-Install-Debug/1.0' -w '\nHTTP %{http_code} size=%{size_download} time=%{time_total}s\n' "$MANIFEST_URL"

MANIFEST_JSON="$(mktemp)"
if timeout 30 curl -fsSL -H 'User-Agent: ATAK-Install-Debug/1.0' -o "$MANIFEST_JSON" "$MANIFEST_URL" 2>/dev/null; then
    echo "--- parsed manifest fields ---"
    if command -v python3 >/dev/null 2>&1; then
        python3 - "$MANIFEST_JSON" "$MANIFEST_URL" <<'PY'
import json, sys, urllib.parse
path, manifest_url = sys.argv[1], sys.argv[2]
data = json.load(open(path, encoding="utf-8"))
ver = data.get("atak_version", "")
rel = data.get("atak_apk_url", "")
print("atak_version:", ver)
print("atak_apk_url (raw):", rel)
full = urllib.parse.urljoin(manifest_url, str(rel))
print("atak_apk_url (resolved):", full)
PY
        APK_URL="$(python3 - "$MANIFEST_JSON" "$MANIFEST_URL" <<'PY'
import json, sys, urllib.parse
data = json.load(open(sys.argv[1], encoding="utf-8"))
print(urllib.parse.urljoin(sys.argv[2], str(data.get("atak_apk_url", ""))))
PY
)"
        if [ -n "${APK_URL:-}" ]; then
            section "ATAK APK download probe (does not download full file)"
            run_timeout 60 "HEAD/headers ATAK APK" curl -fsSIL -H 'User-Agent: ATAK-Install-Debug/1.0' -w 'HTTP %{http_code} size=%{size_download} time=%{time_total}s\n' "$APK_URL"
            run_timeout 120 "first 1MB of APK" curl -fsSL -H 'User-Agent: ATAK-Install-Debug/1.0' -r 0-1048575 -o /dev/null -w 'First 1MB: HTTP %{http_code} bytes=%{size_download} time=%{time_total}s\n' "$APK_URL"
        fi
    else
        cat "$MANIFEST_JSON"
    fi
else
    echo ">>> Could not fetch manifest JSON"
fi
rm -f "$MANIFEST_JSON"

section "GitHub plugin repos (network)"
for repo in "${ATAK_PLUGIN_GITHUB_REPO:-atakmaps/TAK-UV-PRO}" "${ATAK_MESHCORE_PLUGIN_GITHUB_REPO:-atakmaps/TAK-MESHCORE}"; do
    [ -n "$repo" ] || continue
    api="https://api.github.com/repos/${repo}/releases/latest"
    echo "--- $repo ---"
    run_timeout 30 "github $repo" bash -c "curl -fsSL -H 'Accept: application/vnd.github+json' -H 'User-Agent: ATAK-Install-Debug/1.0' '$api' | python3 -c \"import json,sys; d=json.load(sys.stdin); print('tag:', d.get('tag_name')); print('apk assets:', [a.get('name') for a in d.get('assets',[]) if str(a.get('name','')).endswith('.apk')])\""
done

section "GitHub app update check (same API Device Installer uses)"
run_timeout 20 "github atak-imagery latest" bash -c "curl -fsSL -H 'Accept: application/vnd.github+json' -H 'User-Agent: ATAK-Install-Debug/1.0' 'https://api.github.com/repos/atakmaps/atak-imagery/releases/latest' | python3 -c \"import json,sys; d=json.load(sys.stdin); print('latest release:', d.get('tag_name')); print('assets:', [a.get('name') for a in d.get('assets',[])])\""

section "Running processes (look for stuck installer/adb)"
run_timeout 10 "related processes" bash -lc "ps aux | grep -E '[a]db|[a]tak_adb_deploy|[a]tak_downloader|[p]ython.*atak' || true"

section "Recent Device Installer logs"
LOG_DIR="$INSTALL_ROOT/scripts/logs"
ALT_LOG_DIR="$HOME/.local/share/atak-pipeline/installer_logs"
for d in "$LOG_DIR" "$ALT_LOG_DIR"; do
    if [ -d "$d" ]; then
        echo "--- log dir: $d ---"
        ls -lt "$d" 2>/dev/null | head -15
        latest="$(ls -t "$d"/atak_installer_*.log 2>/dev/null | head -1 || true)"
        if [ -n "$latest" ]; then
            tail_file "$latest" 250
        fi
    fi
done
if [ -f "$LOG_DIR/LATEST_LOG.txt" ]; then
    echo "--- LATEST_LOG.txt ---"
    cat "$LOG_DIR/LATEST_LOG.txt"
    latest_path="$(tr -d '\r\n' < "$LOG_DIR/LATEST_LOG.txt")"
    if [ -f "$latest_path" ] && [ "$latest_path" != "$latest" ]; then
        tail_file "$latest_path" 250
    fi
fi

section "Recent Imagery Downloader logs (last 80 lines)"
if [ -d "$LOG_DIR" ]; then
    latest_dl="$(ls -t "$LOG_DIR"/atak_downloader_*.log 2>/dev/null | head -1 || true)"
    if [ -n "$latest_dl" ]; then
        tail_file "$latest_dl" 80
    fi
fi

section "Kernel USB messages (last 40, may be empty without root)"
run_timeout 5 "dmesg usb" bash -lc 'dmesg 2>/dev/null | tail -40 | grep -iE "usb|adb|android" || true'

section "Done"
echo
echo "Debug log saved to:"
echo "  $OUT"
echo
echo "Email or upload this file to support. If Device Installer was frozen, run this"
echo "script again while it is stuck, then once after force-quitting the app."
