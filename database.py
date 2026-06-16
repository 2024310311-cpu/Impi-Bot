from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.ext.declarative import declarative_base

# Cadena de conexión adaptada para la nube (SQLite en archivo local) en lugar de XAMPP
DATABASE_URL = "sqlite:///./impi_bot.db"

# Creamos el motor de la base de datos (con configuración especial para SQLite)
engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})

# Configuramos la "fábrica" de sesiones
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

# Base para crear los modelos de las tablas más adelante
Base = declarative_base()