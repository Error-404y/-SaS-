# SaS Police Simulator verification bot

Python 3.11+ Discord bot for a small community. All data is saved automatically in `data/verification.json`. No SQL or database installation is needed. Messages use embeds, and the Python source contains no comments, docstrings or emojis.

## Application workflow

1. New members receive Verify-Progress. Previously accepted members get Member restored when they return. Revoked members do not regain Member.
2. The public panel offers English through **Verify** or German through **Deutsch**.
3. Applicants read the configured rules and explicitly click the agreement button.
4. A form asks for their game username, preferred patrol times/timezone, and experience.
5. The bot saves the application, sends a submission DM, and posts the staff review embed.
6. Staff may claim the request, ask questions, accept with an optional note, or reject using an editable template/custom reason.
7. Acceptance gives Member and removes Verify-Progress. The acceptance DM includes welcome instructions after role updates succeed.

The in-game username is self-reported, not proof of ownership. Validation permits platform-neutral names, including spaces and punctuation, but rejects empty names, control characters, links and Discord mention syntax. A matching username on another pending/accepted application produces a staff warning rather than automatic rejection.

## Applicant controls

Use **My application / Mein Antrag** on the public panel to view the latest application. This works even with DMs disabled.

- **Cancel application** withdraws a pending request. A new application can then be submitted.
- **Answer staff** appears when staff have asked an unanswered question. The applicant replies through a form; the answer appears on the staff embed and in the private log.
- The saved language controls applicant forms, status messages, system notifications, rejection templates and welcome text. Staff-written text is delivered as written, without automatic translation.
- Rules agreement text, application answers, decisions and explanations are retained locally.

## Staff controls

All staff actions require role `1562535759276417065` and must take place in `verify-check`. Self-review is blocked. Staff access is checked again when submitting a modal.

| Control | Behavior |
| --- | --- |
| Accept | Opens an optional acceptance-note form, then grants verification. |
| Reject | Offers custom, incorrect-username, insufficient-detail and rules templates, then an editable required-reason form. |
| Claim / Release | Claims a pending request or releases your own claim. Only its claimant can ask questions or decide while claimed. |
| Request information | Sends a question to the applicant and displays it under My application. Asking also claims the request. |
| Undo decision | Requires a reason and reopens the latest accepted/rejected application. Undoing acceptance removes Member and restores Verify-Progress. |
| Revoke verification | Requires a reason, changes an accepted request to revoked, removes Member and restores Verify-Progress. |

Claims persist across restarts. The claimant should release a request before handing it to another reviewer. Old applications cannot be reopened after a newer application is submitted. Cancelled and revoked requests cannot be undone; the applicant can submit again. Every action checks the saved revision so an outdated form cannot overwrite a later action.

Use this slash command **inside verify-check** to pause/resume submissions or change capacity:

```text
/verification-settings paused:True queue_limit:40 reason:Staff are catching up on current applications
/verification-settings paused:False queue_limit:40
```

A queue limit of `0` means unlimited. The default is 40 pending applications. Claimed requests and requests awaiting information count toward the limit. Pausing blocks new submissions, including already-open forms, while existing applications can still be reviewed, answered or cancelled. Undo respects the queue limit; increase it before reopening if necessary. Saved command settings persist across restarts; `QUEUE_LIMIT` initializes a new store only.

## Existing server IDs

| Resource | ID |
| --- | --- |
| Verification category | `1562540102394060840` |
| verify-please channel | `1562540152276910093` |
| High-Rank role | `1562535759276417065` |
| Verify-Progress role | `1562535216050864209` |
| Member role | `1562535352290250842` |
| High-Rank category | `1562539595474927637` |
| verify-check channel | `1562539651229683732` |
| verified-log channel | `1562546382772899910` |

Private log embeds record submissions, claims/releases, questions/answers, decisions, cancellations, role application and settings changes. Records include event/application IDs, actor, time and explanation. Failed log delivery retries without preventing role updates or applicant notifications.

## Discord setup

1. Create an application in the [Discord Developer Portal](https://discord.com/developers/applications), create its bot and copy its token to a local `.env` file.
2. Enable **Server Members Intent**. Message Content Intent is not required.
3. Invite the bot with OAuth2 scopes `bot` and `applications.commands`. Grant **Manage Roles**, **View Channels**, **Send Messages**, **Embed Links** and **Read Message History**.
4. Move the bot's highest role above both Member and Verify-Progress. Administrator permission is not required.
5. Allow new members to view `verify-please` and read its message history. They do not need Send Messages to use buttons.
6. Deny @everyone View Channel for `verify-check` and `verified-log`. Allow High-Rank and the bot to view them. Check other role/member overrides too, so applicants cannot see private applications or logs. Allow the bot to send embeds and read history in these channels.
7. Keep both verification channels in their configured categories. The log channel must be a separate text channel in the same server.

The default log channel ID is already configured. If you deliberately set `LOG_CHANNEL_ID=0`, the bot can instead create a private `verification-logs` channel under High-Rank; this optional mode requires Manage Channels. Existing channel permissions are not rewritten. Startup checks report missing channels, roles or permissions.

Guild commands are synced on startup. If Discord hides `/verification-settings` from your staff, check the application's command permissions under Server Settings > Integrations. The command always enforces High-Rank membership in code.

API behavior follows [discord.py interactions documentation](https://discordpy.readthedocs.io/en/stable/interactions/api.html) and [Discord's member-intent documentation](https://github.com/discord/discord-api-docs/blob/main/developers/events/gateway.mdx).

## Install and start

macOS / Linux:

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
cp .env.example .env
```

Edit `.env` and set `DISCORD_TOKEN`, then run:

```sh
python main.py
```

Windows PowerShell:

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
```

Edit `.env`, then run:

```powershell
.\.venv\Scripts\python.exe main.py
```

For your hosting panel, set **Startup File / STARTUP_FILE** to `main.py`. `bot.py` also remains a supported entry point. The host must keep the data directory between restarts and deployments. Never put a real token in GitHub.

Set `RULES_EN`, `RULES_DE`, `WELCOME_EN` and `WELCOME_DE` in `.env` to your server-specific text. Rules can include a link to the rules channel. Defaults provide a short rules acknowledgement and instructions to read announcements and contact High-Rank for a first patrol. English and German are available by default.

## Docker

Create `.env`, then:

```sh
docker compose up -d --build
docker compose logs -f
```

The named volume preserves the JSON file. Run exactly one instance per file. Do not delete the volume when updating.

## Upgrade and data safety

Stop the previous version, back up `data/verification.json`, deploy the new files, and restart. Existing JSON applications and the panel ID are retained. Missing fields are initialized automatically; existing accepted applications become the source for returning-member recognition. Legacy applications default to English and show no rules agreement if none was previously collected.

Each write uses a separate temporary file, flushes it, and atomically replaces the data file. Failed writes preserve the current file and in-memory state. Invalid data stops startup without overwriting the file. Keep regular backups; atomic replacement does not replace backups. Do not edit JSON while the bot is running.

If you still have the original SQLite version, that file is not imported automatically. Retain it as a backup and set `DATABASE_PATH=data/verification.json` before starting a fresh JSON store.

## Recovery behavior and limits

- On startup, human members are reconciled. Saved approval/revocation takes precedence; members with Member and no saved state keep verified access. Other members receive Verify-Progress.
- Accepted returning members regain Member. Role failures retry every 30 seconds. Users who have left remain saved, allowing role updates when they return.
- Role changes use the latest saved member state. Undo and revocation replace earlier pending role work.
- Existing public and review buttons are restored on startup. Deleted review messages are recreated when detected or on restart. The public panel is recreated when its deletion is detected or on restart.
- DMs blocked by Discord are not retried indefinitely. Applicants can use My application instead. Transient Discord failures retry.
- Discord delivery and local file writes are separate operations. A crash after sending a message but before saving success may produce a duplicate DM or log. Untracked duplicate review messages cannot change application state.
- Staff-written questions and previous answers are retained in the event log even when a new question replaces the latest question on the application embed.
- A departed claimant must return and release their claim before another staff member can decide that request. The applicant can also cancel and resubmit.
- Live Discord permissions, hosting availability and successful token login cannot be verified by the offline tests.

## Tests

```sh
python -m unittest discover -s tests -v
```

Tests use real temporary JSON files and mocked Discord calls. They cover persistence and legacy migration, failed writes, claims, cancellation, questions, stale forms, undo/revocation, queue limits, duplicate usernames, authorization, notifications, role recovery and comment/emoji-free source. CI runs Python 3.11 through 3.14.

Before opening applications, use separate applicant and staff accounts to test rules agreement, submission, a question/answer, cancellation, template rejection, acceptance with a note, undo, revocation and rejoining. Repeat with DMs closed, and check that the log channel is visible only to intended staff and the bot.
