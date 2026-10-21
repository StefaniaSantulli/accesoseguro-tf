"""SQL compartido por la API y las Lambdas de documentos y reportes."""
import csv
import io
from datetime import datetime, timedelta

from sqlalchemy import Column, DateTime, ForeignKey, Integer, MetaData, String, Table, select

metadata = MetaData()
documents = Table(
    "documento_validacion", metadata,
    Column("solicitud_id", Integer, ForeignKey("solicitud.id"), primary_key=True),
    Column("s3_key", String(300), nullable=False),
    Column("estado", String(20), nullable=False),
    Column("motivo", String(200)),
    Column("version_id", String(1024)),
    Column("secuenciador", String(128)),
    Column("tamano", Integer),
    Column("actualizado", DateTime, nullable=False),
)


def save_document_result(conn, key, version_id, sequencer, size, reason):
    """Serializa con reemplazos/aprobaciones; ignora eventos duplicados o viejos."""
    requests = Table("solicitud", MetaData(), autoload_with=conn)
    request = conn.execute(select(requests).where(requests.c.documento_s3_key == key)
                           .with_for_update()).mappings().one_or_none()
    if not request or request["estado"] != "pendiente":
        return False
    row = conn.execute(select(documents).where(
        documents.c.solicitud_id == request["id"], documents.c.s3_key == key,
    )).mappings().one_or_none()
    if not row:
        return False
    sequence = int(sequencer, 16)
    if row["secuenciador"] and sequence <= int(row["secuenciador"], 16):
        return False
    conn.execute(documents.update().where(documents.c.solicitud_id == request["id"]).values(
        estado="rechazado" if reason else "valido", motivo=reason,
        version_id=version_id, secuenciador=sequencer, tamano=size, actualizado=datetime.utcnow(),
    ))
    return True


def report_dates(desde=None, hasta=None):
    try:
        start = datetime.strptime(desde, "%Y-%m-%d") if desde else None
        end = datetime.strptime(hasta, "%Y-%m-%d") + timedelta(days=1) if hasta else None
        if start and end and start >= end:
            raise ValueError()
        return start, end
    except (ValueError, TypeError, OverflowError) as exc:
        raise ValueError("Fechas inválidas: usar AAAA-MM-DD y desde menor o igual a hasta") from exc


def report_csv(conn, sede_id, desde=None, hasta=None, limit=10000):
    """Consulta parametrizada, una sede, rango inclusivo y tamaño acotado."""
    start, end = report_dates(desde, hasta)
    schema = MetaData()
    requests = Table("solicitud", schema, autoload_with=conn)
    sites = Table("sede", schema, autoload_with=conn)
    if not conn.execute(select(sites.c.id).where(sites.c.id == sede_id)).first():
        raise ValueError("La sede no existe")
    query = select(
        requests.c.id, requests.c.nombre_solicitante, requests.c.dni, requests.c.patente,
        requests.c.empresa, sites.c.nombre, requests.c.estado, requests.c.fecha_creacion,
        requests.c.hora_ingreso, requests.c.hora_egreso,
    ).join(sites, sites.c.id == requests.c.sede_id).where(requests.c.sede_id == sede_id)
    if start:
        query = query.where(requests.c.fecha_creacion >= start)
    if end:
        query = query.where(requests.c.fecha_creacion < end)
    rows = conn.execute(query.order_by(requests.c.fecha_creacion, requests.c.id).limit(limit + 1)).all()
    if len(rows) > limit:
        raise ValueError(f"El reporte supera {limit} filas; acotá el rango de fechas")
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(["ID", "Nombre", "DNI", "Patente", "Empresa", "Sede", "Estado",
                     "Fecha solicitud", "Hora ingreso", "Hora egreso"])
    for row in rows:
        writer.writerow([csv_cell(value) for value in row])
    return output.getvalue(), len(rows)


def csv_cell(value):
    if value is None:
        return ""
    if isinstance(value, str) and (value.startswith(("\t", "\r", "\n")) or
                                   value.lstrip().startswith(("=", "+", "-", "@"))):
        return "'" + value
    return value
