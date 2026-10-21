FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py notifications.py ./
# Permisos de lectura para todos: el contenedor corre con un usuario sin privilegios.
RUN chmod 644 app.py

# Usuario sin privilegios
RUN useradd --create-home appuser
USER appuser

EXPOSE 5000
# 2 workers por task (0,25 vCPU). Las tablas se crean al importar app.py.
CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "2", "--timeout", "60", "--access-logfile", "-", "app:app"]
