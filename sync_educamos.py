import os
import json
import asyncio
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from bs4 import BeautifulSoup
from icalendar import Calendar, Event, Alarm
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

def crear_alarma(mensaje):
    """Crea una alarma sonora estándar para que suene en el móvil."""
    alarma = Alarm()
    alarma.add('action', 'DISPLAY')
    alarma.add('description', mensaje)
    alarma.add('trigger', timedelta(0))  # Suena en el minuto exacto del evento
    return alarma

async def main():
    historico = cargar_historico()
    ahora = datetime.now(TZ_MADRID)

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
        # 1. HORARIO ESCOLAR Y MAPA DE HORAS
        # =====================================================================
        print("Obteniendo horario escolar semanal...")
        cal_horario = Calendar()
        cal_horario.add('prodid', '-//Sync Educamos Horario//ES')
        cal_horario.add('version', '2.0')
        cal_horario.add('x-wr-calname', 'Horario Escolar')

        hoy = datetime.now()
        lunes = hoy - timedelta(days=hoy.weekday())
        fin_de_curso_dt = datetime(hoy.year if hoy.month < 7 else hoy.year + 1, 6, 25, 23, 59, 59, tzinfo=TZ_UTC)

        mapa_horas = {}

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

                mapa_horas[(dia.weekday(), materia.strip().lower())] = (h_inicio, h_fin)

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

        # =====================================================================
        # 2. EXÁMENES Y GENERACIÓN DE ALERTAS DE AVISO (DÍA D Y DÍA D+1)
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

                # Si es un examen nuevo que nunca habíamos visto, registramos su fecha de detección
                if uid not in historico:
                    historico[uid] = {
                        "materia": materia,
                        "titulo": titulo,
                        "fecha": fecha_str,
                        "fecha_descubierto": ahora.strftime("%Y-%m-%d %H:%M:%S")
                    }
                else:
                    # Mantenemos la fecha original en la que se descubrió por primera vez
                    if "fecha_descubierto" not in historico[uid]:
                        historico[uid]["fecha_descubierto"] = ahora.strftime("%Y-%m-%d %H:%M:%S")

        guardar_historico(historico)

        cal_examenes = Calendar()
        cal_examenes.add('prodid', '-//Sync Educamos Examenes//ES')
        cal_examenes.add('version', '2.0')
        cal_examenes.add('x-wr-calname', 'Examenes y Avisos')

        for uid, item in historico.items():
            try:
                dt_dia = datetime.strptime(item["fecha"], "%d/%m/%Y")
                dia_sem = dt_dia.weekday()
                materia_norm = item["materia"].strip().lower()

                # --- A. EVENTO OFICIAL DEL EXAMEN EN SU DÍA ---
                event = Event()
                event.add('uid', f"{uid}@educamos")
                event.add('summary', f"[{item['materia']}] {item['titulo']}")

                horas = mapa_horas.get((dia_sem, materia_norm))
                if horas:
                    h_ini_str, h_fin_str = horas
                    dt_start = datetime.strptime(f"{item['fecha']} {h_ini_str}", "%d/%m/%Y %H:%M:%S").replace(tzinfo=TZ_MADRID)
                    dt_end = datetime.strptime(f"{item['fecha']} {h_fin_str}", "%d/%m/%Y %H:%M:%S").replace(tzinfo=TZ_MADRID)
                    event.add('dtstart', dt_start)
                    event.add('dtend', dt_end)
                else:
                    dt_evento = dt_dia.date()
                    event.add('dtstart', dt_evento)
                    event.add('dtend', dt_evento + timedelta(days=1))

                cal_examenes.add_component(event)

                # --- B. ALERTAS DE AVISO (DÍA DEL ANUNCIO Y DÍA SIGUIENTE) ---
                fecha_disc_str = item.get("fecha_descubierto")
                if fecha_disc_str:
                    dt_disc = datetime.strptime(fecha_disc_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=TZ_MADRID)

                    # 1. Alerta el día del anuncio (solo si se descubrió antes de las 20:30 h para no despertar de noche)
                    if dt_disc.hour < 20 or (dt_disc.hour == 20 and dt_disc.minute <= 30):
                        aviso_1 = Event()
                        aviso_1.add('uid', f"aviso_hoy_{uid}@educamos")
                        aviso_1.add('summary', f"AVISO NUEVO EXAMEN: [{item['materia']}] para el {item['fecha']}")
                        ini_1 = dt_disc.replace(hour=18, minute=30, second=0, microsecond=0)
                        fin_1 = ini_1 + timedelta(minutes=30)
                        aviso_1.add('dtstart', ini_1)
                        aviso_1.add('dtend', fin_1)
                        aviso_1.add_component(crear_alarma(f"Nuevo examen publicado: [{item['materia']}] el {item['fecha']}"))
                        cal_examenes.add_component(aviso_1)

                    # 2. Alerta al día siguiente del anuncio (a las 16:30, al salir del colegio)
                    dt_manana = dt_disc.date() + timedelta(days=1)
                    # Solo ponemos el aviso del día siguiente si el examen aún no ha pasado
                    if dt_manana <= dt_dia.date():
                        aviso_2 = Event()
                        aviso_2.add('uid', f"aviso_manana_{uid}@educamos")
                        aviso_2.add('summary', f"RECORDATORIO PLANIFICAR: Examen [{item['materia']}] ({item['fecha']})")
                        ini_2 = datetime(dt_manana.year, dt_manana.month, dt_manana.day, 16, 30, 0, tzinfo=TZ_MADRID)
                        fin_2 = ini_2 + timedelta(minutes=30)
                        aviso_2.add('dtstart', ini_2)
                        aviso_2.add('dtend', fin_2)
                        aviso_2.add_component(crear_alarma(f"Recuerda planificar estudio para [{item['materia']}] ({item['fecha']})"))
                        cal_examenes.add_component(aviso_2)

            except Exception:
                continue

        await browser.close()

        # =====================================================================
        # 3. EXPORTAR ARCHIVOS
        # =====================================================================
        os.makedirs("./output", exist_ok=True)

        with open("./output/examenes.ics", "wb") as f:
            f.write(cal_examenes.to_ical())
        with open("./output/agenda.ics", "wb") as f:
            f.write(cal_examenes.to_ical())
        with open("./output/horario.ics", "wb") as f:
            f.write(cal_horario.to_ical())

        print("Completado: Exámenes en hora de clase y alertas de día D y D+1 configuradas con alarmas sonoras.")

if __name__ == "__main__":
    asyncio.run(main())
