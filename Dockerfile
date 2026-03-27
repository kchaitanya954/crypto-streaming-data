FROM python:3.12-slim

WORKDIR /app

# Install dependencies first (cached layer)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source
COPY . .

# SQLite data directory
RUN mkdir -p data

# Port the dashboard listens on
EXPOSE 8000

CMD ["python", "orchestrator.py"]
