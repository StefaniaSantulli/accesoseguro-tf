FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py notifications.py application_jobs.py database_schema.py manage_db.py ./
# Permisos de lectura para todos: el contenedor corre con un usuario sin privilegios.
RUN chmod 644 *.py

# Usuario sin privilegios
RUN useradd --create-home appuser
USER appuser

EXPOSE 5000
# 2 workers por task (0,25 vCPU). La base se prepara antes con manage_db.py.
CMD ["sh", "-c", "exec gunicorn --bind \"0.0.0.0:${PORT:-5000}\" --workers 2 --timeout 60 --access-logfile - app:app"]
