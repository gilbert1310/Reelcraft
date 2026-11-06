FROM python:3.12-slim

# ffmpeg (Debian's build includes vidstab and minterpolate)
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /srv
# CPU-only PyTorch keeps the image ~1.5 GB instead of ~6 GB.
RUN pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Bake the CLIP weights into the image so cold starts don't download them.
ENV CLIP_MODEL=ViT-B-32 CLIP_PRETRAINED=laion2b_s34b_b79k HF_HOME=/srv/.cache
RUN python -c "import open_clip; open_clip.create_model_and_transforms('ViT-B-32', pretrained='laion2b_s34b_b79k')"

COPY app ./app
ENV PYTHONUNBUFFERED=1 WORK_DIR=/tmp/reelcraft
EXPOSE 8000
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1 --timeout-keep-alive 30"]
