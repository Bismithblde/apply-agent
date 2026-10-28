# apply-agent

Auto-applies to Summer 2027 SWE and AI-engineering internships. It pulls listings from Simplify, vanshb03,
speedyapply, and company Greenhouse/Lever/Ashby boards. It fills and submits applications in a hidden
browser, solves CAPTCHAs with CapSolver, reads verification emails through Gmail, and creates and remembers
site accounts in an encrypted vault. Everything it does is logged, with a screenshot of each application.

## Setup (about 10 minutes)

```bash
./setup.sh
```

The script is safe to re-run and requires the project's existing `.venv` with Python 3.14; it does not create a
virtual environment. It installs dependencies, generates the vault key and ntfy topic, asks for missing keys,
offers to install Xvfb, walks you through Gmail OAuth, installs the background service, fetches listings, and
runs `doctor`.

What you need on hand:

| Item | Where | Required? |
|---|---|---|
| `GEMINI_API_KEY` | aistudio.google.com/apikey | yes (already set) |
| `CAPSOLVER_API_KEY` | capsolver.com dashboard, add about $5 | recommended; without it, CAPTCHA jobs are queued |
| `secrets/gmail_client.json` | Google Cloud OAuth "Desktop app" client (steps below) | recommended; without it, email-verification jobs are queued |
| Xvfb | `sudo dnf install -y xorg-x11-server-Xvfb` | recommended; the scheduled service uses a private headed display when available and runs headless otherwise. `apply --visible` opens on your desktop. |
| Street address | `profile.yaml` > `personal.address_line1` | recommended; Workday requires it |

### Gmail OAuth

1. Go to console.cloud.google.com and create a project. Under APIs & Services > Library, enable **Gmail API**.
2. On the **OAuth consent screen**, choose External and keep it in Testing. Add your Gmail address as a **test user**.
3. Under **Credentials**, choose Create credentials > **OAuth client ID** > **Desktop app**, then download the JSON.
4. Save it as `secrets/gmail_client.json`, then run `.venv/bin/python -m agent auth gmail` and click Allow.

Testing-mode tokens expire after 7 days. Re-run `auth gmail` when `doctor` says so.

## Daily use

```bash
.venv/bin/python -m agent doctor                 # is everything set up?
.venv/bin/python -m agent pick                   # interactively review & select new jobs to apply for
.venv/bin/python -m agent pick --export          # export new listings to logs/new_jobs.md
.venv/bin/python -m agent apply --limit 1 --dry-run --visible   # watch one job get filled, no submit
.venv/bin/python -m agent apply --job <job_id>   # apply to one job now (already-submitted jobs need --force)
systemctl --user enable --now apply-agent        # run in the background (survives reboots)
journalctl --user -u apply-agent -f              # live log (also logs/agent.log)
.venv/bin/python -m agent status                 # queue counts
.venv/bin/python -m agent report                 # tonight's report -> logs/report-YYYY-MM-DD.md
.venv/bin/python -m agent review --limit 10      # prefill queued jobs in visible tabs for manual finishing
.venv/bin/python -m agent mark <job_id> --status submitted
.venv/bin/python -m agent resolve <job_id> --outcome submitted  # only after confirming the application was sent
.venv/bin/python -m agent requeue --status failed
.venv/bin/python -m agent accounts list          # saved site accounts
.venv/bin/python -m agent accounts show <tenant> --show-password  # explicitly reveal a password
systemctl --user stop apply-agent                # stop
```

You can keep using the laptop while the service runs. When `xvfb-run` is installed, its browser uses a private
headed display and does not take focus; without Xvfb it runs headless. The process runs at low CPU priority.
Keep the laptop plugged in with the lid open; idle sleep is blocked while the service runs. The scheduler sends
a run notification only when a run submitted or queued jobs, and sends its daily report notification once after
8 AM. ntfy delivery requires `NTFY_TOPIC` in `.env` and a subscription in the ntfy phone app; desktop delivery
requires `notify-send`.

`review` opens `needs_review` jobs in separate visible tabs and runs the matching ATS adapter to prefill what it
can. It always uses dry-run and `fill_only`, disables account creation even if enabled in `config.yaml`, and
does not submit or change job status. Tabs stay open while the command waits for Enter; adapter or page failures
are reported per job so later jobs can still open. Review holds the same browser-profile lock as `apply` until
you finish. If an apply run stops after recording a final-submit intent, the job stays blocked from future
applications, including `apply --force`, until you confirm what happened and run
`python -m agent resolve <job_id> --outcome submitted` or
`python -m agent resolve <job_id> --outcome not_submitted`. Use `submitted` only after confirming the application
was sent; use `not_submitted` only after confirming it was not.

## Settings (`config.yaml`, re-read every 30 s, no restart needed)

- `submit_mode`
  - `full`: submit when every required field has an answer, including Gemini-written essays.
  - `hybrid`: submit only when every required answer comes from `profile.yaml`.
  - `fill_only`: never submit.
- `schedule`
  - `fetch_every` / `apply_every`: intervals, like `30m`.
  - `active_hours`: when to run, like `"22:00-08:00"`.
  - `run_while_user_active`: set false to pause while you're at the keyboard (GNOME).
  - `concurrency`: number of parallel browsers.
  - `max_run_time`: a scheduled batch that runs longer than this (default `3h`) is killed and its jobs requeued.
- Caps: `max_submissions_per_run`, `max_submissions_per_night` (resets at 6 PM), `seconds_between_applications`.
- `captcha.max_spend_per_night_usd`, `email.plus_alias` (uses `you+company@gmail.com` per site), `auto_ats`.
- `accounts.create_in_dry_run`: dry runs stop at "account required" instead of registering on the site (default false).
- Personal knowledge: add private `.md` or `.txt` files to `data/knowledge/`. For questions without a direct
  profile match, the agent retrieves relevant passages from your profile, resume, answer bank, and these files.
  Keep credentials and secrets out of this folder; unsupported factual answers are intended to be queued.
- `.env` extras: `GEMINI_THINKING_LEVEL` (default `low`; keeps vision steps fast), `OPENROUTER_API_KEY` or
  `OPENAI_API_KEY` as a fallback LLM when Gemini is unreachable.
- `sources.*` turns sources on and off. `sources.ats_boards` lists company slugs to watch directly.
- `filters.*` controls terms, US-only, SWE/AI categories, AI title keywords, and title excludes.

The facts it's allowed to state about you live in `profile.yaml`, your resume, and any files you add to
`data/knowledge/`. For unfamiliar questions, the agent retrieves relevant passages before asking Gemini to
answer. Unsupported factual answers are intended to be queued for review, so review remains prudent.

## How it works

`fetch` loads sources into `data/apply.db`. The runner then routes each job by ATS: Greenhouse, Lever, Ashby,
and Workday have dedicated adapters, and everything else goes to a Gemini vision agent. Each adapter extracts
the form fields and answers them, first from your profile and then with Gemini. It solves a CAPTCHA if one is
present, submits according to `submit_mode`, and records the result and a screenshot.

Network note: on eduroam, Google APIs only work over HTTP/2 from some front-end IPs. `agent/config.py`
handles this automatically (`GOOGLEAPIS_PIN=0` disables it).
