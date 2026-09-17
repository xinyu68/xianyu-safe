# Write safety

Apply these rules before operations that can change a Xianyu account.

## Messages

- Resolve the exact existing conversation first and show the recipient, related
  item, and exact message text
- Run `send-message` without `--confirm-send` for a dry-run preview
- Add `--confirm-send` only after the user explicitly approves that recipient
  and message
- After Enter is pressed once, never retry automatically when delivery is
  uncertain

## Listings

- Run `publish`, `edit-item`, or `manage-item` without confirmation flags first
- Show the returned preview, including item ID and requested changes
- A real operation requires the corresponding `--confirm-*` flag and a new
  `--idempotency-key`
- Deletion additionally requires `--confirm-delete <item-id>` with an exact
  match
- Never infer approval for a different item, price, image set, recipient, or
  action from an earlier confirmation

## Human verification

Login challenges, captchas, device confirmation, and risk-control pages are
human actions. Keep the browser visible only when the user needs to complete one
of them. Do not request raw cookies or attempt to bypass these controls.
