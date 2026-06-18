import os
import io
import mimetypes
import requests
import urllib.request
import urllib3
import xlsxwriter
from PIL import Image
from fastapi import FastAPI, HTTPException, Depends
from fastapi.responses import StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from playwright.sync_api import sync_playwright
from thefuzz import fuzz
from sqlalchemy.orm import Session
import time
import zipfile
import glob
import re

import models
from database import SessionLocal, engine

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

models.Base.metadata.create_all(bind=engine)

app = FastAPI(title="Motor de Búsqueda IMPI Profesional", version="8.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], 
    allow_credentials=True,
    allow_methods=["*"], 
    allow_headers=["*"],
)

mimetypes.add_type("application/pdf", ".pdf")

carpeta_destino = "descargas_oficiales"
if not os.path.exists(carpeta_destino):
    os.makedirs(carpeta_destino)

app.mount("/pdfs", StaticFiles(directory=carpeta_destino), name="pdfs")

class PeticionBusqueda(BaseModel):
    denominacion: str
    clase: str

class ItemDescarga(BaseModel):
    expediente: str
    denominacion: str

class PeticionDescarga(BaseModel):
    expedientes: list[ItemDescarga]

class PeticionExcel(BaseModel):
    resultados: list[dict]

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
    
    nueva_busqueda = models.BusquedaModel(
        marca_objetivo=marca_objetivo,
        clase_objetivo=clase_objetivo
    )
    db.add(nueva_busqueda)
    db.commit()
    db.refresh(nueva_busqueda)
    
    resultados_extraidos = []
    
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            )
            page = context.new_page()
            
            # Optimización: Bloquear descarga de imágenes, estilos y fuentes
            page.route("**/*", lambda route: route.abort() if route.request.resource_type in ["image", "stylesheet", "font", "media"] else route.continue_())
            
            page.goto("https://acervomarcas.impi.gob.mx:8181/marcanet/vistas/common/datos/bsqFoneticaCompleta.pgi", timeout=90000)
            
            page.locator("input[name*='denominacion'], input[id*='denominacion']").first.fill(marca_objetivo)
            page.locator("input[name*='clase'], input[id*='clase']").first.fill(str(clase_objetivo))
            
            page.wait_for_timeout(1000)
            page.locator("button:has-text('Buscar'), span:has-text('Buscar')").first.click(force=True)
            
            try:
                page.wait_for_selector(".ui-datatable-data", timeout=90000)
                filas = page.locator("tbody.ui-datatable-data tr.ui-widget-content").all()
                
                for fila in filas:
                    columnas = fila.locator("td").all()
                    if len(columnas) >= 8:
                        try:
                            titular = columnas[3].inner_text()
                            expediente = columnas[4].inner_text()
                            registro = columnas[5].inner_text()
                            denominacion = columnas[6].inner_text()
                            clase_result = columnas[7].inner_text()
                            
                            logo_url = ""
                            if len(columnas) >= 9:
                                imagenes = columnas[8].locator("img").all()
                                if len(imagenes) > 0:
                                    ruta_img = imagenes[0].get_attribute("src")
                                    if ruta_img:
                                        if ruta_img.startswith("http") or ruta_img.startswith("data:"):
                                            logo_url = ruta_img
                                        else:
                                            prefijo = "" if ruta_img.startswith("/") else "/"
                                            logo_url = f"https://acervomarcas.impi.gob.mx:8181{prefijo}{ruta_img}"
                            
                            if denominacion and expediente:
                                resultados_extraidos.append({
                                    "titular": titular.strip(),
                                    "expediente": expediente.strip(),
                                    "registro": registro.strip(),
                                    "denominacion": denominacion.strip(),
                                    "clase": clase_result.strip(),
                                    "logo": logo_url
                                })
                        except Exception:
                            continue
            except Exception as e:
                print(f"Error en la extracción de la tabla: {e}")
            
            browser.close()
            
    except Exception as e:
        print(f"Error general de conexión: {e}")
        resultados_extraidos = []

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
        elif similitud >= 50:
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

@app.post("/api/exportar-excel")
def exportar_excel(datos: PeticionExcel):
    output = io.BytesIO()
    workbook = xlsxwriter.Workbook(output, {'in_memory': True})
    worksheet = workbook.add_worksheet("Resultados IMPI")

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
                image_data = None
                if url_logo.startswith("data:image"):
                    with urllib.request.urlopen(url_logo) as response:
                        image_data = io.BytesIO(response.read())
                elif url_logo.startswith("http"):
                    headers_req = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
                    img_response = requests.get(url_logo, headers=headers_req, timeout=5, verify=False)
                    if img_response.status_code == 200:
                        image_data = io.BytesIO(img_response.content)

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
        worksheet.write(row_num, 5, f"{res.get('similitud', 0)}%", cell_format)

        similitud = res.get("similitud", 0)
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
    carpeta_destino = "descargas_oficiales"
    
    # Limpieza automática de archivos de más de 72 horas
    limpiar_archivos_viejos(carpeta_destino, 72)
    
    resultados_descarga = []
    archivos_descargados = []
    
    timestamp = int(time.time())
    nombre_zip = f"Expedientes_{timestamp}.zip"
    ruta_zip = os.path.join(carpeta_destino, nombre_zip)
    
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                accept_downloads=True
            )
            page = context.new_page()
            
            # Optimización: Bloquear descarga de imágenes, estilos y fuentes para mayor velocidad
            page.route("**/*", lambda route: route.abort() if route.request.resource_type in ["image", "stylesheet", "font", "media"] else route.continue_())
            
            for item in datos.expedientes:
                expediente = item.expediente
                denominacion_limpia = limpiar_nombre_archivo(item.denominacion)
                try:
                    page.goto("https://acervomarcas.impi.gob.mx:8181/marcanet/vistas/common/datos/bsqExpedienteCompleto.pgi", timeout=90000)
                    page.locator("input[name*='expediente']").fill(expediente)
                    page.get_by_role("button", name="Buscar").click()
                    page.wait_for_selector(".ui-datatable-data tr", timeout=30000)
                    page.locator(".ui-datatable-data tr").first.click()
                    page.wait_for_selector("a:has-text('Descargar datos de la consulta')", timeout=30000)
                    with page.expect_download(timeout=30000) as download_info:
                        page.locator("a:has-text('Descargar datos de la consulta')").first.click()
                    download = download_info.value
                    
                    nombre_pdf = f"{denominacion_limpia}_{expediente}.pdf"
                    ruta_final = f"{carpeta_destino}/{nombre_pdf}"
                    download.save_as(ruta_final)
                    
                    import urllib.parse
                    url_pdf = f"https://impi-bot.onrender.com/pdfs/{urllib.parse.quote(nombre_pdf)}"
                    
                    resultados_descarga.append({
                        "expediente": expediente, "estado": "Exitoso", "url": url_pdf
                    })
                    archivos_descargados.append(ruta_final)
                    page.wait_for_timeout(3000)
                except Exception:
                    resultados_descarga.append({"expediente": expediente, "estado": "Error"})
            browser.close()
            
        url_zip = None
        if archivos_descargados:
            with zipfile.ZipFile(ruta_zip, 'w') as zipf:
                for archivo in archivos_descargados:
                    zipf.write(archivo, os.path.basename(archivo))
            url_zip = f"https://impi-bot.onrender.com/pdfs/{nombre_zip}"

    except Exception:
        raise HTTPException(status_code=500, detail="Fallo general")
        
    return {
        "mensaje": "Descarga finalizada", 
        "detalles": resultados_descarga,
        "zip_url": url_zip
    }