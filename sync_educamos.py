import os
import json
import asyncio
from datetime import datetime, timedelta
from bs4 import BeautifulSoup
from icalendar import Calendar, Event
from playwright.async_api import async_playwright

COLEGIO_URL = "https://sdeusto-salesianos-bilbao.educamos.com"
USERNAME = os.environ["EDUCAMOS_USER"]
PASSWORD = os.environ["EDUCAMOS_PASS"]
ALUMNO_ID = os.environ.get("ALUMNO_ID", "f4b3b534-d4b9-41f1-ac90-b1a800bdeaa7")
DB_FILE = "historico_tareas.json"

def cargar_historico():
    if os.path.exists(DB_FILE):
        try:
            with open(DB_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}

def guardar_historico(data):
    with open(DB_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

async def main():
    # 1. Cargar lo que ya teníamos guardado de días anteriores
    historico = cargar_historico()
    print(f"Cargados {len(historico)} eventos previos del histórico.")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context()
        page = await context.new_page()

        print("Iniciando sesión en Educamos...")
        await page.goto(COLEGIO_URL)

        await page.fill('input[type="text"], input[name*="user" i]', USERNAME)
        await page.fill('input[type="password"]', PASSWORD)
        await page.click('button[type="submit"], input[type="submit"]')
        await page.wait_for_load_state("networkidle")

        # Consultar desde hace 30 días hasta final de curso (para captar cualquier rezagado)
        hace_un_mes = (datetime.now() - timedelta(days=30)).strftime("%d/%m/%Y")
        fin_curso = (datetime.now() + timedelta(days=280)).strftime("%d/%m/%Y")

        payload = {
            "alertaValidacion": "ValidationSummary",
            "contexto": "divListadoTareas",
            "AlumnoId": ALUMNO_ID,
            "FechaInicio": hace_un_mes,
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

        soup = BeautifulSoup(html_response, 'html.parser')
        filas = soup.find_all('tr')

        nuevos = 0
        for fila in filas:
            columnas = fila.find_all('td')
            if len(columnas) < 3:
                continue

            materia = columnas[0].get_text(strip=True)
            titulo = columnas[1].get_text(strip=True)
            fecha_str = columnas[2].get_text(strip=True)

            uid = f"{materia}_{titulo}_{fecha_str}".replace(" ", "_")

            # Si no existía o ha cambiado, lo añadimos al histórico
            if uid not in historico:
                nuevos += 1

            historico[uid] = {
                "materia": materia,
                "titulo": titulo,
                "fecha": fecha_str
            }

        print(f"Detectados {nuevos} eventos nuevos. Total acumulado en histórico: {len(historico)}")
        guardar_historico(historico)

        # 2. Generar el .ics con TODO el histórico (pasado + presente + futuro)
        cal = Calendar()
        cal.add('prodid', '-//Sync Educamos Persistente//ES')
        cal.add('version', '2.0')

        for uid, item in historico.items():
            try:
                dt_evento = datetime.strptime(item["fecha"], "%d/%m/%Y").date()
                event = Event()
                event.add('uid', f"{uid}@educamos")
                event.add('summary', f"[{item['materia']}] {item['titulo']}")
                event.add('dtstart', dt_evento)
                event.add('dtend', dt_evento)
                cal.add_component(event)
            except Exception:
                continue

        os.makedirs("./output", exist_ok=True)
        with open("./output/agenda.ics", "wb") as f:
            f.write(cal.to_ical())
        
        print("Archivo agenda.ics generado con todo el histórico.")

if __name__ == "__main__":
    asyncio.run(main())
