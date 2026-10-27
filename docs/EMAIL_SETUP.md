# Email job-alert setup (owner, one time)

This lets users forward job-alert emails (LinkedIn and others) into JobPilot. Every user gets their
own address such as `u-7f3k9a@alerts.yourdomain.com`. All of them arrive in **one dedicated Gmail
inbox** that JobPilot reads over IMAP. JobPilot never sees a user's own mailbox, password or Google
login, only the emails they choose to forward.

```
user's Gmail filter ──► u-7f3k9a@alerts.yourdomain.com
                              │  (Cloudflare Email Routing, catch-all)
                              ▼
                 dedicated Gmail inbox (the platform's)
                              │  (IMAP, every 5 minutes: run_huey)
                              ▼
                 JobPilot: route by address, read, add jobs to the feed
```

You need: a domain whose DNS is on Cloudflare (free plan is fine) and a new Gmail account used only
for this.

## 1. The dedicated Gmail account

1. Create a new Gmail account, for example `jobpilot.alerts@gmail.com`. Do not use a personal one.
2. Turn on **2-Step Verification** (Google Account, Security).
3. Create an **app password**: Google Account, Security, 2-Step Verification, **App passwords**.
   Name it "JobPilot" and copy the 16-letter password. You will not see it again.
4. In Gmail: **Settings, See all settings, Forwarding and POP/IMAP**, and make sure **IMAP access
   is enabled**. Save.

## 2. Cloudflare Email Routing

1. In Cloudflare, open your domain, then **Email, Email Routing**, and press **Enable** (it adds the
   MX and SPF records for you).
2. Pick the domain or subdomain for alerts. A subdomain such as `alerts.yourdomain.com` keeps this
   separate from your normal mail: add it under **Routing**, **Subdomains** (or use the root domain
   if you do not use it for mail).
3. **Destination addresses**: add the dedicated Gmail address and click the verification link
   Cloudflare sends to it.
4. **Routing rules**: turn on the **Catch-all** rule, action **Send to an email**, destination the
   dedicated Gmail address.
5. Test: send an email from anywhere to `anything@alerts.yourdomain.com`. It should land in the
   dedicated Gmail inbox within a minute.

## 3. JobPilot settings

Add to `.env` (see `.env.example`):

```
INBOUND_EMAIL_DOMAIN=alerts.yourdomain.com
INBOUND_IMAP_HOST=imap.gmail.com
INBOUND_IMAP_USER=jobpilot.alerts@gmail.com
INBOUND_IMAP_PASSWORD=the-16-letter-app-password
```

`INBOUND_EMAIL_DOMAIN` must be exactly the domain users forward to. Restart `run_huey`. Until the
user and password are set, email ingestion stays off and the settings page says so.

## 4. Check it

```bash
uv run python manage.py poll_mailbox
```

This reads the inbox once and prints counts (`messages`, `parsed`, `jobs`, `unrouted`,
`duplicates`, `failed`). The worker does the same every 5 minutes.

On the first run JobPilot creates two folders (Gmail labels) in the dedicated inbox:

| Label | What goes there |
|---|---|
| `jobpilot/processed` | Every email that was read, including duplicates and confirmations |
| `jobpilot/unrouted` | Emails not addressed to a known user, kept so you can look at them |

Read emails are marked as read and moved out of the inbox, so the inbox only ever shows what is new.
Open the **unrouted** label now and then: it is where a mistyped address, or spam sent to your
domain, ends up. Delete from it freely.

## 5. A real end-to-end test (about 10 minutes)

1. Log in as a user, open **Settings, Job alerts**, and copy the forwarding address.
2. In that user's Gmail, add it as a forwarding address (**Settings, Forwarding and POP/IMAP, Add a
   forwarding address**). Gmail emails a confirmation to the address. Within 5 minutes JobPilot shows
   a **Confirm forwarding** banner on the dashboard and the settings page, and a Telegram message if
   Telegram is linked. Click the link, or paste the code into Gmail.
3. Create a Gmail filter from the alert sender (for example `jobalerts-noreply@linkedin.com`) that
   forwards to the address.
4. Wait for (or trigger) a real LinkedIn alert. Within 5 minutes the jobs appear on the dashboard.

## What gets read, and what does not

- **LinkedIn:** the title, company, location and the link come from the email itself. JobPilot never
  opens LinkedIn pages for you. Descriptions are short because the email has none.
- **Other senders:** links to a job site JobPilot already reads (for example ExpertWoka) are read
  through that site's parser. Anything else is read by the AI from the email text, up to 15 jobs per
  email, paid by the platform (it counts against the daily AI cap, not the user's allowance). The AI
  may only use links that are actually in the email.
- **Dedicated parsers** for BrighterMonday, Fuzu and MyJobMag are not written yet. Forward a few
  real alert emails from each (as `.eml` files) and they can be added in `apps/inbound/parsers/`.
- **Retention:** the stored email text is deleted after 30 days. The record (date, result, job count)
  stays.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| `poll_mailbox` says the settings are not set | `INBOUND_IMAP_USER` or `INBOUND_IMAP_PASSWORD` is empty |
| Login failed | Use the app password, not the account password. IMAP must be enabled in Gmail |
| Mail never reaches the Gmail inbox | Check the Cloudflare catch-all rule and that the destination is verified |
| Emails all end up in `jobpilot/unrouted` | `INBOUND_EMAIL_DOMAIN` differs from the domain in the user's address, or the user's forwarding address was typed wrongly |
| Confirmation banner never appears | The confirmation email has not arrived, or the worker is not running |
| Jobs appear but with little detail | Expected for LinkedIn alerts; the email has no description |
