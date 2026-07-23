# Changelog

## 1.0.4 — 2026-07-23

- Draft items are now available in the catalog and marked as “Draft”, so the matrix can be prepared before an item is published.
- Declined and blocked items remain excluded from the catalog.

## 1.0.3 — 2026-07-23

- Hidden order fields, including passwords, are no longer included in the Buywell catalog or events.
- Category selection remains optional: a workflow can run without a catalog or filter by item name and other item conditions.

## 1.0.2 — 2026-07-23

- Replaced single-item selection with a category and seller-item mapping matrix.
- One workflow can now map every item in a category to a duration, quantity, or internal tariff.
- Added obtaining method, category attributes, and buyer-entered fields to the matrix.
- A new or unmapped choice safely stops execution until the matrix is updated.

## 1.0.1 — 2026-07-23

- Added message text, sender name, item, game, and category names to message trigger conditions.
- Added item name and price, buyer name, game, category, and delivery method to purchase trigger conditions.
- Kept technical IDs as optional conditions for exact integrations.

## 1.0.0 — 2026-07-23

- Added new purchase and incoming message events.
- Added Playerok item selection in workflow connections.
- Added item, category, game, and buyer filters.
- Added the action that sends a message to the originating chat.
- Added connection setup through Telegram.
- Added a local delivery queue with duplicate protection.
