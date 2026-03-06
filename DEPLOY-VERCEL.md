# Deploy EduBear to Vercel

## Prerequisites

- GitHub repo connected (e.g. https://github.com/MinushaNimsara/kavishki)
- [Vercel account](https://vercel.com/signup)

---

## Step 1: Deploy from GitHub

1. Go to [vercel.com/new](https://vercel.com/new)
2. **Import** your `kavishki` repository
3. Click **Deploy** (Vercel auto-detects Flask from `app.py`)

---

## Step 2: Add environment variables

In your Vercel project: **Settings** → **Environment Variables**. Add:

| Name | Value | Notes |
|------|-------|-------|
| `SECRET_KEY` | A random string (e.g. `openssl rand -hex 32`) | Required for sessions |
| `FIREBASE_CREDENTIALS_JSON` | Full content of `firebase-credentials.json` | Paste the entire JSON (one line) |
| `FIREBASE_WEB_CONFIG_JSON` | Full content of `firebase-web-config.json` | Paste the entire JSON (one line) |

**How to get the JSON values:**
- Open `firebase-credentials.json` → copy entire content → minify to one line (remove line breaks)
- Open `firebase-web-config.json` → copy entire content → minify to one line

Example format for env var: `{"type":"service_account","project_id":"ai-learning-system-29127",...}`

After adding variables, **redeploy** the project.

---

## Step 3: Firebase authorized domains

In [Firebase Console](https://console.firebase.google.com/) → **Authentication** → **Settings** → **Authorized domains**:

- Add your Vercel domain (e.g. `kavishki.vercel.app` or `your-project.vercel.app`)

---

## Important limitations on Vercel

1. **SQLite data is ephemeral** – Data is stored in `/tmp` and is lost on cold starts or redeploys. For persistent data, use a cloud database (e.g. [Vercel Postgres](https://vercel.com/storage/postgres), [Neon](https://neon.tech), or [Supabase](https://supabase.com)).

2. **Bundle size** – scikit-learn and numpy are large. The deployment may approach or exceed Vercel’s 500MB limit. If deployment fails for size, consider lighter ML libraries or an external ML API.

3. **Cold starts** – The first request after inactivity can be slower (often a few seconds).

---

## Alternative: Deploy via CLI

```bash
npm i -g vercel
cd c:\Users\p\Desktop\a
vercel
```

Follow the prompts, then add environment variables in the Vercel dashboard.
