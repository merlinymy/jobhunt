# Bringing the mini up

Everything below was rehearsed on the laptop against an APFS disk image standing in for the
SSD, except steps 3, 12 and 14, which can only be done on the real machine. Run it in order;
each step's check is the reason the next one is safe.

Roughly 40 minutes, most of it waiting on downloads.

## 1. macOS

```bash
sudo scutil --set ComputerName jobhunt-mini
sudo scutil --set LocalHostName jobhunt-mini
sudo scutil --set HostName      jobhunt-mini
sudo pmset -a sleep 0 disablesleep 1 disksleep 0 autorestart 1
```

`disksleep 0` keeps the enclosure awake so the first request after a quiet night isn't slow.
`autorestart 1` brings the machine back after a power cut.

**Decide now: FileVault or auto-login.** They are mutually exclusive, and the choice decides
what an unattended reboot does. Without auto-login there is no Aqua session, so no `gui/$UID`
agents start — *and* the SSD's keychain passphrase is never applied, so the volume never
mounts. The machine is down until someone logs in at the console. With auto-login, everything
returns on its own, but the internal disk (which holds the backups) is unencrypted at rest.

For an always-on box I'd take auto-login and accept that the internal disk is the weaker copy —
the SSD holding the live database stays encrypted either way.

## 2. Tools

```bash
xcode-select --install
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
brew install uv git
```

## 3. The SSD — *not rehearsed*

Format APFS **Encrypted**, and name it `jobhunt` with no spaces so `/Volumes/jobhunt` is
stable. Unlock it in Finder once with **"Remember this password in my keychain"**.

**Then reboot and confirm it comes back mounted before anything depends on it.** Everything
downstream assumes this works; find out now, not at 03:30.

If `/Volumes/jobhunt` ever appears as a plain directory, remove it — otherwise macOS mounts the
real disk beside it as `/Volumes/jobhunt 1` and the guard will (correctly) refuse:

```bash
rmdir /Volumes/jobhunt
```

## 4. The repo — on the internal disk

```bash
git clone git@github.com:merlinymy/jobApplicationHelper.git ~/projects/jobApplicationHelper
cd ~/projects/jobApplicationHelper
make venv          # installs the extras; a bare install dies at 06:30 on `import jobspy`
make build-web     # Node 20+; compiles the React app into jobhunt/web/dist
```

**If this machine already had a checkout from before 2026-08-05, back up `docs/profile/`
before pulling.** That commit untracked it, and git deletes a file a commit removes — the
`.gitignore` entry does not protect a copy that is already tracked in your checkout:

```bash
cp -a docs/profile ~/profile-backup && git pull && cp -a ~/profile-backup/. docs/profile/
```

The repo stays on the internal disk deliberately. On the SSD, launchd could not exec the agents
when that disk is absent, and you would get no log saying why.

## 5. Configure

```bash
cp .env.example .env
```

Set `ANTHROPIC_API_KEY`. Leave `JOBHUNT_DB=/Volumes/jobhunt/jobhunt.db` and
`JOBHUNT_SQLITE_SYNCHRONOUS=FULL` as they are. Leave `JOBHUNT_DB_VOLUME_ID` blank for now.

## 6. Stamp the volume

```bash
./deploy/install.sh --init-volume
```

Paste the `JOBHUNT_DB_VOLUME_ID` it prints into `.env`. This is what lets the guard tell your
disk from any other volume that happens to mount at the same path.

## 6b. Your profile

`docs/profile/` is untracked, so a fresh clone does not have it. Push it from the laptop:

```bash
# on the laptop
make profile-push HOST=jobhunt-mini
```

Or start from the template — `cp -r docs/profile.example docs/profile` — and fill it in.
`make load-profile` fails with the exact command if the directory is missing.

## 7. Seed

Copy `seed-*.db` across from the laptop (`~/jobhunt-seed/`), then:

```bash
cp ~/seed-*.db /Volumes/jobhunt/jobhunt.db
chmod 600 /Volumes/jobhunt/jobhunt.db
make migrate
```

**It must print `up to date`.** If it prints `creating a new database at ...`, stop — the seed
did not land where `.env` points, and continuing gives you an empty database that looks fine.

The seed carries 1,703 jobs, 645 already scored, 3 built packets, and about $1.85 of LLM spend
you don't have to repeat. Starting empty is fine too; `make ingest && make score` rebuilds it.

## 8. Load the profile and check

```bash
make load-profile     # idempotent
make backup           # so the backups check has something to look at
make doctor           # every line ok, except tailscale until step 9
```

## 9. Tailscale — *not rehearsed*

Install the **standalone** build (`brew install --cask tailscale`), not the App Store one,
whose CLI is awkward to reach from a script.

```bash
sudo ln -s /Applications/Tailscale.app/Contents/MacOS/Tailscale /usr/local/bin/tailscale
tailscale up --hostname=jobhunt-mini
tailscale serve --bg --https=443 http://127.0.0.1:8000
tailscale serve status
```

In the admin console, once: **MagicDNS** on, **HTTPS Certificates** on, and **disable key
expiry** for this node. The 180-day default would take the whole thing dark with no warning.

Never `tailscale funnel`. That is the open internet in front of an app with no auth, holding
your legal name, phone number, every resume you've submitted, and 345 other people's contact
details.

## 10. Install the agents

```bash
./deploy/install.sh
```

It asks whether this is the mini, checks the venv and the extras, and runs `doctor` before
touching anything. `launchctl bootstrap` needs a live Aqua session — do this at the console or
over Screen Sharing, not from a bare SSH shell.

## 11. Prove each agent

```bash
launchctl list | grep jobhunt                                    # three labels
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8000/  # 200

launchctl kickstart -k gui/$(id -u)/com.jobhunt.backup
tail ~/Library/Logs/jobhunt/backup.log
.venv/bin/python -m jobhunt.backup --drill

launchctl kickstart -k gui/$(id -u)/com.jobhunt.discover
tail -f ~/Library/Logs/jobhunt/discover.log
```

**The discovery run is the first time it has ever actually executed** — it was broken by the
`__REPO__` bug from the day it was written until 2026-08-05. Watch this one. Budget ~25 minutes:
five for the Indeed sweep at its jittered pacing, ninety seconds for the boards, the rest
waiting on a scoring batch.

## 12. Prove it survives a reboot — *not rehearsed*

```bash
sudo reboot
```

Touch nothing afterwards. Then check, in order: the SSD is mounted, `launchctl list | grep
jobhunt` shows three labels, and `https://jobhunt-mini.<tailnet>.ts.net` loads from the laptop.
If any of those fail, it is almost certainly the FileVault/auto-login decision in step 1.

## 13. Prove the guard, on the real disk

This is the failure the whole design exists to prevent, so confirm it on real hardware:

```bash
make agents-stop
diskutil eject /Volumes/jobhunt
./deploy/install.sh --yes          # doctor will refuse — that is the point
JOBHUNT_WAIT_DB=5 bash deploy/discover.sh ; echo "want 0, got $?"
JOBHUNT_WAIT_DB=5 bash deploy/backup.sh   ; echo "want 1, got $?"
ls -d /Volumes/jobhunt             # must NOT exist
```

Then plug it back in and confirm the dashboard recovers on its own within a few seconds.

## 14. The laptop pull — *not rehearsed*

On the mini: **System Settings → General → Sharing → Remote Login**, limited to your user. Add
the laptop's public key to `~/.ssh/authorized_keys`. Tailnet ACLs already limit who can reach
port 22.

On the laptop, `~/.ssh/config`:

```
Host jobhunt-mini
  HostName jobhunt-mini.<tailnet>.ts.net
  User <you>
  IdentityFile ~/.ssh/id_ed25519
```

```bash
rsync -a --delete-delay --exclude='*.partial' \
  jobhunt-mini:'Library/Application Support/jobhunt/backups/' ~/Backups/jobhunt-mini/
.venv/bin/python -m jobhunt.backup --verify "$(ls -t ~/Backups/jobhunt-mini/*.db | head -1)"
```

Verifying after the pull is what turns a copy into a backup.

## Afterwards

- `make doctor` is the first thing to run when anything looks wrong.
- `make agents-stop` before ejecting the disk, always.
- Deploying a change: `git pull && make migrate && make doctor`. Only restart the dashboard
  (`launchctl kickstart -k gui/$(id -u)/com.jobhunt.dashboard`) if Python changed.
- `out/` is not backed up. Old packet PDFs 404 after a restore until re-rendered; the bytes
  that were actually submitted are safe in `applications.resume_pdf`.
