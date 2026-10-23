import os
import io
import mimetypes
import requests
import base64
import math
import xlsxwriter
from PIL import Image
from fastapi import FastAPI, HTTPException, Depends
from fastapi.responses import StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator
from playwright.sync_api import sync_playwright
from thefuzz import fuzz
from sqlalchemy.orm import Session
import time
import zipfile
import glob
import re
import logging
import threading
import uuid
from contextlib import contextmanager
from impi_search import scrape_brand, SearchFailure, SEARCH_URL, Deadline

import models
from database import SessionLocal, engine

models.Base.metadata.create_all(bind=engine)

app = FastAPI(title="Motor de Búsqueda IMPI Profesional", version="8.1")
logger = logging.getLogger("uvicorn.error")
search_slot = threading.BoundedSemaphore(1)


@contextmanager
def search_capacity():
    if not search_slot.acquire(blocking=False):
        raise HTTPException(status_code=503, detail={"code": "IMPI_BUSY", "message": "El buscador está atendiendo otra consulta. Intenta de nuevo en unos momentos."}, headers={"Retry-After": "15"})
    try:
        yield
    finally:
        search_slot.release()


@app.get("/")
@app.get("/health")
def health():
    # This checks this service only, not the availability of the external IMPI portal.
    return {"status": "ok", "version": app.version, "search_contract": 2}

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

mimetypes.add_type("application/pdf", ".pdf")

carpeta_destino = "descargas_oficiales"
if not os.path.exists(carpeta_destino):
    os.makedirs(carpeta_destino)

app.mount("/pdfs", StaticFiles(directory=carpeta_destino), name="pdfs")

class PeticionBusqueda(BaseModel):
    denominacion: str = Field(min_length=2, max_length=120)
    clase: str

    @field_validator("denominacion", mode="before")
    @classmethod
    def clean_brand(cls, value):
        return " ".join(value.split()) if isinstance(value, str) else value

    @field_validator("clase", mode="before")
    @classmethod
    def validate_class(cls, value):
        if isinstance(value, bool) or not re.fullmatch(r"\d{1,2}", str(value)) or not 1 <= int(value) <= 45:
            raise ValueError("La clase Niza debe ser un número del 1 al 45.")
        return str(int(value))

class ItemDescarga(BaseModel):
    expediente: str = Field(pattern=r"^\d{1,16}$")
    denominacion: str = Field(min_length=1, max_length=120)

class PeticionDescarga(BaseModel):
    expedientes: list[ItemDescarga] = Field(min_length=1, max_length=10)

class PeticionExcel(BaseModel):
    resultados: list[dict] = Field(min_length=1, max_length=500)

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

def limpiar_texto(texto):
    if not texto: return ""
    texto = texto.upper().strip()
    for char in ["+", "-", " ", ",", ".", "S.A.", "C.V."]:
        texto = texto.replace(char, "")
    return texto

def limpiar_archivos_viejos(carpeta, horas=72):
    ahora = time.time()
    tiempo_limite = horas * 3600
    for archivo in glob.glob(os.path.join(carpeta, '*')):
        try:
            if os.path.isfile(archivo):
                if ahora - os.path.getmtime(archivo) > tiempo_limite:
                    os.remove(archivo)
        except Exception as e:
            print(f"Error al limpiar {archivo}: {e}")

def limpiar_nombre_archivo(nombre):
    # Remueve caracteres inválidos para nombres de archivo
    nombre = re.sub(r'[\\/*?:"<>|]', "", nombre)
    return " ".join(nombre.split())

@app.post("/api/buscar")
def iniciar_busqueda(datos: PeticionBusqueda, db: Session = Depends(get_db)):
    marca_objetivo = datos.denominacion
    clase_objetivo = datos.clase
    request_id = uuid.uuid4().hex
    try:
        with search_capacity():
            resultados_extraidos = scrape_brand(marca_objetivo, clase_objetivo, request_id)
    except SearchFailure as error:
        raise HTTPException(status_code=error.status, detail={"code": error.code, "message": str(error), "request_id": request_id}) from error

    nueva_busqueda = models.BusquedaModel(marca_objetivo=marca_objetivo, clase_objetivo=clase_objetivo)
    db.add(nueva_busqueda)
    db.flush()

    marca_limpia = limpiar_texto(marca_objetivo)
    focos_rojos = []
    focos_amarillos = []
    focos_verdes = []

    for item in resultados_extraidos:
        denom_limpia = limpiar_texto(item["denominacion"])
        similitud = fuzz.ratio(marca_limpia, denom_limpia)
        item["similitud"] = similitud

        # Umbrales ajustados: Medio desde 60%
        if similitud >= 80:
            focos_rojos.append(item)
        elif similitud >= 60:
            focos_amarillos.append(item)
        else:
            focos_verdes.append(item)

        nuevo_resultado = models.ResultadoMarcaModel(
            busqueda_id=nueva_busqueda.id,
            expediente=item["expediente"],
            registro=item["registro"],
            denominacion=item["denominacion"],
            clase=item["clase"],
            similitud=similitud
        )
        db.add(nuevo_resultado)

    db.commit()

    return {
        "busqueda_id": nueva_busqueda.id,
        "consulta": {"estado": "completada", "completa": True, "fuente": SEARCH_URL, "request_id": request_id},
        "busqueda": {"marca": marca_objetivo, "clase": clase_objetivo},
        "metricas": {
            "total_analizados": len(resultados_extraidos),
            "focos_rojos": len(focos_rojos),
            "focos_amarillos": len(focos_amarillos),
            "focos_verdes": len(focos_verdes)
        },
        "resultados": {
            "peligro_alto": focos_rojos,
            "peligro_medio": focos_amarillos,
            "peligro_bajo": focos_verdes
        }
    }

def _logo_image_data(url_logo, deadline):
    if not isinstance(url_logo, str) or len(url_logo) > 65536:
        return None
    match = re.fullmatch(r"data:image/(?:png|jpeg|gif|webp);base64,([A-Za-z0-9+/=\s]+)", url_logo)
    if match:
        return io.BytesIO(base64.b64decode(re.sub(r"\s+", "", match.group(1)), validate=True))
    # Export data is public input. Never fetch arbitrary URLs, private addresses,
    # redirects or URLs with TLS verification disabled.
    if not url_logo.startswith("https://acervomarcas.impi.gob.mx:8181/marcanet/"):
        return None
    timeout = min(3, deadline.remaining())
    with requests.get(url_logo, timeout=timeout, stream=True, allow_redirects=False) as response:
        if response.status_code != 200:
            return None
        chunks, size = [], 0
        for chunk in response.iter_content(16384):
            deadline.remaining()
            size += len(chunk)
            if size > 1024 * 1024:
                return None
            chunks.append(chunk)
        return io.BytesIO(b"".join(chunks))


@app.post("/api/exportar-excel")
def exportar_excel(datos: PeticionExcel):
    output = io.BytesIO()
    workbook = xlsxwriter.Workbook(output, {'in_memory': True, 'strings_to_formulas': False, 'strings_to_urls': False})
    worksheet = workbook.add_worksheet("Resultados IMPI")
    image_deadline = Deadline(20)

    header_format = workbook.add_format({'bold': True, 'bg_color': '#1E3A8A', 'font_color': 'white', 'border': 1, 'align': 'center', 'valign': 'vcenter'})
    cell_format = workbook.add_format({'valign': 'vcenter', 'border': 1, 'align': 'center'})
    cell_format_left = workbook.add_format({'valign': 'vcenter', 'border': 1, 'align': 'left'})

    worksheet.set_column('A:A', 25)
    worksheet.set_column('B:B', 40) # Titular
    worksheet.set_column('C:C', 15) # Expediente
    worksheet.set_column('D:D', 15) # Registro
    worksheet.set_column('E:E', 35) # Denominación
    worksheet.set_column('F:F', 15) # Similitud
    worksheet.set_column('G:G', 15) # Riesgo

    headers = ["Logotipo", "Titular", "Expediente", "Registro", "Denominación", "Similitud", "Riesgo"]
    for col_num, data in enumerate(headers):
        worksheet.write(0, col_num, data, header_format)

    for row_num, res in enumerate(datos.resultados, 1):
        worksheet.set_row(row_num, 70)
        url_logo = res.get("logo")
        imagen_insertada = False

        if url_logo:
            try:
                image_deadline.remaining()
                image_data = _logo_image_data(url_logo, image_deadline)

                if image_data:
                    image_data.seek(0)
                    with Image.open(image_data) as img:
                        orig_width, orig_height = img.size

                    target_width = 170
                    target_height = 85

                    scale_w = target_width / float(orig_width)
                    scale_h = target_height / float(orig_height)
                    scale = min(scale_w, scale_h)

                    image_data.seek(0)

                    worksheet.insert_image(row_num, 0, "logo.png", {
                        'image_data': image_data,
                        'x_scale': scale,
                        'y_scale': scale,
                        'x_offset': 5,
                        'y_offset': 5,
                        'object_position': 1
                    })
                    imagen_insertada = True
            except Exception:
                pass

        if not imagen_insertada:
            worksheet.write(row_num, 0, "N/A", cell_format)

        worksheet.write(row_num, 1, res.get("titular", "N/A"), cell_format_left)
        worksheet.write(row_num, 2, res.get("expediente", ""), cell_format)
        worksheet.write(row_num, 3, res.get("registro", "N/A"), cell_format)
        worksheet.write(row_num, 4, res.get("denominacion", ""), cell_format_left)
        try:
            similitud = float(res.get("similitud", 0))
            similitud = min(100, max(0, similitud)) if math.isfinite(similitud) else 0
        except (ValueError, TypeError):
            similitud = 0
        worksheet.write(row_num, 5, f"{similitud:g}%", cell_format)
        riesgo = "BAJO"
        if similitud >= 80: riesgo = "ALTO"
        elif similitud >= 60: riesgo = "MEDIO"

        worksheet.write(row_num, 6, riesgo, cell_format)

    workbook.close()
    output.seek(0)

    return StreamingResponse(
        output,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=Reporte_IMPI.xlsx"}
    )

@app.post("/api/descargar")
def descargar_expedientes(datos: PeticionDescarga):
    with search_capacity():
        return _descargar_expedientes(datos)


def _descargar_expedientes(datos: PeticionDescarga):
    carpeta_destino = "descargas_oficiales"

    # Limpieza automática de archivos de más de 72 horas
    limpiar_archivos_viejos(carpeta_destino, 72)

    resultados_descarga = []
    archivos_descargados = []

    timestamp = uuid.uuid4().hex
    nombre_zip = f"Expedientes_{timestamp}.zip"
    ruta_zip = os.path.join(carpeta_destino, nombre_zip)
    deadline = Deadline(65)

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, timeout=int(deadline.remaining() * 1000))
            context = browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                accept_downloads=True
            )
            page = context.new_page()

            # Optimización: Bloquear descarga de imágenes, estilos y fuentes para mayor velocidad
            page.route("**/*", lambda route: route.abort() if route.request.resource_type in ["image", "font", "media"] else route.continue_())

            for item in datos.expedientes:
                expediente = item.expediente
                denominacion_limpia = limpiar_nombre_archivo(item.denominacion)
                try:
                    remaining = lambda: int(deadline.remaining() * 1000)
                    page.goto("https://acervomarcas.impi.gob.mx:8181/marcanet/vistas/common/datos/bsqExpedienteCompleto.pgi", wait_until="domcontentloaded", timeout=remaining())
                    page.locator("input[name*='expediente']").fill(expediente, timeout=remaining())
                    page.get_by_role("button", name="Buscar", exact=True).click(timeout=remaining())
                    page.wait_for_selector(".ui-datatable-data tr", timeout=remaining())
                    page.locator(".ui-datatable-data tr").first.click(timeout=remaining())
                    page.wait_for_selector("a:has-text('Descargar datos de la consulta')", timeout=remaining())
                    with page.expect_download(timeout=remaining()) as download_info:
                        page.locator("a:has-text('Descargar datos de la consulta')").first.click(timeout=remaining())
                    download = download_info.value

                    nombre_pdf = f"{timestamp}_{denominacion_limpia[:60]}_{expediente}.pdf"
                    ruta_final = f"{carpeta_destino}/{nombre_pdf}"
                    download.save_as(ruta_final)
                    with open(ruta_final, "rb") as pdf:
                        if pdf.read(5) != b"%PDF-":
                            os.remove(ruta_final)
                            raise ValueError("El portal no devolvió un PDF")

                    import urllib.parse
                    url_pdf = f"https://impi-bot.onrender.com/pdfs/{urllib.parse.quote(nombre_pdf)}"

                    resultados_descarga.append({
                        "expediente": expediente, "estado": "Exitoso", "url": url_pdf
                    })
                    archivos_descargados.append(ruta_final)
                except Exception as error:
                    logger.warning("IMPI PDF download failed: %s", type(error).__name__)
                    resultados_descarga.append({"expediente": expediente, "estado": "Error", "mensaje": "No se pudo obtener un PDF oficial de este expediente."})
            browser.close()

        url_zip = None
        if archivos_descargados:
            with zipfile.ZipFile(ruta_zip, 'w') as zipf:
                for archivo in archivos_descargados:
                    zipf.write(archivo, os.path.basename(archivo))
            url_zip = f"https://impi-bot.onrender.com/pdfs/{nombre_zip}"

    except Exception as error:
        logger.warning("IMPI PDF batch failed: %s", type(error).__name__)
        raise HTTPException(status_code=502, detail={"code": "IMPI_DOWNLOAD_FAILED", "message": "No se pudieron obtener los expedientes oficiales del IMPI."}) from error

    return {
        "mensaje": "Descarga finalizada",
        "detalles": resultados_descarga,
        "completa": len(archivos_descargados) == len(datos.expedientes),
        "zip_url": url_zip
    }
