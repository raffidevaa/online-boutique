FROM python:3.12-alpine

WORKDIR /app
COPY server.py /app/server.py

USER nobody
EXPOSE 8081
CMD ["python", "/app/server.py"]
