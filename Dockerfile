FROM python:3.10-slim
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && pip install --no-cache-dir --no-deps face_recognition
COPY . .
ENV DATABASE=/data/attendance.db
VOLUME /data
EXPOSE 5000
CMD ["python", "server.py"]
