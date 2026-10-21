"""Uso en una tarea puntual de ECS: python manage_db.py upgrade."""
import argparse
import os

os.environ["AUTO_INIT_DB"] = "false"
from app import app, db  # noqa: E402
from database_schema import upgrade  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["upgrade"])
    parser.parse_args()
    with app.app_context():
        upgrade(db.engine, db.metadata)
    print("Base inicializada correctamente")


if __name__ == "__main__":
    main()
