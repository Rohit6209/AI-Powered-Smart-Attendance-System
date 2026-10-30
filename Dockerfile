FROM python:3.10-slim
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential cmake libopenblas-dev liblapack-dev libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir cmake dlib && pip install --no-cache-dir -r requirements.txt
COPY . .
ENV DATABASE=/data/attendance.db
VOLUME /data
EXPOSE 5000
CMD ["python", "server.py"]
