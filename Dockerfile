FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY bridge.py web.py make_icons.py setup_paperless.py ./
COPY static ./static
RUN python make_icons.py static
EXPOSE 8000
# No --proxy-headers: the sign-in check needs the real address of whoever connected (the proxy).
CMD ["uvicorn", "web:app", "--host", "0.0.0.0", "--port", "8000", "--no-proxy-headers", "--no-access-log"]
