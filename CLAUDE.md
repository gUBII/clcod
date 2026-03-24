# Project Instructions

## Git — OFF LIMITS

- NEVER run git commands (commit, push, pull, checkout, branch, merge, rebase, tag, etc.).
- NEVER run gh commands (pr, issue, release, etc.).
- NEVER append `Co-Authored-By` lines or any AI attribution/watermarks to anything.
- All git operations are handled manually by the user.

## PM2 Services

| Port | Name | Type |
|------|------|------|
| 4173 | clcod-4173 | Python (supervisor.py + relay) |

**Terminal Commands:**
```bash
pm2 start ecosystem.config.cjs   # First time
pm2 start all                    # After first time
pm2 stop all / pm2 restart all
pm2 start clcod-4173 / pm2 stop clcod-4173
pm2 logs / pm2 status / pm2 monit
pm2 save                         # Save process list
pm2 resurrect                    # Restore saved list
```
