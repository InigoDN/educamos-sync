import os
import json
import asyncio
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from bs4 import BeautifulSoup
from icalendar import Calendar, Event
from playwright.async_api import async_playwright

COLEGIO_URL = "https://sdeusto-salesianos-bilbao.educamos.com"
USERNAME = os.environ["EDUCAMOS_USER"]
PASSWORD = os.environ["EDUCAMOS_PASS"]
ALUMNO_ID = os.environ.get("ALUMNO_ID", "f4b3b534-d4b9-41f1-ac90-b1a800bdeaa7")
DB_FILE = "historico_tareas.json"

TZ_MADRID = ZoneInfo("Europe/Madrid")
TZ_UTC = ZoneInfo("UTC")

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
    historico = cargar_historico()

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context()
        page = await context.new_page()

        print("Iniciando sesión...")
        await page.goto(COLEGIO_URL)
        await page.fill('input[type="text"], input[name*="user" i]', USERNAME)
        await page.fill('input[type="password"]', PASSWORD)
        await page.locator('input[type="password"]').press("Enter")
        await page.wait_for_load_state("networkidle")
        print("Login completado.")

        # =====================================================================
        # 1. GENERAR CALENDARIO DE EXÁMENES Y TAREAS (HISTÓRICO ACUMULATIVO)
        # =====================================================================
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
            "FilasPorPagina": "200",
            "X-Requested-With": "XMLHttpRequest"
        }

        resp_tareas = await page.request.post(
            f"{COLEGIO_URL}/Home/ListadoTareas",
            form=payload,
            headers={
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                "X-Requested-With": "XMLHttpRequest"
            }
        )
        html_tareas = await resp_tareas.text()
        soup_tareas = BeautifulSoup(html_tareas, 'html.parser')

        for fila in soup_tareas.find_all('tr'):
            cols = fila.find_all('td')
            if len(cols) >= 3:
                materia = cols[0].get_text(strip=True)
                titulo = cols[1].get_text(strip=True)
                fecha_str = cols[2].get_text(strip=True)
                uid = f"{materia}_{titulo}_{fecha_str}".replace(" ", "_")
                historico[uid] = {"materia": materia, "titulo": titulo, "fecha": fecha_str}

        guardar_historico(historico)

        cal_examenes = Calendar()
        cal_examenes.add('prodid', '-//Sync Educamos Examenes//ES')
        cal_examenes.add('version', '2.0')
        cal_examenes.add('x-wr-calname', 'Examenes y Deberes')

        for uid, item in historico.items():
            try:
                dt_evento = datetime.strptime(item["fecha"], "%d/%m/%Y").date()
                event = Event()
                event.add('uid', f"{uid}@educamos")
                event.add('summary', f"[{item['materia']}] {item['titulo']}")
                event.add('dtstart', dt_evento)
                event.add('dtend', dt_evento + timedelta(days=1))
                cal_examenes.add_component(event)
            except Exception:
                continue

        # =====================================================================
        # 2. GENERAR HORARIO ESCOLAR SEMANAL (HORA LOCAL ESPAÑA)
        # =====================================================================
        print("Obteniendo horario escolar semanal...")
        cal_horario = Calendar()
        cal_horario.add('prodid', '-//Sync Educamos Horario//ES')
        cal_horario.add('version', '2.0')
        cal_horario.add('x-wr-calname', 'Horario Escolar')

        hoy = datetime.now()
        lunes = hoy - timedelta(days=hoy.weekday())
        fin_de_curso_dt = datetime(hoy.year if hoy.month < 7 else hoy.year + 1, 6, 25, 23, 59, 59, tzinfo=TZ_UTC)

        for i in range(5):
            dia = lunes + timedelta(days=i)
            dia_str = dia.strftime("%Y-%m-%d")

            url_horario = f"{COLEGIO_URL}/Home/ColumnaDerechaHorarioSemanal?fecha={dia_str}&mostrarEventosPersonales=true"
            resp_h = await page.request.get(url_horario)
            soup_h = BeautifulSoup(await resp_h.text(), 'html.parser')

            sesiones = soup_h.find_all('div', class_='linea_agenda')
            for s in sesiones:
                h_inicio = s.get('data-horainicio')
                h_fin = s.get('data-horafin')
                if not h_inicio or not h_fin:
                    continue

                materia_tag = s.find('p', class_='materia')
                materia = materia_tag.get_text(strip=True) if materia_tag else "Clase"
                profesor_tag = s.find('p', class_='nivel')
                profesor = profesor_tag.get_text(strip=True) if profesor_tag else ""

                # Asignación de zona horaria oficial Europe/Madrid
                dt_ini = datetime.strptime(f"{dia.strftime('%d/%m/%Y')} {h_inicio}", "%d/%m/%Y %H:%M:%S").replace(tzinfo=TZ_MADRID)
                dt_fin = datetime.strptime(f"{dia.strftime('%d/%m/%Y')} {h_fin}", "%d/%m/%Y %H:%M:%S").replace(tzinfo=TZ_MADRID)

                ev = Event()
                ev.add('uid', f"horario_{dia.weekday()}_{h_inicio}_{materia}@educamos".replace(" ", "_"))
                ev.add('summary', materia)
                if profesor:
                    ev.add('description', f"Profesor/a: {profesor}")
                ev.add('dtstart', dt_ini)
                ev.add('dtend', dt_fin)
                ev.add('rrule', {'freq': 'weekly', 'until': fin_de_curso_dt})
                cal_horario.add_component(ev)

        await browser.close()

        # =====================================================================
        # 3. EXPORTAR ARCHIVOS A ./output
        # =====================================================================
        os.makedirs("./output", exist_ok=True)

        with open("./output/examenes.ics", "wb") as f:
            f.write(cal_examenes.to_ical())
        with open("./output/agenda.ics", "wb") as f:
            f.write(cal_examenes.to_ical())
        with open("./output/horario.ics", "wb") as f:
            f.write(cal_horario.to_ical())

        print("Completado. Archivos generados correctamente sin emojis y con zona horaria corregida.")

if __name__ == "__main__":
    asyncio.run(main())
