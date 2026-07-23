# Install Playerok Universal

1. Download the runtime archive from the installed module page in Buywell.
2. Open the installed Playerok Universal directory.
3. Extract the archive into `modules`. The resulting path must contain `modules/buywell_playerok/__init__.py`.
4. Restart Playerok Universal. Its module loader checks the bundled `websocket-client` dependency.
5. Open the Playerok Universal Telegram bot and run `/buywell`.
6. Select **Connect**, send the Buywell connection key, and wait for the **Connected** status.

Playerok credentials, cookies, and proxy settings stay inside Playerok Universal. The module sends only events, selected event data, and action results to Buywell.

When updating, replace the files in `modules/buywell_playerok` without deleting the generated `module_data` directory.
