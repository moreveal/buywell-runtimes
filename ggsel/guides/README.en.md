# GGSel Seller

This module connects a GGSel seller account to Buywell workflows.

Available events:

- a new purchase;
- a new buyer message.

The available action sends a message to the current purchase chat. The runtime
uses an outbound connection, stores unacknowledged events in SQLite, and retries
delivery after reconnecting.

A workflow can also ask the buyer for a value, validate the reply, and retry after
an invalid answer. Waiting survives reconnects and does not block other workflows.

Version 1.1.0 intentionally stops at this core workflow. Purchase status changes,
reviews, chat lookup, and file sending are not part of the module contract.

GGSel exposes recent sales without a cursor. The runtime reads at most
`sales_window` entries per scan. Use a shorter polling interval for high-volume
accounts.
