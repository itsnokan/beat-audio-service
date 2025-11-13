import os
import tempfile
import zipfile
import requests
import ffmpeg
from fastapi import FastAPI, UploadFile, Form
from fastapi.middleware.cors import CORSMiddleware
from firebase_admin import credentials, initialize_app, storage

# === CONFIGURAÇÃO FIREBASE ===
FIREBASE_BUCKET = os.getenv("FIREBASE_BUCKET")
CALLBACK_URL = os.getenv("STEMS_CALLBACK_URL")  # ex: https://seusite.vercel.app/api/stems/callback
SERVICE_ACCOUNT = os.getenv("GOOGLE_APPLICATION_CREDENTIALS")

if SERVICE_ACCOUNT and os.path.exists(SERVICE_ACCOUNT):
    cred = credentials.Certificate(SERVICE_ACCOUNT)
    initialize_app(cred, {"storageBucket": FIREBASE_BUCKET})
    print("✅ Firebase initialized")
else:
    print("⚠️ Firebase service account not found or not configured")

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ============================================================
# 🎵 1. STEMS SEPARATION
# ============================================================

def run_separation_dummy(input_path, output_dir):
    stems = ["drums.wav", "bass.wav", "melody.wav", "vocals.wav"]
    for name in stems:
        with open(os.path.join(output_dir, name), "wb") as f:
            f.write(b"fake stem data")


@app.post("/api/separate")
async def separate(beatId: str = Form(...), fileUrl: str = Form(None), file: UploadFile = None):
    try:
        if not beatId:
            return {"error": "beatId required"}

        tmp_input = tempfile.NamedTemporaryFile(delete=False, suffix=".wav")
        if file:
            tmp_input.write(await file.read())
        elif fileUrl:
            r = requests.get(fileUrl, stream=True)
            for chunk in r.iter_content(1024 * 1024):
                tmp_input.write(chunk)
        else:
            return {"error": "No file or URL provided"}
        tmp_input.close()

        out_dir = tempfile.mkdtemp()
        run_separation_dummy(tmp_input.name, out_dir)

        zip_path = os.path.join(tempfile.gettempdir(), f"{beatId}_stems.zip")
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
            for root, _, files in os.walk(out_dir):
                for f in files:
                    full = os.path.join(root, f)
                    rel = os.path.relpath(full, out_dir)
                    z.write(full, rel)

        bucket = storage.bucket()
        blob = bucket.blob(f"stems/{beatId}_stems.zip")
        blob.upload_from_filename(zip_path)
        blob.make_public()
        stems_url = blob.public_url

        if CALLBACK_URL:
            requests.post(
                CALLBACK_URL,
                json={"beatId": beatId, "stemsZipUrl": stems_url},
                timeout=10,
            )

        return {"ok": True, "stemsZipUrl": stems_url}
    except Exception as e:
        if CALLBACK_URL:
            try:
                requests.post(
                    CALLBACK_URL,
                    json={"beatId": beatId, "error": str(e)},
                    timeout=10,
                )
            except Exception:
                pass
        return {"error": str(e)}

# ============================================================
# 💧 2. WATERMARK (usando ffmpeg-python, compatível com Render)
# ============================================================

@app.post("/api/watermark")
async def watermark(fileUrl: str = Form(...), tagUrl: str = Form(...)):
    """
    Aplica uma tag de voz no beat e envia o resultado pro Firebase Storage.
    """
    try:
        if not fileUrl or not tagUrl:
            return {"error": "Missing fileUrl or tagUrl"}

        tmp_audio = tempfile.NamedTemporaryFile(delete=False, suffix=".mp3")
        tmp_tag = tempfile.NamedTemporaryFile(delete=False, suffix=".wav")

        # Download dos arquivos
        os.system(f"curl -s -o {tmp_audio.name} {fileUrl}")
        os.system(f"curl -s -o {tmp_tag.name} {tagUrl}")

        # Define arquivo final
        out_path = tempfile.NamedTemporaryFile(delete=False, suffix=".mp3")

        # Comando ffmpeg para sobrepor a tag de voz a cada 15 segundos
        (
            ffmpeg
            .input(tmp_audio.name)
            .filter_multi_output('asplit')[0]
            .output(out_path.name, af=f"adelay=15000|15000,amix=inputs=2:duration=first")
            .overwrite_output()
            .run(quiet=True)
        )

        # Upload para Firebase
        bucket = storage.bucket()
        blob = bucket.blob(f"watermarked/{os.path.basename(out_path.name)}")
        blob.upload_from_filename(out_path.name)
        blob.make_public()

        return {"ok": True, "url": blob.public_url}
    except Exception as e:
        return {"error": str(e)}

# ============================================================
# 🌐 3. STATUS
# ============================================================

@app.get("/")
def home():
    return {"service": "NOKAN Beat Processor", "status": "running"}


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 10000))
    uvicorn.run(app, host="0.0.0.0", port=port)
