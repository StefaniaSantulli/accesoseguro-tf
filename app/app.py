"""API de AccesoSeguro (PLACEHOLDER).

Versión mínima para que el despliegue funcione de punta a punta: el ALB
necesita /health para considerar sana a la task. Reemplazar por la API real
(validación de JWT, reglas de negocio, RDS, SNS, presigned URLs, etc.).
Todas las variables de entorno las inyecta ECS desde Terraform
(ver local.api_environment en infra/locals.tf).
"""
import os

from flask import Flask, jsonify

app = Flask(__name__)


@app.after_request
def cors(resp):
    resp.headers["Access-Control-Allow-Origin"] = os.environ.get("CORS_ALLOWED_ORIGIN", "*")
    resp.headers["Access-Control-Allow-Headers"] = "Authorization, Content-Type"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, PATCH, DELETE, OPTIONS"
    return resp


@app.get("/health")
def health():
    return jsonify(status="ok")


@app.get("/api/info")
def info():
    return jsonify(
        app="AccesoSeguro",
        env=os.environ.get("APP_ENV"),
        db_host_configurado=bool(os.environ.get("DB_HOST")),
        cognito_pool=os.environ.get("COGNITO_USER_POOL_ID"),
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")))
