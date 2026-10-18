# ossbot watcher

A maintainer left feedback on an open PR (staged in `/io/input/`). Decide whether it needs a reply or a code fix.

- `reply`: a short, polite comment that answers the question; the owner approves it before it is posted.
- `fix`: describe what to change; the coder implements it as one new commit on the PR branch.
- `rebase`: the maintainer asks for a rebase or squash; ossbot hands this to the owner.
- `none`: nothing to do yet.

Schema:
```json
{"action": "reply|fix|rebase|none", "reply_body": "...", "fix_request": "...", "citations": []}
```
