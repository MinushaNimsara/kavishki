"""Quick script to verify firebase-credentials.json is valid."""
import os
import json

APP_DIR = os.path.dirname(os.path.abspath(__file__))
CRED_PATH = os.path.join(APP_DIR, "firebase-credentials.json")

print("1. Checking if file exists...")
if not os.path.exists(CRED_PATH):
    print("   FAIL: firebase-credentials.json not found at", CRED_PATH)
    exit(1)
print("   OK: File exists")

print("\n2. Checking if valid JSON...")
try:
    with open(CRED_PATH) as f:
        cred = json.load(f)
    print("   OK: Valid JSON")
except json.JSONDecodeError as e:
    print("   FAIL:", e)
    exit(1)

print("\n3. Checking required fields...")
required = ["type", "project_id", "private_key", "client_email"]
for field in required:
    if field in cred:
        val = cred[field]
        if field == "private_key":
            val = val[:30] + "..." if len(val) > 30 else val
        print(f"   OK: {field} = {val}")
    else:
        print(f"   FAIL: Missing '{field}'")
        exit(1)

print("\n4. Testing Firebase Admin initialization...")
try:
    import firebase_admin
    from firebase_admin import credentials
    cred_obj = credentials.Certificate(CRED_PATH)
    firebase_admin.initialize_app(cred_obj)
    print("   OK: Firebase Admin initialized successfully")
except ImportError:
    print("   SKIP: Run 'pip install firebase-admin' first, then run this again")
except Exception as e:
    print("   FAIL:", e)
    exit(1)

print("\nAll checks passed! Firebase credentials are valid.")
