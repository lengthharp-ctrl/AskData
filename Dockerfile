FROM python:3.12-slim

WORKDIR /app

# 非 root 用户运行，降低沙箱逃逸影响面
RUN useradd --create-home --shell /bin/bash askdata

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN mkdir -p /app/data && chown -R askdata:askdata /app

USER askdata

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]

