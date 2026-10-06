FROM python:3.12-slim

# pdflatex is required by pdf_generator.py. texlive-latex-extra covers the
# packages the resume/CL templates use; trim further if image size matters.
RUN apt-get update && apt-get install -y --no-install-recommends \
    texlive-latex-base texlive-latex-extra texlive-fonts-recommended \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt fastapi "uvicorn[standard]"

COPY . .

# Fly.io mounts a persistent volume here (see fly.toml) for the JD cache,
# seen-jobs store, and generated resume/CL output to survive restarts.
ENV OUTPUT_BASE_PATH=/data
VOLUME /data

EXPOSE 8000
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8000"]
