"""Inicialización aditiva y versionada; se ejecuta una vez por despliegue."""
from sqlalchemy import Column, Integer, MetaData, Table, select, text

SCHEMA_VERSION = 1
schema_metadata = MetaData()
schema_version = Table("app_schema_version", schema_metadata, Column("version", Integer, primary_key=True))


def upgrade(engine, metadata):
    with engine.begin() as conn:
        if conn.dialect.name == "postgresql":
            conn.execute(text("SELECT pg_advisory_xact_lock(718345921)"))
        schema_metadata.create_all(conn)
        versions = set(conn.execute(select(schema_version.c.version)).scalars())
        if versions and max(versions) > SCHEMA_VERSION:
            raise RuntimeError("La base usa una versión de esquema más nueva que esta aplicación")
        if SCHEMA_VERSION not in versions:
            metadata.create_all(conn)
            sites = metadata.tables["sede"]
            if not conn.execute(select(sites.c.id).limit(1)).first():
                conn.execute(sites.insert(), [{"nombre": name} for name in
                             ("Planta Central", "Deposito Norte", "Deposito Sur")])
            conn.execute(schema_version.insert().values(version=SCHEMA_VERSION))


def check_ready(conn):
    if conn.execute(select(schema_version.c.version).where(
            schema_version.c.version == SCHEMA_VERSION)).scalar_one_or_none() is None:
        raise RuntimeError("Falta ejecutar la inicialización de la base")
