# FunPay Cardinal for Buywell

This module connects FunPay Cardinal to Buywell workflows. It receives paid-order, order-status, and buyer-message events, then runs workflow actions in the right FunPay chat.

## What the module can do

- start a workflow after an order is paid or its status changes;
- provide order, item, buyer, category, and review data to the workflow;
- ask the buyer for a value and wait for a valid response;
- send workflow messages to the original FunPay chat;
- continue waiting for a response after Cardinal reconnects.

## Data available to workflows

IDs and names are separate fields. For example, “Category ID” contains the stable FunPay category identifier, while “Category name” contains the text shown to users. Parent-category fields are separate as well.

Statuses, currencies, and category types use readable codes such as `paid`, `rub`, and `common`. Cardinal's internal numeric enum values are not exposed.

## Security

The FunPay golden key and session stay inside Cardinal. Buywell does not receive auto-delivery secrets, post-payment messages, raw HTML, CSRF tokens, or private links.

## Compatibility

Version 1.2.2 supports FunPay Cardinal 0.1.17.8 and requires outbound access to `https://api.buywell.pro`.
