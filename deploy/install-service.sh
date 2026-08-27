#!/usr/bin/env bash
#
# Install crypto-arb-engine as a systemd service.
#
#   bash deploy/install-service.sh              # mode from .env, or paper
#   bash deploy/install-service.sh monitor      # force a mode
#   bash deploy/install-service.sh --dry-run    # print the unit, change nothing
#
# What this fixes: run via `nohup ... & disown`, the bot dies for good on a
# crash, an OOM kill, or a reboot, and stays down until someone notices. Under
# systemd it comes back on its own, and `MemoryMax` keeps a runaway allocation
# from taking the whole droplet (SSH included) down with it.

set -euo pipefail

SERVICE_NAME="kbot"
UNIT_PATH="/etc/systemd/system/${SERVICE_NAME}.service"

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TEMPLATE="${REPO_DIR}/deploy/${SERVICE_NAME}.service"

DRY_RUN=0
MODE_ARG=""
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=1 ;;
    monitor|paper|live) MODE_ARG="$arg" ;;
    -h|--help) sed -n '2,12p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) echo "unknown argument: $arg (expected monitor|paper|live|--dry-run)" >&2; exit 2 ;;
  esac
done

die() { echo "error: $*" >&2; exit 1; }

# --- Preconditions -----------------------------------------------------------

[[ -f "$TEMPLATE" ]] || die "missing unit template at $TEMPLATE"

VENV_PYTHON="${REPO_DIR}/.venv/bin/python"
# The unit calls this interpreter by absolute path rather than sourcing the
# venv, because `source .venv/bin/activate` is an interactive-shell-ism that
# silently no-ops in a service context -- and a bot started without its venv
# doesn't fail loudly, it fails as "ModuleNotFoundError: ccxt" in a log nobody
# is reading. Fail here instead, where the message is in front of a human.
[[ -x "$VENV_PYTHON" ]] || die "no venv interpreter at $VENV_PYTHON
  create it first:  cd $REPO_DIR && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt"

[[ -f "${REPO_DIR}/main.py" ]] || die "no main.py in $REPO_DIR -- is this the repo root?"

# --- Mode --------------------------------------------------------------------

# Precedence: explicit argument > ARB_MODE already exported > ARB_MODE in .env
# > paper. Baking the mode into the unit is deliberate: a mode switched from
# the dashboard lives in process memory only, so a restart always comes back in
# *this* mode. Nothing can silently resurrect itself in `live`.
resolve_mode() {
  if [[ -n "$MODE_ARG" ]]; then echo "$MODE_ARG"; return; fi
  if [[ -n "${ARB_MODE:-}" ]]; then echo "$ARB_MODE"; return; fi
  if [[ -f "${REPO_DIR}/.env" ]]; then
    local from_env
    from_env="$(grep -E '^[[:space:]]*ARB_MODE[[:space:]]*=' "${REPO_DIR}/.env" 2>/dev/null \
      | tail -n1 | cut -d= -f2- | tr -d '"'\''[:space:]')" || true
    if [[ -n "$from_env" ]]; then echo "$from_env"; return; fi
  fi
  echo "paper"
}

ARB_MODE_RESOLVED="$(resolve_mode)"
case "$ARB_MODE_RESOLVED" in
  monitor|paper|live) ;;
  *) die "ARB_MODE resolved to '$ARB_MODE_RESOLVED'; expected monitor, paper, or live" ;;
esac

# --- Memory limits -----------------------------------------------------------

# Sized from the host's real RAM. Leave the OS a reserve it can't be squeezed
# out of: the failure this guards against isn't the bot dying (systemd restarts
# it), it's the kernel OOM killer picking an arbitrary victim -- last time it
# took sshd, which turns a bot outage into "the droplet is unreachable".
mem_total_kb="$(awk '/^MemTotal:/ {print $2}' /proc/meminfo 2>/dev/null || echo 0)"
if [[ "$mem_total_kb" -le 0 ]]; then
  MEMORY_MAX="512M"
  MEMORY_HIGH="435M"
else
  total_mb=$(( mem_total_kb / 1024 ))
  reserve_mb=$(( total_mb * 15 / 100 ))
  (( reserve_mb < 128 )) && reserve_mb=128
  max_mb=$(( total_mb - reserve_mb ))
  (( max_mb < 192 )) && max_mb=192          # below this it can't load markets at all
  # On a large host, "everything minus a reserve" is not a containment limit at
  # all -- the engine's steady state is a few hundred MB, so anything past this
  # is a leak we want capped rather than headroom we want to hand out.
  (( max_mb > 2048 )) && max_mb=2048
  high_mb=$(( max_mb * 85 / 100 ))          # start reclaiming before the hard cap
  MEMORY_MAX="${max_mb}M"
  MEMORY_HIGH="${high_mb}M"
fi

# --- Render ------------------------------------------------------------------

RENDERED="$(sed \
  -e "s|__REPO_DIR__|${REPO_DIR}|g" \
  -e "s|__ARB_MODE__|${ARB_MODE_RESOLVED}|g" \
  -e "s|__MEMORY_MAX__|${MEMORY_MAX}|g" \
  -e "s|__MEMORY_HIGH__|${MEMORY_HIGH}|g" \
  "$TEMPLATE")"

if grep -q '__[A-Z_]*__' <<<"$RENDERED"; then
  die "unit still contains unsubstituted placeholders:
$(grep -o '__[A-Z_]*__' <<<"$RENDERED" | sort -u)"
fi

echo "repo:   $REPO_DIR"
echo "python: $VENV_PYTHON"
echo "mode:   $ARB_MODE_RESOLVED"
echo "memory: MemoryHigh=$MEMORY_HIGH MemoryMax=$MEMORY_MAX (host has $(( mem_total_kb / 1024 ))M)"
echo

if [[ "$DRY_RUN" -eq 1 ]]; then
  echo "--- ${UNIT_PATH} (dry run, not written) ---"
  echo "$RENDERED"
  exit 0
fi

# --- Install -----------------------------------------------------------------

SUDO=""
if [[ "$(id -u)" -ne 0 ]]; then
  command -v sudo >/dev/null 2>&1 || die "not root and no sudo available; re-run as root"
  SUDO="sudo"
fi

command -v systemctl >/dev/null 2>&1 || die "systemctl not found -- this host doesn't use systemd"

# Stop whatever is running now, so the new service doesn't collide with it on
# the dashboard and /metrics ports. Both paths matter: a previous run of this
# script (service), and the `nohup python main.py &` the README used to
# recommend (bare process, invisible to systemctl).
if $SUDO systemctl list-unit-files "${SERVICE_NAME}.service" --no-legend 2>/dev/null | grep -q .; then
  echo "stopping existing ${SERVICE_NAME}.service..."
  $SUDO systemctl stop "${SERVICE_NAME}.service" || true
fi

# `[p]ython` keeps pgrep from matching its own command line.
stray_pids="$(pgrep -f '[p]ython main.py' || true)"
if [[ -n "$stray_pids" ]]; then
  echo "stopping stray manually-started process(es): $stray_pids"
  # shellcheck disable=SC2086
  kill $stray_pids || true
  for _ in $(seq 1 15); do
    pgrep -f '[p]ython main.py' >/dev/null || break
    sleep 1
  done
  # shellcheck disable=SC2086
  pgrep -f '[p]ython main.py' >/dev/null && kill -9 $(pgrep -f '[p]ython main.py') || true
fi
# Give the kernel a moment to release the listening sockets, or the fresh
# start races the old process and dies with "Address already in use".
sleep 3

echo "writing ${UNIT_PATH}..."
printf '%s\n' "$RENDERED" | $SUDO tee "$UNIT_PATH" >/dev/null

$SUDO systemctl daemon-reload
$SUDO systemctl enable "${SERVICE_NAME}.service" >/dev/null
$SUDO systemctl restart "${SERVICE_NAME}.service"

# --- Verify ------------------------------------------------------------------

# Startup is slow (load_markets across 20 venues), so "active" right away isn't
# the bar -- "didn't fall over during startup" is.
for _ in $(seq 1 20); do
  sleep 1
  $SUDO systemctl is-active --quiet "${SERVICE_NAME}.service" || break
done

echo
if $SUDO systemctl is-active --quiet "${SERVICE_NAME}.service"; then
  echo "${SERVICE_NAME} is running in ${ARB_MODE_RESOLVED} mode, enabled at boot."
  echo
  echo "  systemctl status ${SERVICE_NAME}      # is it up?"
  echo "  journalctl -u ${SERVICE_NAME} -f      # live logs (Ctrl-C to stop watching)"
  echo "  systemctl restart ${SERVICE_NAME}     # after a git pull"
  echo "  systemctl stop ${SERVICE_NAME}        # stop it (stays stopped until started)"
  echo
  echo "Rebooting the droplet is now safe -- the bot starts itself again."
else
  echo "${SERVICE_NAME} failed to stay up. Last 40 log lines:" >&2
  $SUDO journalctl -u "${SERVICE_NAME}" -n 40 --no-pager >&2 || true
  exit 1
fi
