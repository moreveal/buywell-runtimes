# GGSel Seller runtime

Standalone Buywell runtime for GGSel sellers. It runs as one Python process on
Windows or Linux and does not require Telegram or a public inbound port.

Purchase and chat polling uses Seller API V1. The key must have access to the
V1 orders and chats endpoints; a key restricted to the catalog-only V2 API is
not sufficient. The installer verifies this with read-only requests.

The runtime:

- detects new purchases through the GGSel seller API;
- exposes a live catalog of products, fields, and stable choice IDs during connection setup;
- detects new buyer messages for known and unread chats;
- sends typed events to Buywell through an outbound WebSocket;
- executes the in-context `Send message` action;
- asks buyers for validated workflow input and resumes after their reply;
- stores cursors, deduplication records, pending events, and action results in
  SQLite.

The module intentionally exposes only those two events and the in-context message
action. Purchase status changes, reviews, chat lookup, and file sending are not
part of version 1.2.2.

The installable runtime is built as one ZIP from the files in `runtime/`. After
extracting it, run `install.bat` on Windows or `./install.sh` on Linux. The installer creates
the virtual environment, repairs missing pip when possible, securely prompts for credentials,
checks both GGSel and Buywell connectivity, and can install an automatic background service. See the
[Russian installation guide](guides/install.ru.md) or
[English installation guide](guides/install.en.md) for service setup.

The first successful scan records currently visible purchases and messages
without emitting them. Set `emit_existing_on_first_start` to `true` only when
historical items should start workflows.
