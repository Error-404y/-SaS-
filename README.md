# SaS Police Simulator verification bot

Python 3.11+ Discord bot with a local JSON data file. Suitable for a small community of around 40 users; no SQL, database installation or database server required.

## What it does

- Gives new human members **Verify-Progress** on joining.
- Automatically posts an embed with a persistent **Verify** button in `verify-please`.
- Collects the Police Simulator in-game username in a modal.
- Saves the application and posts an embed with **Accept / Reject** in `verify-check`.
- Only members holding the configured **High-Rank** role can review; self-review is disabled.
- Reject opens a required explanation modal. The applicant receives the explanation and may reapply.
- Acceptance gives **Member**, removes **Verify-Progress**, and sends an acceptance DM.
- Sends submission DMs and ephemeral embed confirmations. Closed DMs do not cancel applications.
- Keeps application history, reviewer, timestamps, reasons, message IDs and delivery state in a local JSON file.
- Buttons survive restarts; interrupted application delivery and accepted-role updates retry every 30 seconds.
- Simultaneous reviews cannot overwrite a decision; only one pending application per user/server is allowed.

This collects a self-reported game username for human review; it does not prove game account ownership or connect to a game API. Modals are Discord's native forms; messages are embeds.

## 1. Create and invite the Discord bot

1. Open the [Discord Developer Portal](https://discord.com/developers/applications), create an application, and open **Bot**.
2. Generate/copy the bot token into your local `.env` (never commit or share it).
3. Enable **Server Members Intent** on the Bot page. Message Content Intent is not needed.
4. In **OAuth2 → URL Generator**, select `bot` and permissions **Manage Roles**, **View Channels**, **Send Messages**, **Embed Links**, and **Read Message History**. Open the generated URL to invite it to your server.
5. Move the bot's highest role **above both Verify-Progress and Member** in Server Settings → Roles. Administrator permission is not needed.

Discord requires enabling privileged member intent in the portal as well as the code: [official gateway documentation](https://github.com/discord/discord-api-docs/blob/main/developers/events/gateway.mdx). Persistent buttons follow [discord.py's persistent view example](https://github.com/Rapptz/discord.py/blob/master/examples/views/persistent.py).

## 2. Check server permissions

The bot uses these existing IDs, already filled into `.env.example` and the code:

| Resource | ID |
| --- | --- |
| Verification category | `1562540102394060840` |
| verify-please channel | `1562540152276910093` |
| High-Rank review role | `1562535759276417065` |
| Verify-Progress role | `1562535216050864209` |
| Member role | `1562535352290250842` |
| High-Rank category | `1562539595474927637` |
| verify-check channel | `1562539651229683732` |

Both channels must be text channels in the indicated categories and in the same server. The bot checks this at startup.

Configure channel/category overrides in Discord:

- `verify-please`: allow new members/Verify-Progress to View Channel and Read Message History. You may deny them Send Messages; buttons still work.
- `verify-check`: deny @everyone View Channel; allow High-Rank and the bot View Channel. Allow the bot Send Messages, Embed Links, Read Message History.
- Other member channels: allow Member; restrict unverified users as desired.

The bot does not change channel permissions. Check them so application details stay private.

## 3. Run locally

macOS / Linux:

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
cp .env.example .env
# Edit .env and set DISCORD_TOKEN.
python bot.py
```

Windows PowerShell:

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
# Edit .env and set DISCORD_TOKEN.
.\.venv\Scripts\python.exe bot.py
```

The local file `data/verification.json` is created automatically. Every change is saved immediately using a temporary file and atomic replacement to avoid partial writes. The bot logs startup errors with the relevant configuration/permission problem. When ready, the Verify panel appears automatically. Existing panels are reused; a deleted panel is replaced on the next restart. A bot token and a running host are required; installing the code alone does not keep the bot online.

## Docker (optional, for a server that stays online)

With Docker and Compose installed, create `.env` as above, then:

```sh
docker compose up -d --build
docker compose logs -f
```

The named volume retains the JSON data file across container replacement. Run only **one bot instance** against this file. Do not use `docker compose down -v` unless you intend to delete application data.

## Tests and live smoke test

```sh
python -m unittest discover -s tests -v
```

Automated tests use mocked Discord calls and real temporary JSON files. They cover duplicate applications, concurrent decision protection, persistent views, acceptance/rejection, closed DMs, role failure recovery and authorization. CI runs them on Python 3.11–3.13.

Before using with your community, test with a separate non-staff account:

1. Join the server; confirm Verify-Progress is added.
2. Click Verify, enter a name, and submit. Check the DM and staff embed.
3. Try review as a non-High-Rank user; it must be denied.
4. Reject as High-Rank; confirm the reason DM and reapplication works.
5. Restart the bot with a pending application; accept using the existing buttons.
6. Confirm Member is added, Verify-Progress removed, and the acceptance DM arrives.
7. Repeat with applicant DMs disabled; the workflow must still complete.

## Operational notes

- Keep the process running for join-role assignment. Joins while offline are not backfilled. Join-role permission failures are logged; correct the permission issue and assign that member's role manually.
- If an accepted applicant leaves before roles are applied, the stored decision remains and role application retries when they return. Acceptance DM is sent only after role changes succeed.
- An applicant who has already received their roles and later leaves/rejoins follows the verification flow again.
- Discord and the local JSON file cannot be updated as one atomic operation. A process crash immediately after sending a message but before saving its ID can result in a duplicate DM or staff message. An untracked duplicate review cannot change a decision. Keep one instance running.
- Deleted pending staff messages are not automatically recreated. Restore access/review messages before deleting them; the JSON file retains their application history. Restarting restores the public Verify panel if deleted.
- Back up `data/verification.json` with the bot stopped. Restore it to the same path before restarting. Do not manually edit it while the bot is running. An invalid file stops startup instead of overwriting existing data. Protect the file: it contains Discord IDs, game usernames and rejection reasons. Decide your own retention policy.
- Permission errors after startup are logged and application delivery is retried. Fix channel access or role hierarchy and wait for retry. A closed-DM result is recorded as blocked and is not repeatedly retried.

If you previously ran the SQLite version, keep the old file as a backup. This version starts a new JSON store and does not automatically import old SQLite applications. Update `DATABASE_PATH` in any existing `.env` to `data/verification.json`.
