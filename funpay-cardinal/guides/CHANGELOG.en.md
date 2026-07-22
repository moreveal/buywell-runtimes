# FunPay Cardinal changelog

## 1.3.0 — July 22, 2026

- Connection setup can now accept a regular FunPay category URL; Cardinal loads its custom order fields and choices directly.
- New connections store the category and mappings by stable keys, while Console imports remain compatible with existing connections.

## 1.2.2 — July 22, 2026

- The buyer question and invalid-response message can now be left empty. Cardinal will still wait for and validate a reply without sending an empty message.

## 1.2.1 — July 18, 2026

- Buyer replies are now matched reliably by both chat ID and conversation participants.
- Workflow setup now states clearly whether a value is an ID or a name.
- A FunPay category and its parent category are exposed as separate fields.
- Order statuses, currencies, and category types now use readable codes (`paid`, `rub`, `common`) instead of Cardinal's internal numbers.
- Buywell now shows “About this module” and “What's changed” pages.

When moving from 1.2.0, review launch conditions and field bindings. The corrected names and readable codes intentionally use a new purchase-event version.

## 1.2.0 — July 17, 2026

- Purchase events gained safe full-order data, including seller, category, quantity, total, character, server, side, and review details.
- Added the “Order field” source for values that differ between FunPay categories.
- Auto-delivery secrets and the post-payment message were excluded from event data.

## 1.1.1 — July 17, 2026

- Fixed installed-version removal so removing an old version no longer affects another user's installation.

## 1.1.0 — July 17, 2026

- Cardinal can ask several buyer questions in a defined order.
- Waiting for a reply survives reconnects and does not block other workflows.
- Repeated messages and replies are matched by stable message identity.

## 1.0.3 — July 13, 2026

- Connection errors now explain when the required module version has not been added to the Buywell account.
- Exact runtime-version checks were clarified.

## 1.0.2 — July 13, 2026

- The runtime now connects to the production Buywell address by default.
- Invalid keys and unavailable versions now have separate, useful error messages.

## 1.0.1 — July 12–13, 2026

- Cardinal now handles messages reported by FunPay as changes to the latest chat message.
- The runtime, installation guide, and icon are distributed as one validated module package.
- Buywell gained exact-runtime downloads and installed-version management.

## 1.0.0 — July 12, 2026

- First stable release.
- Added paid-order, order-status, and buyer-message events.
- Added sending workflow messages to a FunPay chat.
- The runtime uses an outbound WebSocket connection and safely retries unfinished work after reconnecting.

## 0.0.6 — July 12, 2026

- Aligned the expected-event list with Buywell subscriptions and added projected event data and launch scope.
- Local development became more reliable when connecting through IPv4 localhost.

## 0.0.5 — July 12, 2026

- Added expected-event hints so Cardinal can avoid collecting data unused by active workflows.

## 0.0.4 — July 11, 2026

- First working Cardinal-to-Buywell connection.
- Added connection keys, order and message events, a local queue, and buyer-message delivery.
