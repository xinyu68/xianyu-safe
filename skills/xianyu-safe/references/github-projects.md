# GitHub references

The implementation was informed by these public projects while keeping a
smaller, approval-oriented safety boundary:

- `jackwener/opencli`: current Xianyu search, item, inbox, reply, and publish
  adapter patterns
- `zhinianboke/xianyu-auto-reply`: message handling, auto-delivery, review,
  refresh, and publishing feature survey
- `dameng2026/xianyu-pilot`: account, item, order, and message capability survey
- `harringtonwalker/xianyu-automation`: approval-gated publish and receipt
  handling patterns
- `smartcat999/xianyu-publish`: publish, edit, unlist, relist, delete, and message
  summary feature survey

This project does not copy credentials, cookies, or bypass logic from those
projects. It uses the user's own browser session on the official Xianyu site.
