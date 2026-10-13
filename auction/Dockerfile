FROM python:3.12-slim

WORKDIR /app
COPY app.py .
COPY web/ web/

EXPOSE 8080
CMD ["python", "-u", "app.py"]
