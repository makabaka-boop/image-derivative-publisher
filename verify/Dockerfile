FROM python:3.12-slim

WORKDIR /verify

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV API_URL=http://api:8000 \
    DATA_DIR=/data

CMD ["pytest", "-v", "--tb=short", "-x", "--timeout=120", "test_verify.py"]
