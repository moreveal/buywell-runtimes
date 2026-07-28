# 1.2.7 Edge

- Purchase and message events provide a safe return to GGSel purchases and support one-time Buywell forms.
- Removed the legacy step-by-step buyer input implementation from the current package.

# 1.2.6 Edge

- Input retries now send the configured invalid-response message; the Buywell server canonically enforces every response constraint.

# 1.2.5 Edge

- Every discovered sale is now registered for message polling immediately, matching the previous runtime; buyer replies no longer depend on the chat separately appearing in the unread list.

# 1.2.4 Edge

- Interactive Edge setup now reads Russian and English field labels from the GGSel package.

# 1.2.3 Edge

- Preserves the published GGSel 1.2.3 contract.
- Removes duplicate Buywell transport while keeping provider cursors and dedupe local.
- Adds shared health, update, and rollback.
