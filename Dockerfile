FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY vps_panel.py main.py .

CMD ["python", "vps_panel.py"]
