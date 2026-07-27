# Deploying the Riphah Voice Agent to a VM

End-to-end guide: PuTTY login → upload code → environment → systemd service →
Nginx reverse proxy → HTTPS → hand the API to other developers.

Assumes an **Ubuntu 22.04/24.04 VM** with a public IP. Commands that differ on
other distros are noted. Everything server-side happens over one PuTTY session.

---

## 1. Log in with PuTTY

1. Open PuTTY. In **Host Name** enter `your-vm-ip` (or `ubuntu@your-vm-ip`),
   Port `22`, Connection type `SSH`.
2. If your cloud provider gave you a **.ppk key**: Connection → SSH → Auth →
   Credentials → browse to the `.ppk` file. (If you only have a `.pem` key,
   convert it once with PuTTYgen: Load → select the .pem → Save private key.)
3. Optional but recommended: Session → type a name under Saved Sessions →
   **Save**, so next time it's one double-click.
4. Click **Open**, accept the host key on first connect, log in
   (typically `ubuntu`, `root`, or the user your provider created).

First-time server prep:

```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y python3 python3-venv python3-pip nginx git ufw
```

---

## 2. Upload the code

Two options — Git is better long-term, PSCP is fastest today.

### Option A — Git (recommended)

Push the project to a **private** GitHub repo from your PC, then on the VM:

```bash
cd /opt
sudo git clone https://github.com/YOUR_USER/Riphah_chatBot.git riphah
sudo chown -R $USER:$USER /opt/riphah
```

Future updates become: `cd /opt/riphah && git pull && sudo systemctl restart riphah`.

> `.gitignore` already excludes `.env` — never commit it. The `data/` folder
> (the knowledge base) is ~100 MB; either commit it, or upload it separately
> with PSCP (Option B) after cloning.

### Option B — PSCP (comes with PuTTY)

From **your Windows PC** (PowerShell, in `E:\Riphah_chatBot`):

```powershell
pscp -r -i C:\path\to\key.ppk `
  agent kb eval frontend scripts data config.py requirements.txt README.md `
  ubuntu@your-vm-ip:/home/ubuntu/riphah/
```

Then on the VM move it into place: `sudo mv /home/ubuntu/riphah /opt/riphah`.
Do **not** upload `.venv/` (rebuilt on the server) or `.env` this way unless
you're comfortable with it in transit — better to create `.env` on the server
by hand (next step).

### Install dependencies (on the VM)

```bash
cd /opt/riphah
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

---

## 3. Configure environment variables

Create `.env` directly on the server (keys never travel through Git):

```bash
nano /opt/riphah/.env
```

```ini
OPENAI_API_KEY=sk-proj-...            # the boss's key
TEXT_PROVIDER=openai
OPENAI_TEXT_MODEL=gpt-4o              # or gpt-4o-mini to cut text cost ~10x

EMBED_MODEL=text-embedding-3-large
EMBED_DIMENSIONS=1536
REALTIME_MODEL=gpt-realtime           # or gpt-realtime-2.1-mini to cut voice cost ~3x
REALTIME_VOICE=marin

# ---- production access control ----
# Generate one key per integrating developer/app:  openssl rand -hex 24
API_KEYS=key-for-website-team,key-for-mobile-team
# The site(s) whose browsers may call the API directly:
ALLOWED_ORIGINS=https://riphah.edu.pk,https://www.riphah.edu.pk

HOST=127.0.0.1        # only Nginx talks to the app directly
PORT=8000
```

Lock it down: `chmod 600 /opt/riphah/.env`

Sanity-check the app runs before daemonizing:

```bash
cd /opt/riphah && .venv/bin/python -m agent.server
# then from a second PuTTY session:  curl http://127.0.0.1:8000/api/health
# Ctrl+C to stop
```

---

## 4. Run as a systemd service (survives logout & reboots)

systemd is the right tool here — it's built into Ubuntu (no PM2/Node needed,
no Docker layer to maintain), restarts the app if it crashes, and starts it on
boot.

```bash
sudo nano /etc/systemd/system/riphah.service
```

```ini
[Unit]
Description=Riphah Voice Agent API
After=network.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/opt/riphah
ExecStart=/opt/riphah/.venv/bin/python -m agent.server
Restart=always
RestartSec=3
# systemd doesn't read .env for the app — the app itself loads it via dotenv,
# so nothing extra is needed here. Add overrides with Environment= if required.

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now riphah     # start now + on every boot
sudo systemctl status riphah           # should say "active (running)"
journalctl -u riphah -f                # live logs (Ctrl+C to exit)
```

You can now close PuTTY — the API stays up. After any code update:
`sudo systemctl restart riphah`.

---

## 5. Nginx reverse proxy

Nginx sits on ports 80/443, terminates TLS, and forwards to the app on
127.0.0.1:8000.

```bash
sudo nano /etc/nginx/sites-available/riphah
```

```nginx
server {
    listen 80;
    server_name api.yourdomain.com;    # the DNS name you'll give developers

    # allow big JSON bodies (voice turn batches) and slow LLM responses
    client_max_body_size 5m;

    location / {
        proxy_pass         http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header   Host $host;
        proxy_set_header   X-Real-IP $remote_addr;
        proxy_set_header   X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto $scheme;
        proxy_read_timeout 180s;       # tool loops can take a while
    }
}
```

```bash
sudo ln -s /etc/nginx/sites-available/riphah /etc/nginx/sites-enabled/
sudo nginx -t                          # validate config
sudo systemctl reload nginx
```

Point DNS: create an **A record** `api.yourdomain.com → your-vm-ip` at your
domain registrar. Wait for it to resolve (`ping api.yourdomain.com`).

Firewall:

```bash
sudo ufw allow OpenSSH
sudo ufw allow 'Nginx Full'            # 80 + 443
sudo ufw enable
```

---

## 6. HTTPS with Let's Encrypt (free, auto-renewing)

HTTPS is not optional here: browsers refuse microphone access on plain HTTP,
so voice mode only works over TLS.

```bash
sudo apt install -y certbot python3-certbot-nginx
sudo certbot --nginx -d api.yourdomain.com
```

Certbot edits the Nginx config for you (adds the 443 server block + HTTP→HTTPS
redirect) and installs a systemd timer that renews automatically. Verify:

```bash
sudo certbot renew --dry-run
curl https://api.yourdomain.com/api/health
```

---

## 7. Reliability checklist

- **Crash recovery**: `Restart=always` in the service — already handled.
- **Reboot recovery**: `systemctl enable` — already handled.
- **Weekly KB refresh** (fees change): `crontab -e` →
  `0 3 * * 1 cd /opt/riphah && .venv/bin/python -m kb.build >> /var/log/riphah-refresh.log 2>&1`
- **Budget cap**: set a hard monthly limit in the OpenAI billing console so a
  traffic spike can't produce a surprise bill.
- **Monitoring**: point any uptime monitor (UptimeRobot etc.) at
  `https://api.yourdomain.com/api/health` — it's public by design and returns
  `"ready": true` plus KB counts.
- **Backups**: the only state is `data/kb.sqlite3` (knowledge base +
  conversations). `cp` it nightly via cron, or snapshot the VM.

---

## 8. What integrating developers get

Base URL: `https://api.yourdomain.com` · Interactive docs: `/docs`

**Every request needs two headers:**

| Header | What | Who supplies it |
|---|---|---|
| `X-API-Key` | The developer key you issued (from `API_KEYS`) | You, once per team/app |
| `X-User-Id` | A stable, opaque id for the *end user* (UUID recommended) | The integrating app mints one per user |

`X-User-Id` is what gives each end user private history — all conversation
storage and listing is scoped to it server-side. Two users can never see each
other's chats; a guessed conversation id returns 404.

**Core endpoints:**

```
POST /api/chat                       {"message": "...", "conversation_id": "..."}
     → {"answer", "conversation_id", "trace": [tool calls]}
POST /api/tools/{name}               {"arguments": {...}}   # direct data lookup, no LLM
GET  /api/tools                      # list the 7 tools
POST /api/realtime/session           {"conversation_id": "..."}   # mint voice token
GET  /api/conversations              # this user's history list
GET  /api/conversations/{id}         # full transcript (owner only)
DELETE /api/conversations/{id}       # owner only
GET  /api/health                     # public, for monitoring
```

**Example integration (JavaScript):**

```js
const API = "https://api.yourdomain.com";
const HEADERS = {
  "Content-Type": "application/json",
  "X-API-Key": "key-for-website-team",
  "X-User-Id": currentUser.uuid,          // stable per end user
};

const res = await fetch(`${API}/api/chat`, {
  method: "POST", headers: HEADERS,
  body: JSON.stringify({ message: "MBBS ki fee kitni hai?", conversation_id: savedId }),
});
const { answer, conversation_id } = await res.json();
```

**Voice**: browsers call `POST /api/realtime/session` to get a ~10-minute
ephemeral OpenAI token, then run WebRTC directly against OpenAI —
`frontend/index.html` is the reference implementation to copy.

---

## Quick reference — daily operations

| Task | Command |
|---|---|
| Status / logs | `systemctl status riphah` · `journalctl -u riphah -f` |
| Restart after code change | `cd /opt/riphah && git pull && sudo systemctl restart riphah` |
| Add a developer key | edit `API_KEYS` in `.env`, then restart |
| Rotate the OpenAI key | edit `.env`, then restart |
| Rebuild knowledge base | `.venv/bin/python -m kb.build` |
| Check retrieval health | `.venv/bin/python eval/run_eval.py --retrieval` |
