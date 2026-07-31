# 1.3.11 Edge

- “Ask the buyer” now always sends its initial question once. Messages observed before the purchase run are not accepted as answers, while newer buffered messages are checked only after the question is sent.

# 1.3.10 Edge

- “Ask the buyer” can collect validated values in the FunPay chat or send one protected Buywell form, selected by the workflow owner.

# 1.3.9 Edge

- Message-based input collection is completely removed from the current package. “Ask the buyer” now uses only the unified form.

# 1.3.8 Edge

- Adds the buyer form with a safe return to the FunPay order or chat.

# 1.3.7 Edge

- A buyer message can now be consumed only once even when FunPay delivers it both as a new-message event and a last-chat-message change. A retry no longer exhausts every attempt on the same response.

# 1.3.6 Edge

- Input retries now send the configured invalid-response message; the Buywell server canonically enforces hosts, paths, and all other response constraints.

# 1.3.5 Edge

- Order events now include dynamic listing parameters (`period`, delivery method, and other category fields), allowing configured input mappings to receive the purchased variant value.

# 1.3.4 Edge

- When a buyer replies right after purchase before the question appears in chat, input waits now use that fresh message and avoid sending an extra repeated prompt.

# 1.3.3 Edge

- Buyer replies are now matched by both chat ID and conversation participants, consistent with the proven Cardinal runtime behavior.
- Redelivery of the same wait after reconnect does not send the question twice and continues waiting for the same reply.
- An expired wait now returns a clear error instead of an empty message.

# 1.3.2 Edge

- Starts the FunPay Runner worker loop so order and message polling actually runs.
- Connection health now follows live polling: lost authorization requests sign-in again, while stalled polling is reported as unavailable.

# 1.3.1 Edge

- Interactive Edge setup now reads Russian and English field labels from the FunPay package.

# 1.3.0 Edge

- Preserves the complete Cardinal module 1.3.0 manifest and contract.
- Runs the provider session directly in Edge with FunPayAPI pinned from
  Cardinal `9d5ce692574ce2705f31715ec916ebede5d44d4e`.
- Adds health, local sign-in, shared transport, update, and rollback.
