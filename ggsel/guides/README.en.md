# GGSel Seller

This module connects a GGSel seller account to Buywell workflows.

Available events:

- a new purchase;
- a new buyer message.

The available action sends a message to the current purchase chat. The runtime
uses an outbound connection, stores unacknowledged events in SQLite, and retries
delivery after reconnecting.

GGSel exposes recent sales without a cursor. The runtime reads at most
`sales_window` entries per scan. Use a shorter polling interval for high-volume
accounts.
