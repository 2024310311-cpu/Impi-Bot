from sqlalchemy import Column, Integer, String, DateTime, ForeignKey
from sqlalchemy.orm import relationship
import datetime
from database import Base

class BusquedaModel(Base):
    __tablename__ = "busquedas"

    id = Column(Integer, primary_key=True, index=True)
    marca_objetivo = Column(String(255), nullable=False)
    clase_objetivo = Column(String(10), nullable=False)
    fecha_busqueda = Column(DateTime, default=datetime.datetime.utcnow)

    # Relación: Una búsqueda puede tener muchos resultados de marcas
    resultados = relationship("ResultadoMarcaModel", back_populates="busqueda", cascade="all, delete")

class ResultadoMarcaModel(Base):
    __tablename__ = "resultados_marcas"

    id = Column(Integer, primary_key=True, index=True)
    busqueda_id = Column(Integer, ForeignKey("busquedas.id", ondelete="CASCADE"))
    expediente = Column(String(50))
    registro = Column(String(50))
    denominacion = Column(String(255))
    clase = Column(String(10))
    similitud = Column(Integer)

    # Relación inversa
    busqueda = relationship("BusquedaModel", back_populates="resultados")