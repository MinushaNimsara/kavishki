# Firebase Web Config Setup

The signup/login page shows "Firebase is not configured" until you add your project's web config.

## Steps

1. Open **Firebase Console**: https://console.firebase.google.com/
2. Select your project **ai-learning-system** (or ai-learning-system-29127).
3. Click the **gear icon** next to "Project Overview" → **Project settings**.
4. Scroll to **"Your apps"**. If you see a web app (</> icon), click it. If not, click **"Add app"** → choose **Web** (</>) → register the app (e.g. name "EduBear Web") → you'll see the config.
5. Copy the **config object**. It looks like:
   ```js
   const firebaseConfig = {
     apiKey: "AIza...",
     authDomain: "ai-learning-system-29127.firebaseapp.com",
     projectId: "ai-learning-system-29127",
     storageBucket: "ai-learning-system-29127.appspot.com",
     messagingSenderId: "123456789",
     appId: "1:123456789:web:abcdef"
   };
   ```
6. Open **firebase-web-config.json** in this folder and replace:
   - `"YOUR_API_KEY"` → paste your **apiKey** (e.g. `"AIza..."`)
   - `"YOUR_MESSAGING_SENDER_ID"` → paste your **messagingSenderId** (numbers)
   - `"YOUR_APP_ID"` → paste your **appId** (e.g. `"1:123456789:web:..."`)

7. Save the file and **restart the Flask app** (or refresh the signup page).

After this, the signup form and "Sign in with Google" will appear and work.
