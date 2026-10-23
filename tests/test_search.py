import asyncio
import json
import unittest
import io
import zipfile
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import main
import models
from impi_search import Deadline, Document, SearchFailure, form_fields, parse_results, result_document, scrape_brand

FORM = '<form id="frmBsqFonetica"><input name="javax.faces.ViewState" value="test-state"><input name="frmBsqFonetica:clases"><input name="frmBsqFonetica:denominacion"></form>'
HEAD = '<table><thead><tr><th>Titular</th><th>Expediente</th><th>Registro</th><th>Denominación</th><th>Clase</th><th>Logo</th></tr></thead><tbody class="ui-datatable-data">'
ROW = '<tr><td>Empresa de prueba</td><td><a>12345</a><script>doNotRun()</script></td><td>900</td><td>Marca ejemplo</td><td>41</td><td><img src="/marcanet/logo.png"></td></tr>'
TAIL = '</tbody></table>'


def envelope(html):
    return '<partial-response><changes><update id="frmBsqFonetica"><![CDATA[' + html + ']]></update></changes></partial-response>'


class ParsingTests(unittest.TestCase):
    def test_real_headers_and_script_in_expediente(self):
        records, _ = parse_results(Document(HEAD + ROW + TAIL))
        self.assertEqual(records[0]['expediente'], '12345')
        self.assertEqual(records[0]['denominacion'], 'Marca ejemplo')
        self.assertTrue(records[0]['logo'].startswith('https://acervomarcas.impi.gob.mx:8181/'))

    def test_only_explicit_empty_marker_is_zero(self):
        records, _ = parse_results(Document(HEAD + '<tr class="ui-datatable-empty-message"><td>No se encontraron registros</td></tr>' + TAIL))
        self.assertEqual(records, [])
        for html in [HEAD + TAIL, '<div>Intenta de nuevo</div>', '<table><tbody class="ui-datatable-data"><tr><td>error</td></tr></tbody></table>']:
            with self.subTest(html=html), self.assertRaises(SearchFailure):
                parse_results(Document(html))

    def test_malformed_rows_do_not_get_silently_dropped(self):
        with self.assertRaises(SearchFailure):
            parse_results(Document(HEAD + ROW.replace('12345', 'invalid') + TAIL))
        with self.assertRaises(SearchFailure):
            parse_results(Document(HEAD + '<tr class="ui-datatable-empty-message"><td>Error de conexión</td></tr>' + TAIL))

    def test_huge_and_external_logos_are_discarded(self):
        for source in ['https://example.com/logo.png', 'http://169.254.169.254/', 'data:image/png;base64,' + 'a' * 70000]:
            records, _ = parse_results(Document(HEAD + ROW.replace('/marcanet/logo.png', source) + TAIL))
            self.assertEqual(records[0]['logo'], '')

    def test_html_entities_are_decoded(self):
        records, _ = parse_results(Document(HEAD + ROW.replace('Marca ejemplo', 'Marca &amp; ejemplo') + TAIL))
        self.assertEqual(records[0]['denominacion'], 'Marca & ejemplo')

    def test_session_is_required(self):
        self.assertEqual(form_fields(Document(FORM))['javax.faces.ViewState'], 'test-state')
        with self.assertRaises(SearchFailure):
            form_fields(Document(FORM.replace('javax.faces.ViewState', 'missing')))

    def test_error_or_login_redirect_is_not_empty(self):
        for xml in ['<partial-response><error>failure</error></partial-response>', '<partial-response><redirect url="/login"/></partial-response>', 'Not XML']:
            with self.subTest(xml=xml), self.assertRaises(SearchFailure):
                result_document(xml)

    def test_one_total_deadline(self):
        clock = [100]
        deadline = Deadline(65, clock=lambda: clock[0])
        clock[0] = 150
        self.assertEqual(deadline.remaining(), 15)
        clock[0] = 166
        with self.assertRaises(SearchFailure) as caught:
            deadline.remaining()
        self.assertEqual(caught.exception.status, 504)


class NetworkTests(unittest.TestCase):
    def session(self, handler):
        return lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))

    def test_submits_original_session_form_and_returns_results(self):
        calls = []
        def handler(request):
            calls.append(request)
            if request.method == 'GET':
                return httpx.Response(200, text=FORM, headers={'set-cookie': 'JSESSIONID=test; Path=/'})
            self.assertIn('JSESSIONID=test', request.headers['cookie'])
            self.assertIn('javax.faces.ViewState=test-state', request.content.decode())
            self.assertEqual(request.headers['faces-request'], 'partial/ajax')
            return httpx.Response(200, text=envelope(HEAD + ROW + TAIL))
        self.assertEqual(len(scrape_brand('Marca ejemplo', '41', 'test-id', self.session(handler))), 1)
        self.assertEqual(len(calls), 2)

    def test_timeout_propagates_without_empty_results(self):
        def handler(request):
            raise httpx.ReadTimeout('private failure', request=request)
        with self.assertRaises(SearchFailure) as caught:
            scrape_brand('Demo', '41', 'test-id', self.session(handler))
        self.assertEqual(caught.exception.status, 504)
        self.assertNotIn('private', str(caught.exception))

    def test_http_failure_does_not_become_zero(self):
        with self.assertRaises(SearchFailure):
            scrape_brand('Demo', '41', 'test-id', self.session(lambda _: httpx.Response(503)))

    def test_pagination_is_not_reported_as_complete(self):
        def handler(request):
            return httpx.Response(200, text=FORM if request.method == 'GET' else envelope(HEAD + ROW + TAIL + '<a class="ui-paginator-next"></a>'))
        with self.assertRaises(SearchFailure) as caught:
            scrape_brand('Demo', '41', 'test-id', self.session(handler))
        self.assertEqual(caught.exception.code, 'IMPI_PARTIAL_RESULTS')


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine('sqlite://', connect_args={'check_same_thread': False}, poolclass=StaticPool)
        models.Base.metadata.create_all(bind=self.engine)
        self.sessions = sessionmaker(bind=self.engine)
        def database():
            with self.sessions() as session:
                yield session
        main.app.dependency_overrides[main.get_db] = database
        self.client = TestClient(main.app)

    def tearDown(self):
        main.app.dependency_overrides.clear()
        self.engine.dispose()

    def test_health_is_local_and_does_not_query_impi(self):
        with patch('main.scrape_brand') as scrape:
            response = self.client.get('/health')
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()['search_contract'], 2)
            scrape.assert_not_called()

    def test_success_includes_verified_contract_and_all_low_matches(self):
        rows, _ = parse_results(Document(HEAD + ROW + TAIL))
        with patch('main.scrape_brand', return_value=rows):
            response = self.client.post('/api/buscar', json={'denominacion': 'ZZ unrelated', 'clase': 41})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['consulta']['completa'])
        self.assertEqual(len(response.json()['resultados']['peligro_bajo']), 1)

    def test_failed_search_is_http_error_and_not_saved_as_completed(self):
        for status in [502, 504]:
            with self.subTest(status=status), patch('main.scrape_brand', side_effect=SearchFailure('IMPI_TIMEOUT', 'Consulta fallida', status)):
                response = self.client.post('/api/buscar', json={'denominacion': 'Demo', 'clase': '41'})
                self.assertEqual(response.status_code, status)
                self.assertNotIn('resultados', response.json())
        with self.sessions() as session:
            self.assertEqual(session.query(models.BusquedaModel).count(), 0)

    def test_invalid_inputs_do_not_start_a_search(self):
        for payload in [{'denominacion': '  ', 'clase': '41'}, {'denominacion': 'Demo', 'clase': '0'}, {'denominacion': 'Demo', 'clase': True}]:
            with self.subTest(payload=payload), patch('main.scrape_brand') as scrape:
                self.assertEqual(self.client.post('/api/buscar', json=payload).status_code, 422)
                scrape.assert_not_called()

    def test_busy_capacity_is_reported_and_released(self):
        with main.search_capacity():
            response = self.client.post('/api/buscar', json={'denominacion': 'Demo', 'clase': '41'})
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.headers['retry-after'], '15')
        with main.search_capacity():
            pass

    def test_download_validation(self):
        for rows in [[], [{'expediente': '../../file', 'denominacion': 'Demo'}], [{'expediente': '12', 'denominacion': 'Demo'}] * 11]:
            self.assertEqual(self.client.post('/api/descargar', json={'expedientes': rows}).status_code, 422)

    def test_export_is_a_real_zip_based_xlsx(self):
        response = self.client.post('/api/exportar-excel', json={'resultados': [{'denominacion': 'Demo', 'similitud': 20}]})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.content.startswith(b'PK\x03\x04'))

    def test_excel_does_not_execute_formulas_or_fetch_private_logos(self):
        with patch('main.requests.get') as fetch:
            response = self.client.post('/api/exportar-excel', json={'resultados': [{'denominacion': '=1+1', 'logo': 'http://169.254.169.254/', 'similitud': 'bad value'}]})
        self.assertEqual(response.status_code, 200)
        fetch.assert_not_called()
        with zipfile.ZipFile(io.BytesIO(response.content)) as workbook:
            self.assertNotIn(b'<f>', workbook.read('xl/worksheets/sheet1.xml'))

    def test_dangerous_logo_schemes_are_not_loaded(self):
        from main import _logo_image_data
        for url in ['file:///etc/passwd', 'data:image/svg+xml;base64,YQ==', 'https://example.com/a.png', 'data:image/png;base64,' + 'a' * 70000]:
            self.assertIsNone(_logo_image_data(url, Deadline()))


if __name__ == '__main__':
    unittest.main()
