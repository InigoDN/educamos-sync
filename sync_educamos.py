import os
import asyncio
from datetime import datetime, timedelta
from bs4 import BeautifulSoup
from icalendar import Calendar, Event
from playwright.async_api import async_playwright

COLEGIO_URL = "https://sdeusto-salesianos-bilbao.educamos.com"
USERNAME = os.environ["EDUCAMOS_USER"]
PASSWORD = os.environ["EDUCAMOS_PASS"]
ALUMNO_ID = os.environ.get("ALUMNO_ID", "f4b3b534-d4b9-41f1-ac90-b1a800bdeaa7")

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context()
        page = await context.new_page()

        print("Conectando con Educamos...")
        await page.goto(COLEGIO_URL)

        # Login
        await page.fill('input[type="text"], input[name*="user" i]', USERNAME)
        await page.fill('input[type="password"]', PASSWORD)
        await page.click('button[type="submit"], input[type="submit"]')
        await page.wait_for_load_state("networkidle")

        # Rango: desde hoy hasta final de curso escolar
        hoy = datetime.now().strftime("%d/%m/%Y")
        fin_curso = (datetime.now() + timedelta(days=280)).strftime("%d/%m/%Y")

        payload = {
            "alertaValidacion": "ValidationSummary",
            "contexto": "divListadoTareas",
            "AlumnoId": ALUMNO_ID,
            "FechaInicio": hoy,
            "FechaFin": fin_curso,
            "Pagina": "0",
            "OrdenarPor": "Fecha",
            "OrdenarModo": "ASC",
            "OperacionGrid": "",
            "NumTotalElemsGrid": "0",
            "FilasPorPagina": "200",
            "X-Requested-With": "XMLHttpRequest"
        }

        headers = {
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "X-Requested-With": "XMLHttpRequest"
        }

        response = await page.request.post(
            f"{COLEGIO_URL}/Home/ListadoTareas",
            form=payload,
            headers=headers
        )

        html_response = await response.text()
        await browser.close()

        # Parsear tabla HTML
        cal = Calendar()
        cal.add('prodid', '-//Sync Educamos//ES')
        cal.add('version', '2.0')

        soup = BeautifulSoup(html_response, 'html.parser')
        filas = soup.find_all('tr')

        contador = 0
        for fila in filas:
            columnas = fila.find_all('td')
            if len(columnas) < 3:
                continue

            materia = columnas[0].get_text(strip=True)
            titulo = columnas[1].get_text(strip=True)
            fecha_str = columnas[2].get_text(strip=True)

            try:
                dt_evento = datetime.strptime(fecha_str, "%d/%m/%Y").date()
                event = Event()
                event.add('uid', f"{materia}_{titulo}_{fecha_str}@educamos".replace(" ", "_"))
                event.add('summary', f"[{materia}] {titulo}")
                event.add('dtstart', dt_evento)
                event.add('dtend', dt_evento)
                cal.add_component(event)
                contador += 1
            except Exception:
                continue

        # Crear carpeta de salida
        os.makedirs("./output", exist_ok=True)
        # Nombre con identificador único para que nadie pueda adivinar la URL
        with open("./output/agenda.ics", "wb") as f:
            f.write(cal.to_ical())
        
        print(f"Sincronización completada: {contador} tareas/exámenes exportados.")

if __name__ == "__main__":
    asyncio.run(main())
