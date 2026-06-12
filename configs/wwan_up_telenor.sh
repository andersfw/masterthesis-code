#!/usr/bin/env bash
# wwan_up_telenor.sh
set -euo pipefail

# TELENOR defaults
APN="${APN:-telenor.smart}"
IFACE="${IFACE:-wwan1}"
MTU="${MTU:-1400}"

# Pin THIS script to the modem you intend to insert the Telenor SIM into.
TARGET_IMEI="${TARGET_IMEI:-868371053827909}"

TRIES="${TRIES:-30}"         # total recovery attempts (only used when unhealthy)
SLEEP="${SLEEP:-6}"          # seconds between attempts

PING_IP="${PING_IP:-8.8.8.8}"
PING_HOST="${PING_HOST:-vg.no}"
PING_COUNT="${PING_COUNT:-3}"

# IMPORTANT: log to STDERR so command substitutions never capture log lines.
log(){ echo "[$(date -Is)] $*" >&2; }

get_modem() {
  local ids id imei
  ids="$(mmcli -L 2>/dev/null | grep -oE 'Modem/[0-9]+' | cut -d/ -f2 || true)"

  if [[ -n "${TARGET_IMEI}" ]]; then
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

    log "No modem matched TARGET_IMEI=${TARGET_IMEI}"
    return 1
  fi

  echo "$ids" | head -n1
}

get_bearer() {
  mmcli -m "$1" 2>/dev/null | grep -oE 'Bearer/[0-9]+' | tail -n1 | cut -d/ -f2
}

bval() {
  local bid="$1" key="$2"
  mmcli -b "$bid" 2>/dev/null | sed -nE "s/.*${key}:[[:space:]]*//p" | head -n1 | sed -E 's/[[:space:]]+$//'
}

if ping -I "$IFACE" -c 2 "$PING_IP" >/dev/null 2>&1 && ping -c 2 "$PING_HOST" >/dev/null 2>&1; then
  log "Healthy: $PING_IP OK via $IFACE and $PING_HOST OK (DNS OK). Leaving modem untouched."
  exit 0
fi

MODEM=""
while [[ -z "$MODEM" ]]; do
  MODEM="$(get_modem || true)"

  if [[ -n "$MODEM" && ! "$MODEM" =~ ^[0-9]+$ ]]; then
    log "Invalid MODEM value: '$MODEM' (expected numeric modem id). Waiting 10s..."
    MODEM=""
    sleep 10
    continue
  fi

  if [[ -z "$MODEM" ]]; then
    log "No matching modem for TARGET_IMEI=${TARGET_IMEI} yet; waiting 10s..."
    sleep 10
  fi
done
log "Using modem $MODEM (TARGET_IMEI=${TARGET_IMEI})"

for ((i=1; i<=TRIES; i++)); do
  log "Attempt $i/$TRIES"

  sudo mmcli -m "$MODEM" --simple-disconnect >/dev/null 2>&1 || true
  sudo mmcli -m "$MODEM" --disable >/dev/null 2>&1 || true
  sleep 2
  sudo mmcli -m "$MODEM" --enable  >/dev/null 2>&1 || true
  sleep 8

  sudo mmcli -m "$MODEM" --simple-connect="apn=${APN},ip-type=ipv4" >/dev/null 2>&1 || true
  sleep 2

  BEARER="$(get_bearer "$MODEM" || true)"
  if [[ -z "$BEARER" ]]; then
    log "No bearer found yet; sleeping ${SLEEP}s..."
    sleep "$SLEEP"
    continue
  fi

  ADDR="$(bval "$BEARER" "address")"
  PREFIX="$(bval "$BEARER" "prefix")"
  GW="$(bval "$BEARER" "gateway")"

  if [[ -z "$ADDR" || -z "$PREFIX" || -z "$GW" ]]; then
    log "Bearer $BEARER missing address/prefix/gateway (dumping bearer):"
    mmcli -b "$BEARER" || true
    sleep "$SLEEP"
    continue
  fi

  log "Bearer $BEARER: ${ADDR}/${PREFIX} gw=${GW}"

  sudo ip link set "$IFACE" mtu "$MTU" || true
  sudo ip addr flush dev "$IFACE" || true
  sudo ip link set "$IFACE" up || true
  sudo ip addr add "${ADDR}/${PREFIX}" dev "$IFACE"

  sudo ip route del default 2>/dev/null || true
  sudo ip route add default via "$GW" dev "$IFACE"

  if ping -I "$IFACE" -c "$PING_COUNT" "$PING_IP" >/dev/null 2>&1; then
    log "Ping to $PING_IP OK via $IFACE"
  else
    log "Ping to $PING_IP FAILED; sleeping ${SLEEP}s..."
    sleep "$SLEEP"
    continue
  fi

  if ping -c "$PING_COUNT" "$PING_HOST" >/dev/null 2>&1; then
    log "Ping to $PING_HOST OK (DNS OK)"
    exit 0
  else
    log "Ping to $PING_HOST FAILED (DNS or route); sleeping ${SLEEP}s..."
    sleep "$SLEEP"
    continue
  fi
done

log "FAILED after $TRIES attempts."
exit 1