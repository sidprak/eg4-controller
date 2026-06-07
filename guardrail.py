#!/usr/bin/env python3
"""EG4 battery discharge guardrail.

Stateless script intended for cron. Each run:
  authenticate → read telemetry → decide desired discharge cap →
  read current setting → write only if different → verify by re-read →
  emit one structured JSON log line.

See README.md for usage; the design rationale is summarized there.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from datetime import datetime, timezone
from typing import Any

from eg4_inverter_api import EG4InverterAPI
from eg4_inverter_api.constants import INVERTER_PARAMETER_READ
from eg4_inverter_api.exceptions import EG4APIError, EG4AuthError

# Same banks the lib sweeps, but FlexBOSS21 firmware doesn't implement 2000/5000.
# We try them all and skip any that fail, instead of bailing on first failure.
SETTING_BANKS = (0, 127, 240, 500, 2000, 5000)

# Default hold-register key for "On-Grid Cut-Off SOC (%)" on FlexBOSS21.
# UI label: "On-Grid Cut-Off SOC(%)". When SOC <= this value, the inverter
# stops discharging the battery TO ON-GRID LOADS. Critically, this does NOT
# affect off-grid/EPS discharge — that uses HOLD_SOC_LOW_LIMIT_EPS_DISCHG.
# So setting this to 100 effectively forbids any on-grid discharge while
# preserving full battery backup if the grid fails.
DEFAULT_HOLD_PARAM_DISCHARGE = "HOLD_DISCHG_CUT_OFF_SOC_EOD"

# Default value (SOC %) to write when the cap is ON. 100 means "stop
# discharging at any SOC". Some firmware silently clamps below 100; if you
# see verify=fail, try 99 or 95 via EG4_CAP_ON_SOC.
DEFAULT_CAP_ON_SOC = "100"


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return int(raw)


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="EG4 battery discharge guardrail")
    p.add_argument(
        "--discover",
        "--list-settings",
        action="store_true",
        dest="discover",
        help="Read-only: dump all hold-register keys+values and exit. "
             "Use this to identify EG4_HOLD_PARAM_DISCHARGE.",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Decide and log, never write. Overrides EG4_DRY_RUN=0.",
    )
    p.add_argument(
        "--apply",
        action="store_true",
        help="Force write mode even if EG4_DRY_RUN=1.",
    )
    return p.parse_args()


def _emit(record: dict[str, Any]) -> None:
    """Emit a single structured log line: human prefix + JSON blob."""
    record.setdefault("ts", datetime.now(timezone.utc).isoformat())
    decision = record.get("decision", "?")
    action = record.get("action", "?")
    verify = record.get("verify", "?")
    pv_w = record.get("pv_w")
    current = record.get("current_soc")
    desired = record.get("desired_soc")
    summary = (
        f"decision={decision} pv_w={pv_w} current_soc={current} "
        f"desired_soc={desired} action={action} verify={verify}"
    )
    line = f"{summary} | {json.dumps(record, default=str, sort_keys=True)}"
    logging.info(line)


def _setup_logging() -> None:
    # logs go to stderr so --discover's stdout JSON stays clean for piping
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    log_file = os.getenv("EG4_LOG_FILE")
    if log_file:
        handlers.append(logging.FileHandler(log_file))
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=handlers,
        force=True,
    )


def _extract_pv_w(rt: Any, field: str) -> float | None:
    """Pull PV power (W) from a RuntimeData object using `field` (default `ppv`).

    Returns None if missing/unparseable — caller treats that as fail-safe.
    """
    val = getattr(rt, field, None)
    if val is None:
        # Last-resort: sum any pv1..pv4 power fields if present.
        parts = [getattr(rt, f"ppv{i}", None) for i in (1, 2, 3, 4)]
        nums = [float(p) for p in parts if p not in (None, "")]
        if not nums:
            return None
        return sum(nums)
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _extract_setting_value(settings: dict[str, Any], hold_param: str) -> str | None:
    """Locate the hold-register value in the merged settings dict.

    EG4 settings appear under their hold-register names
    (e.g. HOLD_DISCHG_POWER_PERCENT_CMD).
    """
    val = settings.get(hold_param)
    if val is None:
        return None
    # Some EG4 responses wrap values in dicts; flatten common shapes.
    if isinstance(val, dict):
        for key in ("valueText", "value", "text"):
            if key in val:
                return str(val[key])
        return json.dumps(val, sort_keys=True)
    return str(val)


async def _read_settings_tolerant(
    api: EG4InverterAPI,
) -> tuple[dict[str, Any], dict[int, dict[str, Any]]]:
    """Read every hold-register bank, tolerating per-bank failures.

    Returns (merged_fields, per_bank_status). The lib's read_settings_async
    bails on the first non-success and discards everything already collected;
    FlexBOSS21 firmware returns success=false for banks 2000/5000, so we have
    to iterate ourselves and merge what we can.
    """
    url = f"{api._base_url}{INVERTER_PARAMETER_READ}"
    session = await api._get_session()
    merged: dict[str, Any] = {}
    status: dict[int, dict[str, Any]] = {}
    skip = {"success", "valueFrame", "inverterSn", "startRegister",
            "pointNumber", "error"}
    for start in SETTING_BANKS:
        payload = (f"inverterSn={api._serialNum}&startRegister={start}"
                   f"&pointNumber=127&autoRetry=true")
        try:
            resp = await api._request("POST", url, payload)
        except Exception as e:  # noqa: BLE001 — record per-bank, keep going
            status[start] = {"success": False, "error": f"{type(e).__name__}: {e}",
                             "n_fields": 0}
            continue
        ok = bool(resp.get("success"))
        extras = {k: v for k, v in resp.items() if k not in skip}
        status[start] = {"success": ok, "error": resp.get("error"),
                         "n_fields": len(extras)}
        if ok:
            merged.update(extras)
    return merged, status


async def _discover(api: EG4InverterAPI) -> int:
    settings, bank_status = await _read_settings_tolerant(api)
    rt = await api.get_inverter_runtime_async()
    if hasattr(rt, "success") and rt.success is False:
        logging.error("runtime read failed: %r", rt)
        return 2
    rt_dict = {k: v for k, v in (rt.to_dict() if hasattr(rt, "to_dict") else {}).items()
               if not k.startswith("_")}
    # Pull a few keys that are useful for picking a hold_param and PV field.
    discharge_candidates = {
        k: v for k, v in settings.items()
        if "DISCH" in k or "SOC" in k or "EOD" in k
    }
    print(json.dumps(
        {
            "serial": api._serialNum,
            "bank_status": bank_status,
            "settings": settings,
            "discharge_candidates": discharge_candidates,
            "runtime_keys": sorted(rt_dict.keys()),
            "runtime_pv_sample": {
                k: rt_dict.get(k)
                for k in ("ppv", "ppv1", "ppv2", "ppv3", "ppv4",
                          "pDisCharge", "pCharge", "pToGrid", "pToUser")
            },
        },
        indent=2,
        default=str,
        sort_keys=True,
    ))
    return 0


async def _run(args: argparse.Namespace) -> int:
    username = os.getenv("EG4_USERNAME")
    password = os.getenv("EG4_PASSWORD")
    if not username or not password:
        logging.error("EG4_USERNAME and EG4_PASSWORD are required")
        return 2

    base_url = os.getenv("EG4_BASE_URL", "https://monitor.eg4electronics.com")
    ignore_ssl = _env_bool("EG4_DISABLE_VERIFY_SSL", False)
    serial = os.getenv("EG4_SERIAL_NUMBER")

    api = EG4InverterAPI(username, password, base_url=base_url)
    try:
        await api.login(ignore_ssl=ignore_ssl)
        if serial:
            api.set_selected_inverter(serialNum=serial)
        else:
            api.set_selected_inverter(inverterIndex=0)

        if args.discover:
            return await _discover(api)

        return await _decide_and_write(api, args)
    except EG4AuthError as e:
        _emit({"decision": "error", "action": "none", "verify": "skipped",
               "error": f"auth: {e}"})
        return 2
    except EG4APIError as e:
        _emit({"decision": "error", "action": "none", "verify": "skipped",
               "error": f"api: {e}"})
        return 2
    except Exception as e:  # noqa: BLE001 — fail-safe top-level guard
        _emit({"decision": "error", "action": "none", "verify": "skipped",
               "error": f"unexpected: {type(e).__name__}: {e}"})
        return 2
    finally:
        await api.close()


async def _decide_and_write(api: EG4InverterAPI, args: argparse.Namespace) -> int:
    # Hysteresis thresholds. Two separate W cutoffs so spiky/cloudy days don't
    # flip the cap every 15 min. Defaults derived from EG4_PV_THRESHOLD_W (legacy
    # single-threshold var) if the new ones aren't explicitly set, so existing
    # deploys keep their behavior unless reconfigured.
    legacy = _env_int("EG4_PV_THRESHOLD_W", 1700)
    pv_cap_on_w = _env_int("EG4_PV_CAP_ON_W", legacy)
    pv_cap_off_w = _env_int("EG4_PV_CAP_OFF_W", max(0, legacy - 500))
    normal_soc = os.getenv("EG4_NORMAL_DISCHARGE_SOC", "2")
    cap_on_soc = os.getenv("EG4_CAP_ON_SOC", DEFAULT_CAP_ON_SOC)
    hold_param = os.getenv("EG4_HOLD_PARAM_DISCHARGE", DEFAULT_HOLD_PARAM_DISCHARGE)
    pv_field = os.getenv("EG4_PV_FIELD", "ppv")

    if pv_cap_on_w < pv_cap_off_w:
        _emit({"decision": "error", "action": "none", "verify": "skipped",
               "error": f"EG4_PV_CAP_ON_W ({pv_cap_on_w}) must be >= "
                        f"EG4_PV_CAP_OFF_W ({pv_cap_off_w}); otherwise "
                        "hysteresis logic is inverted"})
        return 2

    try:
        normal_soc_int = int(float(normal_soc))
    except ValueError:
        _emit({"decision": "error", "action": "none", "verify": "skipped",
               "error": f"EG4_NORMAL_DISCHARGE_SOC not numeric: {normal_soc!r}"})
        return 2
    try:
        cap_on_soc_int = int(float(cap_on_soc))
    except ValueError:
        _emit({"decision": "error", "action": "none", "verify": "skipped",
               "error": f"EG4_CAP_ON_SOC not numeric: {cap_on_soc!r}"})
        return 2
    if not 0 <= normal_soc_int <= 100:
        _emit({"decision": "error", "action": "none", "verify": "skipped",
               "error": f"EG4_NORMAL_DISCHARGE_SOC must be 0..100, got {normal_soc_int}"})
        return 2
    if not 0 <= cap_on_soc_int <= 100:
        _emit({"decision": "error", "action": "none", "verify": "skipped",
               "error": f"EG4_CAP_ON_SOC must be 0..100, got {cap_on_soc_int}"})
        return 2
    if cap_on_soc_int <= normal_soc_int:
        _emit({"decision": "error", "action": "none", "verify": "skipped",
               "error": f"EG4_CAP_ON_SOC ({cap_on_soc_int}) must be > "
                        f"EG4_NORMAL_DISCHARGE_SOC ({normal_soc_int}); "
                        "otherwise cap-on would not throttle discharge"})
        return 2

    dry_run = _env_bool("EG4_DRY_RUN", True)
    if args.dry_run:
        dry_run = True
    if args.apply:
        dry_run = False

    rt = await api.get_inverter_runtime_async()
    if hasattr(rt, "success") and rt.success is False:
        _emit({"decision": "error", "action": "none", "verify": "skipped",
               "error": f"runtime read failed: {getattr(rt, 'error_message', '?')}"})
        return 2

    pv_w = _extract_pv_w(rt, pv_field)
    if pv_w is None:
        _emit({"decision": "error", "action": "none", "verify": "skipped",
               "pv_field": pv_field,
               "error": f"PV field '{pv_field}' missing/unparseable in runtime data"})
        return 2

    # Read current setting first; needed both for hold-zone behavior and the
    # idempotency check below.
    settings, bank_status = await _read_settings_tolerant(api)
    if not settings:
        _emit({"decision": "error", "action": "none", "verify": "skipped",
               "pv_w": pv_w, "bank_status": bank_status,
               "error": "settings read failed: every bank returned non-success"})
        return 2

    current_raw = _extract_setting_value(settings, hold_param)
    if current_raw is None:
        _emit({"decision": "error", "action": "none", "verify": "skipped",
               "pv_w": pv_w, "hold_param": hold_param, "bank_status": bank_status,
               "error": f"hold_param '{hold_param}' not found in settings; "
                        "run --discover to confirm key"})
        return 2

    # Three-zone hysteresis:
    #   PV >  cap_on_w   → cap_on  (write cap_on_soc)
    #   PV <  cap_off_w  → cap_off (write normal_soc)
    #   between          → hold    (leave current value alone)
    # The "hold" zone is what prevents flapping when PV spikes around a single
    # threshold. We define `desired_soc = current_raw` so the idempotency check
    # below will naturally short-circuit with action=none.
    if pv_w > pv_cap_on_w:
        decision = "cap_on"
        desired_soc = str(cap_on_soc_int)
    elif pv_w < pv_cap_off_w:
        decision = "cap_off"
        desired_soc = str(normal_soc_int)
    else:
        decision = "hold"
        desired_soc = current_raw

    if _numeric_equal(current_raw, desired_soc):
        reason = ("in hysteresis hold zone" if decision == "hold"
                  else "already at desired value")
        _emit({"decision": decision, "action": "none", "verify": "skipped",
               "pv_w": pv_w, "pv_cap_on_w": pv_cap_on_w,
               "pv_cap_off_w": pv_cap_off_w,
               "current_soc": current_raw, "desired_soc": desired_soc,
               "hold_param": hold_param, "dry_run": dry_run,
               "reason": reason})
        return 0

    if dry_run:
        _emit({"decision": decision, "action": "would_write", "verify": "skipped",
               "pv_w": pv_w, "pv_cap_on_w": pv_cap_on_w,
               "pv_cap_off_w": pv_cap_off_w,
               "current_soc": current_raw, "desired_soc": desired_soc,
               "hold_param": hold_param, "dry_run": True})
        return 0

    ok = await api.write_setting_async(hold_param, desired_soc)
    if not ok:
        _emit({"decision": decision, "action": "write", "verify": "fail",
               "pv_w": pv_w, "pv_cap_on_w": pv_cap_on_w,
               "pv_cap_off_w": pv_cap_off_w,
               "current_soc": current_raw, "desired_soc": desired_soc,
               "hold_param": hold_param, "dry_run": False,
               "error": "write_setting_async returned non-success"})
        return 1

    verify_settings, _ = await _read_settings_tolerant(api)
    verify_raw = _extract_setting_value(verify_settings, hold_param)
    verify = "pass" if verify_raw is not None and _numeric_equal(verify_raw, desired_soc) else "fail"
    _emit({"decision": decision, "action": "write", "verify": verify,
           "pv_w": pv_w, "pv_cap_on_w": pv_cap_on_w,
           "pv_cap_off_w": pv_cap_off_w,
           "current_soc": current_raw, "desired_soc": desired_soc,
           "post_write_soc": verify_raw, "hold_param": hold_param,
           "dry_run": False})
    return 0 if verify == "pass" else 1


def _numeric_equal(a: str, b: str) -> bool:
    try:
        return float(a) == float(b)
    except (TypeError, ValueError):
        return str(a).strip() == str(b).strip()


def main() -> int:
    _setup_logging()
    args = _parse_args()
    return asyncio.run(_run(args))


if __name__ == "__main__":
    sys.exit(main())
