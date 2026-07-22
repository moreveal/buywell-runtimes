# Install GGSel Seller

You need Python 3.11+, a GGSel seller API key with Orders and Chats V1 access, and a Buywell connection key. No public IP or inbound port is required.

## 1. Download and extract

Download the runtime ZIP from the installed GGSel Seller `1.1.0` module in Buywell and extract it to a permanent folder. Avoid moving the folder after setup.

## 2. Run the installer

On Windows, double-click `install.bat`.

On Linux:

```bash
chmod +x install.sh
./install.sh
```

The installer automatically:

- finds Python and tries to restore a missing `pip` with `ensurepip`;
- creates an isolated environment and installs dependencies;
- asks for the Buywell key, seller ID, and GGSel API key;
- checks purchase and chat access;
- completes a real runtime handshake with Buywell;
- offers to install and immediately start a background service for the current user.

Linux uses systemd with the current user and folder. Windows creates a `Buywell GGSel Runtime` task that starts when the current user signs in.

## 3. Check it

Linux:

```bash
sudo systemctl status buywell-ggsel
sudo journalctl -u buywell-ggsel -f
```

On Windows, open Task Scheduler and find `Buywell GGSel Runtime`.

A successful installation confirms configuration, GGSel API, and Buywell
connectivity. Running the installer again updates and restarts the existing service
or task instead of creating a duplicate.

Then enable the required event connections in Buywell. The first scan records existing purchases and messages without starting workflows for them.

## Manual start

Use `run.bat` on Windows or `./run.sh` on Linux when automatic startup is not wanted.

If Linux reports missing virtual-environment support, run `sudo apt install python3-venv` and retry. A GGSel `403` means the API key lacks Orders and Chats V1 access.

`config.json` and the `state` folder contain secrets and runtime state. Keep them private and move them together with the runtime.
