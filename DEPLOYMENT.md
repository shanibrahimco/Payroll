# Deploying to the Hostinger VPS

Docker is only needed on the VPS. These steps assume an Ubuntu VPS and that the latest code has been pushed to the private GitHub repository.

## 1. Point the domain at the VPS

At your domain provider, add an **A record** for the address staff will use (for example `payroll.yourdomain.com`) with the VPS IP address as its value. Wait until it is active before step 5, because the HTTPS certificate depends on it.

## 2. Prepare the VPS

Sign in with `ssh root@YOUR_VPS_IP`, then run:

```bash
curl -fsSL https://get.docker.com | sh
ufw allow OpenSSH && ufw allow 80/tcp && ufw allow 443/tcp && ufw allow 443/udp && ufw --force enable
mkdir -p /srv/payroll/data /srv/payroll/backups
git clone https://github.com/shanibrahimco/Payroll.git /srv/payroll/app
```

GitHub will ask for your username and a personal access token, because the repository is private.

## 3. Enter your domain

```bash
cd /srv/payroll/app
cp .env.example .env
nano .env
```

Change `PAYROLL_DOMAIN` to your address, then save with `Ctrl+O`, `Enter`, `Ctrl+X`.

## 4. Move the live data

On **this computer**, stop the payroll app, then run these from the project folder:

```powershell
.\.venv\Scripts\python.exe deploy\prepare_data.py
scp payroll_data_for_server.zip root@YOUR_VPS_IP:/srv/payroll/
```

On the **VPS**:

```bash
cd /srv/payroll
python3 -m zipfile -e payroll_data_for_server.zip data/
chown -R 1000:1000 data backups
rm payroll_data_for_server.zip
```

Delete `payroll_data_for_server.zip` from this computer too. After this point, use only the VPS copy, or the two sets of records will drift apart.

## 5. Start the system

```bash
cd /srv/payroll/app
docker compose up -d --build
docker compose ps
```

All three services should show as running, and `payroll` as healthy. Then open `https://your-domain` and sign in.

## 6. Check before staff use it

- The padlock shows in the browser and the login page opens.
- The Payroll tab shows the correct staff and net pay.
- A test check-in shows the correct Pakistan time.
- `docker compose logs backup` shows `Backup saved` after 2 a.m.
- Staff change their starting passwords at first sign-in.

## Everyday tasks

| Task | Command (from `/srv/payroll/app`) |
|---|---|
| Install an update | `git pull && docker compose up -d --build` |
| See whether it is running | `docker compose ps` |
| See recent messages | `docker compose logs --tail 100 payroll` |
| Restart | `docker compose restart payroll` |
| Take a backup now | `docker compose exec backup python deploy/backup.py --once` |

## Backups

A verified backup is saved to `/srv/payroll/backups` every day at about 2 a.m., and the last 14 days are kept. To keep a copy away from the server, run this on your own computer regularly:

```powershell
.\deploy\download_backups.ps1 -Server YOUR_VPS_IP -Destination "D:\Payroll Backups"
```

To restore a backup:

```bash
cd /srv/payroll/app
docker compose stop payroll backup
python3 -m zipfile -e /srv/payroll/backups/BACKUP_FILE_NAME.zip /srv/payroll/data/
chown -R 1000:1000 /srv/payroll/data
docker compose start payroll backup
```
