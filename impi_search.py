"""Submit the public IMPI form using its session and ViewState, without scripts.

No credentials, CAPTCHA bypass or private endpoint is used. Failed/partial searches
raise SearchFailure; only a verified result table can produce an empty list.
"""
from html.parser import HTMLParser
import asyncio
import json
import logging
import os
import re
import time
import unicodedata
import xml.etree.ElementTree as ET
from urllib.parse import urljoin

import httpx

SEARCH_URL = "https://acervomarcas.impi.gob.mx:8181/marcanet/vistas/common/datos/bsqFoneticaCompleta.pgi"
logger = logging.getLogger("uvicorn.error")
MAX_RESPONSE_BYTES = 25 * 1024 * 1024


class SearchFailure(Exception):
    def __init__(self, code, message, status=502):
        super().__init__(message)
        self.code, self.status = code, status


class Deadline:
    def __init__(self, seconds=65, clock=time.monotonic):
        self.clock, self.started, self.seconds = clock, clock(), seconds

    def remaining(self):
        remaining = self.seconds - (self.clock() - self.started)
        if remaining <= 0:
            raise SearchFailure("IMPI_TIMEOUT", "El IMPI no respondió dentro del tiempo disponible. No se pudo completar la búsqueda.", 504)
        return remaining


def log_step(request_id, step, deadline, **fields):
    logger.info(json.dumps({"event": "impi_search", "request_id": request_id, "step": step,
                            "elapsed_ms": round((deadline.clock() - deadline.started) * 1000), **fields}))


def normalized(value):
    return " ".join("".join(c for c in unicodedata.normalize("NFD", str(value).lower())
                            if unicodedata.category(c) != "Mn").split())


class Node:
    def __init__(self, tag="root", attrs=None, parent=None):
        self.tag, self.attrs, self.parent, self.children = tag, attrs or {}, parent, []

    def find(self, predicate):
        for child in self.children:
            if isinstance(child, Node):
                if predicate(child):
                    yield child
                yield from child.find(predicate)

    def text(self):
        return " ".join(" ".join(child.text() if isinstance(child, Node) else child
                                  for child in self.children).split())

    def has_class(self, value):
        return value in self.attrs.get("class", "").split()


class Document(HTMLParser):
    VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "wbr"}

    def __init__(self, html):
        super().__init__(convert_charrefs=True)
        self.root, self.stack, self.skip = Node(), [], None
        self.stack = [self.root]
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        if self.skip:
            return
        if tag in {"script", "style"}:
            self.skip = tag
            return
        attrs = dict(attrs)
        if tag == "img" and len(attrs.get("src", "")) > 65536:
            attrs["src"] = ""
        node = Node(tag, attrs, self.stack[-1])
        self.stack[-1].children.append(node)
        if tag not in self.VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in self.VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if self.skip:
            if tag == self.skip:
                self.skip = None
            return
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                del self.stack[i:]
                break

    def handle_data(self, data):
        if not self.skip and data.strip():
            self.stack[-1].children.append(data)


def form_fields(document):
    forms = list(document.root.find(lambda n: n.tag == "form" and n.attrs.get("id") == "frmBsqFonetica"))
    if len(forms) != 1:
        raise SearchFailure("IMPI_FORM_CHANGED", "El formulario público del IMPI cambió o no está disponible.")
    fields = {n.attrs["name"]: n.attrs.get("value", "") for n in
              forms[0].find(lambda n: n.tag == "input" and bool(n.attrs.get("name")))}
    if not fields.get("javax.faces.ViewState"):
        raise SearchFailure("IMPI_SESSION_FAILED", "No se pudo iniciar una sesión de consulta con el IMPI.")
    return fields


def result_document(xml):
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as error:
        raise SearchFailure("IMPI_INVALID_RESPONSE", "El IMPI no devolvió una respuesta de búsqueda válida.") from error
    if root.tag != "partial-response" or root.find("error") is not None or root.find("redirect") is not None:
        raise SearchFailure("IMPI_SESSION_FAILED", "El IMPI no pudo completar la sesión de consulta. Intenta de nuevo.")
    updates = root.findall("./changes/update")
    state = next((n.text for n in updates if "javax.faces.ViewState" in n.get("id", "")), None)
    content = "\n".join(n.text or "" for n in updates if "javax.faces.ViewState" not in n.get("id", ""))
    return Document(content), state


def parse_results(document):
    bodies = list(document.root.find(lambda n: n.tag == "tbody" and n.has_class("ui-datatable-data")))
    if len(bodies) != 1:
        raise SearchFailure("IMPI_LAYOUT_CHANGED", "El IMPI no mostró una tabla verificable. La consulta no se considera completada.")
    body = bodies[0]
    table = body.parent
    if table.tag != "table":
        raise SearchFailure("IMPI_LAYOUT_CHANGED", "Cambió el formato de los resultados del IMPI.")
    headers = [normalized(n.text()) for n in table.find(lambda n: n.tag == "th")]
    indexes = {field: next((i for i, h in enumerate(headers) if field in h), None)
               for field in ["titular", "expediente", "registro", "denominacion", "clase"]}
    if indexes["expediente"] is None or indexes["denominacion"] is None:
        raise SearchFailure("IMPI_LAYOUT_CHANGED", "Cambió el formato de resultados del IMPI. No se pudo validar la búsqueda.")
    rows = list(body.find(lambda n: n.tag == "tr"))
    empty_rows = [row for row in rows if row.has_class("ui-datatable-empty-message")]
    if empty_rows:
        confirmed_empty = re.search(r"no (?:se )?(?:encontraron|encontro|hay|existen).*?(?:registros|resultados|coincidencias|informacion|datos)|sin (?:registros|resultados|coincidencias)|no (?:records|results)(?: found)?", normalized(empty_rows[0].text()))
        if len(rows) == 1 and confirmed_empty:
            return [], body
        raise SearchFailure("IMPI_INVALID_RESULTS", "El IMPI devolvió información contradictoria. Intenta nuevamente.")
    if not rows:
        raise SearchFailure("IMPI_INVALID_RESULTS", "El IMPI devolvió una tabla vacía sin confirmar el resultado.")
    records = []
    for row in rows:
        cells = [n for n in row.children if isinstance(n, Node) and n.tag == "td"]
        def cell(field):
            index = indexes[field]
            return cells[index].text() if index is not None and index < len(cells) else ""
        expediente, denominacion = cell("expediente"), cell("denominacion")
        if not re.fullmatch(r"\d{1,16}", expediente) or not denominacion:
            raise SearchFailure("IMPI_INVALID_RESULTS", "El IMPI devolvió registros incompletos. La búsqueda no se considera completada.")
        image = next(row.find(lambda n: n.tag == "img"), None)
        logo = image.attrs.get("src", "") if image else ""
        if logo.startswith("data:image/") and re.match(r"data:image/(png|jpeg|gif|webp);base64,", logo):
            logo = re.sub(r"\s+", "", logo)
        elif logo:
            logo = urljoin(SEARCH_URL, logo)
            if not logo.startswith("https://acervomarcas.impi.gob.mx:8181/"):
                logo = ""
        records.append({"titular": cell("titular"), "expediente": expediente, "registro": cell("registro"),
                        "denominacion": denominacion, "clase": cell("clase"), "logo": logo})
    return records, body


async def request_page(session, method, deadline, data=None):
    remaining = deadline.remaining()
    async with session.stream(method, SEARCH_URL, data=data,
                              headers={"Faces-Request": "partial/ajax", "Referer": SEARCH_URL} if data else {},
                              timeout=httpx.Timeout(remaining, connect=min(10, remaining)), follow_redirects=False) as response:
        if response.status_code != 200:
            raise SearchFailure("IMPI_UNAVAILABLE", "El portal del IMPI no está disponible en este momento.")
        parts, size = [], 0
        async for chunk in response.aiter_bytes(65536):
            deadline.remaining()
            size += len(chunk)
            if size > MAX_RESPONSE_BYTES:
                raise SearchFailure("IMPI_TOO_MANY_RESULTS", "La consulta devuelve demasiada información. Usa una denominación más específica.")
            parts.append(chunk)
        return b"".join(parts).decode("utf-8", errors="strict")


def scrape_brand(denominacion, clase, request_id, session_factory=httpx.AsyncClient):
    try:
        seconds = max(10, min(70, int(os.getenv("IMPI_SEARCH_TIMEOUT_SECONDS", "65"))))
    except ValueError:
        seconds = 65
    deadline, step = Deadline(seconds), "open_portal"
    log_step(request_id, step, deadline)
    async def run():
        async with session_factory() as session:
            fields = form_fields(Document(await request_page(session, "GET", deadline)))
            payload = {**fields, "javax.faces.partial.ajax": "true", "javax.faces.source": "frmBsqFonetica:busquedaId2",
                       "javax.faces.partial.execute": "@all", "javax.faces.partial.render": "frmBsqFonetica",
                       "frmBsqFonetica:busquedaId2": "frmBsqFonetica:busquedaId2", "frmBsqFonetica": "frmBsqFonetica",
                       "frmBsqFonetica:clases": str(clase), "frmBsqFonetica:denominacion": denominacion}
            nonlocal step
            step = "submit_search"
            log_step(request_id, step, deadline)
            document, _ = result_document(await request_page(session, "POST", deadline, payload))
            step = "read_results"
            log_step(request_id, step, deadline)
            records, _ = parse_results(document)
            if any(document.root.find(lambda n: n.has_class("ui-paginator-next") and not n.has_class("ui-state-disabled"))):
                raise SearchFailure("IMPI_PARTIAL_RESULTS", "La consulta tiene más páginas de resultados. Usa una denominación más específica para obtener una búsqueda completa.")
            deadline.remaining()
            log_step(request_id, "complete", deadline, records=len(records))
            return records
    async def bounded():
        return await asyncio.wait_for(run(), timeout=seconds)
    try:
        return asyncio.run(bounded())
    except SearchFailure as error:
        log_step(request_id, "failed", deadline, failed_step=step, code=error.code)
        raise
    except (asyncio.TimeoutError, httpx.TimeoutException, httpx.TransportError) as error:
        status = 504 if isinstance(error, (asyncio.TimeoutError, httpx.TimeoutException)) else 502
        code = "IMPI_TIMEOUT" if status == 504 else "IMPI_CONNECTION_FAILED"
        log_step(request_id, "failed", deadline, failed_step=step, code=code)
        raise SearchFailure(code, "El IMPI no respondió correctamente. No se pudo completar la búsqueda; intenta más tarde.", status) from error
    except Exception as error:
        log_step(request_id, "failed", deadline, failed_step=step, code="IMPI_INVALID_RESPONSE", error_type=type(error).__name__)
        raise SearchFailure("IMPI_INVALID_RESPONSE", "No se pudo validar la respuesta del IMPI. La búsqueda no se considera completada.") from error
