# Playerok Universal

This module connects an installed Playerok Universal bot to Buywell workflows.

- Starts a workflow for a new paid sale.
- Starts a workflow for a new customer message.
- Lists seller categories and an item mapping matrix in the workflow connection editor.
- Filters messages by text, sender name, item, game, and category.
- Filters purchases by item, price, buyer name, game, category, and delivery method.
- Keeps ID filters available for exact integrations and lists.
- Implements **Send message** in the chat that produced the event.
- Keeps delivery state and duplicate protection in a local SQLite database.

Choose a category to load all of your items in it. The **Playerok item** field can map every item independently to workflow inputs such as duration, quantity, tariff, or another normalized value. Obtaining methods, category attributes, and order fields are available in the same matrix.

A new choice that has not been mapped or explicitly excluded cannot start a workflow with incorrect data. Buywell asks you to refresh the matrix first.

Existing sales and messages do not start workflows on the first connection.
