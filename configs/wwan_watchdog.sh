#!/usr/bin/env bash
set -euo pipefail

NAME="${NAME:?NAME is required}"
TARGET_IMEI="${TARGET_IMEI:?TARGET_IMEI is required}"
APN="${APN:?APN is required}"
IFACE="${IFACE:?IFACE is required}"

MTU="${MTU:-1400}"
ROUTE_METRIC="${ROUTE_METRIC:-100}"

PING_IP="${PING_IP:-1.1.1.1}"
PING_COUNT="${PING_COUNT:-2}"
PING_TIMEOUT="${PING_TIMEOUT:-2}"

FAIL_THRESHOLD="${FAIL_THRESHOLD:-3}"         # recover after N consecutive failed checks
RECOVERY_TRIES="${RECOVERY_TRIES:-6}"         # attempts inside one recovery run
RECOVERY_SLEEP="${RECOVERY_SLEEP:-8}"         # seconds between recovery attempts
RECOVERY_COOLDOWN="${RECOVERY_COOLDOWN:-180}" # minimum seconds between full recovery runs

STATE_DIR="/var/lib/wwan-watchdog"
RUN_DIR="/run/wwan-watchdog"
STATE_FILE="${STATE_DIR}/${NAME}.state"
LOCK_FILE="${RUN_DIR}/${NAME}.lock"

mkdir -p "$STATE_DIR" "$RUN_DIR"

log() {
  echo "[$(date -Is)] [$NAME] $*" >&2
}

# lock so timer overlap cannot cause concurrent recovery
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
  log "Another watchdog instance is already running; exiting."
  exit 0
fi

get_state_value() {
  local key="$1"
  [[ -f "$STATE_FILE" ]] || return 0
  sed -n "s/^${key}=//p" "$STATE_FILE" | tail -n1
}

set_state() {
  local failures="$1"
  local last_recovery="$2"
  cat > "$STATE_FILE" <<EOF
failures=${failures}
last_recovery=${last_recovery}
EOF
}

get_failures() {
  local v
  v="$(get_state_value failures || true)"
  [[ "$v" =~ ^[0-9]+$ ]] && echo "$v" || echo 0
}

get_last_recovery() {
  local v
  v="$(get_state_value last_recovery || true)"
  [[ "$v" =~ ^[0-9]+$ ]] && echo "$v" || echo 0
}

reset_failures() {
  set_state 0 "$(get_last_recovery)"
}

record_failure() {
  local f
  f="$(get_failures)"
  f=$((f + 1))
  set_state "$f" "$(get_last_recovery)"
  echo "$f"
}

record_recovery_now() {
  local now
  now="$(date +%s)"
  set_state 0 "$now"
}

get_modem_id_by_imei() {
  local ids id imei
  ids="$(mmcli -L 2>/dev/null | grep -oE 'Modem/[0-9]+' | cut -d/ -f2 || true)"

  for id in $ids; do
    imei="$(
      mmcli -m "$id" -K 2>/dev/null \
        | sed -n 's/^modem.generic.equipment-identifier=//p' \
        | head -n1 \
        | tr -d "'\"[:space:]"
    )"

    if [[ -z "$imei" ]]; then
      imei="$(
        mmcli -m "$id" 2>/dev/null \
          | sed -nE "s/.*equipment id:[[:space:]]*'?([0-9]+)'?.*/\1/p" \
          | head -n1
      )"
    fi

    if [[ -n "$imei" && "$imei" == "$TARGET_IMEI" ]]; then
      echo "$id"
      return 0
    fi
  done

  return 1
}

get_bearer_id() {
  local modem_id="$1"
  mmcli -m "$modem_id" 2>/dev/null | grep -oE 'Bearer/[0-9]+' | tail -n1 | cut -d/ -f2
}

bearer_value() {
  local bearer_id="$1"
  local key="$2"
  mmcli -b "$bearer_id" 2>/dev/null \
    | sed -nE "s/.*${key}:[[:space:]]*//p" \
    | head -n1 \
    | sed -E 's/[[:space:]]+$//'
}

iface_has_ipv4() {
  ip -4 -o addr show dev "$IFACE" scope global 2>/dev/null | grep -q 'inet '
}

iface_ipv4() {
  ip -4 -o addr show dev "$IFACE" scope global 2>/dev/null | awk '{print $4}' | head -n1
}

health_check() {
  local modem_id

  if ! modem_id="$(get_modem_id_by_imei)"; then
    log "Health check failed: modem with IMEI ${TARGET_IMEI} not present."
    return 1
  fi

  if ! ip link show "$IFACE" >/dev/null 2>&1; then
    log "Health check failed: interface ${IFACE} does not exist."
    return 1
  fi

  if ! iface_has_ipv4; then
    log "Health check failed: interface ${IFACE} has no IPv4 address."
    return 1
  fi

  if ! ping -I "$IFACE" -c "$PING_COUNT" -W "$PING_TIMEOUT" "$PING_IP" >/dev/null 2>&1; then
    log "Health check failed: ping via ${IFACE} to ${PING_IP} failed."
    return 1
  fi

  log "Healthy: modem ${modem_id}, ${IFACE} has IP $(iface_ipv4), ping via ${IFACE} OK."
  return 0
}

recover_modem() {
  local last now modem_id bearer_id addr prefix gw attempt

  now="$(date +%s)"
  last="$(get_last_recovery)"

  if (( now - last < RECOVERY_COOLDOWN )); then
    log "Recovery skipped: still in cooldown ($((RECOVERY_COOLDOWN - (now - last)))s left)."
    return 1
  fi

  log "Starting recovery."

  for ((attempt=1; attempt<=RECOVERY_TRIES; attempt++)); do
    modem_id="$(get_modem_id_by_imei || true)"

    if [[ -z "$modem_id" ]]; then
      log "Recovery attempt ${attempt}/${RECOVERY_TRIES}: modem not present yet."
      sleep "$RECOVERY_SLEEP"
      continue
    fi

    log "Recovery attempt ${attempt}/${RECOVERY_TRIES}: using modem ${modem_id}."

    mmcli -m "$modem_id" --simple-disconnect >/dev/null 2>&1 || true
    mmcli -m "$modem_id" --disable >/dev/null 2>&1 || true
    sleep 2
    mmcli -m "$modem_id" --enable >/dev/null 2>&1 || true
    sleep 8

    mmcli -m "$modem_id" --simple-connect="apn=${APN},ip-type=ipv4" >/dev/null 2>&1 || true
    sleep 3

    bearer_id="$(get_bearer_id "$modem_id" || true)"
    if [[ -z "$bearer_id" ]]; then
      log "No bearer found after connect."
      sleep "$RECOVERY_SLEEP"
      continue
    fi

    addr="$(bearer_value "$bearer_id" address)"
    prefix="$(bearer_value "$bearer_id" prefix)"
    gw="$(bearer_value "$bearer_id" gateway)"

    if [[ -z "$addr" || -z "$prefix" || -z "$gw" ]]; then
      log "Bearer ${bearer_id} missing address/prefix/gateway."
      mmcli -b "$bearer_id" || true
      sleep "$RECOVERY_SLEEP"
      continue
    fi

    log "Configuring ${IFACE}: ${addr}/${prefix}, gw=${gw}"

    ip link set "$IFACE" mtu "$MTU" || true
    ip link set "$IFACE" up || true
    ip addr flush dev "$IFACE" || true
    ip addr add "${addr}/${prefix}" dev "$IFACE" || true

    # Important: do NOT delete the global default route.
    # Just install/refresh a default route for this interface with its own metric.
    ip route replace default via "$gw" dev "$IFACE" metric "$ROUTE_METRIC" || true

    if ping -I "$IFACE" -c "$PING_COUNT" -W "$PING_TIMEOUT" "$PING_IP" >/dev/null 2>&1; then
      log "Recovery succeeded."
      record_recovery_now
      return 0
    fi

    log "Recovery attempt ${attempt}/${RECOVERY_TRIES} still unhealthy after reconnect."
    sleep "$RECOVERY_SLEEP"
  done

  log "Recovery failed after ${RECOVERY_TRIES} attempts."
  return 1
}

main() {
  local failures

  if health_check; then
    reset_failures
    exit 0
  fi

  failures="$(record_failure)"
  log "Consecutive failures: ${failures}/${FAIL_THRESHOLD}"

  if (( failures < FAIL_THRESHOLD )); then
    log "Threshold not yet reached. No recovery performed."
    exit 0
  fi

  if recover_modem; then
    exit 0
  fi

  exit 1
}

main "$@"