# Changelog

## 1.2.0 — July 22, 2026

- Added a live product and parameter catalog for configuring workflows with stable IDs.
- Purchase events now include a required product ID, the safe product name from recent sales, field values, and separate selected-choice IDs.
- Buyer-delivered content can no longer leak into the product name or ordinary event data.
- The HTTP client now uses separate timeouts and safe retries for read-only operations only.

## 1.1.0 — July 22, 2026

- Workflows can now ask a buyer for a value, validate the reply, and continue after a valid response arrives.
- Active waits and unprocessed replies survive runtime reconnects.
- The installer tries to restore a missing `pip` with `ensurepip` and explains missing virtual-environment support more clearly.
- Added automatic background-service setup using the current user and runtime folder on Linux and Windows.
- The installer verifies a complete Buywell runtime connection; reinstalling updates and restarts the existing service without duplicates.
- The installation guide is shorter and no longer requires manually writing a systemd unit or Windows task.

## 1.0.1 — July 22, 2026

- The runtime is now delivered as one ZIP containing the Windows and Linux install/run scripts, configurator, dependency list, and Python code.

## 1.0.0 — July 22, 2026

- Added new-purchase and buyer-message events.
- Added message delivery to the current GGSel chat.
- Added durable cursors, deduplication, and event delivery retries.
- Added standalone Windows and Linux operation.
