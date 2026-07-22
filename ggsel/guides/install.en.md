# Install the GGSel Seller runtime

## Requirements

- Windows 10/11 or a current Linux distribution;
- Python 3.11 or newer;
- a GGSel seller API key with V1 Orders and Chats access;
- GGSel Seller package `1.0.0` installed in Buywell;
- a Buywell API key with the `modules:connect` permission.

Run the process on a computer or server that remains online. No inbound port or
public IP address is required.

## 1. Automatic installation

Open a terminal in the `ggsel` directory.

Windows:

```bat
install.bat
```

Linux:

```bash
chmod +x install.sh run.sh
./install.sh
```

The installer creates `.venv`, installs dependencies, prompts for the Buywell
key, seller ID, and GGSel API key, writes `config.json`, validates the
configuration and read-only V1 purchase/chat access, and offers to start the
runtime. Secret input is not displayed. A key restricted to the newer
catalog-only V2 API cannot provide purchase and message events.

`config.json` contains secrets. Do not share or commit it.

## 2. Later starts

Windows:

```bat
run.bat
```

Linux:

```bash
./run.sh
```

After `Connected to Buywell` appears, enable the required event connections in
Buywell. The first scan records currently visible purchases and messages without
starting workflows for them.

## 3. Keep it running on Linux

Create `/etc/systemd/system/buywell-ggsel.service`:

```ini
[Unit]
Description=Buywell GGSel runtime
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=buywell
WorkingDirectory=/opt/buywell-runtimes/ggsel
ExecStart=/opt/buywell-runtimes/ggsel/.venv/bin/python runtime/ggsel_runtime.py --config config.json
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Enable it:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now buywell-ggsel
sudo systemctl status buywell-ggsel
```

## 4. Keep it running on Windows

Create a Windows Task Scheduler task:

1. Start the task at sign-in or system startup.
2. Set the program to the full path of `.venv\Scripts\python.exe`.
3. Set arguments to `runtime\ggsel_runtime.py --config config.json`.
4. Set the working directory to the `ggsel` directory.

The SQLite file at `database_path` contains runtime cursors and pending work.
Include it in backups and move it together with the runtime.
