# SaS verification bot

A small Discord verification bot using Python and one local JSON file. No SQL or database server. All messages use embeds. No comments, docstrings or emojis in the Python source.

## How it works

1. New members receive Verify-Process.
2. They choose **Verify - Member** or **Verify - Allies** and enter their Police Simulator username.
3. An embed appears in `verify-check` with exactly two buttons: **Accept** and **Reject**.
4. Accept adds the requested Member or Allies role, removes Verify-Process, and sets the server nickname to the game username. Reject asks for a reason and leaves roles unchanged.
5. Applicants receive submission and result DMs. Closed DMs do not block verification.
6. The staff embed is updated with the result, reviewer and Zurich time. Completed requests have no buttons.

Administrators and High-Rank staff can review requests, including their own. The bot rechecks permissions when a decision is submitted. Ordinary members cannot approve themselves. Only one pending application per user is allowed, and a second reviewer cannot overwrite a decision.

There are no claims, extra questions, rules prompts, language controls, cancellation/status buttons, undo/revoke buttons, rejection-template menus, or settings commands. The old verification-settings command is removed on startup. To reject a request, enter a plain reason. After rejection, wait 10 minutes before applying again. The same cooldown applies to both buttons, including already-open forms, and survives restarts and deletion of the rejected review. Set `REJECTION_COOLDOWN_SECONDS` in `.env` to change the duration for future rejections (0 disables it).

## Names and verification types

Each person has one verification type and one pending application across both buttons. Selecting Allies grants only Allies, not Member. Already-verified users cannot use the other button to obtain a second role; staff can manage later membership changes manually.

Staff embeds highlight duplicate game names already registered to another accepted user with a completed role update. Matching ignores letter case, repeated whitespace and Unicode compatibility differences. Warnings work across Member and Allies records and do not automatically reject anyone. Pending reviews refresh after another application completes. This compares the local verification records, not a Police Simulator account service, so it cannot prove ownership of a game name.

New approvals set the nickname once. Discord nicknames are limited to 32 characters; longer submitted game names remain intact in the application but use their first 32 characters as the nickname. Transient failures retry. If Discord denies or rejects the change, the review embed tells staff to set it manually; role approval and the result DM still complete. The bot cannot rename the server owner or members above it in the role hierarchy, even when they can approve themselves. Move the bot role above users it should rename and grant Manage Nicknames.

Existing completed applications retain their nicknames during upgrade. Version 2 JSON is read compatibly: previous applications are treated as Member requests, without replaying old role changes or applying cooldowns retroactively. The public panel is updated in place with both buttons on startup.

## Quiet verification history

The `verified-log` channel receives a history embed only when an accepted application completes **both** changes:

- The requested Member or Allies role was absent and is now present.
- Verify-Process was present and is now absent.

The bot checks Discord before and after the role update. If an update partially fails, it saves the original state and waits until the remaining change is confirmed. The successful transfer is recorded once, including across restarts.

There is **one history embed per user**, which is edited when another qualifying transfer is recorded. It shows username, accepting staff member and Zurich time. The latest eight entries are displayed; all entries remain in the JSON file. Submissions, rejections, joins, unchanged roles and partial changes do not generate log messages. Zurich time automatically switches between CET and CEST.

Old log messages are not automatically deleted or replayed. You can delete them yourself. Deleting a history embed suppresses further posting for that saved history; its data stays in JSON.

## Deleted staff messages stay deleted

The bot never recreates a known deleted message in `verify-check`, whether deletion happens while running, during downtime, or in a bulk deletion.

- A deleted completed request stays completed and is never posted again.
- A deleted pending request is closed, so its applicant can submit a new request. A one-time DM explains this.
- Deleting an accepted request does not undo acceptance or cancel an already-saved role update.
- New requests are still posted normally. Failed initial delivery is retried; the bot checks for an already-sent matching message before posting to reduce duplicates after a crash.

## Roles and restarts

There is no server-wide role reassignment on startup. Existing Member and Allies roles are never removed by this version. Startup resumes unfinished accepted applications and queued join-role work only.

New joiners get Verify-Process. Previously accepted users get their saved Member or Allies role restored when they return. A member who joined while the bot was offline can click Verify; the bot adds Verify-Process when they submit. Existing Member or Allies holders do not need to apply and keep their roles.

If earlier versions removed Member from someone incorrectly, restore Member manually once. This version will preserve it. Returning-member restoration uses saved accepted applications; it does not treat every historical role assignment as an acceptance.

## Server configuration

| Resource | Default ID |
| --- | --- |
| Verification category | `1562540102394060840` |
| verify-please | `1562540152276910093` |
| High-Rank role | `1562535759276417065` |
| Verify-Process role | `1562535216050864209` |
| Member role | `1562535352290250842` |
| Allies role | `1562903902662168587` |
| High-Rank category | `1562539595474927637` |
| verify-check | `1562539651229683732` |
| verified-log | `1562546382772899910` |

Create a bot in the [Discord Developer Portal](https://discord.com/developers/applications). Enable **Server Members Intent**. Invite it with the `bot` scope and **Manage Roles**, **Manage Nicknames**, **View Channels**, **Send Messages**, **Embed Links** and **Read Message History** permissions. Move the bot role above Member, Allies and Verify-Process. Message Content Intent and Administrator permission are not required for the bot.

Allow new users to view `verify-please`. Keep `verify-check` and `verified-log` private to intended staff and the bot. Deny @everyone View Channel and inspect other role/member overrides. Allow the bot to read message history and send embeds in all three channels. Keep the verification channels inside their configured categories.

## Install and run

macOS / Linux:

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
cp .env.example .env
```

Windows PowerShell:

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
```

Edit `.env` and set `DISCORD_TOKEN`, then run `python main.py` with the virtual environment active. On Windows you can also use `.\.venv\Scripts\python.exe main.py` directly.

For the hosting panel, set **STARTUP_FILE** to `main.py`. `bot.py` also works. Keep `data/verification.json` across deployments. Never put the token in GitHub.

Docker:

```sh
docker compose up -d --build
docker compose logs -f
```

The named Docker volume preserves JSON data. Run only one bot instance per file.

## Updating from the previous bot

Stop the bot and back up `data/verification.json`, then deploy and restart. The first run converts version 1 JSON to version 2 and also saves the original as `data/verification.json.v1.bak`. Applications and the public panel ID are preserved. Old question/claim/event data remains in that backup but no longer drives the bot.

Old completed requests are simplified in place if their messages still exist. Missing messages stay deleted. Legacy pending records without a message ID are closed conservatively so the bot cannot accidentally recreate a deleted request; those users may apply again. Existing successful applications do not create a burst of history posts during migration.

The active configuration contains the token, JSON path, channel IDs, category IDs, role IDs and rejection cooldown. Old rules, language, queue and extra-question settings are ignored and may be removed from `.env`.

## Reliability and tests

Writes use a flushed temporary file and atomic replacement. Invalid data is not overwritten. Keep backups; atomic writes are not a substitute for backups. Discord and the JSON file cannot share a transaction: an interrupted send can still have an ambiguous result. Review/history discovery reduces duplicate posts, but an interrupted DM may be sent twice.

Role updates, initial review delivery, DMs and history edits retry every 30 seconds. Permanent DM denial is recorded rather than repeatedly retried. A deleted history or review message is not retried as a new post. If an accepted applicant leaves, role completion waits until they return.

```sh
python -m unittest discover -s tests -v
```

Tests exercise real temporary JSON files, migration/backups, role-state changes, complete versus partial transfers, deletion across restarts, staff permissions and self-review, duplicate decisions, DM failures, history deduplication, actual Discord interaction serialization, Zurich time and comment/emoji-free source. Live testing still requires your bot token and server permissions.
