# REV Inbox — Local Mail Import Directory

Drop locally-exported `.eml` files here and run `vertex rev run --eml-inbox <this-dir> --program <id> --mailbox <upn>`.

## Directory layout (auto-created on first run)

```
rev_inbox/
  inbox/       ← Drop .eml files here (EmlEnumerator reads from this dir)
  claimed/     ← In-flight: atomic rename from inbox/ before processing
  processed/   ← Completed: moved after successful hydration + staging
  quarantine/  ← Failed: parse error / timeout / body_empty / crash_loop
  cycle.lock   ← Portalocker file; prevents concurrent vertex rev run invocations
```

## Usage

1. Export emails from Outlook (File → Save As → .eml) or via PowerShell `Save-Message`.
2. Copy the `.eml` files into `inbox/`.
3. Run: `vertex rev run --program nova --mailbox you@example.com --eml-inbox programs/nova/rev_inbox`

## Privacy

- All files under `rev_inbox/` are gitignored (covered by `programs/[^_]*/` in `.gitignore`).
- Raw `.eml` files are never committed to git.
- Restrict directory ACL to your user account only (OA-4 privacy gate).
- Processed files are purged after 90 days or after evidence excerpts are vaulted.
