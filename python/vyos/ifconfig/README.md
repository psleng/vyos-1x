ModemConnection D-Bus Service

This module implements a D-Bus service that wraps ModemManager for LTE modems and a state service that keeps connections active.

Running

The service is implemented in `interfaces_wwan_ModemConnectionDbusService.py`. To run it for development:

```bash
python3 python/vyos/ifconfig/interfaces_wwan_ModemConnectionDbusService.py
```

It requests the bus name `com.perle.ModemConnectionService` and exports objects under `/com/perle/ModemConnectionService` and `/com/perle/ModemConnectionStateService<N>`.

Testing

Basic tests are under `python/vyos/ifconfig/tests/`. They require `pytest` to run:

```bash
pip install pytest
pytest python/vyos/ifconfig/tests/test_modem_service.py
```

Notes

- The code assumes ModemManager is available. Use `busctl` or `gdbus` to inspect exported signals while the service runs.
- The auto-connect loop will attempt to use APN values set via `SetConnectionParams` on the `ModemConnectionService` object.
- This is a development helper; before production use, ensure proper permissions, systemd service unit, and configuration handling.

Systemd installation (example)

Copy the provided unit file to `/etc/systemd/system/` and enable the service:

```bash
sudo cp python/vyos/ifconfig/modem-connection.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now modem-connection.service
```

Adjust `ExecStart` in the unit file to reflect where the package will be installed on your system (the example assumes `/usr/share/vyos/python/...`).
