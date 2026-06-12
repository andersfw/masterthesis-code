#!/usr/bin/env python3
from datetime import datetime
import time
from sheet_qmicli import append_row, get_service, create_sheet
import socket
import sys
import subprocess
import re
import os
import csv
import io
import json
import gps
import signal

QMI_INTERVAL_SECONDS = 5
SHEETS_TIMEOUT_SECONDS = 5
MAX_CONSECUTIVE_FAILURES = 10  # 10 × 5s = 50s of consecutive qmicli failures before exiting for systemd restart


class SheetsTimeoutError(Exception):
    pass


def _timeout_handler(signum, frame):
    raise SheetsTimeoutError("Google Sheets append timed out")


def append_row_with_timeout(csv_string, service, spreadsheet_id, sheet_name):
    previous_handler = signal.getsignal(signal.SIGALRM)
    signal.signal(signal.SIGALRM, _timeout_handler)
    signal.alarm(SHEETS_TIMEOUT_SECONDS)

    try:
        return append_row(csv_string, service, spreadsheet_id, sheet_name)
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous_handler)


def wait_for_internet():
    while True:
        try:
            socket.create_connection(("8.8.8.8", 53), timeout=3)
            return
        except OSError:
            time.sleep(5)


def _run_qmicli_cell_location():
    base_cmd = ["qmicli", "--device=/dev/cdc-wdm0", "-p", "--nas-get-cell-location-info"]

    if hasattr(os, "geteuid") and os.geteuid() == 0:
        return subprocess.check_output(base_cmd, text=True, timeout=5)

    sudo_cmd = ["sudo", "-n"] + base_cmd
    return subprocess.check_output(sudo_cmd, text=True, timeout=5)


def parse_interfreq_neighbors(out):
    """
    Parse 'Interfrequency LTE Info' block from qmicli output.

    Returns a list of dicts, one per cell, e.g.:
        [
            {"earfcn": "3348", "pci": "408", "rsrp": "-99.6", "rsrq": "-8.8", "rssi": "-81.8"},
            ...
        ]
    Returns an empty list if the block is absent or unparseable.
    """
    neighbors = []

    try:
        if "Interfrequency LTE Info" not in out:
            return neighbors

        block = out.split("Interfrequency LTE Info", 1)[1]

        top_level_section = re.split(r"\n(?=[A-Za-z])", block, maxsplit=1)
        block = top_level_section[0]

        freq_chunks = re.split(r"Frequency\s*\[\d+\]:", block)

        for freq_chunk in freq_chunks[1:]:
            earfcn_match = re.search(
                r"EUTRA Absolute RF Channel Number:\s*'(\d+)'", freq_chunk
            )
            earfcn = earfcn_match.group(1) if earfcn_match else ""

            cell_chunks = re.split(r"Cell\s*\[\d+\]:", freq_chunk)

            for cell_chunk in cell_chunks[1:]:
                def gv(pattern, text=cell_chunk):
                    m = re.search(pattern, text)
                    return m.group(1).strip() if m else ""

                neighbors.append({
                    "earfcn": earfcn,
                    "pci":    gv(r"Physical Cell ID:\s*'(\d+)'"),
                    "rsrp":   gv(r"RSRP:\s*'([-\d.]+)"),
                    "rsrq":   gv(r"RSRQ:\s*'([-\d.]+)"),
                    "rssi":   gv(r"RSSI:\s*'([-\d.]+)"),
                })

    except Exception as e:
        print(
            f"Warning: failed to parse interfrequency neighbors: {e}",
            file=sys.stderr,
            flush=True,
        )

    return neighbors


def get_cell_info_fields():
    out = _run_qmicli_cell_location()

    def g(m):
        return m.group(1).strip() if m else ""

    if "5GNR cell information" in out:
        rat_mode = "5gsa"
    elif "5GNR ARFCN" in out:
        rat_mode = "nsa"
    else:
        rat_mode = "lte"

    lte_block = ""
    nr_block = ""

    if "Intrafrequency LTE Info" in out:
        lte_block = out.split("Intrafrequency LTE Info", 1)[1]

    if "5GNR cell information" in out:
        nr_block = out.split("5GNR cell information", 1)[1]

    def find_lte(pattern):
        return re.search(pattern, lte_block, re.MULTILINE)

    lte_plmn   = g(find_lte(r"PLMN:\s*'(\d+)'"))
    lte_tac    = g(find_lte(r"Tracking Area Code:\s*'(\d+)'"))
    lte_ecgi   = g(find_lte(r"Global Cell ID:\s*'(\d+)'"))
    lte_earfcn = g(find_lte(r"EUTRA Absolute RF Channel Number:\s*'(\d+)'"))
    lte_pci    = g(find_lte(r"Physical Cell ID:\s*'(\d+)'"))
    lte_rsrp   = g(find_lte(r"RSRP:\s*'([-\d.]+)"))
    lte_rsrq   = g(find_lte(r"RSRQ:\s*'([-\d.]+)"))
    lte_rssi   = g(find_lte(r"RSSI:\s*'([-\d.]+)"))

    def find_nr(pattern):
        return re.search(pattern, nr_block, re.MULTILINE)

    nr_arfcn = g(re.search(r"5GNR ARFCN:\s*'(\d+)'", out))

    nr_plmn = nr_tac = nr_nci = nr_pci = ""
    nr_rsrp = nr_rsrq = nr_snr = ""

    if nr_block:
        nr_plmn = g(find_nr(r"PLMN:\s*'(\d+)'"))
        nr_tac  = g(find_nr(r"Tracking Area Code:\s*'(\d+)'"))
        nr_nci  = g(find_nr(r"Global Cell ID:\s*'(\d+)'"))
        nr_pci  = g(find_nr(r"Physical Cell ID:\s*'(\d+)'"))
        nr_rsrp = g(find_nr(r"RSRP:\s*'([-\d.]+)"))
        nr_rsrq = g(find_nr(r"RSRQ:\s*'([-\d.]+)"))
        nr_snr  = g(find_nr(r"SNR:\s*'([-\d.]+)"))

    interfreq_neighbors = parse_interfreq_neighbors(out)
    interfreq_json = json.dumps(interfreq_neighbors, separators=(",", ":"))

    return [
        rat_mode,
        lte_plmn, lte_tac, lte_ecgi, lte_earfcn, lte_pci,
        lte_rsrp, lte_rsrq, lte_rssi,
        nr_plmn, nr_tac, nr_nci, nr_arfcn, nr_pci,
        nr_rsrp, nr_rsrq, nr_snr,
        interfreq_json,
    ]


def read_gps_lat_lon(session):
    for _ in range(5):
        try:
            report = session.next()
            if report.get("class") == "TPV":
                lat = getattr(report, "lat", None)
                lon = getattr(report, "lon", None)
                if lat is not None and lon is not None:
                    return str(lat), str(lon)
        except Exception:
            pass
    return "", ""


def qmi_to_csv_row(timestamp, gps_lat, gps_lon, fields):
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([timestamp, gps_lat, gps_lon, "QMI"] + fields)
    return output.getvalue().rstrip("\n")


try:
    # Wait for internet only once at startup, for sheet initialisation
    wait_for_internet()

    spreadsheet_id = "1JAve1C8ZZdh-QTcwNqT8ckeNzdU0mN4Qt1zflYWQ3MY"
    sheet_name = "qmicli " + datetime.now().strftime("%Y.%m.%d %H.%M.%S")

    os.makedirs("measurements/ice", exist_ok=True)

    header = (
        "timestamp,gps_lat,gps_lon,qmi_tag,"
        "rat_mode,"
        "lte_plmn,lte_tac,lte_ecgi,lte_earfcn,lte_pci,lte_rsrp_dbm,lte_rsrq_db,lte_rssi_dbm,"
        "nr_plmn,nr_tac,nr_nci,nr_arfcn,nr_pci,nr_rsrp_dbm,nr_rsrq_db,nr_snr_db,"
        "lte_interfreq_neighbors_json"
    )

    local_path = f"measurements/ice/{sheet_name}.csv"
    with open(local_path, "w") as f:
        f.write(header + "\n")

    while True:
        try:
            svc = get_service(spreadsheet_id)
            create_sheet(sheet_name, svc, spreadsheet_id)
            append_row_with_timeout(header, svc, spreadsheet_id, sheet_name)
            break
        except SheetsTimeoutError as e:
            print(f"Sheet init timed out, retrying in 5s: {e}", file=sys.stderr, flush=True)
            time.sleep(5)
        except Exception as e:
            print(f"Sheet init failed, retrying in 5s: {e}", file=sys.stderr, flush=True)
            time.sleep(5)

    gps_session = gps.gps(mode=gps.WATCH_ENABLE)

    consecutive_failures = 0

    while True:
        try:
            # NOTE: wait_for_internet() intentionally removed from here.
            # qmicli works without internet. Sheets append has its own
            # timeout and error handling below. This ensures cell measurements
            # continue during connectivity gaps (coverage loss, USB resets, etc.)

            ts = datetime.utcnow().isoformat() + "Z"
            gps_lat, gps_lon = read_gps_lat_lon(gps_session)

            fields = get_cell_info_fields()
            results = qmi_to_csv_row(ts, gps_lat, gps_lon, fields)

            print(results, flush=True)

            with open(local_path, "a") as f:
                f.write(results + "\n")

            # Successful measurement — reset failure counter
            consecutive_failures = 0

            try:
                append_row_with_timeout(results, svc, spreadsheet_id, sheet_name)
            except SheetsTimeoutError as e:
                print(
                    f"Warning: Google Sheets append timed out (continuing): {e}",
                    file=sys.stderr,
                    flush=True,
                )
                try:
                    svc = get_service(spreadsheet_id)
                except Exception as recreate_error:
                    print(
                        f"Warning: Failed to recreate Google Sheets service: {recreate_error}",
                        file=sys.stderr,
                        flush=True,
                    )
            except Exception as e:
                print(
                    f"Warning: Google Sheets append failed (continuing): {e}",
                    file=sys.stderr,
                    flush=True,
                )
                try:
                    svc = get_service(spreadsheet_id)
                except Exception as recreate_error:
                    print(
                        f"Warning: Failed to recreate Google Sheets service: {recreate_error}",
                        file=sys.stderr,
                        flush=True,
                    )

        except subprocess.TimeoutExpired as e:
            consecutive_failures += 1
            print(
                f"Error: qmicli timed out ({e}) [{consecutive_failures}/{MAX_CONSECUTIVE_FAILURES}]",
                file=sys.stderr,
                flush=True,
            )
        except subprocess.CalledProcessError as e:
            consecutive_failures += 1
            print(
                f"Error: qmicli failed ({e}) [{consecutive_failures}/{MAX_CONSECUTIVE_FAILURES}]",
                file=sys.stderr,
                flush=True,
            )
        except Exception as e:
            consecutive_failures += 1
            print(
                f"Error in main loop: {str(e)} [{consecutive_failures}/{MAX_CONSECUTIVE_FAILURES}]",
                file=sys.stderr,
                flush=True,
            )

        if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
            print(
                f"Fatal: {consecutive_failures} consecutive qmicli failures — exiting for systemd restart",
                file=sys.stderr,
                flush=True,
            )
            sys.exit(1)

        time.sleep(QMI_INTERVAL_SECONDS)

except Exception as e:
    print(f"Fatal error: {str(e)}", file=sys.stderr, flush=True)
    sys.exit(1)