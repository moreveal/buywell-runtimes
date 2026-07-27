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
