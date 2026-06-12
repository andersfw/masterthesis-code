# 5G National Roaming — Measurement Platform and Datasets

Repository for the master's thesis *5G Connectivity and National Roaming* (TTM4905, NTNU, 2026).

## Structure

```
.
├── python_raspi/          # Measurement scripts running on the Raspberry Pi
├── configs/               # Bash scripts and systemd service/timer units
└── datasets/
    ├── stationary/        # Indoor sessions at campus Gløshaugen
    │   ├── ice/
    │   └── telenor/
    └── marienborg_vaernes_marienborg/   # Mobile train corridor runs
        ├── ice/
        └── telenor/
└── figs/                  # Figures generated from the datasets, used in the thesis
```

## Contents

**`python_raspi/`** — Python scripts for passive radio logging (`measure_qmicli_*.py`) and active Speedtest measurements (`measure_speedtest_*.py`), plus shared Google Sheets helper libraries (`sheet_qmicli.py`, `sheet_speedtest.py`).

**`configs/`** — Bash scripts for modem bring-up and watchdog recovery (`wwan_up_*.sh`, `wwan_watchdog.sh`), and the corresponding systemd service and timer units.

**`datasets/`** — Raw CSV files retrieved from the Raspberry Pi after each measurement campaign. Each session contains a qmicli file and a speedtest file per operator.
