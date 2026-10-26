# SaS verification bot

A small Discord verification bot using Python and one local JSON file. No SQL or database server. All messages use embeds. No comments, docstrings or emojis in the Python source.

## How it works

1. New members receive Verify-Process.
2. They choose **Verify - Member** or **Verify - Allies** and enter their Police Simulator username. Allies also enter their clan name.
3. An embed appears in `verify-check` with exactly two buttons: **Accept** and **Reject**.
4. Accept adds the requested Member or Allies role, removes Verify-Process, and sets the nickname to `[SaS] Username` for Members or `[Clan] Username` for Allies. Reject asks for a reason and leaves roles unchanged.
5. Applicants receive submission and result DMs. The review shows submission and result delivery separately: sent successfully, failed, or pending retry. Sent means Discord accepted delivery, not that the user read it. Closed DMs do not block verification.
6. The staff embed is updated with the result, reviewer and Zurich time. Completed requests have no buttons.

Administrators and High-Rank staff can review requests, including their own. The bot rechecks permissions when a decision is submitted. Ordinary members cannot approve themselves. Only one pending or unfinished approved request per user is allowed, and a second reviewer cannot overwrite a decision.

Staff reviews keep exactly Accept and Reject. The public panel also provides Name change and Clan change. Management buttons live in the staff control center. The old verification-settings command is removed on startup. To reject a request, enter a plain reason. After rejection, wait 10 minutes before applying again. The same cooldown applies to verification and change requests, including already-open forms, and survives restarts and deletion of the rejected review. Set `REJECTION_COOLDOWN_SECONDS` in `.env` to change the duration for future rejections (0 disables it).

## Names and verification types

Each person has one verification type and one pending request across all forms. An Allies application requires **in-game username** and **clan name**. For example, username `hqayoo` and clan `SaS` produce `[SaS] hqayoo`, but the chosen Allies application still grants only Allies. A tag is not proof of membership; access comes from staff-approved roles.

**Name change** asks for the username only and retains the approved clan tag. **Clan change** asks whether the user is joining SaS:

- **Yes - Join SaS** creates a Member verification request using the saved username. On acceptance, Member is added, Allies is removed, and the nickname becomes `[SaS] Username`.
- **No - Another clan** asks which clan they are joining. On acceptance, the new tag is applied. A current Member moving to another clan becomes an Ally; Member is removed only after Allies is granted. The review explicitly shows this role change.

Nothing changes before approval. Rejected or deleted pending changes leave the saved profile, nickname and access untouched. Name changes do not change roles. If a role conversion partially fails, the original profile remains until the update completes; a restart resumes it. New requests wait until the previous approved update finishes, preventing older retries from overwriting newer names.

Staff embeds highlight duplicate game names across current saved profiles. Matching ignores letter case, repeated whitespace and Unicode compatibility differences. Warnings do not automatically reject anyone. After an approved name change, the old name is released for duplicate matching. This compares local records, not a Police Simulator account service, so it cannot prove ownership of a game name.

New requests must fit Discord's 32-character nickname limit including `[Clan] `. Clan names allow 1 to 12 characters: letters, numbers, spaces, hyphens and underscores. Usernames should not include a clan tag. Oversized new requests get an explanation instead of silent truncation. Legacy long names are shortened only for the Discord nickname, preserving the tag and the full saved name.

Transient nickname failures retry. If Discord denies or rejects the change, the review and result DM explain that staff must set it manually; approval still completes and the new approved profile is saved. The bot cannot rename the server owner or users at or above its role, even if those users can approve themselves. Grant Manage Nicknames and move the bot role above users it should rename.

## Existing Member nicknames

On startup the bot discovers current Member-role holders and queues a one-time `[SaS]` nickname update for each person. It uses the saved game username when available; otherwise it uses the current nickname or Discord username with existing bracketed tags removed. It does not duplicate `[SaS]`, change roles, send DMs or post verification logs. Newly granted Member roles and returning Members are queued too.

Jobs persist across restarts. Completed jobs are not repeated on every startup. A permanent permission failure is recorded once in the console; staff should then set the nickname manually. Transient failures retry. This pass leaves Allies alone, and legacy Allies without a recorded clan can use Clan change to supply it.

## Pausing applications

In `staff-control-center` (`1564388402055483533`), use the persistent **Pause applications** button and enter an explanation, or select **Resume applications**. The bot posts and maintains one control embed there. It reuses the saved panel after a restart. The public verification panel has no management controls. Only role `1562535759276417065` (the configured High-Rank role) can perform either action. Administrator permission alone does not grant this control. The bot checks the channel and High-Rank role before opening the pause form and fetches current roles again before applying pause or resume. Old controls in the public channel cannot change the setting.

A pause disables both Verify buttons and displays the explanation in the public embed. The saved pause survives restarts, blocks previously opened verification forms, and blocks new Join SaS requests. Existing staff reviews and name/other-clan changes remain available. Public and staff panel updates retry independently after failures; the server-side pause takes effect immediately even if the old buttons are still visible.

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

There is no server-wide role reassignment on startup. Existing roles are preserved except for an explicitly approved clan conversion. Startup resumes unfinished requests and queued join-role work, and discovers Member nickname-prefix jobs.

New joiners get Verify-Process. Previously accepted users get their saved Member or Allies role restored when they return. A member who joined while the bot was offline can click Verify; the bot adds Verify-Process when they submit. Existing Member or Allies holders do not need to apply and keep their roles.

If earlier versions removed Member from someone incorrectly, restore Member manually once. The nickname-prefix pass preserves it. Returning-member restoration uses saved accepted applications; it does not treat every historical role assignment as an acceptance.

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
| staff-control-center | `1564388402055483533` |

Create a bot in the [Discord Developer Portal](https://discord.com/developers/applications). Enable **Server Members Intent**. Invite it with the `bot` scope and **Manage Roles**, **Manage Nicknames**, **View Channels**, **Send Messages**, **Embed Links** and **Read Message History** permissions. Move the bot role above Member, Allies and Verify-Process. Message Content Intent and Administrator permission are not required for the bot.

Allow new users, Members and Allies to view `verify-please`, so verified users can access name and clan change controls. Keep `verify-check` and `verified-log` private to intended staff and the bot. Deny @everyone View Channel and inspect other role/member overrides. Keep staff-control-center visible to High-Rank staff and the bot. Allow the bot View Channel, Send Messages, Embed Links and Read Message History in all four channels. CONTROL_CHANNEL_ID can be changed in .env. Keep the verification channels inside their configured categories.

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

Stop the bot and back up `data/verification.json`, then deploy and restart. The first run converts version 1 JSON to version 2 and also saves the original as `data/verification.json.v1.bak`. Applications and the public panel ID are preserved. Existing version 2 files gain compatible profile, clan, pause and nickname-job defaults; no JSON reset is required. The updated Docker image includes the `identity.py` and `delivery.py` modules, which must also be uploaded for non-Docker hosting. Old question/claim/event data remains in that backup but no longer drives the bot.

Completed reviews are not rewritten on every restart. Pending updates resume when their saved state requires it. Missing messages stay deleted. Legacy pending records without a message ID are closed conservatively so the bot cannot accidentally recreate a deleted request; those users may apply again. Existing successful applications do not create a burst of history posts during migration.

The active configuration contains the token, JSON path, channel IDs, category IDs, role IDs and rejection cooldown. Old rules, language, queue and extra-question settings are ignored and may be removed from `.env`.

## Message updates and rate limits

Startup no longer marks every historical review for editing. Dirty reviews, history embeds and public/staff panels are compared with the message already on Discord; unchanged embeds and buttons do not produce PATCH requests. Approval processing combines role, nickname and DM results before its final review edit. Necessary edits use a persistent per-channel queue. The bot starts with a conservative six-second spacing policy, spreads remaining capacity according to Discord response headers, and increases spacing after a 429. Server retry delays and reset windows always take precedence. The queue saves its next allowed edit time before sending, so a restart cannot reset the cooldown.

The queue checks for ready messages every five seconds without sleeping inside a staff action. Multiple changes to the same review merge into its latest saved state. Role updates and DMs can complete while a review waits; its old buttons cannot override a saved decision. Delayed review/history edits do not repeatedly fetch the same message while their channel is cooling down. Role and DM recovery retains its separate 30-second interval.

Discord can still return a temporary 429 when it imposes a previously unknown tighter limit or other activity consumes the quota. discord.py continues to honor Discord's retry delay; rate-limit warnings are not hidden. The HTTP trace reads the response headers and 429 body using the supported discord.py trace hook, while leaving the original response available for library retry handling. Rate-limit state is saved in the existing JSON file. The changes remove avoidable bursts rather than promise unlimited requests. See [Discord rate-limit documentation](https://docs.discord.com/developers/topics/rate-limits).

The requirements now include PyNaCl, removing the optional voice-library warning after dependencies are installed. Run `python -m pip install -r requirements.txt` before restarting, or rebuild the Docker image. The bot does not join voice channels.

## Reliability and tests

Writes use a flushed temporary file and atomic replacement. Invalid data is not overwritten. Keep backups; atomic writes are not a substitute for backups. Discord and the JSON file cannot share a transaction: an interrupted send can still have an ambiguous result. Review/history discovery reduces duplicate posts, but an interrupted DM may be sent twice.

Role updates and DMs retry every 30 seconds. Dirty message updates are checked every five seconds and sent only when their pacing window permits. Permanent DM denial is recorded rather than repeatedly retried. A deleted history or review message is not retried as a new post. If an accepted applicant leaves, role completion waits until they return.

```sh
python -m unittest discover -s tests -v
```

Tests exercise approval-only name and clan changes, role conversions, pause authorization, DM indicators, nickname-prefix migration, real temporary JSON files, migration/backups, role-state changes, complete versus partial transfers, deletion across restarts, staff permissions and self-review, duplicate decisions, DM failures, history deduplication, actual Discord interaction serialization, Zurich time and comment/emoji-free source. Live testing still requires your bot token and server permissions.
