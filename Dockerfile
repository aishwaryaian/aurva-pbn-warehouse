FROM python:3.12-slim
WORKDIR /app
COPY app.py ./app.py
COPY static ./static
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
EXPOSE 8080
CMD ["python", "-u", "app.py"]
