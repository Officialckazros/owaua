FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY bot.py memory_store.py persona.py .env.example ./
RUN mkdir -p /app/data
VOLUME ["/app/data"]
CMD ["python", "bot.py"]
