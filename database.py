from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.ext.declarative import declarative_base

# Cadena de conexión para XAMPP local (Usuario: root, Password: "", Puerto: 3306)
DATABASE_URL = "mysql+pymysql://root:@localhost:3306/impi_bot_db"

# Creamos el motor de la base de datos
engine = create_engine(DATABASE_URL)

# Configuramos la "fábrica" de sesiones
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

# Base para crear los modelos de las tablas más adelante
Base = declarative_base()