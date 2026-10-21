"""Acceso SQL de los workers; se distribuye junto con notifications en una Layer."""
import json
import os
from datetime import datetime, timedelta

import boto3
from sqlalchemy import MetaData, Table, URL, create_engine, select, update
from sqlalchemy.pool import NullPool
import notifications


def database_engine():
    secret = json.loads(boto3.client("secretsmanager").get_secret_value(
        SecretId=os.environ["DB_SECRET_ARN"])["SecretString"])
    url = URL.create("postgresql+psycopg", username=secret["username"], password=secret["password"],
                     host=secret["host"], port=int(secret.get("port", 5432)), database=secret["dbname"])
    return create_engine(url, poolclass=NullPool, connect_args={
        "connect_timeout": 5, "sslmode": "verify-full", "sslrootcert": "/opt/python/rds-ca.pem",
        "options": "-c statement_timeout=10000 -c lock_timeout=2000 -c timezone=UTC",
    })


def expire(conn, seconds, limit=100, now=None):
    """Actualiza estado, auditoría y outbox en una única transacción."""
    if seconds <= 0:
        raise ValueError("La vigencia debe ser positiva")
    now = now or datetime.utcnow()
    schema = MetaData()
    requests = Table("solicitud", schema, autoload_with=conn)
    sites = Table("sede", schema, autoload_with=conn)
    logs = Table("log_auditoria", schema, autoload_with=conn)
    log_sites = Table("auditoria_sede", schema, autoload_with=conn)
    rows = conn.execute(select(requests).where(
        requests.c.estado == "aprobada", requests.c.hora_ingreso.is_(None),
        requests.c.fecha_aprobacion < now - timedelta(seconds=seconds),
    ).order_by(requests.c.id).limit(limit).with_for_update(skip_locked=True)).mappings().all()
    for row in rows:
        conn.execute(update(requests).where(requests.c.id == row["id"]).values(estado="vencida"))
        log_id = conn.execute(logs.insert().values(
            accion="Expiración automática", detalle=f"Solicitud #{row['id']} - vencida",
            usuario="sistema", fecha=now,
        ).returning(logs.c.id)).scalar_one()
        conn.execute(log_sites.insert().values(log_id=log_id, sede_id=row["sede_id"]))
        site = conn.execute(select(sites.c.nombre).where(sites.c.id == row["sede_id"])).scalar_one()
        notifications.enqueue(conn, solicitud_id=row["id"], tipo="permiso_vencido",
                              sede_id=row["sede_id"], sede=site, estado="vencida", now=now)
    return len(rows)


def link_legacy(conn, sub, email, ids):
    """Migración explícita del operador, sin inferir identidad de filas históricas."""
    if not sub or not email or not ids or len(ids) > 100:
        raise ValueError("Indicar usuario verificado y entre 1 y 100 solicitudes")
    requests = Table("solicitud", MetaData(), autoload_with=conn)
    linked = 0
    for request_id in sorted(set(int(i) for i in ids)):
        row = conn.execute(select(requests).where(requests.c.id == request_id)
                           .with_for_update()).mappings().one()
        if (row["email_solicitante"] or "").lower() != email.lower():
            raise ValueError("El correo histórico no coincide; requiere revisión manual")
        owner = conn.execute(select(notifications.owners.c.usuario_sub).where(
            notifications.owners.c.solicitud_id == request_id)).scalar_one_or_none()
        if owner and owner != sub:
            raise ValueError("La solicitud ya está vinculada a otra cuenta")
        if owner:
            continue
        conn.execute(notifications.owners.insert().values(solicitud_id=request_id, usuario_sub=sub))
        conn.execute(update(notifications.outbox).where(
            notifications.outbox.c.solicitud_id == request_id, notifications.outbox.c.publicado.is_(None),
        ).values(proximo_intento=datetime.utcnow()))
        linked += 1
    return linked
