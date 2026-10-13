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

## Favorites

- Preview `favorite` / `unfavorite` for the exact item before executing. A
  user's explicit request naming the item and desired action is authorization;
  do not require redundant confirmation of that same request.
- Real execution uses `--confirm-action` and a unique `--idempotency-key`.
  Read `buyerDO.isCollected`, not `collectCnt` or `favored`, to determine the
  current account's collection state. If already matching, do not submit.
- Persist a receipt before the single add/remove request; verify the state
  afterward with read-only item-detail requests. Unknown results stop. Do not
  use a new key to automatically retry them. Replayed receipts describe the
  original attempt, not necessarily today's state.
- Read-only `favorites` uses the official collection-list API; unrelated
  recommendation cards are not evidence of a saved item.

## Human verification

Login challenges, captchas, device confirmation, and risk-control pages are
human actions. Keep the browser visible only when the user needs to complete one
of them. Do not request raw cookies or attempt to bypass these controls.
