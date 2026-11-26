# 4.18.1 — use the existing Windows OneDrive client without new OAuth consent

The user cannot grant an additional Microsoft app access without administrator
approval. Provide a local, on-demand downloader using the existing approved
OneDrive sync folder at AI/tradingbot/Backups. It authenticates only to the bot
and stores that password with current-user Windows DPAPI outside OneDrive.
No Microsoft OAuth, app registration, refresh tokens or new OneDrive consent.

Installer/launcher copies go under AI/tradingbot/Oppsett. The downloader uses
the existing authenticated, budget-guarded asynchronous export endpoint and
checks the ZIP and each database hash before moving it into OneDrive.

PC and OneDrive must be running for downloading/syncing; PAPER trading stays
on Render. Local download success is not claimed as verified cloud upload.
No Render source data is deleted and no background/hourly task is enabled.
