# CalmLearn 🌿 (Starter)

Gentle Duolingo-inspired adaptive learning prototype (Math, English, Science).

## Run

**Option A – use system Python (if .venv is broken):**
```bash
pip install -r requirements.txt
python app.py
```

**Option B – use a virtual environment:**
```bash
python -m venv .venv
# Windows:
.venv\Scripts\activate
# Mac/Linux:
# source .venv/bin/activate
pip install -r requirements.txt
python app.py
```

Then open **http://127.0.0.1:5000**

If you see "No Python at ..." when using `.venv`, delete the `.venv` folder and run Option B again (creates a new venv with your current Python).

## Notes
- Math questions are generated automatically.
- English & Science questions are stored in JSON in /data.
