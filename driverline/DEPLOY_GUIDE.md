# Driverline: Running it for your community

Do the **local test** first (Part 1). Only then move to a real server (Part 2). Never put the app on the internet without the login in place and HTTPS turned on.

## Part 1: Test accounts locally (Termux)

1. Unzip the update into `~/driverline` (see the chat instructions), then stop and restart the server.
2. Create your admin account (you'll be asked for a password):
   ```
   cd ~/driverline
   python -m app.manage create-admin you@example.com
   ```
3. Adopt any trades you logged before accounts existed:
   ```
   python -m app.manage claim-trades you@example.com
   ```
4. Make invite codes:
   ```
   python -m app.manage invite --tier free --uses 20
   python -m app.manage invite --tier member --uses 1
   ```
   `--uses` is how many people can use one code.
5. Open `http://localhost:8000`. You'll land on the login page. Sign in, then test creating a second account with an invite code (use a private browser tab).

### Day-to-day admin commands
```
python -m app.manage users                          # list everyone
python -m app.manage set-tier friend@x.com member   # upgrade after they pay
python -m app.manage set-tier friend@x.com free     # downgrade
python -m app.manage block friend@x.com             # lock out
python -m app.manage unblock friend@x.com
python -m app.manage reset-password friend@x.com   # forgotten password
```

## How the free and member tiers work
- **free:** 3 fresh AI analyses per day (change with `DRIVERLINE_FREE_LIMIT`). No AI journal review.
- **member:** 30 per day (`DRIVERLINE_MEMBER_LIMIT`) and the AI journal review.
- **Shared cache:** an AI analysis is kept for 5 minutes (`DRIVERLINE_CACHE_SECONDS`). If a member asks for an instrument someone already analysed, they get it instantly and it costs no AI credit and none of their allowance.
- **When the limit or your credit runs out:** members still see the computed bias, timeframes and levels, just without the AI card.
- Members pay you directly (bank, mobile money, whatever you use). You then run `set-tier`. Automatic billing can be added later.

## Part 1b: Free pilot straight from your phone (no server needed)

Good for a **small test group** (a handful of trusted members) while you can't afford a server. It uses Cloudflare's free quick tunnel, which gives your phone a public HTTPS link while the script runs.

1. Install the tunnel tool: `pkg install cloudflared`
2. Put `start_public.sh` in `~/driverline` and run:
   ```
   cd ~/driverline
   bash start_public.sh
   ```
3. It prints `Share this link: https://something.trycloudflare.com`. Send that link plus an invite code to your pilot members. Press Ctrl+C to stop everything.

Know the limits before you rely on it:
- The link **changes every time** you start the script, so you must re-send it.
- It only works while your phone is on, charged, connected and Termux is running. Keep Termux set to Unrestricted battery and keep the wake lock on.
- Cloudflare describes quick tunnels as a way to experiment, not for production use, so treat this as a pilot only.
- Your app is reachable from the internet while it runs. The login and invite codes protect it, so use a strong admin password and only give codes to people you trust.
- Free members' analyses still need funded Claude API credit; without it they see the computed-facts view.

When you can afford a server, jump to Part 2. Copy your `data/` folder across and nobody loses their account or journal.

## Part 2: Putting it on a server

Your phone can't do this job: it sleeps, changes network and isn't secure enough. Use a small cloud server (a basic Linux VPS from any provider is enough) and a domain name.

1. **Server:** Ubuntu 22.04 or newer. Create a normal user (not root), and enable the firewall allowing only SSH, 80 and 443.
2. **Install:**
   ```
   sudo apt update && sudo apt install -y python3 python3-venv unzip caddy
   mkdir ~/driverline && cd ~/driverline        # upload your files here
   python3 -m venv .venv && source .venv/bin/activate
   pip install starlette uvicorn websockets httpx
   ```
3. **Secrets:** create `~/driverline/.env` (and keep it private: `chmod 600 .env`):
   ```
   ANTHROPIC_API_KEY=sk-ant-...
   DRIVERLINE_SECURE=1
   DRIVERLINE_SECRET=put-a-long-random-string-here
   ```
   Generate the random string with `python3 -c "import secrets;print(secrets.token_hex(32))"`.
   Use a **separate API key** just for this app and set a spend limit in the Anthropic console.
4. **Run it as a service.** Create `/etc/systemd/system/driverline.service`:
   ```
   [Unit]
   Description=Driverline
   After=network.target

   [Service]
   User=YOURUSER
   WorkingDirectory=/home/YOURUSER/driverline
   EnvironmentFile=/home/YOURUSER/driverline/.env
   ExecStart=/home/YOURUSER/driverline/.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000
   Restart=always

   [Install]
   WantedBy=multi-user.target
   ```
   Then: `sudo systemctl daemon-reload && sudo systemctl enable --now driverline`.
5. **HTTPS with Caddy** (it gets the certificate for you). Point your domain's DNS at the server, then edit `/etc/caddy/Caddyfile`:
   ```
   yourdomain.com {
       reverse_proxy 127.0.0.1:8000
   }
   ```
   Then `sudo systemctl reload caddy`.
6. **Create your admin and invites on the server** (run from `~/driverline` with the venv active):
   ```
   set -a; source .env; set +a
   python -m app.manage create-admin you@example.com
   python -m app.manage invite --tier free --uses 50
   ```
7. **Backups:** copy `~/driverline/data/` (it holds the database and the secret key) somewhere safe every day, for example with a cron job.

## Before you invite people
- Test the whole flow yourself on the live domain: sign up with an invite, log a trade, hit the daily limit.
- Add a short privacy note (you store emails and trade journals) and keep the "education only, not financial advice" wording visible.
- Check your local rules on sharing trade analysis with a community.
- Password resets work by email (see below) and from the admin page.

## Email for password resets
Members tap **Forgot your password?** on the login page and get a one-hour link by email. The link works once, and using it signs out every older session on that account. Driverline sends mail through any SMTP service (most email providers and transactional email services offer SMTP details; check their current free limits). Add these to your `.env`:
```
SMTP_HOST=smtp.yourprovider.com
SMTP_PORT=587
SMTP_USER=your-smtp-username
SMTP_PASSWORD=your-smtp-password
SMTP_FROM=Driverline <no-reply@yourdomain.com>
DRIVERLINE_BASE_URL=https://yourdomain.com
```
Port 587 uses STARTTLS and port 465 uses SSL; both work. `DRIVERLINE_BASE_URL` makes sure the link in the email points at your real domain. Restart the service after editing `.env`, then test with your own account. If mail lands in spam, set up SPF and DKIM for your domain in your email provider's settings.

Without `SMTP_HOST` set (for example while testing in Termux), no email is sent. The link is printed in the server log instead, and you can always create a link yourself from the admin page and send it to the member through WhatsApp or anywhere else.

## The admin page
Sign in as an admin and open `/admin.html` (an **Admin** link also appears at the bottom of the main screen, for admins only). It lets you:
- see members, how many are free or paid, and today's AI usage;
- create and delete invite codes;
- change a member's tier, or block and unblock them;
- create a one-time password-reset link for anyone;
- check that every Deriv symbol code is still valid.

You can't change your own tier or block yourself there, so you can't lock yourself out. The command line (`python -m app.manage ...`) is only needed once, to create the very first admin.
