#!/usr/bin/env python3
import speedtest
from datetime import datetime
import time
from sheet_speedtest import append_row, get_service, create_sheet, json_to_csv
import socket
import sys
import os
import gps
import fcntl
import struct

SPEEDTEST_INTERVAL_SECONDS = 10 * 60  # 10 minutes
WWAN_INTERFACE = "wwan1"


def get_interface_ip(ifname: str) -> str:
    """Return the IPv4 address assigned to a network interface."""
    SIOCGIFADDR = 0x8915
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        result = fcntl.ioctl(
            s.fileno(),
            SIOCGIFADDR,
            struct.pack("256s", ifname[:15].encode("utf-8")),
        )
        return socket.inet_ntoa(result[20:24])
    except OSError as e:
        raise RuntimeError(
            f"Could not get IP for interface '{ifname}': {e}. "
            "Is the interface up and connected?"
        ) from e
    finally:
        s.close()


def run_speedtest():
    try:
        source_ip = get_interface_ip(WWAN_INTERFACE)
        print(f"Running speedtest via {WWAN_INTERFACE} ({source_ip})", file=sys.stdout, flush=True)
        s = speedtest.Speedtest(secure=True, source_address=source_ip)
    except RuntimeError as e:
        print(f"Warning: {e} — falling back to default interface.", file=sys.stderr, flush=True)
        s = speedtest.Speedtest(secure=True)

    s.get_servers()
    s.get_best_server()
    s.download()
    s.upload()
    try:
        s.results.share()
    except Exception as e:
        print(f"Warning: speedtest share() failed: {e}", file=sys.stderr, flush=True)
    return s.results.dict()


def wait_for_internet():
    print("Checking internet connection...", file=sys.stdout, flush=True)
    while True:
        try:
            try:
                source_ip = get_interface_ip(WWAN_INTERFACE)
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.bind((source_ip, 0))
            except RuntimeError:
                # wwan1 unavailable — use default interface
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(3)
            sock.connect(("8.8.8.8", 53))
            sock.close()
            print("Internet connection established!", file=sys.stdout, flush=True)
            return
        except OSError:
            print("No internet connection. Waiting 5 seconds...", file=sys.stderr, flush=True)
            time.sleep(5)


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


try:
    wait_for_internet()

    spreadsheet_id = "18ujbvaGVbd0SXOCY7c7I22d2ejVnzaBwpBsPYt1zRPw"
    sheet_name = "speedtest " + datetime.now().strftime("%Y.%m.%d %H.%M.%S")

    os.makedirs("measurements/telenor", exist_ok=True)

    header = (
        "timestamp,download,upload,ping,"
        "server_lat,server_lon,server_name,server_country,server_sponsor,server_id,server_latency,"
        "share_url,client_lat,client_lon,gps_lat,gps_lon"
    )

    local_path = f"measurements/telenor/{sheet_name}.csv"
    with open(local_path, "w") as f:
        f.write(header + "\n")

    # REQUIRED: Ensure sheet exists + header written before starting measurements
    while True:
        try:
            svc = get_service(spreadsheet_id)
            create_sheet(sheet_name, svc, spreadsheet_id)
            append_row(header, svc, spreadsheet_id, sheet_name)
            print(f"Creating new sheet: {sheet_name}", file=sys.stdout, flush=True)
            break
        except Exception as e:
            print(f"Sheet init failed, retrying in 5s: {e}", file=sys.stderr, flush=True)
            time.sleep(5)

    gps_session = gps.gps(mode=gps.WATCH_ENABLE)

    while True:
        try:
            wait_for_internet()

            gps_lat, gps_lon = read_gps_lat_lon(gps_session)

            results = json_to_csv(run_speedtest()).rstrip("\n")
            results = results.rstrip("\r\n") + f",{gps_lat},{gps_lon}"
            results = results.replace("\r", " ").replace("\n", " ")

            print(results, file=sys.stdout, flush=True)

            with open(local_path, "a") as f:
                f.write(results + "\n")

            try:
                append_row(results, svc, spreadsheet_id, sheet_name)
            except Exception as e:
                print(f"Warning: Google Sheets append failed (continuing): {e}", file=sys.stderr, flush=True)

        except Exception as e:
            print(f"Error in main loop: {str(e)}", file=sys.stderr, flush=True)

        print("Waiting seconds before next test...", file=sys.stdout, flush=True)
        time.sleep(SPEEDTEST_INTERVAL_SECONDS)

except Exception as e:
    print(f"Fatal error: {str(e)}", file=sys.stderr, flush=True)