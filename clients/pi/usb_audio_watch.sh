#!/usr/bin/env bash
# Halloween-night USB audio recovery (CM108 0d8c:013c / USB PnP Sound Device).
# Usage: usb_audio_watch.sh check|recover|watch [--dry-run] [--allow-reboot] [--once] [--interval SEC]
# Auto-reboot only with --allow-reboot or CRATE_USB_ALLOW_REBOOT=1.
# Exit 3 / USB_CONTROLLER_DEAD: xHCI host died or Pi 4 VL805 hub missing — reboot required.
set -euo pipefail
CM108_VIDPID="0d8c:013c"
VL805_VIDPID="${CRATE_USB_HUB_VIDPID:-2109:3431}"
DT_MODEL_FILE="${CRATE_USB_DT_MODEL_FILE:-/proc/device-tree/model}"
BOOT_ID_FILE="${CRATE_USB_BOOT_ID_FILE:-/proc/sys/kernel/random/boot_id}"
# Kernel lines that mean the host controller itself is dead (not a missing CM108).
HC_DEAD_ERE='HC died|Host halt failed|xHCI host not responding to stop endpoint command|xHCI host controller not responding, assume dead'
XHCI_PCI="0000:01:00.0"
USBRESET_BIN="${USBRESET_BIN:-/usr/bin/usbreset}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
LOCAL_ENV="${CRATE_LOCAL_ENV:-${SCRIPT_DIR}/local.env}"
LOG_DIR="${CRATE_USB_LOG_DIR:-${REPO_ROOT}/logs}"
LOG_FILE="${CRATE_USB_LOG:-${LOG_DIR}/usb-audio-watch.log}"
STATE_DIR="${CRATE_USB_STATE_DIR:-${LOG_DIR}/usb-audio-state}"
COOLDOWN_SEC="${CRATE_USB_COOLDOWN_SEC:-120}"
MAX_REBOOTS_PER_HOUR="${CRATE_USB_MAX_REBOOTS_PER_HOUR:-2}"
LOG_MAX_BYTES="${CRATE_USB_LOG_MAX_BYTES:-2097152}"
WATCH_INTERVAL="${CRATE_USB_WATCH_INTERVAL:-90}"
CMD="${1:-}"; shift || true
ALLOW_REBOOT=0; DRY_RUN=0; WATCH_ONCE=0; INTERVAL="${WATCH_INTERVAL}"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --allow-reboot) ALLOW_REBOOT=1; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    --once) WATCH_ONCE=1; shift ;;
    --interval) INTERVAL="${2:-90}"; shift 2 ;;
    -h|--help) sed -n '2,5p' "$0"; exit 0 ;;
    *) echo "Unknown option: $1" >&2; exit 2 ;;
  esac
done
mkdir -p "${LOG_DIR}" "${STATE_DIR}"
load_local_env() {
  if [[ -f "${LOCAL_ENV}" ]]; then
    set -a
    # shellcheck disable=SC1091
    source <(grep -E '^[A-Za-z_][A-Za-z0-9_]*=' "${LOCAL_ENV}" || true)
    set +a
  fi
  if [[ "${CRATE_USB_ALLOW_REBOOT:-0}" == "1" ]]; then ALLOW_REBOOT=1; fi
}
iso_now() { date -Iseconds; }
rotate_log_if_needed() {
  [[ -f "${LOG_FILE}" ]] || return 0
  local sz; sz=$(wc -c < "${LOG_FILE}" | tr -d ' ')
  if [[ "${sz}" -gt "${LOG_MAX_BYTES}" ]]; then
    local keep=$(( LOG_MAX_BYTES / 2 ))
    tail -c "${keep}" "${LOG_FILE}" > "${LOG_FILE}.tmp" && mv "${LOG_FILE}.tmp" "${LOG_FILE}"
    echo "$(iso_now) log_rotated kept_bytes=${keep}" >> "${LOG_FILE}"
  fi
}
log_line() { rotate_log_if_needed; echo "$(iso_now) $*" | tee -a "${LOG_FILE}"; }
cap() { set +e; local o; o="$("$@" 2>&1)"; local rc=$?; set -e; printf '%s' "$o"; return $rc; }
lsusb_text() { cap lsusb || true; }
arecord_text() { cap arecord -l || true; }
aplay_text() { cap aplay -l || true; }
# Full kernel log — do not tail. Rate-mismatch spam can scroll an "HC died" line
# out of the last 80 lines while the controller stays dead.
kernel_log_text() {
  local d="" rc=0
  set +e
  d="$(dmesg -T 2>&1)"
  rc=$?
  if [[ "${rc}" -ne 0 || -z "${d}" ]]; then
    d="$(journalctl -k -b --no-pager -q 2>&1)"
  fi
  set -e
  printf '%s\n' "${d}"
}
current_boot_id() {
  if [[ -r "${BOOT_ID_FILE}" ]]; then
    tr -d '[:space:]\0' < "${BOOT_ID_FILE}"
    return 0
  fi
  echo "unknown"
}
expect_vl805() {
  # Empty CRATE_USB_HUB_VIDPID disables the Pi-4-only hub check.
  [[ -n "${VL805_VIDPID}" ]] || return 1
  [[ -f "${DT_MODEL_FILE}" ]] || return 1
  local model
  model="$(tr -d '\0' < "${DT_MODEL_FILE}" 2>/dev/null || true)"
  [[ "${model}" == *"Raspberry Pi 4"* ]]
}
vl805_present() {
  [[ -n "${VL805_VIDPID}" ]] || return 1
  if grep -qiF -- "${VL805_VIDPID}" <<<"$(lsusb_text)"; then
    return 0
  fi
  local sysfs="${CRATE_USB_SYSFS_DIR:-/sys/bus/usb/devices}"
  [[ -d "${sysfs}" ]] || return 1
  local want_vid want_pid d vid pid
  want_vid="${VL805_VIDPID%%:*}"
  want_pid="${VL805_VIDPID##*:}"
  want_vid="${want_vid,,}"
  want_pid="${want_pid,,}"
  for d in "${sysfs}"/*; do
    [[ -e "${d}" ]] || continue
    [[ -f "${d}/idVendor" && -f "${d}/idProduct" ]] || continue
    vid="$(tr -d '[:space:]' < "${d}/idVendor" 2>/dev/null || true)"
    pid="$(tr -d '[:space:]' < "${d}/idProduct" 2>/dev/null || true)"
    if [[ "${vid,,}" == "${want_vid}" && "${pid,,}" == "${want_pid}" ]]; then
      return 0
    fi
  done
  return 1
}
# Echo comma-separated reasons and return 0 when the host controller is dead.
usb_controller_dead() {
  local log="" reasons=()
  log="$(kernel_log_text)"
  if grep -qiE "${HC_DEAD_ERE}" <<<"${log}"; then
    reasons+=("hc_died")
  fi
  if expect_vl805; then
    if ! vl805_present; then
      reasons+=("vl805_hub_missing")
    fi
  fi
  if [[ ${#reasons[@]} -eq 0 ]]; then
    return 1
  fi
  local IFS=','
  printf '%s\n' "${reasons[*]}"
  return 0
}
cm108_present() { grep -qiE "${CM108_VIDPID}|C-Media.*CM108|USB PnP Sound Device" <<<"$(lsusb_text)"; }
alsa_has_usb_pnp() { grep -qiE "USB PnP Sound Device|USB Audio" <<<"$(arecord_text)"$'\n'"$(aplay_text)"; }
xhci_looks_dead() {
  # If CM108 is visible, host controller is not dead for our purposes.
  if cm108_present; then return 1; fi
  local d; d="$(cap dmesg -T 2>/dev/null | tail -n 80 || true)"
  if grep -qiE 'xHCI host controller not responding|HC died|xhci_hcd.*cannot reset' <<<"$d"; then return 0; fi
  [[ ! -e "/sys/bus/pci/devices/${XHCI_PCI}" ]] && return 0
  local n; n="$(grep -Eiv 'root hub|Linux Foundation .* root hub' <<<"$(lsusb_text)" | grep -c 'Bus' || true)"
  [[ "${n}" -eq 0 ]] && return 0
  return 1
}
find_cm108_usb_bus_dev() {
  local line bus dev
  line="$(lsusb_text | grep -i "${CM108_VIDPID}" | head -n1 || true)"
  [[ -z "${line}" ]] && return 1
  bus="$(sed -n 's/^Bus \([0-9]*\) Device \([0-9]*\).*/\1/p' <<<"${line}")"
  dev="$(sed -n 's/^Bus \([0-9]*\) Device \([0-9]*\).*/\2/p' <<<"${line}")"
  [[ -z "${bus}" || -z "${dev}" ]] && return 1
  printf '%03d/%03d' "$((10#${bus}))" "$((10#${dev}))"
}
alsa_card_index_for_usb_pnp() {
  local text idx
  text="$(arecord_text)"
  idx="$(sed -n 's/^card \([0-9]*\): .*\[USB PnP Sound Device\].*/\1/p' <<<"${text}" | head -n1)"
  if [[ -z "${idx}" ]]; then
    text="$(aplay_text)"
    idx="$(sed -n 's/^card \([0-9]*\): .*\[USB PnP Sound Device\].*/\1/p' <<<"${text}" | head -n1)"
  fi
  [[ -n "${idx}" ]] && printf '%s' "${idx}"
}
summarize_health() {
  local usb cards vidpid_present=0 alsa_ok=0 why=()
  usb="$(lsusb_text | tr '\n' '|' | sed 's/|$//')"
  cards="$( { arecord_text; echo '---'; aplay_text; } | tr '\n' ';' )"
  if cm108_present; then vidpid_present=1; else why+=("cm108_absent"); fi
  if alsa_has_usb_pnp; then alsa_ok=1; else why+=("alsa_usb_pnp_absent"); fi
  if xhci_looks_dead; then why+=("xhci_dead_or_empty"); fi
  local controller_dead=0 ctrl_why="" part
  if ctrl_why="$(usb_controller_dead)"; then
    controller_dead=1
    why+=("controller_dead")
    local parts=()
    IFS=',' read -r -a parts <<<"${ctrl_why}"
    for part in "${parts[@]}"; do
      [[ -n "${part}" ]] && why+=("${part}")
    done
  fi
  local status="ok"
  if [[ "${controller_dead}" -eq 1 ]]; then
    status="controller_dead"
  elif [[ "${vidpid_present}" -ne 1 || "${alsa_ok}" -ne 1 ]]; then
    status="fail"
  fi
  [[ ${#why[@]} -eq 0 ]] && why+=("healthy")
  printf 'status=%s vidpid_present=%s alsa_usb_pnp=%s controller_dead=%s why=%s\n' \
    "${status}" "${vidpid_present}" "${alsa_ok}" "${controller_dead}" "$(IFS=,; echo "${why[*]}")"
  printf 'lsusb_summary=%s\n' "${usb}"
  printf 'alsa_summary=%s\n' "${cards}"
}
cmd_check() {
  load_local_env
  local report; report="$(summarize_health)"
  log_line "check ${report//$'\n'/ | }"
  if grep -q '^status=ok' <<<"${report}"; then
    echo "OK: USB audio healthy (CM108 ${CM108_VIDPID} / USB PnP Sound Device)"
    echo "${report}"; return 0
  fi
  if grep -q '^status=controller_dead' <<<"${report}"; then
    local reasons=""
    reasons="$(usb_controller_dead || true)"
    note_controller_dead "${reasons}"
    echo "FAIL: USB controller dead (xHCI HC died) — reboot required"
    echo "${report}"
    return 3
  fi
  echo "FAIL: USB audio unhealthy"; echo "${report}"; return 1
}
controller_dead_noted_this_boot() {
  local marker="${STATE_DIR}/controller_dead" id first
  [[ -f "${marker}" ]] || return 1
  id="$(current_boot_id)"
  [[ -n "${id}" ]] || return 1
  first="$(awk 'NR==1{print $1; exit}' "${marker}")"
  [[ "${first}" == "${id}" ]]
}
# Log once per boot. A marker from a previous boot is replaced.
note_controller_dead() {
  local reasons="${1:-}"
  local id marker first n=0 line log
  id="$(current_boot_id)"
  marker="${STATE_DIR}/controller_dead"
  if [[ -f "${marker}" ]]; then
    first="$(awk 'NR==1{print $1; exit}' "${marker}")"
    if [[ "${first}" == "${id}" ]]; then
      return 0
    fi
    rm -f "${marker}"
  fi
  printf '%s %s %s\n' "${id}" "$(iso_now)" "${reasons}" > "${marker}"
  log_line "USB_CONTROLLER_DEAD reboot_required=1 why=${reasons} boot_id=${id} hint='xHCI host controller died; usbreset/xhci rebind cannot recover it — reboot the Pi'"
  log="$(kernel_log_text)"
  while IFS= read -r line; do
    [[ -z "${line}" ]] && continue
    if grep -qiE "${HC_DEAD_ERE}" <<<"${line}"; then
      log_line "kernel| ${line}"
      n=$((n + 1))
      [[ "${n}" -ge 5 ]] && break
    fi
  done <<<"${log}"
  capture_dmesg_snapshot
}
capture_dmesg_snapshot() {
  local stamp path; stamp="$(date +%Y%m%d-%H%M%S)"
  path="${LOG_DIR}/usb-audio-dmesg-${stamp}.log"
  { echo "# dmesg snapshot ${stamp}"; dmesg -T 2>/dev/null | tail -n 40 || dmesg 2>/dev/null | tail -n 40 || true; } > "${path}"
  log_line "dmesg_snapshot path=${path}"
  log_line "dmesg_tail_begin"
  dmesg -T 2>/dev/null | tail -n 40 | while IFS= read -r line; do echo "$(iso_now) dmesg| ${line}" >> "${LOG_FILE}"; done || true
  log_line "dmesg_tail_end"
}
in_cooldown() {
  local marker="${STATE_DIR}/last_recover_ts"
  [[ -f "${marker}" ]] || return 1
  local last now delta; last="$(cat "${marker}")"; now="$(date +%s)"; delta=$(( now - last ))
  if [[ "${delta}" -lt "${COOLDOWN_SEC}" ]]; then
    log_line "cooldown_active remaining=$((COOLDOWN_SEC - delta))s"; return 0
  fi
  return 1
}
mark_recover() { date +%s > "${STATE_DIR}/last_recover_ts"; }
reboot_count_hour() {
  local f="${STATE_DIR}/reboot_timestamps"; [[ -f "${f}" ]] || { echo 0; return; }
  local cutoff now; now="$(date +%s)"; cutoff=$(( now - 3600 )); local kept=()
  while read -r ts; do [[ -z "${ts}" ]] && continue; [[ "${ts}" -ge "${cutoff}" ]] && kept+=("${ts}"); done < "${f}"
  printf '%s\n' "${kept[@]-}" > "${f}"; echo "${#kept[@]}"
}
record_reboot() { date +%s >> "${STATE_DIR}/reboot_timestamps"; }
rewrite_local_env_indices() {
  local card; card="$(alsa_card_index_for_usb_pnp || true)"
  [[ -z "${card}" ]] && { log_line "env_rewrite skipped reason=no_alsa_card"; return 0; }
  [[ -f "${LOCAL_ENV}" ]] || { log_line "env_rewrite skipped reason=no_local_env"; return 0; }
  local tmp; tmp="$(mktemp)"
  awk -v card="${card}" 'BEGIN{i=0;o=0} /^CRATE_INPUT=/{print "CRATE_INPUT=" card;i=1;next} /^CRATE_OUTPUT=/{print "CRATE_OUTPUT=" card;o=1;next} {print} END{if(!i)print "CRATE_INPUT=" card; if(!o)print "CRATE_OUTPUT=" card}' \
    "${LOCAL_ENV}" > "${tmp}"
  if [[ "${DRY_RUN}" -eq 1 ]]; then
    log_line "env_rewrite dry_run would_set CRATE_INPUT=${card} CRATE_OUTPUT=${card}"; rm -f "${tmp}"; return 0
  fi
  mv "${tmp}" "${LOCAL_ENV}"; log_line "env_rewrite set CRATE_INPUT=${card} CRATE_OUTPUT=${card}"
}
crate_client_running() { pgrep -f 'clients/pi/crate_client.py --continuous' >/dev/null 2>&1; }
restart_crate_client_if_needed() {
  if crate_client_running; then
    touch "${STATE_DIR}/crate_was_continuous"; log_line "crate_client already_running action=none"; return 0
  fi
  [[ -f "${STATE_DIR}/crate_was_continuous" ]] || { log_line "crate_client restart skipped reason=not_marked_running"; return 0; }
  [[ "${DRY_RUN}" -eq 1 ]] && { log_line "crate_client restart dry_run"; return 0; }
  (
    cd "${REPO_ROOT}"; mkdir -p logs
    pkill -f 'clients/pi/crate_client.py --continuous' 2>/dev/null || true; sleep 1
    nohup bash -c 'tail -f /dev/null | PYTHONPATH=src .venv-crate/bin/python clients/pi/crate_client.py --continuous >> logs/crate-pi.log 2>&1' >/dev/null 2>&1 &
    disown || true
  )
  sleep 1
  if crate_client_running; then log_line "crate_client restart ok"; touch "${STATE_DIR}/crate_was_continuous"
  else log_line "crate_client restart fail"; fi
}
try_usbreset() {
  local path; path="$(find_cm108_usb_bus_dev || true)"
  [[ -z "${path}" ]] && { log_line "usbreset skipped reason=cm108_not_listed"; return 1; }
  [[ -x "${USBRESET_BIN}" ]] || { log_line "usbreset skipped reason=no_binary path=${USBRESET_BIN}"; return 1; }
  log_line "usbreset attempt device=${path}"
  [[ "${DRY_RUN}" -eq 1 ]] && { log_line "usbreset dry_run device=${path}"; return 0; }
  if sudo "${USBRESET_BIN}" "/dev/bus/usb/${path}"; then sleep 2; log_line "usbreset outcome=ok device=${path}"; return 0; fi
  log_line "usbreset outcome=fail device=${path}"; return 1
}
try_xhci_rebind() {
  local driver_link unbind bind
  driver_link="/sys/bus/pci/devices/${XHCI_PCI}/driver"
  [[ -e "/sys/bus/pci/devices/${XHCI_PCI}" ]] || { log_line "xhci_rebind skipped reason=pci_device_missing"; return 1; }
  log_line "xhci_rebind attempt pci=${XHCI_PCI}"
  [[ "${DRY_RUN}" -eq 1 ]] && { log_line "xhci_rebind dry_run pci=${XHCI_PCI}"; return 0; }
  if [[ -d "${driver_link}" ]]; then
    unbind="$(readlink -f "${driver_link}")/unbind"; bind="$(readlink -f "${driver_link}")/bind"
    if [[ -w "${unbind}" ]]; then
      echo "${XHCI_PCI}" | sudo tee "${unbind}" >/dev/null || true; sleep 1
      echo "${XHCI_PCI}" | sudo tee "${bind}" >/dev/null || true; sleep 3
      log_line "xhci_rebind outcome=attempted"; return 0
    fi
  fi
  log_line "xhci_rebind outcome=no_driver_sysfs"; return 1
}
try_pci_remove_rescan() {
  log_line "pci_remove_rescan attempt pci=${XHCI_PCI} NOTE=may_reboot_pi"
  [[ "${DRY_RUN}" -eq 1 ]] && { log_line "pci_remove_rescan dry_run pci=${XHCI_PCI}"; return 0; }
  if [[ -e "/sys/bus/pci/devices/${XHCI_PCI}/remove" ]]; then
    echo 1 | sudo tee "/sys/bus/pci/devices/${XHCI_PCI}/remove" >/dev/null || true; sleep 2
    echo 1 | sudo tee /sys/bus/pci/rescan >/dev/null || true; sleep 5
    log_line "pci_remove_rescan outcome=attempted"; return 0
  fi
  log_line "pci_remove_rescan outcome=sysfs_missing"; return 1
}
try_reboot() {
  local count; count="$(reboot_count_hour)"
  if [[ "${ALLOW_REBOOT}" -ne 1 ]]; then
    log_line "reboot decision=denied reason=allow_reboot_off hint='pass --allow-reboot or CRATE_USB_ALLOW_REBOOT=1'"; return 1
  fi
  if [[ "${count}" -ge "${MAX_REBOOTS_PER_HOUR}" ]]; then
    log_line "reboot decision=denied reason=max_reboots_per_hour count=${count} max=${MAX_REBOOTS_PER_HOUR}"; return 1
  fi
  log_line "reboot decision=approved count_hour=${count}"
  [[ "${DRY_RUN}" -eq 1 ]] && { log_line "reboot dry_run"; return 0; }
  record_reboot; sudo reboot
}
health_ok() { local report; report="$(summarize_health)"; grep -q '^status=ok' <<<"${report}"; }
cmd_recover() {
  load_local_env
  if crate_client_running; then touch "${STATE_DIR}/crate_was_continuous"; fi
  # A dead HC cannot be usbreset/rebind'd (probe fails -110). Reboot gate only.
  local ctrl_reasons=""
  if ctrl_reasons="$(usb_controller_dead)"; then
    if controller_dead_noted_this_boot && [[ "${ALLOW_REBOOT}" -ne 1 ]]; then
      return 3
    fi
    note_controller_dead "${ctrl_reasons}"
    try_reboot && return 0
    return 3
  fi
  local report; report="$(summarize_health)"
  log_line "recover_start ${report//$'\n'/ | } dry_run=${DRY_RUN} allow_reboot=${ALLOW_REBOOT}"
  if grep -q '^status=ok' <<<"${report}"; then
    log_line "recover noop reason=already_healthy"
    echo "Already healthy — no recovery actions taken."
    rewrite_local_env_indices || true; return 0
  fi
  capture_dmesg_snapshot
  if in_cooldown; then echo "In cooldown — skipping recovery storm."; return 1; fi
  mark_recover
  if cm108_present; then
    try_usbreset || true; sleep 2
    if health_ok; then log_line "recover success via=usbreset"; rewrite_local_env_indices || true; restart_crate_client_if_needed || true; cmd_check || true; return 0; fi
  fi
  if xhci_looks_dead || ! cm108_present; then
    try_xhci_rebind || true; sleep 2
    if health_ok; then log_line "recover success via=xhci_rebind"; rewrite_local_env_indices || true; restart_crate_client_if_needed || true; cmd_check || true; return 0; fi
    try_pci_remove_rescan || true; sleep 3
    if health_ok; then log_line "recover success via=pci_remove_rescan"; rewrite_local_env_indices || true; restart_crate_client_if_needed || true; cmd_check || true; return 0; fi
  fi
  log_line "recover escalating_to_reboot_gate"
  try_reboot && return 0
  log_line "recover failed remaining_unhealthy"; cmd_check || true; return 1
}
cmd_watch() {
  load_local_env
  log_line "watch_start once=${WATCH_ONCE} interval=${INTERVAL} allow_reboot=${ALLOW_REBOOT}"
  while true; do
    if crate_client_running; then touch "${STATE_DIR}/crate_was_continuous"; fi
    if health_ok; then
      local report; report="$(summarize_health)"
      log_line "watch healthy ${report//$'\n'/ | }"
    elif usb_controller_dead >/dev/null; then
      # cmd_recover logs USB_CONTROLLER_DEAD once per boot; don't repeat every tick.
      cmd_recover || true
    else
      log_line "watch unhealthy invoking_recover"; cmd_recover || true
    fi
    [[ "${WATCH_ONCE}" -eq 1 ]] && break
    sleep "${INTERVAL}"
  done
}
case "${CMD}" in
  check) cmd_check ;;
  recover) cmd_recover ;;
  watch) cmd_watch ;;
  ""|-h|--help) sed -n '2,5p' "$0"; exit 0 ;;
  *) echo "Usage: $0 {check|recover|watch} [--dry-run] [--allow-reboot] [--once] [--interval SEC]" >&2; exit 2 ;;
esac
