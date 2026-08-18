from nicegui import ui, events
import uuid
import datetime
import json
import os
import subprocess
import shutil
import asyncio
import pandas as pd
import openpyxl
from docxtpl import DocxTemplate
import intranet_data
import intranet_jobs


def build_zakaznici_veletrh_xlsx(zakaznici: list) -> str:
    """[BĚŽÍ V PROCESU] Sestaví Excel se zákazníky veletrhu z hotových dat
    (picklovatelný list dictů) → vrací cestu k .xlsx. Bez DB a NiceGUI."""
    df = pd.DataFrame([{
        'Jméno': z.get('jmeno', ''),
        'Firma': z.get('firma', ''),
        'IČ': z.get('ic', ''),
        'Telefon': z.get('tel', ''),
        'OZ': z.get('oz', ''),
        'ASM': z.get('asm', ''),
        'Datum zadání': z.get('datum', ''),
    } for z in zakaznici])
    slozka = 'Exporty_Veletrh'
    os.makedirs(slozka, exist_ok=True)
    cesta = os.path.join(slozka, f"Zakaznici_Veletrh_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx")
    with pd.ExcelWriter(cesta, engine='openpyxl') as writer:
        df.to_excel(writer, sheet_name='Zákazníci', index=False)
        ws = writer.sheets['Zákazníci']
        for c_idx, col in enumerate(df.columns, start=1):
            ws.cell(row=1, column=c_idx).font = openpyxl.styles.Font(bold=True)
            max_len = max([len(str(col))] + [len(str(v)) for v in df.iloc[:, c_idx - 1]])
            ws.column_dimensions[openpyxl.utils.get_column_letter(c_idx)].width = min(max_len + 4, 50)
        ws.freeze_panes = 'A2'
    return cesta


# ==========================================
# 💾 DATABÁZOVÝ SOUBOR
# ==========================================
DATA_FILE = 'veletrh_data.json'

# ==========================================
# 📊 CENÍKY A DATA
# ==========================================
CENA_M2_DEFAULT = 11111.11111111111

ELEKTRO_CENIK = {
    "0": {"nazev": "Bez připojení", "cena": 0},
    "2.2": {"nazev": "do 2,2 kW", "cena": 0},
    "4.4": {"nazev": "do 4,4 kW", "cena": 2500},
    "6.6": {"nazev": "do 6,6 kW", "cena": 3200},
    "10.5": {"nazev": "do 10,5 kW", "cena": 4000},
    "16.5": {"nazev": "do 16,5 kW", "cena": 5000},
    "21.0": {"nazev": "do 21,0 kW", "cena": 6000},
    "26.0": {"nazev": "do 26,0 kW", "cena": 6875},
    "32.0": {"nazev": "do 32,0 kW", "cena": 7750},
    "40.0": {"nazev": "do 40,0 kW", "cena": 8750},
    "50.0": {"nazev": "do 50,0 kW", "cena": 10000},
    "60.0": {"nazev": "do 60,0 kW", "cena": 11500},
    "70.0": {"nazev": "do 70,0 kW", "cena": 13000},
}

VODA_CENIK = {
    "0": {"nazev": "Bez připojení", "cena": 0},
    "1": {"nazev": "Přípojné místo bez dřezu", "cena": 4000}
}

def get_img_version(filename):
    try:
        return f"/static/{filename}?v={int(os.path.getmtime(filename))}"
    except Exception:
        return f"/static/{filename}"

PATRA = {
    'prizemi': {
        'nazev': 'Přízemí', 'img': get_img_version('prizemi.svg'),
        'legenda_pos': {'x': 9022, 'y': 1703, 'zoom': 2.2}
    }
}

POVOLENE_ZONY = {
    'prizemi': []
}

def je_v_zone(x, y, patro):
    zony = POVOLENE_ZONY.get(patro, [])
    if not zony: return True
    for z in zony:
        if z['xmin'] <= x <= z['xmax'] and z['ymin'] <= y <= z['ymax']:
            return True
    return False

# ==========================================
# 🧠 GLOBÁLNÍ PAMĚŤ APLIKACE
# ==========================================
# Sdílená DATA pro všechny uživatele (stánky, rezervace, konfigurace)
state = {
    'cell_size': 29.5,
    'grid_offset_x': 0.0,   # posun mřížky v SVG souřadnicích (osa X)
    'grid_offset_y': 0.0,   # posun mřížky v SVG souřadnicích (osa Y)
    'cena_m2': CENA_M2_DEFAULT,
    'stanky': [],
    'rezervace': {},
    'poznamky': [],
    'kruhy': [],
    'zakaznici': [],

    '_grid':          {},  # (patro, gx, gy) → stánek  — O(1) hit-detection
    '_res_idx':       {},  # res_id → [stánek, ...]    — O(1) rezervace lookup
    '_stanky_by_id':  {},  # id → stánek               — O(1) id lookup
    # SVG fragment cache — přestavuje se jen to, co se skutečně změnilo
    '_svg_cache':     {},  # res_id → [svg_fragment_str, ...]
    '_svg_cache_cs':  None,    # cell_size při posledním sestavení cache
    '_svg_cache_patro': None,  # patro při posledním sestavení cache
}

# ==========================================
# ⚡ PROSTOROVÉ INDEXY — O(1) hit-detection
# ==========================================

def _grid_klic(x, y, patro):
    cs = state['cell_size']
    ox, oy = state['grid_offset_x'], state['grid_offset_y']
    return (patro, round((x - ox) / cs), round((y - oy) / cs))

def _rebuild_grid():
    cs = state['cell_size']
    ox, oy = state['grid_offset_x'], state['grid_offset_y']
    g = {}
    for s in state['stanky']:
        g[(s['patro'], round((s['x'] - ox) / cs), round((s['y'] - oy) / cs))] = s
    state['_grid'] = g

def _rebuild_res_idx():
    idx = {}
    for s in state['stanky']:
        rid = s.get('res_id')
        if rid:
            idx.setdefault(rid, []).append(s)
    state['_res_idx'] = idx

def _rebuild_stanky_by_id():
    state['_stanky_by_id'] = {s['id']: s for s in state['stanky']}

def _rebuild_all():
    _rebuild_grid(); _rebuild_res_idx(); _rebuild_stanky_by_id()

def _renormalize_stanky():
    """Přesune všechny stánky na nejbližší střed buňky aktuální mřížky (po změně offsetu)."""
    cs = state['cell_size']
    ox, oy = state['grid_offset_x'], state['grid_offset_y']
    for s in state['stanky']:
        s['x'] = round((s['x'] - ox) / cs) * cs + ox
        s['y'] = round((s['y'] - oy) / cs) * cs + oy
    _rebuild_grid()

def _snap_xy(ix, iy):
    """Zaokrouhlí souřadnice kliknutí na střed nejbližší mřížkové buňky (s offsetem)."""
    cs = state['cell_size']
    ox, oy = state['grid_offset_x'], state['grid_offset_y']
    return round((ix - ox) / cs) * cs + ox, round((iy - oy) / cs) * cs + oy

def _grid_idx(x, y):
    """Vrátí (gx, gy) — celočíselný index mřížkové buňky (s offsetem)."""
    cs = state['cell_size']
    ox, oy = state['grid_offset_x'], state['grid_offset_y']
    return round((x - ox) / cs), round((y - oy) / cs)

def _grid_hit(ix, iy, patro):
    """Vrátí stánek přesně na mřížkové buňce pod kurzorem — O(1), žádný radius."""
    cs = state['cell_size']
    ox, oy = state['grid_offset_x'], state['grid_offset_y']
    return state['_grid'].get((patro, round((ix - ox) / cs), round((iy - oy) / cs)))

def _grid_hit_vyber(ix, iy, patro):
    """Jako _grid_hit — s přesným gridem je na buňce max. 1 stánek."""
    return _grid_hit(ix, iy, patro)

def _grid_has_nearby(ix, iy, patro):
    """Vrátí True pokud je daná mřížková buňka již obsazena — O(1)."""
    cs = state['cell_size']
    ox, oy = state['grid_offset_x'], state['grid_offset_y']
    return (patro, round((ix - ox) / cs), round((iy - oy) / cs)) in state['_grid']

def _invalidate_cache(res_id):
    """Invaliduje SVG cache pro konkrétní rezervaci. Volat po změně jejích stánků/údajů."""
    if res_id:
        state['_svg_cache'].pop(res_id, None)

def _stanky_append(s):
    """Přidá stánek a aktualizuje všechny indexy."""
    state['stanky'].append(s)
    state['_stanky_by_id'][s['id']] = s
    state['_grid'][_grid_klic(s['x'], s['y'], s['patro'])] = s
    rid = s.get('res_id')
    if rid:
        state['_res_idx'].setdefault(rid, []).append(s)
        _invalidate_cache(rid)

def _stanky_remove(s):
    """Odstraní stánek a aktualizuje všechny indexy."""
    try: state['stanky'].remove(s)
    except ValueError: pass
    state['_stanky_by_id'].pop(s['id'], None)
    state['_grid'].pop(_grid_klic(s['x'], s['y'], s['patro']), None)
    rid = s.get('res_id')
    if rid and rid in state['_res_idx']:
        try: state['_res_idx'][rid].remove(s)
        except ValueError: pass
        _invalidate_cache(rid)

def _res_idx_set(s, new_rid):
    """Přiřadí res_id stánku a aktualizuje reverse index."""
    old = s.get('res_id')
    if old == new_rid: return
    if old and old in state['_res_idx']:
        try: state['_res_idx'][old].remove(s)
        except ValueError: pass
        _invalidate_cache(old)
    s['res_id'] = new_rid
    if new_rid:
        state['_res_idx'].setdefault(new_rid, []).append(s)
        _invalidate_cache(new_rid)

# Per-user UI stav — každý uživatel má vlastní klávesnici/výběr/zoom
_UI_STATE = {}

# Registr keyboard handlerů — naplňuje vykresli_veletrh, volá ho ui.keyboard z intranet.py
_VELETRH_KBD_HANDLERS = {}

# Registr per-user update_svg callbacků — pro live broadcast změn všem připojeným uživatelům
_VELETRH_UPDATE_CALLBACKS = {}

def dispatch_kbd(user_id, key):
    """Externí volání z intranet.py: zpracovat stisk klávesy pro daného uživatele.
    Vrací True pokud se zkratka aplikovala (jinak False — klávesa nás nezajímá)."""
    h = _VELETRH_KBD_HANDLERS.get(user_id)
    if h:
        return h(key)
    return False

def _broadcast_svg_update():
    """Po mutaci sdíleného state: zavolat update_svg() pro všechny připojené uživatele.
    Každý uživatel si filtruje vlastní patro/highlights — re-render je levný díky cache."""
    dead = []
    for uid, cb in list(_VELETRH_UPDATE_CALLBACKS.items()):
        try:
            cb()
        except Exception as e:
            # Klient se odpojil nebo má rozbitý kontext — odregistrovat
            dead.append(uid)
    for uid in dead:
        _VELETRH_UPDATE_CALLBACKS.pop(uid, None)
        _VELETRH_KBD_HANDLERS.pop(uid, None)

def fmt(cislo):
    return f"{int(round(cislo)):,}".replace(',', ' ')

def load_data():
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
                state['stanky'] = data.get('stanky', [])
                state['rezervace'] = data.get('rezervace', {})
                state['cell_size'] = data.get('cell_size', 29.5)
                state['grid_offset_x'] = float(data.get('grid_offset_x', 0.0))
                state['grid_offset_y'] = float(data.get('grid_offset_y', 0.0))
                state['cena_m2'] = data.get('cena_m2', CENA_M2_DEFAULT)
                state['kruhy'] = data.get('kruhy', [])
                state['zakaznici'] = data.get('zakaznici', [])

                stare_poznamky = data.get('poznamky', [])
                if isinstance(stare_poznamky, str):
                    if stare_poznamky.strip():
                        state['poznamky'] = [{'id': str(uuid.uuid4()), 'text': stare_poznamky, 'datum': 'Starší poznámka'}]
                    else:
                        state['poznamky'] = []
                else:
                    state['poznamky'] = stare_poznamky

                cs = state['cell_size']
                ox, oy = state['grid_offset_x'], state['grid_offset_y']
                for s in state['stanky']:
                    if 'batch_id' not in s: s['batch_id'] = s['id']
                    # Normalizace starých dat na přesné mřížkové souřadnice (s offsetem)
                    s['x'] = round((s['x'] - ox) / cs) * cs + ox
                    s['y'] = round((s['y'] - oy) / cs) * cs + oy
        except Exception as e: print(f"❌ Chyba při načítání: {e}")

# --- OPTIMALIZACE: ASYNCHRONNÍ + ATOMIC + DEBOUNCED UKLÁDÁNÍ ---
def _fyzicky_zapis_veletrh(data_str):
    """Atomický zápis: nejdřív do .tmp, pak rename. Chrání proti corrupted JSON při pádu serveru."""
    try:
        tmp_path = DATA_FILE + '.tmp'
        with open(tmp_path, 'w', encoding='utf-8') as f:
            f.write(data_str)
        os.replace(tmp_path, DATA_FILE)  # atomický rename
    except Exception as e: print(f"❌ Chyba při fyzickém ukládání na disk: {e}")

# Debounce: agreguje rychle po sobě jdoucí save_data() volání do jednoho zápisu
_SAVE_DEBOUNCE_S = 0.5
_save_pending_task = None

async def _debounced_save_worker():
    """Počká _SAVE_DEBOUNCE_S, pak zapíše aktuální state. Pokud přijde další save_data() během čekání,
    timer se restartuje a nakonec se zapíše jen jednou — ušetří desítky disk writes/min při vícero uživatelích."""
    global _save_pending_task
    try:
        await asyncio.sleep(_SAVE_DEBOUNCE_S)
        data_to_save = {
            'stanky': state['stanky'],
            'rezervace': state['rezervace'],
            'cell_size': state['cell_size'],
            'grid_offset_x': state['grid_offset_x'],
            'grid_offset_y': state['grid_offset_y'],
            'cena_m2': state['cena_m2'],
            'poznamky': state['poznamky'],
            'kruhy': state['kruhy'],
            'zakaznici': state['zakaznici'],
        }
        # Kompaktní JSON (separators) je ~30% menší než indent=4
        data_str = json.dumps(data_to_save, ensure_ascii=False, separators=(',', ':'))
        await asyncio.to_thread(_fyzicky_zapis_veletrh, data_str)
    except asyncio.CancelledError:
        pass
    except Exception as e:
        print(f"❌ Chyba při serializaci/zápisu JSON: {e}")
    finally:
        _save_pending_task = None

def save_data():
    """Naplánuje uložení s debounce. Více volání během krátkého okna se spojí do jednoho zápisu.
    Také okamžitě broadcastne SVG update všem připojeným uživatelům (live sync)."""
    global _save_pending_task
    # Debounce: zruš předchozí (pokud čeká), naplánuj nový
    if _save_pending_task and not _save_pending_task.done():
        _save_pending_task.cancel()
    try:
        _save_pending_task = asyncio.create_task(_debounced_save_worker())
    except RuntimeError:
        # Není-li event loop (např. při importu), zapsat synchronně přes thread
        pass
    # Broadcast — okamžitě, ne debounced (uživatelé musí vidět změny live)
    _broadcast_svg_update()

load_data()
_rebuild_all()

ui.add_head_html('''
    <script src="https://cdnjs.cloudflare.com/ajax/libs/html2pdf.js/0.10.1/html2pdf.bundle.min.js"></script>
    <style>
        .no-scrollbar::-webkit-scrollbar { display: none; }
        .no-scrollbar { -ms-overflow-style: none; scrollbar-width: none; }
        .cursor-packa { cursor: grab !important; }
        .cursor-packa:active { cursor: grabbing !important; }
        .cursor-kresleni { cursor: crosshair !important; }
        .cursor-oblast { cursor: cell !important; }
        .cursor-ladeni { cursor: move !important; }
        .cursor-kruh { cursor: crosshair !important; }
        .cursor-guma { cursor: not-allowed !important; }
        .cursor-vyber { cursor: pointer !important; }
    </style>
    <script>
        window.current_tool = 'vyber';

        // =====================================================================
        // Plynulý zoom — čistý CSS transform, žádné scrollLeft/scrollTop/minWidth
        // → nulové layout reflow, vše na GPU kompozitoru
        // =====================================================================
        window._mapZoom = (function () {
            // Aktuální stav (aplikovaný na DOM)
            var zoom = 1.0, panX = 0, panY = 0;
            // Cílový stav (animujeme k němu)
            var targetZoom = 1.0, targetPanX = 0, targetPanY = 0;
            // Uložený stav pro tlačítko "Zpět z legendy"
            var savedZoom = 1.0, savedPanX = 0, savedPanY = 0;
            var rafId = null;

            function getEl()  { return document.getElementById('map-export-area'); }
            function getCtr() { return document.getElementById('map-container');   }

            // Aplikuje transform na DOM — POUZE transform, žádný layout reflow
            function applyTransform(z, px, py) {
                var el = getEl();
                if (!el) return;
                zoom = z; panX = px; panY = py;
                el.style.transform = 'translate(' + px + 'px,' + py + 'px) scale(' + z + ')';
            }

            // rAF animační smyčka — exponenciální lerp
            function animStep() {
                var dz  = targetZoom - zoom;
                var dpx = targetPanX - panX;
                var dpy = targetPanY - panY;
                if (Math.abs(dz) < 0.0004 && Math.abs(dpx) < 0.25 && Math.abs(dpy) < 0.25) {
                    applyTransform(targetZoom, targetPanX, targetPanY);
                    rafId = null; return;
                }
                var lp = 0.22;
                applyTransform(zoom + dz * lp, panX + dpx * lp, panY + dpy * lp);
                rafId = requestAnimationFrame(animStep);
            }

            // Spustit nebo aktualizovat animaci k novému cíli
            function setTarget(nz, npx, npy) {
                targetZoom = nz; targetPanX = npx; targetPanY = npy;
                if (!rafId) rafId = requestAnimationFrame(animStep);
            }

            // ── Kolečko myši / touchpad ───────────────────────────────────────
            // ctrlKey=true  → pinch gesture nebo Ctrl+scroll → ZOOM s kotvou
            // ctrlKey=false → 2-prstý scroll touchpadu       → PAN (jako Google Maps)
            // Nástroj Kruh: wheel vždy mění poloměr, Python přečte _kruhWheelDelta
            function onWheel(e) {
                e.preventDefault();
                var ctr = getCtr(); if (!ctr) return;

                // Kruh nástroj: akumulovat delta, Python přečte přes timer
                if (window.current_tool === 'kruh') {
                    window._kruhWheelDelta = (window._kruhWheelDelta || 0) + (e.deltaY > 0 ? 1 : -1);
                    return;
                }

                if (e.ctrlKey) {
                    // Pinch / Ctrl+scroll → ZOOM s kotvou pod kurzorem
                    var rect = ctr.getBoundingClientRect();
                    var vx = e.clientX - rect.left;
                    var vy = e.clientY - rect.top;
                    var cx = (vx - targetPanX) / targetZoom;
                    var cy = (vy - targetPanY) / targetZoom;
                    // Touchpad pinch má malý deltaY (<20) → jemnější citlivost
                    var sensitivity = Math.abs(e.deltaY) < 20 ? 0.025 : 0.009;
                    var factor = e.deltaY > 0 ? (1 - sensitivity) : 1 / (1 - sensitivity);
                    var nz = Math.max(0.1, Math.min(4.0, targetZoom * factor));
                    setTarget(nz, vx - cx * nz, vy - cy * nz);
                } else {
                    // 2-prstý scroll → PAN (přirozené chování touchpadu i myši s Shift)
                    setTarget(targetZoom, targetPanX - e.deltaX, targetPanY - e.deltaY);
                }
            }

            // ── Python tlačítka +/– zoom (volá se přes ui.run_javascript) ────
            function syncFromPython(newZoom, cxFrac, cyFrac) {
                if (rafId) { cancelAnimationFrame(rafId); rafId = null; }
                var ctr = getCtr();
                if (ctr) {
                    var vx = ctr.clientWidth  * cxFrac;
                    var vy = ctr.clientHeight * cyFrac;
                    var cx = (vx - panX) / (zoom || 1);
                    var cy = (vy - panY) / (zoom || 1);
                    panX = vx - cx * newZoom;
                    panY = vy - cy * newZoom;
                }
                zoom = newZoom; targetZoom = newZoom;
                targetPanX = panX; targetPanY = panY;
                applyTransform(zoom, panX, panY);
            }

            // ── Přímý posun (pro nástroj Packa) ─────────────────────────────
            function setPan(px, py) {
                panX = px; panY = py;
                targetPanX = px; targetPanY = py;
                applyTransform(zoom, px, py);
            }

            // ── Animovaný zoom + posun na bod obsahu (cx, cy) do středu viewportu ──
            function zoomToContent(cx, cy, z) {
                var ctr = getCtr();
                var vx  = ctr ? ctr.clientWidth  / 2 : 400;
                var vy  = ctr ? ctr.clientHeight / 2 : 300;
                setTarget(z, vx - cx * z, vy - cy * z);
            }

            // ── Animovaný posun na bod obsahu (cx, cy) do středu viewportu ──
            function panToContent(cx, cy) {
                var ctr = getCtr();
                var vx  = ctr ? ctr.clientWidth  / 2 : 400;
                var vy  = ctr ? ctr.clientHeight / 2 : 300;
                setTarget(targetZoom, vx - cx * targetZoom, vy - cy * targetZoom);
            }

            // ── Uložit / obnovit stav pro legenda ────────────────────────────
            function savePan()    { savedZoom = zoom; savedPanX = panX; savedPanY = panY; }
            function restorePan() {
                if (rafId) { cancelAnimationFrame(rafId); rafId = null; }
                zoom = savedZoom; targetZoom = savedZoom;
                panX = savedPanX; panY = savedPanY;
                targetPanX = panX; targetPanY = panY;
                applyTransform(zoom, panX, panY);
            }

            function getZoom() { return zoom; }
            function getPanX() { return panX; }
            function getPanY() { return panY; }

            // ── Animovaný posun o relativní offset (pro tlačítka posunu) ─────
            function panBy(dx, dy) {
                setTarget(targetZoom, targetPanX + dx, targetPanY + dy);
            }

            return {
                onWheel: onWheel, syncFromPython: syncFromPython,
                setPan: setPan, panBy: panBy, zoomToContent: zoomToContent, panToContent: panToContent,
                savePan: savePan, restorePan: restorePan,
                getZoom: getZoom, getPanX: getPanX, getPanY: getPanY
            };
        })();

        document.addEventListener('DOMContentLoaded', () => {
            const observer = new MutationObserver(() => {
                const ele = document.getElementById('map-container');
                if(!ele) return;

                // --- Packa (drag-to-pan) — pointer events (myš, touchpad tap+drag, stylus) ---
                // setPointerCapture zajistí příjem events i mimo element bez document listeneru
                let pos = { origPanX: 0, origPanY: 0, x: 0, y: 0 };
                const pointerDownHandler = function(e) {
                    if (window.current_tool !== 'packa') return;
                    e.preventDefault();
                    pos = {
                        origPanX: window._mapZoom.getPanX(),
                        origPanY: window._mapZoom.getPanY(),
                        x: e.clientX, y: e.clientY
                    };
                    ele.setPointerCapture(e.pointerId);
                };
                const pointerMoveHandler = function(e) {
                    if (!ele.hasPointerCapture(e.pointerId)) return;
                    window._mapZoom.setPan(pos.origPanX + (e.clientX - pos.x), pos.origPanY + (e.clientY - pos.y));
                };
                const pointerUpHandler = function(e) {
                    if (ele.hasPointerCapture(e.pointerId)) ele.releasePointerCapture(e.pointerId);
                };
                ele.addEventListener('pointerdown', pointerDownHandler);
                ele.addEventListener('pointermove', pointerMoveHandler);
                ele.addEventListener('pointerup',   pointerUpHandler);
                ele.addEventListener('pointercancel', pointerUpHandler);

                // --- Zoom kolečkem — deleguje na singleton výše ---
                ele.addEventListener('wheel', window._mapZoom.onWheel, { passive: false });

                observer.disconnect();
            });
            observer.observe(document.body, { childList: true, subtree: true });

            // =====================================================================
            // Canvas overlay pro okamžitý vizuální náhled malování (bez round-tripu)
            // =====================================================================
            window._veletrh_cs   = 24;   // aktualizováno z Pythonu při změně cell_size
            window._vGridOffsetX = 0.0;  // posun mřížky X — sync. z Pythonu
            window._vGridOffsetY = 0.0;  // posun mřížky Y — sync. z Pythonu
            window._vPainting    = false;

            // Vrátí souřadnicový prostor overlay SVG (= prostor kliknutí NiceGUI)
            function getSvgViewBox() {
                // NiceGUI renderuje SVG overlay vedle <img>; jeho viewBox = koordinátní systém
                var svgEl = document.querySelector('#map-export-area svg');
                if (svgEl) {
                    var vb = svgEl.viewBox && svgEl.viewBox.baseVal;
                    if (vb && vb.width > 0) return { w: vb.width, h: vb.height };
                }
                // Záloha: naturalWidth (funguje pro rastrové, pro SVG = 0)
                var img = document.querySelector('#map-export-area img');
                if (img && img.naturalWidth > 0) return { w: img.naturalWidth, h: img.naturalHeight };
                return null;   // ještě není připraveno
            }

            function initPaintCanvas() {
                if (document.getElementById('veletrh-paint-canvas')) return;
                var imgEl = document.querySelector('#map-export-area img');
                if (!imgEl) { setTimeout(initPaintCanvas, 300); return; }
                var vb = getSvgViewBox();
                if (!vb)   { setTimeout(initPaintCanvas, 300); return; }
                // Canvas má rozlišení = SVG souřadnicový prostor → 1 canvas px = 1 SVG unit
                var parent = imgEl.parentNode;
                parent.style.position = 'relative';
                var c = document.createElement('canvas');
                c.id     = 'veletrh-paint-canvas';
                c.width  = vb.w;
                c.height = vb.h;
                // CSS 100% × 100% → škáluje s img (CSS transform zoom)
                c.style.cssText = 'position:absolute;top:0;left:0;width:100%;height:100%;pointer-events:none;z-index:5;';
                parent.appendChild(c);
                window._vPaintCtx  = c.getContext('2d');
                window._vbW = vb.w;
                window._vbH = vb.h;
            }
            window.initPaintCanvas = initPaintCanvas;
            setTimeout(initPaintCanvas, 600);

            // =====================================================================
            // Throttle: omezit mousemove události do Pythonu na max ~12/s
            // + lokální canvas preview pro okamžitou odezvu
            // =====================================================================
            (function() {
                var lastSent = 0;
                var MIN_INTV = 80;  // ms — ~12 událostí/s do Pythonu
                var PBARVA   = { kresleni: '#86efac', krizek: 'rgba(255,255,255,0.85)', jip: '#fef08a', guma: 'rgba(239,68,68,0.55)' };

                document.addEventListener('mousedown', function() {
                    if (PBARVA[window.current_tool] !== undefined) {
                        window._vPainting = true;
                        if (!document.getElementById('veletrh-paint-canvas')) initPaintCanvas();
                    }
                }, true);

                document.addEventListener('mouseup', function() {
                    if (!window._vPainting) return;
                    window._vPainting = false;
                    // Canvas vymazat — Python pošle aktuální SVG na mouseup
                    var ctx = window._vPaintCtx;
                    var c   = document.getElementById('veletrh-paint-canvas');
                    if (ctx && c) ctx.clearRect(0, 0, c.width, c.height);
                }, true);

                // Přepočet viewport px → SVG souřadnice (koordinátní prostor NiceGUI/SVG overlay)
                function imgCoords(e) {
                    var img = document.querySelector('#map-export-area img');
                    if (!img) return null;
                    // Zoom čteme ze singletonu — transform je teď "translate(…) scale(…)"
                    var z   = window._mapZoom ? window._mapZoom.getZoom() : 1.0;
                    var r   = img.getBoundingClientRect();
                    var cs  = window._veletrh_cs || 24;
                    // CSS px před zoomem
                    var csx = (e.clientX - r.left) / z;
                    var csy = (e.clientY - r.top)  / z;
                    // CSS zobrazená velikost img (bez zoom)
                    var cssW = r.width  / z;
                    var cssH = r.height / z;
                    // Převod CSS px → SVG souřadnice pomocí viewBox
                    var vbW = window._vbW || cssW;
                    var vbH = window._vbH || cssH;
                    var rx  = csx * vbW / cssW;
                    var ry  = csy * vbH / cssH;
                    var ox  = window._vGridOffsetX || 0;
                    var oy  = window._vGridOffsetY || 0;
                    return {
                        gx: Math.round((rx - ox) / cs) * cs + ox,
                        gy: Math.round((ry - oy) / cs) * cs + oy,
                        cs: cs
                    };
                }

                function clearCanvas() {
                    var c = document.getElementById('veletrh-paint-canvas');
                    if (c && window._vPaintCtx) window._vPaintCtx.clearRect(0, 0, c.width, c.height);
                }

                document.addEventListener('mousemove', function(e) {
                    if (!PBARVA[window.current_tool]) return;
                    var coord = imgCoords(e);
                    if (!coord) return;

                    if (window._vPainting) {
                        // Během tahu: persistentně přidávat buňky (canvas se smaže až na mouseup)
                        if (window._vPaintCtx) {
                            window._vPaintCtx.fillStyle   = PBARVA[window.current_tool];
                            window._vPaintCtx.globalAlpha = 0.65;
                            window._vPaintCtx.fillRect(coord.gx - coord.cs/2, coord.gy - coord.cs/2, coord.cs, coord.cs);
                        }
                    } else {
                        // Hover (bez stisku): smazat a ukázat buňku pod kurzorem
                        clearCanvas();
                        if (window._vPaintCtx) {
                            var ctx = window._vPaintCtx;
                            var x0  = coord.gx - coord.cs/2, y0 = coord.gy - coord.cs/2;
                            ctx.fillStyle   = PBARVA[window.current_tool];
                            ctx.globalAlpha = 0.35;
                            ctx.fillRect(x0, y0, coord.cs, coord.cs);
                            // Ohraničení buňky pro přesnou orientaci
                            ctx.globalAlpha = 0.55;
                            ctx.strokeStyle = 'rgba(30,30,30,0.6)';
                            ctx.lineWidth   = 1.2;
                            ctx.strokeRect(x0 + 0.6, y0 + 0.6, coord.cs - 1.2, coord.cs - 1.2);
                        }
                    }

                    // Throttle: zahazovat přebytečné události — Python dostane max 12/s
                    var now = Date.now();
                    if (now - lastSent < MIN_INTV) {
                        e.stopImmediatePropagation();
                    } else {
                        lastSent = now;
                    }
                }, true);

                // Vymazat hover highlight při opuštění oblasti mapy (ne při kliknutí na panel)
                setTimeout(function() {
                    var mc = document.getElementById('map-container');
                    if (mc) mc.addEventListener('mouseleave', function() {
                        if (!window._vPainting) clearCanvas();
                    });
                }, 800);

            })();

        });

        window._pendingKey = null;

        function stahniMapuPDF(nazevPatra) {
            var element   = document.getElementById("map-export-area");
            var container = document.getElementById("map-container");

            // Uložit aktuální transform string (translate+scale) pro obnovení po exportu
            var originalTransform = element.style.transform;
            // Dočasně resetovat transform: žádný zoom ani posun, obsah viditelný celý
            element.style.transform = 'translate(0px,0px) scale(1)';
            container.style.overflow = 'visible';

            setTimeout(() => {
                var w = element.scrollWidth;
                var h = element.scrollHeight;

                var opt = {
                    margin:       0,
                    filename:     'Veletrh_Mapa_' + nazevPatra + '.pdf',
                    image:        { type: 'jpeg', quality: 0.95 },
                    html2canvas:  {
                        scale: 1,
                        useCORS: true,
                        logging: false,
                        width: w,
                        height: h,
                        windowWidth: w,
                        windowHeight: h,
                        removeContainer: true
                    },
                    jsPDF: { unit: 'px', format: [w, h], orientation: w > h ? 'landscape' : 'portrait' }
                };

                function restore() {
                    element.style.transform = originalTransform;
                    container.style.overflow = 'hidden';
                }
                html2pdf().set(opt).from(element).save().then(restore).catch(function(err) {
                    console.error("Chyba při tvorbě PDF:", err);
                    restore();
                });
            }, 300);
        }
    </script>
''', shared=True)


@ui.refreshable
def vykresli_veletrh(user_id, user_name, vsechna_prava):
    # --- Per-user UI state init ---
    if user_id not in _UI_STATE:
        _UI_STATE[user_id] = {
            'role': 'host',
            'aktivni_patro': 'prizemi',
            'nastroj': 'vyber',
            'aktivni_sekce': 'Info',
            'panel_open': False,
            'vybrane_pro_rezervaci': set(),
            'zoom': 1.0,
            'pre_legenda_zoom': 1.0,
            'is_painting': False,
            'paint_mode': 'add',
            'align_mode': False,
            'temp_p1': None,
            'current_batch_id': None,
            'current_res_id': None,
            'selected_batch': None,
            'is_dragging_batch': False,
            'drag_start': (0, 0),
            'batch_start_pos': {},
            '_batch_stanky': [],
            'highlighted_res': None,
            'tah_pocet': 0,
            'kruh_r': 8,
            'kruh_aktivni': None,
            'kruh_selected': None,
            'kruh_show_label': False,
            'undo_stack': [],
            '_undo_removed': [],
        }
    st = _UI_STATE[user_id]
    # Sanity: pokud má uživatel v UI state staré patro (suteren / 2.NP), přepnout na přízemí
    if st.get('aktivni_patro') not in PATRA:
        st['aktivni_patro'] = 'prizemi'

    if 'vse' in vsechna_prava or 'veletrh_admin' in vsechna_prava: st['role'] = 'admin'
    elif 'veletrh_uzivatel' in vsechna_prava: st['role'] = 'uzivatel'
    elif 'veletrh_komentator' in vsechna_prava: st['role'] = 'komentator'
    else: st['role'] = 'host'

    ui_refs = {'ii': None, 'zoom_container': None, 'lbl_vybrano': None, 'lbl_cena_celkem': None, 'f_typ': None, 'f_ele': None, 'f_voda': None, 'stat_kontejner': None, 'stat_labels': None, 'detail_dialog': None, 'vykresli_archiv': None, 'panely_obsah': None, 'seznam_rezervaci_ui': None, 'patro_toggle': None, 'btn_zpet_legenda': None, 'btn_tisk_word': None, 'btn_tisk_pdf': None, 'sel_overlay': None, 'lbl_overlay_m2': None, '_toggle_panel_fn': None, 'ladeni_panel_ref': None, 'nastroje_panel': None}
    aktualni_res_id = {'id': None}

    SEKCE = ['Info', 'Rozmístění stánků', 'Rezervace stánků', 'Rezervované stánky', 'Kapacita']
    if st['role'] in ['admin', 'uzivatel', 'komentator']:
        SEKCE.append('Zákazníci')
    if st['role'] == 'admin':
        SEKCE.append('Poznámky')

    # ==========================================
    # LOGICKÉ FUNKCE
    # ==========================================
    def update_var(key, val):
        if val is None: return                    # prázdný input — ignorovat
        if key == 'cell_size' and float(val) <= 0: return  # nulová/záporná hodnota
        state[key] = float(val) if key in ['cell_size', 'grid_offset_x', 'grid_offset_y'] else val
        if key in ['cena_m2', 'cell_size']:
            state['_svg_cache'].clear()  # cena/velikost buňky se změnila → cache neplatná
            if key == 'cell_size':
                ui.run_javascript(f'window._veletrh_cs = {val};')
            update_svg(); save_data()
        elif key in ['grid_offset_x', 'grid_offset_y']:
            state['_svg_cache'].clear()
            # Přesunutí všech stánků na nejbližší střed nové mřížky
            _renormalize_stanky()
            ox, oy = state['grid_offset_x'], state['grid_offset_y']
            ui.run_javascript(f'window._vGridOffsetX = {ox}; window._vGridOffsetY = {oy};')
            update_svg(); save_data()

    def set_zoom(delta):
        st['zoom'] = max(0.1, min(4.0, st['zoom'] + delta))
        new_zoom = st['zoom']
        # JS singleton zajistí zoom na střed viewportu (cxFrac=0.5, cyFrac=0.5) + scroll kompenzaci
        ui.run_javascript(f'window._mapZoom.syncFromPython({new_zoom}, 0.5, 0.5)')

    def obnov_statistiky():
        if st['role'] != 'admin' or not ui_refs['stat_kontejner']: return

        # ── Výpočty (vždy aktuální) ───────────────────────────────────────────
        celkem_m2 = len(state['stanky'])
        m2_blok   = sum(len(r['stanky_ids']) for r in state['rezervace'].values() if r['typ'] == 'Křížek (Blokováno)')
        m2_jip    = sum(len(r['stanky_ids']) for r in state['rezervace'].values() if r['typ'] == 'JIP Zóna')
        m2_gr     = sum(len(r['stanky_ids']) for r in state['rezervace'].values() if r['typ'] in ['Gastro', 'Retail'])
        m2_disp   = celkem_m2 - m2_blok - m2_jip
        m2_zbyva  = m2_disp - m2_gr
        hod_cel   = int(round(m2_disp  * state['cena_m2']))
        hod_rez   = int(round(m2_gr    * state['cena_m2']))
        hod_zb    = int(round(m2_zbyva * state['cena_m2']))
        ele_pocet = sum(1 for r in state['rezervace'].values() if r['elektro'] != "0" and r['typ'] not in ['Křížek (Blokováno)', 'JIP Zóna'])
        ele_cena  = sum(ELEKTRO_CENIK[r['elektro']]['cena'] for r in state['rezervace'].values() if r['typ'] not in ['Křížek (Blokováno)', 'JIP Zóna'])
        voda_poc  = sum(1 for r in state['rezervace'].values() if r['voda'] != "0" and r['typ'] not in ['Křížek (Blokováno)', 'JIP Zóna'])
        voda_cena = sum(VODA_CENIK[r['voda']]['cena'] for r in state['rezervace'].values() if r['typ'] not in ['Křížek (Blokováno)', 'JIP Zóna'])

        sl = ui_refs.get('stat_labels')
        if sl:
            # ── Rychlá cesta: pouze set_text(), žádné DOM operace ────────────
            sl['kap_m2'].set_text(f'{m2_disp} m²')
            sl['kap_kc'].set_text(f'{fmt(hod_cel)} Kč')
            sl['kap_det'].set_text(f'Dále blokováno: {m2_blok}m² | JIP: {m2_jip}m²')
            sl['rez_m2'].set_text(f'{m2_gr} m²')
            sl['rez_kc'].set_text(f'{fmt(hod_rez)} Kč')
            sl['zb_m2'].set_text(f'{m2_zbyva} m²')
            sl['zb_kc'].set_text(f'{fmt(hod_zb)} Kč')
            sl['ele_poc'].set_text(f'{ele_pocet} ks')
            sl['ele_cen'].set_text(f'{fmt(ele_cena)} Kč')
            sl['voda_poc'].set_text(f'{voda_poc} ks')
            sl['voda_cen'].set_text(f'{fmt(voda_cena)} Kč')
            return

        # ── První volání: sestavit DOM a uložit reference labelů ─────────────
        with ui_refs['stat_kontejner']:
            with ui.row().classes('w-full justify-between items-stretch gap-4 mb-4'):
                with ui.card().classes('flex-1 bg-gray-50 border border-gray-200 shadow-sm p-4 items-center text-center rounded-xl'):
                    ui.label('Kapacita čisté plochy').classes('text-xs text-gray-500 font-bold uppercase tracking-wider mb-1')
                    l_kap_m2  = ui.label(f'{m2_disp} m²').classes('text-3xl font-black text-gray-800')
                    l_kap_kc  = ui.label(f'{fmt(hod_cel)} Kč').classes('text-sm text-gray-600 font-medium mt-1 bg-gray-200 px-2 py-1 rounded w-full')
                    l_kap_det = ui.label(f'Dále blokováno: {m2_blok}m² | JIP: {m2_jip}m²').classes('text-[10px] text-gray-400 mt-2')
                with ui.card().classes('flex-1 bg-green-50 border border-green-200 shadow-sm p-4 items-center text-center rounded-xl'):
                    ui.label('Zarezervováno (G+R)').classes('text-xs text-green-600 font-bold uppercase tracking-wider mb-1')
                    l_rez_m2 = ui.label(f'{m2_gr} m²').classes('text-3xl font-black text-green-900')
                    l_rez_kc = ui.label(f'{fmt(hod_rez)} Kč').classes('text-sm text-green-700 font-medium mt-1 bg-green-200 px-2 py-1 rounded w-full')
                with ui.card().classes('flex-1 bg-blue-50 border border-blue-200 shadow-sm p-4 items-center text-center rounded-xl'):
                    ui.label('Zbývá k rezervaci').classes('text-xs text-blue-600 font-bold uppercase tracking-wider mb-1')
                    l_zb_m2  = ui.label(f'{m2_zbyva} m²').classes('text-3xl font-black text-blue-900')
                    l_zb_kc  = ui.label(f'{fmt(hod_zb)} Kč').classes('text-sm text-blue-700 font-medium mt-1 bg-blue-200 px-2 py-1 rounded w-full')
            with ui.row().classes('w-full justify-between items-stretch gap-4'):
                with ui.card().classes('flex-1 bg-yellow-50 border border-yellow-200 shadow-sm p-4 items-center text-center rounded-xl'):
                    ui.label('Elektro přípojky').classes('text-xs text-yellow-600 font-bold uppercase tracking-wider mb-1')
                    l_ele_poc = ui.label(f'{ele_pocet} ks').classes('text-2xl font-black text-yellow-900')
                    l_ele_cen = ui.label(f'{fmt(ele_cena)} Kč').classes('text-sm text-yellow-700 font-medium mt-1')
                with ui.card().classes('flex-1 bg-cyan-50 border border-cyan-200 shadow-sm p-4 items-center text-center rounded-xl'):
                    ui.label('Přípojky vody').classes('text-xs text-cyan-600 font-bold uppercase tracking-wider mb-1')
                    l_voda_poc = ui.label(f'{voda_poc} ks').classes('text-2xl font-black text-cyan-900')
                    l_voda_cen = ui.label(f'{fmt(voda_cena)} Kč').classes('text-sm text-cyan-700 font-medium mt-1')
        ui_refs['stat_labels'] = {
            'kap_m2': l_kap_m2,   'kap_kc': l_kap_kc,    'kap_det': l_kap_det,
            'rez_m2': l_rez_m2,   'rez_kc': l_rez_kc,
            'zb_m2':  l_zb_m2,    'zb_kc':  l_zb_kc,
            'ele_poc': l_ele_poc,  'ele_cen': l_ele_cen,
            'voda_poc': l_voda_poc, 'voda_cen': l_voda_cen,
        }

    def push_undo(action_type, data):
        st['undo_stack'].append({'type': action_type, 'data': data})
        if len(st['undo_stack']) > 30:
            st['undo_stack'].pop(0)

    def perform_undo():
        if not st['undo_stack']:
            ui.notify('Není co vrátit zpět.', type='info', position='top')
            return
        op = st['undo_stack'].pop()
        t, d = op['type'], op['data']

        if t == 'add_stanky':
            # Undo přidání stánků: smazat je z mapy
            for s_id in d:
                s = state['_stanky_by_id'].get(s_id)
                if s: _stanky_remove(s)
            update_svg(); save_data()
            ui.notify(f'↩ Vráceno přidání {len(d)} stánků.', type='info', position='top')

        elif t == 'remove_stanky':
            # Undo smazání stánků: obnovit je
            for s in d:
                if s['id'] not in state['_stanky_by_id']:
                    _stanky_append(s)
            update_svg(); save_data()
            ui.notify(f'↩ Vráceno smazání {len(d)} stánků.', type='info', position='top')

        elif t == 'add_rezervace':
            # Undo vytvoření rezervace: smazat ji, stánky zůstanou volné
            res_id = d['res_id']
            if res_id in state['rezervace']:
                for s_id in list(state['rezervace'][res_id]['stanky_ids']):
                    s = state['_stanky_by_id'].get(s_id)
                    if s: _res_idx_set(s, None)
                del state['rezervace'][res_id]
            update_svg(); save_data()
            if ui_refs.get('seznam_rezervaci_ui'): ui_refs['seznam_rezervaci_ui'].refresh()
            ui.notify('↩ Vrácena rezervace.', type='info', position='top')

        elif t == 'remove_rezervace':
            # Undo uvolnění stánku: obnovit rezervaci
            state['rezervace'][d['res_id']] = d['rezervace_data']
            for s_id in d['rezervace_data']['stanky_ids']:
                s = state['_stanky_by_id'].get(s_id)
                if s: _res_idx_set(s, d['res_id'])
            update_svg(); save_data()
            if ui_refs.get('seznam_rezervaci_ui'): ui_refs['seznam_rezervaci_ui'].refresh()
            ui.notify('↩ Vráceno uvolnění stánku.', type='info', position='top')

        elif t == 'move_batch':
            # Undo přesunu: vrátit původní pozice
            for s_id, (ox, oy) in d.items():
                s = state['_stanky_by_id'].get(s_id)
                if s:
                    s['x'], s['y'] = ox, oy
                    _invalidate_cache(s.get('res_id'))
            _rebuild_grid()
            update_svg(); save_data()
            ui.notify('↩ Vrácen přesun.', type='info', position='top')

        elif t == 'add_kruh':
            # Undo přidání kruhu: odebrat ho (d = index do state['kruhy'])
            if d < len(state['kruhy']):
                state['kruhy'].pop(d)
                st['kruh_selected'] = None
                st['kruh_aktivni'] = None
            update_svg(); save_data()
            if ui_refs.get('nastroje_panel'): ui_refs['nastroje_panel'].refresh()
            ui.notify('↩ Vráceno přidání kruhu.', type='info', position='top')

        elif t == 'remove_kruh':
            # Undo smazání kruhu: vložit zpět na původní index
            idx = d.get('index', len(state['kruhy']))
            state['kruhy'].insert(idx, d['data'])
            st['kruh_selected'] = idx
            update_svg(); save_data()
            if ui_refs.get('nastroje_panel'): ui_refs['nastroje_panel'].refresh()
            ui.notify('↩ Vráceno smazání kruhu.', type='info', position='top')

    def aktualizuj_cenu_v_panelu(e=None):
        if not ui_refs.get('lbl_cena_celkem'): return
        m2 = len(st['vybrane_pro_rezervaci'])

        typ = ui_refs['f_typ'].value if ui_refs.get('f_typ') else 'Gastro'
        if typ in ['JIP Zóna', 'Křížek (Blokováno)']:
            celkem = 0
        else:
            cena_plocha = int(round(m2 * state['cena_m2']))
            c_ele = ELEKTRO_CENIK.get(ui_refs['f_ele'].value, {}).get('cena', 0) if ui_refs.get('f_ele') and ui_refs['f_ele'].value else 0
            c_voda = VODA_CENIK.get(ui_refs['f_voda'].value, {}).get('cena', 0) if ui_refs.get('f_voda') and ui_refs['f_voda'].value else 0
            celkem = cena_plocha + c_ele + c_voda

        ui_refs['lbl_cena_celkem'].set_text(f"{fmt(celkem)} Kč")

    def update_svg():
        if not ui_refs['ii']: return
        parts, cs = [], state['cell_size']
        patro = st['aktivni_patro']

        # ── Kruhové podklady — renderují se jako první (za vším ostatním) ──
        for _orig_idx, _kruh in enumerate(state['kruhy']):
            if _kruh['patro'] != patro:
                continue
            _cx, _cy = _kruh['x'], _kruh['y']
            _r  = _kruh['r']
            _rp = _r * cs
            _d  = _r * 2
            _is_sel = (_orig_idx == st.get('kruh_selected'))
            _fill   = 'rgba(249,115,22,0.15)' if _is_sel else 'rgba(59,130,246,0.07)'
            _color  = '#f97316' if _is_sel else '#3b82f6'
            _stroke = 'stroke-width="3"' if _is_sel else 'stroke-width="2" stroke-dasharray="8,4"'
            parts.append(
                f'<circle cx="{_cx:.2f}" cy="{_cy:.2f}" r="{_rp:.2f}" '
                f'fill="{_fill}" stroke="{_color}" {_stroke} pointer-events="none"/>'
                f'<line x1="{_cx-14:.2f}" y1="{_cy:.2f}" x2="{_cx+14:.2f}" y2="{_cy:.2f}" '
                f'stroke="{_color}" stroke-width="1.5" pointer-events="none"/>'
                f'<line x1="{_cx:.2f}" y1="{_cy-14:.2f}" x2="{_cx:.2f}" y2="{_cy+14:.2f}" '
                f'stroke="{_color}" stroke-width="1.5" pointer-events="none"/>'
            )
            if st.get('kruh_show_label') and _is_sel:
                parts.append(
                    f'<text x="{_cx:.2f}" y="{_cy - _rp - cs*0.7:.2f}" text-anchor="middle" '
                    f'font-size="{cs*0.95:.2f}" fill="{_color}" font-family="Arial,Helvetica,sans-serif" '
                    f'pointer-events="none">⌀ {_d} m</text>'
                )

        # ── SVG cache: invalidovat při změně cell_size nebo patra ────────────
        svg_cache = state['_svg_cache']
        if state['_svg_cache_cs'] != cs or state['_svg_cache_patro'] != patro:
            svg_cache.clear()
            state['_svg_cache_cs']    = cs
            state['_svg_cache_patro'] = patro

        # ── Virtuální mřížka — viditelná v režimech kreslení ─────────────────
        # Středy buněk jsou na: ox + n*cs, oy + n*cs  (ox/oy = grid offset)
        # Hranice buněk jsou tedy na: ox + (n+0.5)*cs = ox + n*cs + cs/2
        # Pattern offset = (ox + cs/2) % cs  →  pattern lines land on boundaries.
        if st['nastroj'] in ['kresleni', 'krizek', 'jip', 'oblast', 'kruh', 'guma']:
            ox  = state['grid_offset_x']
            oy  = state['grid_offset_y']
            half = cs / 2
            pat_x = (ox + half) % cs
            pat_y = (oy + half) % cs
            parts.append(
                f'<defs>'
                f'<pattern id="vgrid" width="{cs:.4f}" height="{cs:.4f}" '
                f'patternUnits="userSpaceOnUse" x="{pat_x:.4f}" y="{pat_y:.4f}">'
                f'<path d="M {cs:.4f} 0 L 0 0 0 {cs:.4f}" '
                f'fill="none" stroke="rgba(70,70,70,0.28)" stroke-width="0.6"/>'
                f'</pattern>'
                f'</defs>'
                f'<rect x="-99999" y="-99999" width="199998" height="199998" '
                f'fill="url(#vgrid)" pointer-events="none"/>'
            )

        if st['temp_p1'] and st['nastroj'] == 'oblast':
            # Zvýraznit rohovou buňku prvního kliknutí
            rx0, ry0 = st['temp_p1']
            parts.append(
                f'<rect x="{rx0 - half:.4f}" y="{ry0 - half:.4f}" '
                f'width="{cs:.4f}" height="{cs:.4f}" '
                f'fill="rgba(250,204,21,0.5)" stroke="#ca8a04" stroke-width="1.5" '
                f'pointer-events="none"/>'
            )

        # Volné stánky — vždy přestavit (mění se při malování)
        for s in state['stanky']:
            if s['patro'] != patro or s['res_id'] is not None: continue
            x, y = s['x'] - cs/2, s['y'] - cs/2
            is_selected = s['id'] in st['vybrane_pro_rezervaci']
            barva = "#f59e0b" if is_selected else "#86efac"
            if st['nastroj'] == 'ladeni' and s['batch_id'] == st['selected_batch']:
                border, opacita = 'stroke="#14532d" stroke-width="2" stroke-dasharray="2,2"', "0.9"
            else:
                border, opacita = 'stroke="white" stroke-width="1"', "0.85" if is_selected else "0.5"
            stav_text = "Vybráno k rezervaci" if is_selected else "Volný stánek"
            tooltip_volne = f"{stav_text}&#10;Plocha: 1 m²&#10;Cena za m²: {fmt(state['cena_m2'])} Kč"
            parts.append(f'<rect x="{x}" y="{y}" width="{cs}" height="{cs}" fill="{barva}" opacity="{opacita}" {border} pointer-events="all"><title>{tooltip_volne}</title></rect>')

        highlighted = st.get('highlighted_res')

        # Rezervace, pro které cache NELZE použít:
        #  • právě se malující  (current_res_id) — mění se každý mousemove
        #  • zvýrazněná         (highlighted)    — má animaci
        #  • vybraný batch      (selected_batch) — jiný border v ladění
        no_cache: set = {st.get('current_res_id'), highlighted}
        if st['nastroj'] == 'ladeni':
            no_cache.add(st.get('selected_batch'))
        no_cache.discard(None)

        for res_id, r in state['rezervace'].items():
            stanky_rezervace = [s for s in state['_res_idx'].get(res_id, []) if s['patro'] == patro]
            if not stanky_rezervace: continue

            # ── Cache hit: přeskočit přepočítávání ──────────────────────────
            if res_id not in no_cache and res_id in svg_cache:
                parts.extend(svg_cache[res_id])
                continue

            # ── Cache miss: sestavit SVG fragmenty pro tuto rezervaci ────────
            res_parts = []
            m2  = len(r['stanky_ids'])
            typ = r['typ']
            animace = '<animate attributeName="opacity" values="1;0.2;1" dur="0.6s" repeatCount="5" />' if res_id == highlighted else ''

            if typ == 'Křížek (Blokováno)':
                border_style = 'stroke="#14532d" stroke-width="2" stroke-dasharray="2,2"' if st['nastroj'] == 'ladeni' and st['selected_batch'] == res_id else 'stroke="#d1d5db" stroke-width="1"'
                for s in stanky_rezervace:
                    x, y = s["x"] - cs/2, s["y"] - cs/2
                    res_parts.append(f'<rect x="{x}" y="{y}" width="{cs+0.5}" height="{cs+0.5}" fill="white" {border_style} pointer-events="all"><title>Blokováno</title>{animace}</rect>')
                    res_parts.append(f'<line x1="{x}" y1="{y}" x2="{x+cs}" y2="{y+cs}" stroke="#9ca3af" stroke-width="2" pointer-events="none" />')
                    res_parts.append(f'<line x1="{x+cs}" y1="{y}" x2="{x}" y2="{y+cs}" stroke="#9ca3af" stroke-width="2" pointer-events="none" />')
            else:
                if typ == 'JIP Zóna':
                    barva, barva_textu, barva_ikony, ikonka, zkraceny_nazev = "#fef08a", "#713f12", "#713f12", "", "JIP"
                elif typ == 'Gastro':
                    barva, barva_textu, barva_ikony, ikonka = "#6ee7b7", "#064e3b", "#064e3b", "🍴"
                    zkraceny_nazev = r['nazev'][:12] + ('.' if len(r['nazev']) > 12 else '')
                else:
                    barva, barva_textu, barva_ikony, ikonka = "#93c5fd", "#1e3a8a", "#1e3a8a", "🍷"
                    zkraceny_nazev = r['nazev'][:12] + ('.' if len(r['nazev']) > 12 else '')

                cena_pl  = int(round(m2 * state['cena_m2'])) if typ not in ['JIP Zóna', 'Křížek (Blokováno)'] else 0
                cena_ele = ELEKTRO_CENIK[r['elektro']]['cena']
                cena_vo  = VODA_CENIK[r['voda']]['cena']
                cena_tot = cena_pl + cena_ele + cena_vo

                tooltip = (f"{'Dodavatel' if typ != 'JIP Zóna' else 'Název'}: {r['nazev']} (IČ: {r['ico']})&#10;"
                           f"Typ stánku: {r['typ']}&#10;Plocha: {m2} m² ({fmt(cena_pl)} Kč)&#10;"
                           f"Elektro: {ELEKTRO_CENIK[r['elektro']]['nazev']} ({fmt(cena_ele)} Kč)&#10;"
                           f"Voda: {VODA_CENIK[r['voda']]['nazev']} ({fmt(cena_vo)} Kč)&#10;----------------------&#10;"
                           f"Celková cena: {fmt(cena_tot)} Kč")

                border_style = 'stroke="#14532d" stroke-width="2" stroke-dasharray="2,2"' if st['nastroj'] == 'ladeni' and st['selected_batch'] == res_id else f'stroke="{barva}" stroke-width="1"'
                xs = [s['x'] for s in stanky_rezervace]
                ys = [s['y'] for s in stanky_rezervace]
                for s in stanky_rezervace:
                    x, y = s["x"] - cs/2, s["y"] - cs/2
                    res_parts.append(f'<rect x="{x}" y="{y}" width="{cs+0.5}" height="{cs+0.5}" fill="{barva}" opacity="1.0" {border_style} pointer-events="all"><title>{tooltip}</title>{animace}</rect>')

                center_x       = (min(xs) + max(xs)) / 2
                center_y       = (min(ys) + max(ys)) / 2
                velikost_fontu = cs * min(2.0, max(0.8, (m2 ** 0.4)))

                if ikonka:
                    posun_ikony = velikost_fontu * 0.2
                    posun_textu = velikost_fontu * 0.4
                    res_parts.append(f'<text x="{center_x}" y="{center_y - posun_ikony}" dy="0.35em" font-size="{velikost_fontu*0.6}" fill="{barva_ikony}" text-anchor="middle" pointer-events="none">{ikonka}</text>')
                else:
                    posun_textu = 0

                res_parts.append(f'<text x="{center_x}" y="{center_y + posun_textu}" dy="0.35em" font-size="{velikost_fontu*0.3}" fill="{barva_textu}" font-family="Arial, Helvetica, sans-serif" text-anchor="middle" pointer-events="none">{zkraceny_nazev}</text>')

            parts.extend(res_parts)

            # Uložit do cache — jen pro statické prvky (ne aktivní/zvýrazněné)
            if res_id not in no_cache:
                svg_cache[res_id] = res_parts

        ui_refs['ii'].content = ''.join(parts)
        n_vyb = len(st['vybrane_pro_rezervaci'])
        if ui_refs['lbl_vybrano']: ui_refs['lbl_vybrano'].set_text(f"{n_vyb} m²")
        aktualizuj_cenu_v_panelu()
        # Plovoucí výběrová karta — aktualizovat jen mimo aktivní tah (úspora DOM ops)
        if not st.get('is_painting') and not st.get('is_dragging_batch'):
            obnov_statistiky()
            if ui_refs.get('sel_overlay'):
                ui_refs['sel_overlay'].style(f'display: {"flex" if n_vyb > 0 else "none"}; transform: translateX(-50%);')
            if n_vyb > 0 and ui_refs.get('lbl_overlay_m2'):
                ui_refs['lbl_overlay_m2'].set_text(f'{n_vyb} m² vybráno')

    def uloz_poznamku_stanku(res_id, text_poznamky):
        if res_id in state['rezervace']:
            state['rezervace'][res_id]['interni_poznamka'] = text_poznamky
            save_data()
            ui.notify('Poznámka uložena.', type='positive', position='top')

    def najdi_stanek(res_id):
        stanky_rezervace = list(state['_res_idx'].get(res_id, []))
        if not stanky_rezervace: return

        patro_stanku = stanky_rezervace[0]['patro']
        if st['aktivni_patro'] != patro_stanku:
            if ui_refs.get('patro_toggle'):
                ui_refs['patro_toggle'].set_value(patro_stanku)

        st['highlighted_res'] = res_id
        update_svg()

        min_x = min(s['x'] for s in stanky_rezervace)
        max_x = max(s['x'] for s in stanky_rezervace)
        min_y = min(s['y'] for s in stanky_rezervace)
        max_y = max(s['y'] for s in stanky_rezervace)
        center_x = (min_x + max_x) / 2
        center_y = (min_y + max_y) / 2

        js_code = f'setTimeout(function(){{ window._mapZoom.panToContent({center_x:.1f}, {center_y:.1f}); }}, 200);'
        ui.run_javascript(js_code)

    def handle_click(e: events.MouseEventArguments):
        if st.get('highlighted_res'):
            st['highlighted_res'] = None
            update_svg()

        # ── Pravý klik: odstranit vybraný (oranžový) kruh — jen při nástroji Kruh ──
        if e.type == 'contextmenu':
            if st.get('nastroj') == 'kruh':
                _sel = st.get('kruh_selected')
                if _sel is not None and _sel < len(state['kruhy']):
                    push_undo('remove_kruh', {'index': _sel, 'data': dict(state['kruhy'][_sel])})
                    state['kruhy'].pop(_sel)
                    st['kruh_selected'] = None
                    st['kruh_aktivni'] = None
                    update_svg(); save_data()
                    if ui_refs.get('nastroje_panel'):
                        ui_refs['nastroje_panel'].refresh()
            return

        if st['nastroj'] == 'packa': return
        ix, iy, cs = e.image_x, e.image_y, state['cell_size']
        ox, oy = state['grid_offset_x'], state['grid_offset_y']

        # ── Kalibrační režim: zarovnat mřížku na kliknutý bod ────────────────
        if st.get('align_mode') and e.type == 'mousedown':
            st['align_mode'] = False
            state['grid_offset_x'] = ix % cs
            state['grid_offset_y'] = iy % cs
            state['_svg_cache'].clear()
            _renormalize_stanky()
            new_ox, new_oy = state['grid_offset_x'], state['grid_offset_y']
            ui.run_javascript(f'window._vGridOffsetX = {new_ox:.4f}; window._vGridOffsetY = {new_oy:.4f};')
            update_svg(); save_data()
            ui.notify(f'✅ Mřížka zarovnána (offset X={new_ox:.2f}, Y={new_oy:.2f})', type='positive')
            return

        if st['nastroj'] == 'kruh':
            if e.type == 'mousedown':
                gx, gy = _snap_xy(ix, iy)
                _hit_idx = None
                _hit_dist = float('inf')
                for _ci, _ck in enumerate(state['kruhy']):
                    if _ck['patro'] != st['aktivni_patro']:
                        continue
                    _d = ((_ck['x'] - ix) ** 2 + (_ck['y'] - iy) ** 2) ** 0.5
                    # Klik kdekoliv uvnitř kruhu (+ malá rezerva 10 px) ho vybere
                    _threshold = _ck['r'] * state['cell_size'] + 10
                    if _d < _threshold and _d < _hit_dist:
                        _hit_dist = _d
                        _hit_idx = _ci
                if _hit_idx is not None:
                    # Klik poblíž středu existujícího kruhu → vyber a přetáhni
                    st['kruh_selected'] = _hit_idx
                    st['kruh_aktivni'] = _hit_idx
                    st['kruh_r'] = state['kruhy'][_hit_idx]['r']
                else:
                    # Klik do prázdna → nový kruh
                    state['kruhy'].append({'x': gx, 'y': gy, 'r': st.get('kruh_r', 8), 'patro': st['aktivni_patro']})
                    st['kruh_selected'] = len(state['kruhy']) - 1
                    st['kruh_aktivni'] = st['kruh_selected']
                    st['_kruh_novy'] = True
                st['is_painting'] = True
                update_svg()
                if ui_refs.get('nastroje_panel'):
                    ui_refs['nastroje_panel'].refresh()
            elif e.type == 'mousemove' and st.get('is_painting') and st.get('kruh_aktivni') is not None:
                gx, gy = _snap_xy(ix, iy)
                _k = state['kruhy'][st['kruh_aktivni']]
                if gx != _k['x'] or gy != _k['y']:
                    _k['x'], _k['y'] = gx, gy
                    update_svg()
            elif e.type == 'mouseup':
                if st.get('_kruh_novy'):
                    push_undo('add_kruh', len(state['kruhy']) - 1)
                    st['_kruh_novy'] = False
                st['is_painting'] = False
                st['kruh_aktivni'] = None
                save_data()
            return

        if st['nastroj'] == 'vyber' and e.type == 'mousedown':
            clicked_s = _grid_hit_vyber(ix, iy, st['aktivni_patro'])

            if clicked_s:
                s = clicked_s
                if s['res_id']:
                    r = state['rezervace'][s['res_id']]

                    if r['typ'] == 'Křížek (Blokováno)':
                        ui.notify('Tato plocha je blokovaná. Pro její uvolnění použijte nástroj "❌ Křížek" (funguje jako guma).', type='info')
                        return

                    aktualni_res_id['id'] = s['res_id']
                    det_nazev.value, det_ico.value, det_adresa.value, det_banka.value = r['nazev'], r['ico'], r['adresa'], r['banka']
                    m2 = len(r['stanky_ids'])

                    c_plocha = int(round(m2 * state['cena_m2'])) if r['typ'] not in ['JIP Zóna', 'Křížek (Blokováno)'] else 0
                    c_ele = ELEKTRO_CENIK[r['elektro']]['cena']
                    c_vod = VODA_CENIK[r['voda']]['cena']
                    c_tot = c_plocha + c_ele + c_vod

                    html_info = f"<b>Typ:</b> {r['typ']}<br><b>Plocha:</b> {m2} m² ({fmt(c_plocha)} Kč)<br><b>Elektro:</b> {ELEKTRO_CENIK[r['elektro']]['nazev']} ({fmt(c_ele)} Kč)<br><b>Voda:</b> {VODA_CENIK[r['voda']]['nazev']} ({fmt(c_vod)} Kč)<br><b style='color:#16a34a; font-size:1.2em;'>Celkem: {fmt(c_tot)} Kč</b>"
                    det_info.set_content(html_info)

                    is_komercni = r['typ'] not in ['JIP Zóna', 'Křížek (Blokováno)']
                    det_ico.set_visibility(is_komercni)
                    det_adresa.set_visibility(is_komercni)
                    det_banka.set_visibility(is_komercni)

                    if ui_refs.get('btn_tisk_word'):
                        ui_refs['btn_tisk_word'].set_visibility(is_komercni)
                    if ui_refs.get('btn_tisk_pdf'):
                        ui_refs['btn_tisk_pdf'].set_visibility(is_komercni)

                    if st['role'] != 'admin':
                        det_nazev.disable(); det_ico.disable(); det_adresa.disable(); det_banka.disable()
                    if ui_refs['detail_dialog']: ui_refs['detail_dialog'].open()
                else:
                    if st['role'] in ['admin', 'uzivatel']:
                        if s['id'] in st['vybrane_pro_rezervaci']: st['vybrane_pro_rezervaci'].remove(s['id'])
                        else: st['vybrane_pro_rezervaci'].add(s['id'])
                        update_svg()
            else:
                if st['role'] == 'admin':
                    ui.notify(f'📍 Souřadnice mapy pro kód (x,y): x={int(ix)}, y={int(iy)}', position='bottom-right', color='grey', timeout=4000)
            return

        if st['nastroj'] == 'ladeni':
            if e.type == 'mousedown':
                hit_s = _grid_hit(ix, iy, st['aktivni_patro'])
                if hit_s:
                    if not hit_s['res_id']:
                        st['selected_batch'], st['is_dragging_batch'], st['drag_start'] = hit_s['batch_id'], True, (ix, iy)
                        batch_list = [s2 for s2 in state['stanky'] if s2['batch_id'] == st['selected_batch']]
                        st['batch_start_pos'] = {s2['id']: (s2['x'], s2['y']) for s2 in batch_list}
                        st['_batch_stanky'] = batch_list
                        ladeni_panel.refresh(); update_svg()
                        return
                    else:
                        r = state['rezervace'].get(hit_s['res_id'])
                        if r and r['typ'] in ['JIP Zóna', 'Křížek (Blokováno)']:
                            st['selected_batch'], st['is_dragging_batch'], st['drag_start'] = hit_s['res_id'], True, (ix, iy)
                            batch_list = state['_res_idx'].get(st['selected_batch'], [])
                            st['batch_start_pos'] = {s2['id']: (s2['x'], s2['y']) for s2 in batch_list}
                            st['_batch_stanky'] = list(batch_list)
                            ladeni_panel.refresh(); update_svg()
                            return
                st['selected_batch'] = None; ladeni_panel.refresh(); update_svg()
            elif e.type == 'mousemove' and st['is_dragging_batch']:
                dx, dy = ix - st['drag_start'][0], iy - st['drag_start'][1]
                for s in st.get('_batch_stanky', []):
                    if s['id'] in st['batch_start_pos']:
                        s['x'] = st['batch_start_pos'][s['id']][0] + dx
                        s['y'] = st['batch_start_pos'][s['id']][1] + dy
                update_svg()
            elif e.type == 'mouseup' and st['is_dragging_batch']:
                st['is_dragging_batch'] = False
                if st.get('batch_start_pos'):
                    push_undo('move_batch', dict(st['batch_start_pos']))
                # Invalidovat SVG cache pro přesunuté rezervace (pozice se změnily)
                for _s in st.get('_batch_stanky', []):
                    _invalidate_cache(_s.get('res_id'))
                _rebuild_grid()   # pozice stánků se změnily — grid je neplatný
                save_data()
            return

        LIMIT_TAH = 50

        if st['nastroj'] == 'oblast' and e.type == 'mousedown':
            # Vždy zaokrouhlit klik na střed buňky
            gx_klik, gy_klik = _snap_xy(ix, iy)
            if not st['temp_p1']:
                st['temp_p1'] = (gx_klik, gy_klik)
                update_svg()
            else:
                x1, y1 = st['temp_p1']   # již zaokrouhleno z prvního kliknutí
                # Indexy rohových buněk
                gx1, gy1 = _grid_idx(x1, y1)
                gx2, gy2 = _grid_idx(gx_klik, gy_klik)
                min_gx, max_gx = min(gx1, gx2), max(gx1, gx2)
                min_gy, max_gy = min(gy1, gy2), max(gy1, gy2)
                batch_id = str(uuid.uuid4())
                pridano = 0
                preruseno = False
                # Iterace přes celočíselné indexy — žádný float drift
                for cgx in range(min_gx, max_gx + 1):
                    if preruseno: break
                    for cgy in range(min_gy, max_gy + 1):
                        if pridano >= LIMIT_TAH:
                            preruseno = True; break
                        cx, cy = cgx * cs + ox, cgy * cs + oy
                        if je_v_zone(cx, cy, st['aktivni_patro']):
                            if not _grid_has_nearby(cx, cy, st['aktivni_patro']):
                                _stanky_append({'id': str(uuid.uuid4()), 'batch_id': batch_id, 'x': cx, 'y': cy, 'patro': st['aktivni_patro'], 'res_id': None})
                                pridano += 1
                if pridano > 0:
                    added_ids = [s['id'] for s in state['stanky'] if s.get('batch_id') == batch_id]
                    push_undo('add_stanky', added_ids)
                st['temp_p1'] = None; update_svg(); save_data()
                if preruseno:
                    ui.notify(f'Jedním tahem lze zakreslit maximálně {LIMIT_TAH} m². Oblast byla ořezána.', type='warning', position='top')
            return

        if st['nastroj'] == 'guma':
            def _guma_smaz(hit):
                """Smaže jakýkoliv stánek bez ohledu na typ. Pokud patří k rezervaci, odstraní ho z ní (a smaže prázdnou rezervaci)."""
                if not hit: return False
                st['_undo_removed'].append(dict(hit))
                rid = hit.get('res_id')
                if rid and rid in state['rezervace']:
                    r = state['rezervace'][rid]
                    if hit['id'] in r['stanky_ids']:
                        r['stanky_ids'].remove(hit['id'])
                    if not r['stanky_ids']:
                        del state['rezervace'][rid]
                _stanky_remove(hit)
                return True

            if e.type == 'mousedown':
                st['is_painting'] = True
                st['_undo_removed'] = []
                st['_last_paint_cell'] = None  # reset throttle
                if _guma_smaz(_grid_hit(ix, iy, st['aktivni_patro'])):
                    update_svg()
            elif e.type == 'mousemove' and st.get('is_painting'):
                gx, gy = _snap_xy(ix, iy)
                # Throttle: pokud zůstáváme ve stejné buňce, přeskočit
                if st.get('_last_paint_cell') == (gx, gy):
                    return
                st['_last_paint_cell'] = (gx, gy)
                hit_s = _grid_hit(gx, gy, st['aktivni_patro'])
                if hit_s:
                    _guma_smaz(hit_s)
                    # SVG odložen na mouseup — canvas dělá náhled
            elif e.type == 'mouseup':
                if st.get('is_painting'):
                    st['is_painting'] = False
                    if st.get('_undo_removed'):
                        push_undo('remove_stanky', st['_undo_removed'])
                        st['_undo_removed'] = []
                    update_svg(); save_data()
            return

        if st['nastroj'] in ['kresleni', 'krizek', 'jip']:
            if e.type == 'mousedown':
                st['is_painting'] = True
                st['tah_pocet'] = 0
                st['_last_paint_cell'] = None  # reset throttle
                st['current_batch_id'] = str(uuid.uuid4())
                hit_s = _grid_hit(ix, iy, st['aktivni_patro'])

                if hit_s:
                    if st['nastroj'] == 'kresleni':
                        if hit_s['res_id'] is None:
                            st['paint_mode'] = 'remove_stand'
                            st['_undo_removed'] = [dict(hit_s)]  # undo: zachytit před smazáním
                            _stanky_remove(hit_s)
                            update_svg()
                        else: st['paint_mode'] = 'none'

                    elif st['nastroj'] == 'krizek':
                        if hit_s['res_id'] is None:
                            st['paint_mode'] = 'add_krizek'
                            st['current_res_id'] = str(uuid.uuid4())
                            state['rezervace'][st['current_res_id']] = {'nazev': 'Blokováno', 'ico': '', 'adresa': '', 'banka': '', 'typ': 'Křížek (Blokováno)', 'elektro': '0', 'voda': '0', 'stanky_ids': [hit_s['id']]}
                            _res_idx_set(hit_s, st['current_res_id'])
                            update_svg()
                        else:
                            r = state['rezervace'].get(hit_s['res_id'])
                            if r and r['typ'] == 'Křížek (Blokováno)':
                                st['paint_mode'] = 'remove_res'
                                if hit_s['id'] in r['stanky_ids']: r['stanky_ids'].remove(hit_s['id'])
                                _stanky_remove(hit_s)
                                update_svg()
                            else: st['paint_mode'] = 'none'

                    elif st['nastroj'] == 'jip':
                        if hit_s['res_id'] is None:
                            st['paint_mode'] = 'add_jip'
                            st['current_res_id'] = str(uuid.uuid4())
                            state['rezervace'][st['current_res_id']] = {'nazev': 'JIP', 'ico': '', 'adresa': '', 'banka': '', 'typ': 'JIP Zóna', 'elektro': '0', 'voda': '0', 'stanky_ids': [hit_s['id']]}
                            _res_idx_set(hit_s, st['current_res_id'])
                            update_svg()
                        else:
                            r = state['rezervace'].get(hit_s['res_id'])
                            if r and r['typ'] == 'JIP Zóna':
                                st['paint_mode'] = 'remove_res'
                                if hit_s['id'] in r['stanky_ids']: r['stanky_ids'].remove(hit_s['id'])
                                _stanky_remove(hit_s)
                                update_svg()
                            else: st['paint_mode'] = 'none'
                else:
                    # Snap na přesnou mřížkovou buňku
                    gx, gy = _snap_xy(ix, iy)
                    if not je_v_zone(gx, gy, st['aktivni_patro']):
                        ui.notify('Nelze kreslit mimo vymezenou zónu!', type='warning')
                        st['is_painting'] = False
                        return

                    if st.get('tah_pocet', 0) >= LIMIT_TAH:
                        ui.notify(f'Jedním tahem lze zakreslit maximálně {LIMIT_TAH} m².', type='warning', position='top')
                        st['is_painting'] = False; return

                    if st['nastroj'] == 'kresleni':
                        new_stand = {'id': str(uuid.uuid4()), 'batch_id': st['current_batch_id'], 'x': gx, 'y': gy, 'patro': st['aktivni_patro'], 'res_id': None}
                        _stanky_append(new_stand)
                        st['tah_pocet'] = st.get('tah_pocet', 0) + 1
                        st['paint_mode'] = 'add_stand'
                    elif st['nastroj'] == 'krizek':
                        st['paint_mode'] = 'add_krizek'
                        st['current_res_id'] = str(uuid.uuid4())
                        new_stand = {'id': str(uuid.uuid4()), 'batch_id': st['current_batch_id'], 'x': gx, 'y': gy, 'patro': st['aktivni_patro'], 'res_id': st['current_res_id']}
                        state['rezervace'][st['current_res_id']] = {'nazev': 'Blokováno', 'ico': '', 'adresa': '', 'banka': '', 'typ': 'Křížek (Blokováno)', 'elektro': '0', 'voda': '0', 'stanky_ids': [new_stand['id']]}
                        _stanky_append(new_stand)
                        st['tah_pocet'] = st.get('tah_pocet', 0) + 1
                    elif st['nastroj'] == 'jip':
                        st['paint_mode'] = 'add_jip'
                        st['current_res_id'] = str(uuid.uuid4())
                        new_stand = {'id': str(uuid.uuid4()), 'batch_id': st['current_batch_id'], 'x': gx, 'y': gy, 'patro': st['aktivni_patro'], 'res_id': st['current_res_id']}
                        state['rezervace'][st['current_res_id']] = {'nazev': 'JIP', 'ico': '', 'adresa': '', 'banka': '', 'typ': 'JIP Zóna', 'elektro': '0', 'voda': '0', 'stanky_ids': [new_stand['id']]}
                        _stanky_append(new_stand)
                        st['tah_pocet'] = st.get('tah_pocet', 0) + 1
                    update_svg()

            elif e.type == 'mouseup':
                if st['is_painting']:
                    st['is_painting'] = False
                    empty_res = [k for k, v in state['rezervace'].items() if not v['stanky_ids']]
                    for k in empty_res: del state['rezervace'][k]
                    # Push undo pro dokončený tah
                    if st['paint_mode'] == 'add_stand':
                        batch_ids = [s['id'] for s in state['stanky'] if s.get('batch_id') == st.get('current_batch_id') and s['res_id'] is None]
                        if batch_ids: push_undo('add_stanky', batch_ids)
                    elif st['paint_mode'] == 'remove_stand' and st.get('_undo_removed'):
                        push_undo('remove_stanky', st['_undo_removed'])
                        st['_undo_removed'] = []
                    elif st['paint_mode'] in ['add_krizek', 'add_jip']:
                        rid = st.get('current_res_id')
                        if rid and rid in state['rezervace']:
                            push_undo('add_rezervace', {'res_id': rid})
                    update_svg()   # finální render po dokončení tahu
                    save_data()

            elif e.type == 'mousemove' and st['is_painting']:
                if st['paint_mode'] == 'none': return

                # Throttle: pokud kurzor zůstává ve stejné mřížkové buňce jako naposledy, přeskočit
                _gx_thr, _gy_thr = _snap_xy(ix, iy)
                _last_cell = st.get('_last_paint_cell')
                if _last_cell == (_gx_thr, _gy_thr):
                    return
                st['_last_paint_cell'] = (_gx_thr, _gy_thr)

                # Snap kurzoru na střed buňky — veškerá logika pracuje s mřížkou
                gx, gy = _snap_xy(ix, iy)
                hit_s = _grid_hit(gx, gy, st['aktivni_patro'])

                if st['paint_mode'] == 'add_stand':
                    if not hit_s and je_v_zone(gx, gy, st['aktivni_patro']):
                        if not _grid_has_nearby(gx, gy, st['aktivni_patro']):
                            if st.get('tah_pocet', 0) >= LIMIT_TAH:
                                ui.notify(f'Jedním tahem lze zakreslit maximálně {LIMIT_TAH} m².', type='warning', position='top')
                                st['paint_mode'] = 'none'
                            else:
                                _stanky_append({'id': str(uuid.uuid4()), 'batch_id': st['current_batch_id'], 'x': gx, 'y': gy, 'patro': st['aktivni_patro'], 'res_id': None})
                                st['tah_pocet'] = st.get('tah_pocet', 0) + 1
                                # SVG se neaktualizuje při každém pohybu — canvas zajišťuje náhled
                elif st['paint_mode'] == 'remove_stand':
                    if hit_s and hit_s['res_id'] is None:
                        st['_undo_removed'].append(dict(hit_s))  # undo tracking
                        _stanky_remove(hit_s)  # SVG odložen na mouseup

                elif st['paint_mode'] in ['add_krizek', 'add_jip']:
                    if hit_s:
                        if hit_s['res_id'] is None:
                            _res_idx_set(hit_s, st['current_res_id'])
                            state['rezervace'][st['current_res_id']]['stanky_ids'].append(hit_s['id'])
                            # SVG odložen na mouseup
                    else:
                        if je_v_zone(gx, gy, st['aktivni_patro']) and not _grid_has_nearby(gx, gy, st['aktivni_patro']):
                            if st.get('tah_pocet', 0) >= LIMIT_TAH:
                                ui.notify(f'Jedním tahem lze zakreslit maximálně {LIMIT_TAH} m².', type='warning', position='top')
                                st['paint_mode'] = 'none'
                            else:
                                new_stand = {'id': str(uuid.uuid4()), 'batch_id': st['current_batch_id'], 'x': gx, 'y': gy, 'patro': st['aktivni_patro'], 'res_id': st['current_res_id']}
                                _stanky_append(new_stand)
                                state['rezervace'][st['current_res_id']]['stanky_ids'].append(new_stand['id'])
                                st['tah_pocet'] = st.get('tah_pocet', 0) + 1
                                # SVG odložen na mouseup

                elif st['paint_mode'] == 'remove_res':
                    if hit_s and hit_s['res_id']:
                        r = state['rezervace'].get(hit_s['res_id'])
                        if r and r['typ'] in ['Křížek (Blokováno)', 'JIP Zóna']:
                            if hit_s['id'] in r['stanky_ids']:
                                r['stanky_ids'].remove(hit_s['id'])
                            _stanky_remove(hit_s)
                            # SVG odložen na mouseup

    def zpet_z_legendy():
        if 'pre_legenda_zoom' in st:
            st['zoom'] = st['pre_legenda_zoom']
        ui.run_javascript('window._mapZoom.restorePan();')
        if ui_refs.get('btn_zpet_legenda'):
            ui_refs['btn_zpet_legenda'].style('display: none;')

    # ==========================================
    # 📱 HLAVNÍ ROZHRANÍ
    # ==========================================
    with ui.row().classes('w-full h-[85vh] min-h-[700px] no-wrap overflow-hidden rounded-xl border border-gray-200 relative') as main_row:

        map_w_class = 'w-3/4' if st['panel_open'] else 'w-full'
        left_col = ui.column().classes(f'{map_w_class} h-full relative bg-gray-200 transition-all duration-300 flex-1')
        with left_col:
            @ui.refreshable
            def ladeni_panel():
                if st['nastroj'] == 'ladeni' and st['role'] == 'admin':
                    with ui.card().classes('absolute top-24 left-4 z-50 bg-yellow-50 border border-yellow-300 p-4 w-64 shadow-xl'):
                        ui.label('🎯 Ladička pozic').classes('font-bold text-yellow-800 mb-2')
                        if not st['selected_batch']: ui.label('Klikněte na mapě na zónu volných stánků pro posun.').classes('text-xs text-yellow-700')
                        else:
                            def posun_sipkou(dx, dy):
                                if not st['selected_batch']: return
                                for s in st.get('_batch_stanky') or [s for s in state['stanky'] if s['batch_id'] == st['selected_batch'] or s['res_id'] == st['selected_batch']]:
                                    s['x'] += dx; s['y'] += dy
                                _rebuild_grid()
                                update_svg(); save_data()
                            with ui.column().classes('items-center w-full'):
                                ui.button(icon='arrow_drop_up', on_click=lambda: posun_sipkou(0, -0.5)).classes('w-12 h-8 bg-white text-gray-800 border')
                                with ui.row().classes('gap-2 my-1'):
                                    ui.button(icon='arrow_left', on_click=lambda: posun_sipkou(-0.5, 0)).classes('w-12 h-8 bg-white text-gray-800 border')
                                    ui.button(icon='circle').classes('w-12 h-8 bg-transparent text-gray-300').props('flat disable')
                                    ui.button(icon='arrow_right', on_click=lambda: posun_sipkou(0.5, 0)).classes('w-12 h-8 bg-white text-gray-800 border')
                                ui.button(icon='arrow_drop_down', on_click=lambda: posun_sipkou(0, 0.5)).classes('w-12 h-8 bg-white text-gray-800 border')

            def zmen_patro(patro):
                st['aktivni_patro'], st['temp_p1'], st['selected_batch'] = patro, None, None
                st['highlighted_res'] = None
                st['is_painting'] = False
                if st['role'] == 'admin': ladeni_panel.refresh()
                ui_refs['ii'].source = PATRA[patro]['img']
                # Vymazat canvas preview při přepnutí patra
                ui.run_javascript('var c=document.getElementById("veletrh-paint-canvas");if(c&&window._vPaintCtx)window._vPaintCtx.clearRect(0,0,c.width,c.height);window._vPainting=false;')
                update_svg()

            with ui.row().classes('absolute top-4 left-4 z-40 gap-4 bg-white/95 p-3 rounded-xl shadow-lg items-center border border-gray-200'):
                @ui.refreshable
                def nastroje_panel():
                    moznosti = {'vyber': '👆 Výběr', 'packa': '🖐️ Mapa'}
                    if st['role'] == 'admin':
                        moznosti.update({
                            'kresleni': '🖌️ Kreslit',
                            'oblast': '🔲 Oblast',
                            'ladeni': '🎯 Posun',
                            'krizek': '❌ Křížek',
                            'jip': '🟡 JIP',
                            'kruh': '⭕ Kruh',
                            'guma': '🩹 Guma',
                        })
                    if st['nastroj'] not in moznosti: st['nastroj'] = 'vyber'; ui.run_javascript("window.current_tool = 'vyber';")
                    def zmen_nastroj(v):
                        st['nastroj'], st['temp_p1'], st['selected_batch'] = v, None, None
                        st['is_painting'] = False
                        st['align_mode'] = False
                        if st['role'] == 'admin': ladeni_panel.refresh()
                        update_svg()
                        ui.run_javascript(
                            f"window.current_tool = '{v}';"
                            "var c=document.getElementById('veletrh-paint-canvas');"
                            "if(c&&window._vPaintCtx)window._vPaintCtx.clearRect(0,0,c.width,c.height);"
                            "window._vPainting=false;"
                        )
                        if ui_refs.get('map_container'):
                            ui_refs['map_container'].classes(remove='cursor-packa cursor-kresleni cursor-krizek cursor-jip cursor-oblast cursor-ladeni cursor-vyber cursor-kruh cursor-guma')
                            ui_refs['map_container'].classes(add=f'cursor-{v}')
                    ui.toggle(moznosti, value=st['nastroj'], on_change=lambda e: zmen_nastroj(e.value)).classes('bg-blue-100 text-blue-800 font-bold mr-4')
                    if st['nastroj'] == 'kruh':
                        _kr = st.get('kruh_r', 8)
                        _pocet_kruhu = len([k for k in state['kruhy'] if k['patro'] == st['aktivni_patro']])

                        def _zmen_r(delta):
                            st['kruh_r'] = max(1, min(50, st.get('kruh_r', 8) + delta))
                            _sel = st.get('kruh_selected')
                            if _sel is not None and _sel < len(state['kruhy']):
                                state['kruhy'][_sel]['r'] = st['kruh_r']
                                save_data()
                            if ui_refs.get('nastroje_panel'):
                                ui_refs['nastroje_panel'].refresh()
                            update_svg()

                        ui.button(icon='remove', on_click=lambda: _zmen_r(-1)).props('flat round size=sm').classes('text-blue-700 bg-blue-50 hover:bg-blue-100 ml-1').tooltip('Zmenšit kruh')
                        _kruh_r_lbl = ui.label(f'⌀ {_kr*2} m').classes(
                            'text-xs text-blue-700 font-bold bg-blue-50 px-2 py-1 rounded-lg')
                        ui_refs['kruh_r_lbl'] = _kruh_r_lbl
                        ui.button(icon='add', on_click=lambda: _zmen_r(+1)).props('flat round size=sm').classes('text-blue-700 bg-blue-50 hover:bg-blue-100').tooltip('Zvětšit kruh')

                        if _pocet_kruhu > 0:
                            ui.label(f'{_pocet_kruhu}×').classes('text-xs text-blue-400 font-bold ml-2')

                        _sel_idx = st.get('kruh_selected')
                        if _sel_idx is not None and _sel_idx < len(state['kruhy']):
                            def _smazat_vybrany():
                                _i = st.get('kruh_selected')
                                if _i is not None and _i < len(state['kruhy']):
                                    push_undo('remove_kruh', {'index': _i, 'data': dict(state['kruhy'][_i])})
                                    state['kruhy'].pop(_i)
                                    st['kruh_selected'] = None
                                    st['kruh_aktivni'] = None
                                    update_svg(); save_data()
                                if ui_refs.get('nastroje_panel'):
                                    ui_refs['nastroje_panel'].refresh()
                            ui.button(icon='delete', on_click=_smazat_vybrany).props('flat round size=xs').classes('text-red-500 ml-1').tooltip('Smazat vybraný kruh')

                        def _smazat_vsechny_kruhy():
                            state['kruhy'] = [k for k in state['kruhy'] if k['patro'] != st['aktivni_patro']]
                            st['kruh_aktivni'] = None
                            st['kruh_selected'] = None
                            st['kruh_show_label'] = False
                            update_svg(); save_data()
                            if ui_refs.get('nastroje_panel'):
                                ui_refs['nastroje_panel'].refresh()
                        if _pocet_kruhu > 0:
                            ui.button(icon='delete_sweep', on_click=_smazat_vsechny_kruhy).props('flat round size=xs').classes('text-red-400').tooltip('Smazat všechny kruhy v tomto patře')
                ui_refs['nastroje_panel'] = nastroje_panel
                ui_refs['ladeni_panel_ref'] = ladeni_panel
                nastroje_panel()

                async def _poll_kruh_wheel():
                    if st.get('nastroj') != 'kruh': return
                    try:
                        delta = await ui.run_javascript(
                            'var d=window._kruhWheelDelta||0;window._kruhWheelDelta=0;d',
                            timeout=1.0
                        )
                        if delta:
                            st['kruh_r'] = max(1, min(50, st.get('kruh_r', 8) + int(delta)))
                            # Aktualizovat poloměr vybraného kruhu (nebo posledního na patře)
                            _sel = st.get('kruh_selected')
                            if _sel is not None and _sel < len(state['kruhy']):
                                state['kruhy'][_sel]['r'] = st['kruh_r']
                                _has_kruh = True
                                save_data()
                            else:
                                _kruhy_pat = [k for k in state['kruhy'] if k['patro'] == st['aktivni_patro']]
                                if _kruhy_pat:
                                    _kruhy_pat[-1]['r'] = st['kruh_r']
                                    save_data()
                                _has_kruh = bool(_kruhy_pat)
                            if ui_refs.get('kruh_r_lbl'):
                                ui_refs['kruh_r_lbl'].set_text(f'⌀ {st["kruh_r"]*2} m')
                            if _has_kruh:
                                st['kruh_show_label'] = True
                                update_svg()
                                async def _skryt_label():
                                    await asyncio.sleep(1.5)
                                    st['kruh_show_label'] = False
                                    if state['kruhy']:
                                        update_svg()
                                asyncio.create_task(_skryt_label())
                    except Exception:
                        pass
                ui.timer(0.20, _poll_kruh_wheel)
                ui.label('Patro:').classes('font-bold text-gray-600 ml-2')
                patro_toggle = ui.toggle({k: v['nazev'] for k, v in PATRA.items()}, value=st['aktivni_patro'], on_change=lambda e: zmen_patro(e.value)).classes('bg-gray-100')
                ui_refs['patro_toggle'] = patro_toggle

            def toggle_top_button():
                st['panel_open'] = not st['panel_open']
                if st['panel_open']:
                    right_wrapper.classes(remove='translate-x-full', add='translate-x-0')
                    left_col.classes(remove='w-full', add='w-3/4')
                    btn_toggle.props('icon=chevron_right text-color=dark round size=md')
                else:
                    right_wrapper.classes(remove='translate-x-0', add='translate-x-full')
                    left_col.classes(remove='w-3/4', add='w-full')
                    btn_toggle.props('icon=menu text-color=dark round size=md')
                for s in SEKCE:
                    if f'menu_btn_{s}' in ui_refs: ui_refs[f'menu_btn_{s}'].refresh()

            vychozi_ikona = 'chevron_right' if st['panel_open'] else 'menu'
            btn_toggle = ui.button(icon=vychozi_ikona, color='white', on_click=toggle_top_button).props('text-color=dark round size=md').classes(
                'absolute top-4 right-4 z-50 shadow-xl border border-gray-200'
            ).tooltip('Skrýt/Zobrazit boční panel')

            btn_zpet_legenda = ui.button('⬅ Zpět z legendy', color='blue', on_click=zpet_z_legendy).classes('absolute top-6 left-1/2 -translate-x-1/2 z-50 text-white font-bold shadow-2xl rounded-full px-6 py-2').style('display: none;')
            ui_refs['btn_zpet_legenda'] = btn_zpet_legenda

            with ui.element('div').classes(f'w-full h-full overflow-hidden relative cursor-{st["nastroj"]}').props('id="map-container"') as map_container:
                ui_refs['map_container'] = map_container
                with ui.element('div').props('id="map-export-area"') as zc:
                    ui_refs['zoom_container'] = zc
                    ui_refs['ii'] = ui.interactive_image(PATRA[st['aktivni_patro']]['img'], on_mouse=lambda e: handle_click(e), events=['mousedown', 'mousemove', 'mouseup', 'contextmenu'], cross=False).classes('w-max max-w-none')
                    # position:absolute → vyjmout z flow; will-change:transform → vlastní GPU vrstva
                    zc.style('transform-origin: 0 0; position: absolute; top: 0; left: 0; will-change: transform;')
                    # Inicializace canvas overlaye + sync globálních proměnných JS
                    ui.run_javascript(
                        f'window._veletrh_cs   = {state["cell_size"]};'
                        f'window._vGridOffsetX = {state["grid_offset_x"]};'
                        f'window._vGridOffsetY = {state["grid_offset_y"]};'
                        f'setTimeout(initPaintCanvas, 200);'
                    )

            with ui.column().classes('absolute bottom-8 left-8 z-40 gap-1'):
                # ── Posun mapy (touchpad-friendly) ───────────────────────────
                _ps = 200  # px posunu na klik
                with ui.column().classes('items-center gap-0.5 mb-2'):
                    ui.button(icon='keyboard_arrow_up',
                        on_click=lambda: ui.run_javascript(f'window._mapZoom.panBy(0, {_ps})')
                    ).props('text-color=dark round size=sm').classes('shadow-md bg-white/90 border border-gray-200').tooltip('Posun nahoru')
                    with ui.row().classes('gap-0.5'):
                        ui.button(icon='keyboard_arrow_left',
                            on_click=lambda: ui.run_javascript(f'window._mapZoom.panBy({_ps}, 0)')
                        ).props('text-color=dark round size=sm').classes('shadow-md bg-white/90 border border-gray-200').tooltip('Posun doleva')
                        ui.button(icon='keyboard_arrow_right',
                            on_click=lambda: ui.run_javascript(f'window._mapZoom.panBy(-{_ps}, 0)')
                        ).props('text-color=dark round size=sm').classes('shadow-md bg-white/90 border border-gray-200').tooltip('Posun doprava')
                    ui.button(icon='keyboard_arrow_down',
                        on_click=lambda: ui.run_javascript(f'window._mapZoom.panBy(0, -{_ps})')
                    ).props('text-color=dark round size=sm').classes('shadow-md bg-white/90 border border-gray-200').tooltip('Posun dolů')
                # ── Zoom ─────────────────────────────────────────────────────
                ui.button(icon='zoom_in', color='white', on_click=lambda: set_zoom(0.2)).props('text-color=dark round size=lg').classes('shadow-xl')
                ui.button(icon='zoom_out', color='white', on_click=lambda: set_zoom(-0.2)).props('text-color=dark round size=lg').classes('shadow-xl')

                with ui.button(icon='picture_as_pdf', color='red').props('text-color=white round size=lg').classes('shadow-xl mt-4').tooltip('Stáhnout mapu jako PDF'):
                    with ui.menu().classes('p-2 rounded-xl'):
                        ui.label('Zvolte patro k exportu:').classes('text-xs text-gray-500 font-bold uppercase mb-2 px-2')
                        for pk, pv in PATRA.items():
                            def trigger_export(k=pk):
                                if st['aktivni_patro'] != k:
                                    if ui_refs.get('patro_toggle'):
                                        ui_refs['patro_toggle'].set_value(k)
                                ui.notify(f'Generuji PDF pro {PATRA[k]["nazev"]}...', type='info', icon='hourglass_empty')
                                ui.timer(1.0, lambda k=k: ui.run_javascript(f"stahniMapuPDF('{PATRA[k]['nazev']}')"), once=True)

                            ui.menu_item(pv['nazev'], on_click=trigger_export).classes('font-bold text-gray-800 rounded-lg hover:bg-red-50')

            # --- PLOVOUCÍ VÝBĚROVÁ KARTA --- zobrazí se automaticky při výběru stánků
            with ui.row().classes('absolute bottom-8 left-1/2 z-50 items-center gap-3 bg-white shadow-2xl rounded-full px-5 py-3 border-2 border-orange-400') as sel_overlay:
                sel_overlay.style('display: none; transform: translateX(-50%); pointer-events: auto;')
                lbl_overlay_m2 = ui.label('0 m² vybráno').classes('font-bold text-orange-700 whitespace-nowrap')
                ui.button('Rezervovat', icon='check_circle',
                    on_click=lambda: ui_refs.get('_toggle_panel_fn') and ui_refs['_toggle_panel_fn']('Rezervace stánků')
                ).classes('bg-green-600 text-white font-bold rounded-full text-sm').props('no-caps')
                ui.button(icon='close',
                    on_click=lambda: [st['vybrane_pro_rezervaci'].clear(), update_svg()]
                ).props('flat round size=sm').classes('text-gray-500')
            ui_refs['sel_overlay'] = sel_overlay
            ui_refs['lbl_overlay_m2'] = lbl_overlay_m2

        # --- PRAVÁ STRANA: VYSOUVACÍ PANEL ---
        panel_pos = 'translate-x-0' if st['panel_open'] else 'translate-x-full'
        right_wrapper = ui.element('div').classes(f'absolute top-0 right-0 w-1/4 h-full bg-white shadow-2xl transition-transform duration-300 z-50 flex flex-col border-l border-gray-300 {panel_pos}')

        with right_wrapper:

            def toggle_panel(target_sekce=None):
                if target_sekce:
                    st['aktivni_sekce'] = target_sekce
                    if not st['panel_open']:
                        st['panel_open'] = True
                        right_wrapper.classes(remove='translate-x-full', add='translate-x-0')
                        left_col.classes(remove='w-full', add='w-3/4')
                        btn_toggle.props('icon=chevron_right text-color=dark round size=md')
                else:
                    st['panel_open'] = not st['panel_open']
                    if st['panel_open']:
                        right_wrapper.classes(remove='translate-x-full', add='translate-x-0')
                        left_col.classes(remove='w-full', add='w-3/4')
                        btn_toggle.props('icon=chevron_right text-color=dark round size=md')
                    else:
                        right_wrapper.classes(remove='translate-x-0', add='translate-x-full')
                        left_col.classes(remove='w-3/4', add='w-full')
                        btn_toggle.props('icon=menu text-color=dark round size=md')

                if ui_refs.get('panely_obsah'):
                    ui_refs['panely_obsah'].set_value(st['aktivni_sekce'])

                for s in SEKCE:
                    if f'menu_btn_{s}' in ui_refs: ui_refs[f'menu_btn_{s}'].refresh()

            ui_refs['_toggle_panel_fn'] = toggle_panel

            # 1. OUŠKA
            menu_container = ui.column().classes('absolute top-1/2 -translate-y-1/2 right-full z-50 flex flex-col gap-2 p-0 m-0')
            with menu_container:
                def vytvor_tlacitko_menu(nazev_sekce):
                    @ui.refreshable
                    def btn_ui():
                        is_active = st['aktivni_sekce'] == nazev_sekce and st['panel_open']
                        bg_col = '#2563eb' if is_active else '#4b5563'
                        css_class = 'text-white cursor-pointer items-center justify-end shadow-xl transition-all duration-300 ' + ('' if is_active else 'hover:bg-gray-600')

                        with ui.row().classes(css_class).style(f'background-color: {bg_col}; border-radius: 20px 0 0 20px; padding: 14px 20px; width: 240px; margin: 0;').on('click', lambda: toggle_panel(target_sekce=nazev_sekce)):
                            ui.label(nazev_sekce.upper()).classes('font-bold text-xs tracking-widest whitespace-nowrap w-full text-right')

                    btn_ui()
                    ui_refs[f'menu_btn_{nazev_sekce}'] = btn_ui

                for s in SEKCE:
                    vytvor_tlacitko_menu(s)

            # 2. Vlastní scrollovací obsah panelu
            with ui.column().classes('w-full h-full overflow-y-auto p-8 bg-white shadow-2xl border-l border-gray-300'):

                with ui.row().classes('w-full justify-between items-center mb-2 border-b-2 border-blue-100 pb-4'):
                    ui.label('Veletrh JIP 2027').classes('text-3xl font-black text-blue-800')
                    ui.button(icon='close', on_click=lambda: toggle_panel()).props('flat round').classes('text-gray-500 hover:bg-gray-100')

                with ui.tab_panels(value=st['aktivni_sekce']).classes('w-full h-full bg-transparent p-0 mt-4').props('animated=false') as panely_obsah:
                    ui_refs['panely_obsah'] = panely_obsah

                    # --- TAB INFO ---
                    with ui.tab_panel('Info'):
                        ui.label('Základní informace').classes('font-bold text-xl mb-6 text-gray-800')

                        ui.label('Legenda mapy a stánků').classes('font-bold text-sm mb-4 text-gray-500 uppercase')
                        with ui.row().classes('items-center mb-2 text-sm bg-gray-50 p-2 rounded w-full'):
                            ui.element('div').classes('w-4 h-4 bg-[#86efac] border border-[#16a34a] mr-3 rounded-sm')
                            ui.label('Volný stánek k výběru')
                        with ui.row().classes('items-center mb-6 text-sm bg-gray-50 p-2 rounded w-full'):
                            ui.element('div').classes('w-4 h-4 bg-[#f59e0b] border border-[#b45309] mr-3 rounded-sm')
                            ui.label('Váš rozpracovaný výběr')

                        with ui.row().classes('items-center mb-2 text-sm bg-green-50 p-3 rounded-lg w-full justify-between border border-green-100'):
                            ui.label('Zarezervováno: Gastro').classes('font-bold text-green-800')
                            ui.label('🍴').style('color: #064e3b; font-size: 1.4em; background-color: #6ee7b7; padding: 2px 6px; border-radius: 4px;')
                        with ui.row().classes('items-center mb-2 text-sm bg-blue-50 p-3 rounded-lg w-full justify-between border border-blue-100'):
                            ui.label('Zarezervováno: Retail').classes('font-bold text-blue-800')
                            ui.label('🍷').style('color: #1e3a8a; font-size: 1.4em; background-color: #93c5fd; padding: 2px 6px; border-radius: 4px;')

                        if st['role'] == 'admin':
                            with ui.row().classes('items-center mb-8 text-sm bg-yellow-50 p-3 rounded-lg w-full justify-between border border-yellow-100'):
                                ui.label('Zarezervováno: JIP').classes('font-bold text-yellow-800')
                                ui.label('JIP').style('color: #713f12; font-size: 1em; font-weight: bold; background-color: #fef08a; padding: 4px 6px; border-radius: 4px;')

                        def ukaz_legendu():
                            st['pre_legenda_zoom'] = st['zoom']

                            pos = PATRA[st['aktivni_patro']]['legenda_pos']
                            st['zoom'] = pos['zoom']
                            # zoomToContent() níže provede zoom i posun — syncFromPython není potřeba

                            st['nastroj'] = 'packa'
                            st['temp_p1'] = None
                            st['selected_batch'] = None
                            ui.run_javascript("window.current_tool = 'packa';")
                            if ui_refs.get('nastroje_panel'): ui_refs['nastroje_panel'].refresh()
                            if ui_refs.get('map_container'):
                                ui_refs['map_container'].classes(remove='cursor-packa cursor-kresleni cursor-oblast cursor-ladeni cursor-vyber')
                                ui_refs['map_container'].classes(add='cursor-packa')

                            if st['panel_open']:
                                st['panel_open'] = False
                                right_wrapper.classes(remove='translate-x-full', add='translate-x-0')
                                left_col.classes(remove='w-full', add='w-3/4')
                                btn_toggle.props('icon=menu text-color=dark round size=md')
                                for s in SEKCE:
                                    if f'menu_btn_{s}' in ui_refs: ui_refs[f'menu_btn_{s}'].refresh()

                            js_code = (
                                f'window._mapZoom.savePan();'
                                f'window._mapZoom.zoomToContent({pos["x"]}, {pos["y"]}, {pos["zoom"]});'
                            )
                            ui.run_javascript(js_code)

                            if ui_refs.get('btn_zpet_legenda'):
                                ui_refs['btn_zpet_legenda'].style('display: block;')

                        with ui.card().classes('w-full bg-white border border-gray-200 p-4 shadow-sm'):
                            ui.label('Architektonická legenda').classes('font-bold text-gray-800 mb-2')
                            ui.label('Kliknutím na tlačítko níže se mapa přesune přímo na legendu výkresu daného patra.').classes('text-xs text-gray-500 mb-4')
                            ui.button('🔍 Zobrazit legendu na mapě', on_click=ukaz_legendu).classes('w-full bg-gray-800 hover:bg-gray-700 text-white font-bold shadow-md h-12')

                    # --- TAB ROZMÍSTĚNÍ STÁNKŮ ---
                    with ui.tab_panel('Rozmístění stánků'):
                        ui.label('Editace ploch (Admin)').classes('font-bold text-xl mb-6 text-gray-800')

                        if st['role'] == 'admin':
                            ui.label('Nástroje pro kalibraci a správu sítě.').classes('text-sm text-gray-500 mb-6')

                            ui.number('Velikost 1 m² na mapě (px)', value=state['cell_size'], step=0.5, on_change=lambda e: update_var('cell_size', e.value)).classes('w-full mb-4').props('outlined')

                            # ── Kalibrace mřížky (posun origin) ───────────────────────────────
                            ui.separator().classes('my-2')
                            ui.label('Kalibrace posunu mřížky').classes('font-bold text-sm text-gray-700 mt-2 mb-1')
                            ui.label('Pokud čtverce nesedí na půdorysu, upravte posun osy X a Y, nebo klikněte "Zarovnat" a poté na střed libovolné buňky.').classes('text-xs text-gray-400 mb-3')
                            ui.label('⚠️ Změna offsetu přesune všechny stávající stánky na novou mřížku.').classes('text-xs text-orange-500 mb-3')

                            with ui.row().classes('w-full gap-1 items-center mb-2'):
                                ox_input = ui.number('Posun X (px)', value=state['grid_offset_x'], step=0.5, format='%.2f',
                                                     on_change=lambda e: update_var('grid_offset_x', e.value)).classes('flex-1').props('outlined dense')
                                def _ox_minus():
                                    nv = round(state['grid_offset_x'] - 0.5, 4)
                                    ox_input.set_value(nv); update_var('grid_offset_x', nv)
                                def _ox_plus():
                                    nv = round(state['grid_offset_x'] + 0.5, 4)
                                    ox_input.set_value(nv); update_var('grid_offset_x', nv)
                                ui.button(icon='remove', on_click=_ox_minus).classes('w-9 h-9 bg-white border text-gray-700').props('flat dense')
                                ui.button(icon='add',    on_click=_ox_plus ).classes('w-9 h-9 bg-white border text-gray-700').props('flat dense')

                            with ui.row().classes('w-full gap-1 items-center mb-4'):
                                oy_input = ui.number('Posun Y (px)', value=state['grid_offset_y'], step=0.5, format='%.2f',
                                                     on_change=lambda e: update_var('grid_offset_y', e.value)).classes('flex-1').props('outlined dense')
                                def _oy_minus():
                                    nv = round(state['grid_offset_y'] - 0.5, 4)
                                    oy_input.set_value(nv); update_var('grid_offset_y', nv)
                                def _oy_plus():
                                    nv = round(state['grid_offset_y'] + 0.5, 4)
                                    oy_input.set_value(nv); update_var('grid_offset_y', nv)
                                ui.button(icon='remove', on_click=_oy_minus).classes('w-9 h-9 bg-white border text-gray-700').props('flat dense')
                                ui.button(icon='add',    on_click=_oy_plus ).classes('w-9 h-9 bg-white border text-gray-700').props('flat dense')

                            def aktivovat_zarovnat():
                                # Přepnout na kreslit NEJDŘÍVE (zmen_nastroj by smazal align_mode)
                                if st['nastroj'] not in ['kresleni', 'krizek', 'jip', 'oblast']:
                                    st['nastroj'] = 'kresleni'
                                st['align_mode'] = True  # musí být až po zmen_nastroj
                                ui.notify('🎯 Klikněte na mapu přesně do středu buňky půdorysu — mřížka se zarovná na tento bod.', type='info', position='top', timeout=6000)

                            ui.button('🎯 Zarovnat kliknutím na mapu', on_click=aktivovat_zarovnat).classes('w-full h-10 font-bold bg-indigo-600 text-white mb-6')
                            ui.separator().classes('my-2')

                            def smazat_vse_volne():
                                volne = [s for s in state['stanky'] if s['patro'] == st['aktivni_patro'] and s['res_id'] is None]
                                push_undo('remove_stanky', [dict(s) for s in volne])
                                state['stanky'] = [s for s in state['stanky'] if s['patro'] != st['aktivni_patro'] or s['res_id'] is not None]
                                _rebuild_all()
                                st['vybrane_pro_rezervaci'].clear()
                                update_svg(); save_data()
                                ui.notify(f'Smazáno {len(volne)} volných stánků. Ctrl+Z pro vrácení.', type='warning')

                            async def potvrdit_smazat():
                                volne = [s for s in state['stanky'] if s['patro'] == st['aktivni_patro'] and s['res_id'] is None]
                                if not volne:
                                    ui.notify('Žádné volné stánky k smazání.', type='info')
                                    return
                                patro_nazev = PATRA[st['aktivni_patro']]['nazev']
                                with ui.dialog() as dlg, ui.card().classes('p-6 rounded-2xl max-w-sm'):
                                    ui.label('Potvrdit smazání').classes('text-xl font-bold text-red-700 mb-2')
                                    ui.label(f'Bude odstraněno {len(volne)} volných stánků v patře {patro_nazev}. Akci lze vrátit přes Ctrl+Z.').classes('text-sm text-gray-600 mb-6')
                                    with ui.row().classes('gap-3 w-full'):
                                        ui.button('Zrušit', on_click=dlg.close).classes('flex-1 h-12 font-bold')
                                        ui.button(f'Smazat {len(volne)}', icon='delete', color='red',
                                            on_click=lambda: (smazat_vse_volne(), dlg.close())
                                        ).classes('flex-1 h-12 font-bold')
                                dlg.open()

                            ui.label('Hromadné akce:').classes('font-bold text-sm mb-2 text-gray-700')
                            ui.button('🗑️ Smazat POUZE volné stánky (v tomto patře)', color='red', on_click=potvrdit_smazat).classes('w-full font-bold h-12 shadow-sm')
                        else:
                            ui.label('Tato sekce je vyhrazena pouze pro správce veletrhu. Ostatní uživatelé nemohou měnit stavební dispozice.').classes('text-red-500 italic mt-4 bg-red-50 p-4 rounded-lg border border-red-200')

                    # --- TAB REZERVACE ---
                    with ui.tab_panel('Rezervace stánků'):
                        ui.label('Nová rezervace').classes('font-bold text-xl mb-4 text-gray-800')

                        if st['role'] in ['admin', 'uzivatel']:
                            with ui.row().classes('w-full justify-between items-center mb-2 bg-yellow-50 p-4 rounded-xl border border-yellow-200'):
                                ui.label('Vybraná plocha:').classes('text-sm text-yellow-800 font-bold uppercase')
                                ui_refs['lbl_vybrano'] = ui.label('0 m²').classes('text-3xl font-black text-yellow-600')

                            with ui.row().classes('w-full justify-between items-center mb-6 bg-green-50 p-4 rounded-xl border border-green-200'):
                                ui.label('Cena celkem (vč. služeb):').classes('text-sm text-green-800 font-bold uppercase')
                                ui_refs['lbl_cena_celkem'] = ui.label('0 Kč').classes('text-3xl font-black text-green-600')

                            ui.label('1) Typ stánku:').classes('font-bold text-sm text-gray-700')
                            moznosti_typu = ['Gastro', 'Retail']
                            f_typ = ui.radio(moznosti_typu, value='Gastro', on_change=aktualizuj_cenu_v_panelu).classes('mb-6 w-full bg-gray-50 p-2 rounded')
                            ui_refs['f_typ'] = f_typ

                            f_nazev = ui.input('Název dodavatele').classes('w-full mb-3').props('outlined dense')
                            f_ico = ui.input('IČO dodavatele').classes('w-full mb-3').props('outlined dense')
                            f_adresa = ui.input('Adresa').classes('w-full mb-3').props('outlined dense')
                            f_banka = ui.input('Bankovní spojení').classes('w-full mb-6').props('outlined dense')

                            ui.label('2) Služby a připojení:').classes('font-bold text-sm text-gray-700 mb-2')

                            f_ele = ui.select({k: f"{v['nazev']} ({v['cena']} Kč)" for k,v in ELEKTRO_CENIK.items()}, value="0", label="Elektrika", on_change=aktualizuj_cenu_v_panelu).classes('w-full mb-3').props('outlined dense')
                            ui_refs['f_ele'] = f_ele

                            f_voda = ui.select({k: f"{v['nazev']} ({v['cena']} Kč)" for k,v in VODA_CENIK.items()}, value="0", label="Voda", on_change=aktualizuj_cenu_v_panelu).classes('w-full mb-8').props('outlined dense')
                            ui_refs['f_voda'] = f_voda

                            def proved_rezervaci():
                                if not st['vybrane_pro_rezervaci']: return ui.notify('Nejprve naklikejte stánky nástrojem "Výběr"!', type='warning')

                                typ_rez = f_typ.value
                                if typ_rez in ['Gastro', 'Retail'] and not f_nazev.value:
                                    return ui.notify('Doplňte název dodavatele!', type='warning')

                                res_id = str(uuid.uuid4())
                                stanky_ids = list(st['vybrane_pro_rezervaci'])
                                finalni_nazev = f_nazev.value if f_nazev.value else typ_rez

                                state['rezervace'][res_id] = {'nazev': finalni_nazev, 'ico': f_ico.value, 'adresa': f_adresa.value, 'banka': f_banka.value, 'typ': typ_rez, 'elektro': f_ele.value, 'voda': f_voda.value, 'stanky_ids': stanky_ids}
                                for s_id in stanky_ids:
                                    s = state['_stanky_by_id'].get(s_id)
                                    if s: _res_idx_set(s, res_id)
                                push_undo('add_rezervace', {'res_id': res_id})
                                st['vybrane_pro_rezervaci'].clear()
                                ui.notify('Úspěšně uloženo do mapy!', type='positive')
                                f_nazev.value, f_ico.value, f_adresa.value, f_banka.value, f_ele.value, f_voda.value = '', '', '', '', "0", "0"
                                update_svg()
                                save_data()
                                if ui_refs.get('seznam_rezervaci_ui'): ui_refs['seznam_rezervaci_ui'].refresh()

                            ui.button('Uložit a potvrdit rezervaci', icon='check_circle', on_click=proved_rezervaci).classes('w-full bg-green-600 text-white font-bold min-h-[56px] py-3 shadow-lg rounded-xl text-base hover:bg-green-700 mb-8').style('white-space: normal; line-height: 1.2;')
                        else:
                            ui.label('Pro vytváření rezervací nemáte oprávnění.').classes('text-red-500 italic mt-4 bg-red-50 p-4 rounded-lg')

                    # --- TAB REZERVOVANÉ STÁNKY ---
                    with ui.tab_panel('Rezervované stánky'):
                        ui.label('Seznam rezervovaných stánků').classes('font-bold text-xl mb-6 text-gray-800')

                        if st['role'] in ['admin', 'uzivatel']:
                            filtr_rez = {'q': ''}
                            filtr_input = ui.input(placeholder='🔍 Hledat dodavatele nebo IČO...').classes('w-full mb-3').props('outlined dense clearable')

                            @ui.refreshable
                            def seznam_rezervaci_ui():
                                q = filtr_rez['q'].lower().strip()
                                if not state['rezervace']:
                                    ui.label('Zatím nejsou žádné stánky zarezervovány.').classes('italic text-gray-500')
                                    return

                                rezervace_sorted = sorted(state['rezervace'].items(), key=lambda item: item[1]['nazev'].lower())
                                rezervace_komercni = [(r_id, r) for r_id, r in rezervace_sorted if r['typ'] not in ['Křížek (Blokováno)', 'JIP Zóna']]

                                if not rezervace_komercni:
                                    ui.label('Zatím nejsou žádné komerční stánky zarezervovány.').classes('italic text-gray-500')
                                    return

                                filtrovane = [(r_id, r) for r_id, r in rezervace_komercni
                                              if not q or q in r['nazev'].lower() or q in r.get('ico', '').lower()]

                                if not filtrovane:
                                    ui.label(f'Žádný dodavatel neodpovídá hledání „{filtr_rez["q"]}".').classes('italic text-gray-400 text-sm')
                                    return

                                with ui.column().classes('w-full gap-2'):
                                    for r_id, r in filtrovane:
                                        m2 = len(r['stanky_ids'])
                                        with ui.card().classes('w-full p-0 border border-gray-200 shadow-sm rounded-lg overflow-hidden'):
                                            with ui.row().classes('w-full justify-between items-center p-3 cursor-pointer hover:bg-blue-50 transition-colors').on('click', lambda r_id=r_id: najdi_stanek(r_id)):
                                                with ui.row().classes('items-center gap-3'):
                                                    ikonka = "🍴" if r['typ'] == 'Gastro' else "🍷"
                                                    barva = "bg-green-100 text-green-800" if r['typ'] == 'Gastro' else "bg-blue-100 text-blue-800"

                                                    ui.label(ikonka).classes(f'text-lg p-2 rounded-full {barva}')
                                                    ui.label(r['nazev']).classes('font-bold text-gray-800 text-lg')
                                                ui.label(f"{m2} m²").classes('font-black text-gray-600 bg-gray-100 px-3 py-1 rounded-lg')

                                            with ui.row().classes('w-full p-3 bg-gray-50 border-t border-gray-100'):
                                                ui.textarea(label='Interní poznámka (uloží se automaticky po kliknutí vedle)', value=r.get('interni_poznamka', '')).classes('w-full bg-yellow-50/50').props('outlined').on('blur', lambda e, sid=r_id: uloz_poznamku_stanku(sid, e.sender.value))

                            seznam_rezervaci_ui()
                            ui_refs['seznam_rezervaci_ui'] = seznam_rezervaci_ui

                            def _on_filtr(e):
                                filtr_rez['q'] = e.value or ''
                                seznam_rezervaci_ui.refresh()
                            filtr_input.on('input', _on_filtr)
                            filtr_input.on('clear', lambda: [filtr_rez.update({'q': ''}), seznam_rezervaci_ui.refresh()])
                        else:
                            ui.label('Pro zobrazení seznamu nemáte oprávnění.').classes('text-red-500 italic mt-4 bg-red-50 p-4 rounded-lg')

                    # --- TAB KAPACITA ---
                    with ui.tab_panel('Kapacita'):
                        ui.label('Statistika a Data').classes('font-bold text-xl mb-6 text-gray-800')

                        if st['role'] == 'admin':
                            ui.number('Cena za 1 m² (Kč)', value=state['cena_m2'], format='%.2f', on_change=lambda e: update_var('cena_m2', e.value)).classes('w-full mb-8').props('outlined step="any"')

                        ui_refs['stat_kontejner'] = ui.column().classes('w-full gap-0 mb-8')
                        obnov_statistiky()

                        if st['role'] == 'admin':
                            ui.label('Archiv smluv').classes('font-bold text-xl mb-4 text-gray-800 border-t border-gray-200 pt-6')
                            archiv_kontejner = ui.column().classes('w-full gap-3')

                            @ui.refreshable
                            def vykresli_archiv():
                                archiv_kontejner.clear()
                                smlouvy = intranet_data.ziskej_smlouvy_veletrh()
                                with archiv_kontejner:
                                    if not smlouvy:
                                        ui.label('Zatím nebyly vygenerovány žádné smlouvy.').classes('text-gray-500 italic')
                                    for s in smlouvy:
                                        with ui.card().classes('w-full p-4 shadow-sm border border-blue-100 bg-blue-50/30 rounded-xl'):
                                            ui.label(s['dodavatel']).classes('font-bold text-blue-900 text-lg')
                                            ui.label(f"IČO: {s['ico']} | Vytvořeno: {s['vytvoreno'].strftime('%d.%m.%Y %H:%M')}").classes('text-xs text-gray-600 mb-3')

                                            async def stahni_soubor(cesta, typ):
                                                cesta_docx = cesta.replace('.pdf', '.docx')
                                                cesta_pdf = cesta.replace('.docx', '.pdf')

                                                if typ == 'docx':
                                                    if os.path.exists(cesta_docx):
                                                        ui.download(cesta_docx)
                                                    else:
                                                        ui.notify('Word soubor fyzicky neexistuje na serveru.', type='negative')
                                                elif typ == 'pdf':
                                                    if os.path.exists(cesta_pdf):
                                                        ui.download(cesta_pdf)
                                                    elif os.path.exists(cesta_docx):
                                                        ui.notify('Generuji PDF, malý moment...', type='info')

                                                        def _run_lo():
                                                            slozka = os.path.dirname(cesta_docx)
                                                            absolutni_slozka = os.path.abspath(slozka)
                                                            lo_exec = shutil.which('libreoffice') or shutil.which('soffice') or '/usr/bin/libreoffice'
                                                            tmp_profile = f"-env:UserInstallation=file:///tmp/lo_profile_{uuid.uuid4()}"

                                                            custom_env = os.environ.copy()
                                                            custom_env["PATH"] = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:" + custom_env.get("PATH", "")

                                                            return subprocess.run(
                                                                [lo_exec, tmp_profile, '--headless', '--convert-to', 'pdf', '--outdir', absolutni_slozka, cesta_docx],
                                                                capture_output=True, text=True, env=custom_env
                                                            )

                                                        try:
                                                            proces = await asyncio.to_thread(_run_lo)
                                                            if proces.returncode != 0:
                                                                print(f"Chyba LibreOffice: {proces.stderr}")
                                                                ui.notify(f'Chyba převodu: {proces.stderr[:100]}', type='negative', timeout=8000)
                                                            elif os.path.exists(cesta_pdf):
                                                                ui.download(cesta_pdf)
                                                            else:
                                                                ui.notify('Převod do PDF selhal.', type='negative')
                                                        except Exception as err:
                                                            ui.notify(f'Systémová chyba: {err}', type='negative')
                                                    else:
                                                        ui.notify('Zdrojový Word soubor neexistuje, nelze vytvořit PDF.', type='negative')

                                            def smazat_smlouvu(sid):
                                                if intranet_data.smaz_smlouvu_veletrh(sid):
                                                    ui.notify('Smlouva odstraněna ze serveru i z databáze.', type='info')
                                                    vykresli_archiv.refresh()

                                            with ui.row().classes('gap-2 w-full mt-2'):
                                                ui.button('Word', icon='description', on_click=lambda c=s['cesta_k_souboru']: asyncio.create_task(stahni_soubor(c, 'docx'))).classes('bg-blue-600 text-white font-bold flex-1 h-10')
                                                ui.button('PDF', icon='picture_as_pdf', on_click=lambda c=s['cesta_k_souboru']: asyncio.create_task(stahni_soubor(c, 'pdf'))).classes('bg-red-600 text-white font-bold flex-1 h-10')
                                                ui.button(icon='delete', color='red', on_click=lambda sid=s['id']: smazat_smlouvu(sid)).classes('h-10 px-4')
                            vykresli_archiv()
                            ui_refs['vykresli_archiv'] = vykresli_archiv

                    # --- TAB ZÁKAZNÍCI (ADMIN + UZIVATEL) ---
                    if st['role'] in ['admin', 'uzivatel']:
                        with ui.tab_panel('Zákazníci'):
                            ui.label('Registrace zákazníků').classes('font-bold text-xl mb-2 text-gray-800')
                            ui.label('Evidence zákazníků pozvaných na veletrh. Přidávat záznamy může správce veletrhu a komentátor.').classes('text-sm text-gray-500 mb-4')

                            filtr_zak = {'q': ''}
                            filtr_zak_input = ui.input(placeholder='🔍 Hledat jméno, firmu, IČ, OZ…').classes('w-full mb-3').props('outlined dense clearable')

                            @ui.refreshable
                            def vykresli_zakazniky():
                                q = filtr_zak['q'].lower().strip()
                                zakaznici = state.get('zakaznici', [])
                                if not zakaznici:
                                    ui.label('Zatím nejsou evidováni žádní zákazníci.').classes('italic text-gray-500')
                                    return
                                filtrovani = [z for z in zakaznici if not q or any(
                                    q in str(z.get(k, '')).lower()
                                    for k in ('jmeno', 'firma', 'ic', 'oz', 'asm', 'tel')
                                )]
                                if not filtrovani:
                                    ui.label(f'Žádný zákazník neodpovídá hledání „{filtr_zak["q"]}".').classes('italic text-gray-400 text-sm')
                                    return
                                with ui.column().classes('w-full gap-2'):
                                    for z in filtrovani:
                                        with ui.card().classes('w-full p-4 border border-gray-200 shadow-sm rounded-xl'):
                                            with ui.row().classes('w-full justify-between items-start'):
                                                with ui.column().classes('flex-1 gap-0.5'):
                                                    ui.label(z.get('jmeno', '')).classes('font-bold text-gray-800 text-base')
                                                    if z.get('firma'):
                                                        ui.label(f"🏢 {z['firma']}").classes('text-sm text-gray-600')
                                                    if z.get('ic'):
                                                        ui.label(f"IČ: {z['ic']}").classes('text-xs text-gray-500')
                                                    with ui.row().classes('gap-3 mt-1 flex-wrap'):
                                                        if z.get('tel'):
                                                            ui.label(f"📞 {z['tel']}").classes('text-sm text-blue-700')
                                                        if z.get('oz'):
                                                            ui.label(f"OZ: {z['oz']}").classes('text-sm text-gray-600')
                                                        if z.get('asm'):
                                                            ui.label(f"ASM: {z['asm']}").classes('text-sm text-gray-600')
                                                    ui.label(z.get('datum', '')).classes('text-xs text-gray-400 mt-1')
                                                if st['role'] == 'admin':
                                                    with ui.column().classes('gap-1 ml-2'):
                                                        ui.button(icon='edit', on_click=lambda z=z: otevri_dialog_zakaznika(z)).props('flat round size=sm').classes('text-blue-500').tooltip('Upravit')
                                                        ui.button(icon='delete', on_click=lambda zid=z['id']: smaz_zakaznika(zid)).props('flat round size=sm').classes('text-red-500').tooltip('Smazat')

                            def otevri_dialog_zakaznika(zakaznik=None):
                                je_edit = zakaznik is not None
                                with ui.dialog() as dlg_zak, ui.card().classes('w-96 p-6 rounded-2xl shadow-2xl'):
                                    ui.label('Upravit zákazníka' if je_edit else 'Přidat zákazníka').classes('text-xl font-bold mb-4 text-gray-800')
                                    inp_jmeno  = ui.input('Jméno',                   value=zakaznik.get('jmeno','')  if je_edit else '').classes('w-full mb-2').props('outlined dense')
                                    inp_tel    = ui.input('Tel. číslo',              value=zakaznik.get('tel','')    if je_edit else '').classes('w-full mb-2').props('outlined dense')
                                    inp_ic     = ui.input('IČ firmy',               value=zakaznik.get('ic','')     if je_edit else '').classes('w-full mb-2').props('outlined dense')
                                    inp_firma  = ui.input('Firma (název)',           value=zakaznik.get('firma','')  if je_edit else '').classes('w-full mb-2').props('outlined dense')
                                    inp_oz     = ui.input('OZ (obch. zástupce)',    value=zakaznik.get('oz','')     if je_edit else '').classes('w-full mb-2').props('outlined dense')
                                    inp_asm    = ui.input('ASM (area sales mgr.)',  value=zakaznik.get('asm','')    if je_edit else '').classes('w-full mb-4').props('outlined dense')

                                    def _uloz():
                                        if not inp_jmeno.value or not inp_jmeno.value.strip():
                                            ui.notify('Jméno je povinné!', type='warning')
                                            return
                                        if je_edit:
                                            zakaznik.update({
                                                'jmeno': inp_jmeno.value.strip(),
                                                'tel':   inp_tel.value.strip(),
                                                'ic':    inp_ic.value.strip(),
                                                'firma': inp_firma.value.strip(),
                                                'oz':    inp_oz.value.strip(),
                                                'asm':   inp_asm.value.strip(),
                                            })
                                        else:
                                            state['zakaznici'].append({
                                                'id':    str(uuid.uuid4()),
                                                'jmeno': inp_jmeno.value.strip(),
                                                'tel':   inp_tel.value.strip(),
                                                'ic':    inp_ic.value.strip(),
                                                'firma': inp_firma.value.strip(),
                                                'oz':    inp_oz.value.strip(),
                                                'asm':   inp_asm.value.strip(),
                                                'datum': datetime.datetime.now().strftime('%d.%m.%Y %H:%M'),
                                            })
                                        save_data()
                                        ui.notify('Zákazník upraven!' if je_edit else 'Zákazník přidán!', type='positive')
                                        dlg_zak.close()
                                        vykresli_zakazniky.refresh()

                                    with ui.row().classes('w-full gap-3'):
                                        ui.button('Zrušit', on_click=dlg_zak.close).classes('flex-1 h-12 font-bold')
                                        ui.button('Uložit', icon='save', color='blue', on_click=_uloz).classes('flex-1 h-12 font-bold')
                                dlg_zak.open()

                            def smaz_zakaznika(zid):
                                state['zakaznici'] = [z for z in state.get('zakaznici', []) if z['id'] != zid]
                                save_data()
                                ui.notify('Zákazník odstraněn.', type='info')
                                vykresli_zakazniky.refresh()

                            async def exportuj_zakazniky():
                                zakaznici = state.get('zakaznici', [])
                                if not zakaznici:
                                    ui.notify('Nejsou evidováni žádní zákazníci k exportu.', type='warning', position='top')
                                    return
                                try:
                                    # Stavba Excelu v PROCESU – neblokuje ostatní uživatele.
                                    cesta = await intranet_jobs.cpu(build_zakaznici_veletrh_xlsx, zakaznici)
                                    ui.download(cesta)
                                    ui.notify(f'Export {len(zakaznici)} zákazníků byl stažen.', type='positive', position='top')
                                except Exception as e:
                                    ui.notify(f'Chyba exportu: {e}', type='negative', position='top')

                            with ui.row().classes('w-full gap-3 mb-4'):
                                if st['role'] in ['admin', 'komentator']:
                                    ui.button('➕ Přidat zákazníka', icon='person_add',
                                              on_click=lambda: otevri_dialog_zakaznika()
                                    ).classes('flex-1 bg-blue-600 text-white font-bold min-h-[56px] shadow-md rounded-xl text-base hover:bg-blue-700')
                                ui.button('Export do Excelu', icon='download',
                                          on_click=exportuj_zakazniky
                                ).classes('flex-1 bg-green-600 text-white font-bold min-h-[56px] shadow-md rounded-xl text-base hover:bg-green-700')

                            vykresli_zakazniky()

                            def _on_filtr_zak(e):
                                filtr_zak['q'] = e.value or ''
                                vykresli_zakazniky.refresh()
                            filtr_zak_input.on('input', _on_filtr_zak)
                            filtr_zak_input.on('clear', lambda: [filtr_zak.update({'q': ''}), vykresli_zakazniky.refresh()])

                    # --- TAB POZNÁMKY (ADMIN ONLY) ---
                    if st['role'] == 'admin':
                        with ui.tab_panel('Poznámky'):
                            ui.label('Interní poznámky (Admin)').classes('font-bold text-xl mb-4 text-gray-800')
                            ui.label('Tento prostor slouží čistě pro organizátory a správce veletrhu. Nikdo jiný ho nevidí.').classes('text-sm text-gray-500 mb-4')

                            novy_text = ui.textarea(label='Napsat novou poznámku...').classes('w-full mb-2').props('outlined')

                            def pridej_poznamku():
                                if not novy_text.value or not novy_text.value.strip(): return
                                nova = {
                                    'id': str(uuid.uuid4()),
                                    'text': novy_text.value.strip(),
                                    'datum': datetime.datetime.now().strftime('%d.%m.%Y %H:%M')
                                }
                                state['poznamky'].append(nova)
                                save_data()
                                novy_text.value = ''
                                ui.notify('Poznámka uložena!', type='positive')
                                vykresli_poznamky.refresh()

                            ui.button('Přidat poznámku', icon='add', on_click=pridej_poznamku).classes('w-full bg-gray-800 hover:bg-gray-700 text-white font-bold min-h-[56px] shadow-md rounded-xl text-base mb-6')

                            ui.label('Historie poznámek').classes('font-bold text-lg mb-2 text-gray-700 border-b border-gray-200 pb-2 w-full')

                            @ui.refreshable
                            def vykresli_poznamky():
                                with ui.column().classes('w-full gap-3'):
                                    if not state['poznamky']:
                                        ui.label('Zatím nemáte žádné poznámky.').classes('italic text-gray-500')
                                    else:
                                        for p in reversed(state['poznamky']):
                                            with ui.card().classes('w-full bg-yellow-50/60 border border-yellow-200 shadow-sm p-3'):
                                                with ui.row().classes('w-full justify-between items-start mb-1'):
                                                    ui.label(p.get('datum', '')).classes('text-xs text-gray-500 font-bold')
                                                    ui.button(icon='delete', color='red', on_click=lambda pid=p['id']: smaz_poznamku(pid)).props('flat dense size=sm').classes('p-0 m-0 -mt-1 -mr-1')
                                                ui.label(p.get('text', '')).classes('text-gray-800 text-sm whitespace-pre-wrap')

                            def smaz_poznamku(pid):
                                state['poznamky'] = [p for p in state['poznamky'] if p.get('id') != pid]
                                save_data()
                                ui.notify('Poznámka smazána.', type='info')
                                vykresli_poznamky.refresh()

                            vykresli_poznamky()

    # ==========================================
    # ⚙️ DIALOG PRO ÚPRAVU EXISTUJÍCÍ REZERVACE
    # ==========================================
    with ui.dialog() as ui_refs['detail_dialog'], ui.card().classes('w-96 p-6 rounded-2xl'):
        ui.label('Správa plochy').classes('text-2xl font-black mb-4 text-blue-900')
        det_nazev = ui.input('Dodavatel / Název').classes('w-full mb-2').props('outlined dense')
        det_ico = ui.input('IČO').classes('w-full mb-2').props('outlined dense')
        det_adresa = ui.input('Adresa').classes('w-full mb-2').props('outlined dense')
        det_banka = ui.input('Banka').classes('w-full mb-4').props('outlined dense')
        det_info = ui.html().classes('w-full bg-blue-50 p-4 rounded-xl mb-6 text-sm border border-blue-100 text-blue-900')

        def uloz_zmeny():
            r = state['rezervace'][aktualni_res_id['id']]
            r['nazev'], r['ico'], r['adresa'], r['banka'] = det_nazev.value, det_ico.value, det_adresa.value, det_banka.value
            ui.notify('Změny úspěšně uloženy', type='positive'); ui_refs['detail_dialog'].close(); update_svg(); save_data()
            if ui_refs.get('seznam_rezervaci_ui'): ui_refs['seznam_rezervaci_ui'].refresh()

        def uvolnit_stanek():
            res_id = aktualni_res_id['id']
            if res_id not in state['rezervace']: return
            push_undo('remove_rezervace', {'res_id': res_id, 'rezervace_data': dict(state['rezervace'][res_id])})
            for s in state['_res_idx'].get(res_id, []):
                s['res_id'] = None
            state['_res_idx'].pop(res_id, None)
            del state['rezervace'][res_id]
            ui.notify('Plocha uvolněna! Ctrl+Z pro vrácení.', type='info'); ui_refs['detail_dialog'].close(); update_svg(); save_data()
            if ui_refs.get('seznam_rezervaci_ui'): ui_refs['seznam_rezervaci_ui'].refresh()

        async def generuj_a_uloz_smlouvu(format_exportu='docx'):
            r = state['rezervace'][aktualni_res_id['id']]
            m2 = len(r['stanky_ids'])

            cena_plocha = int(round(m2 * state['cena_m2'])) if r['typ'] not in ['JIP Zóna', 'Křížek (Blokováno)'] else 0
            cena_ele = ELEKTRO_CENIK[r['elektro']]['cena']
            cena_voda = VODA_CENIK[r['voda']]['cena']
            celkem = cena_plocha + cena_ele + cena_voda

            slozka = "Smlouvy_Veletrh"
            os.makedirs(slozka, exist_ok=True)
            absolutni_slozka = os.path.abspath(slozka)

            bezpecne_jmeno = "".join([c for c in r['nazev'] if c.isalpha() or c.isdigit() or c==' ']).rstrip().replace(" ", "_")
            jmeno_souboru_docx = f"Smlouva_JIP_{bezpecne_jmeno}_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.docx"
            cesta_docx = os.path.join(absolutni_slozka, jmeno_souboru_docx)

            sablona_cesta = 'smlouva_veletrh_2027.docx'
            if not os.path.exists(sablona_cesta):
                ui.notify(f'Chyba: Soubor šablony "{sablona_cesta}" nebyl nalezen!', type='negative', position='top')
                return

            try:
                # OPTIMALIZACE: Tvorba wordu na pozadí
                def _render_docx():
                    doc = DocxTemplate(sablona_cesta)
                    context = {
                        'nazev': r['nazev'], 'ico': r['ico'], 'adresa': r['adresa'], 'banka': r['banka'],
                        'datum': datetime.datetime.now().strftime('%d.%m.%Y'), 'castka': f"{fmt(celkem)} Kč",
                        'm2': m2, 'cena_plocha': f"{fmt(cena_plocha)} Kč",
                        'elektro_nazev': ELEKTRO_CENIK[r['elektro']]['nazev'], 'cena_ele': f"{fmt(cena_ele)} Kč",
                        'voda_nazev': VODA_CENIK[r['voda']]['nazev'], 'cena_voda': f"{fmt(cena_voda)} Kč",
                        'typ_stanku': r['typ']
                    }
                    doc.render(context)
                    doc.save(cesta_docx)

                await asyncio.to_thread(_render_docx)

                if format_exportu == 'pdf':
                    cesta_pdf = cesta_docx.replace('.docx', '.pdf')
                    try:
                        # OPTIMALIZACE: Generování PDF LibreOffice na pozadí
                        def _convert_pdf():
                            lo_exec = shutil.which('libreoffice') or shutil.which('soffice') or '/usr/bin/libreoffice'
                            tmp_profile = f"-env:UserInstallation=file:///tmp/lo_profile_{uuid.uuid4()}"
                            custom_env = os.environ.copy()
                            custom_env["PATH"] = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:" + custom_env.get("PATH", "")

                            return subprocess.run(
                                [lo_exec, tmp_profile, '--headless', '--convert-to', 'pdf', '--outdir', absolutni_slozka, cesta_docx],
                                capture_output=True, text=True, env=custom_env
                            ), lo_exec

                        proces, lo_exec = await asyncio.to_thread(_convert_pdf)

                        if proces.returncode != 0:
                            print(f"Chyba LibreOffice: {proces.stderr}")
                            ui.notify(f'Chyba převodu: {proces.stderr[:100]}', type='negative', timeout=8000)
                        else:
                            relativni_cesta_pdf = os.path.join(slozka, jmeno_souboru_docx.replace('.docx', '.pdf'))
                            intranet_data.uloz_smlouvu_veletrh(aktualni_res_id['id'], r['nazev'], r['ico'], relativni_cesta_pdf)
                            ui.download(cesta_pdf)
                            ui.notify('PDF úspěšně vygenerováno!', type='positive')

                    except Exception as err:
                        ui.notify(f'Chyba spuštění PDF ({lo_exec}): {err}', type='negative', timeout=8000)
                else:
                    relativni_cesta_docx = os.path.join(slozka, jmeno_souboru_docx)
                    intranet_data.uloz_smlouvu_veletrh(aktualni_res_id['id'], r['nazev'], r['ico'], relativni_cesta_docx)
                    ui.download(cesta_docx)
                    ui.notify('Word úspěšně vygenerován a uložen.', type='positive')

                ui_refs['detail_dialog'].close()
                try:
                    if ui_refs.get('vykresli_archiv'): ui_refs['vykresli_archiv'].refresh()
                except Exception: pass

            except Exception as e:
                ui.notify(f'Chyba při generování smlouvy: {e}', type='negative', position='top')

        if st['role'] == 'admin':
            with ui.row().classes('w-full justify-between gap-3 mb-4'):
                ui.button('Uvolnit místo', icon='clear', color='red', on_click=uvolnit_stanek).classes('flex-1 font-bold h-12 rounded-xl')
                ui.button('Uložit úpravy', icon='save', color='blue', on_click=uloz_zmeny).classes('flex-1 font-bold h-12 rounded-xl')

        with ui.row().classes('w-full gap-2 mt-4'):
            ui_refs['btn_tisk_word'] = ui.button('📄 Tisk Word', on_click=lambda e: asyncio.create_task(generuj_a_uloz_smlouvu('docx'))).classes('flex-1 font-bold h-14 rounded-xl text-lg shadow-xl bg-blue-600 text-white hover:bg-blue-700')
            ui_refs['btn_tisk_pdf'] = ui.button('📕 Tisk PDF', color='red', on_click=lambda e: asyncio.create_task(generuj_a_uloz_smlouvu('pdf'))).classes('flex-1 font-bold h-14 rounded-xl text-lg shadow-xl bg-red-600 text-white hover:bg-red-700')

    update_svg()

    # Klávesové zkratky — registrace handleru pro tohoto uživatele.
    # Volá se z ui.keyboard() v intranet.py (na úrovni hlavní stránky, ne uvnitř tab_panel).
    def _veletrh_kbd_handler(key: str) -> bool:
        if key == 'ctrl+z':
            perform_undo()
            return True
        tool_map = {'v': 'vyber', 'p': 'packa', 'k': 'kresleni',
                    'o': 'oblast', 'l': 'ladeni', 'x': 'krizek',
                    'j': 'jip', 'r': 'kruh', 'g': 'guma'}
        if key not in tool_map:
            return False
        target = tool_map[key]
        valid = {'vyber', 'packa'}
        if st['role'] == 'admin':
            valid.update({'kresleni', 'oblast', 'ladeni', 'krizek', 'jip', 'kruh', 'guma'})
        if target not in valid:
            return False
        st['nastroj'] = target
        st['temp_p1'] = None
        st['selected_batch'] = None
        st['is_painting'] = False
        st['align_mode'] = False
        update_svg()
        ui.run_javascript(
            f"window.current_tool = '{target}';"
            "var c=document.getElementById('veletrh-paint-canvas');"
            "if(c&&window._vPaintCtx)window._vPaintCtx.clearRect(0,0,c.width,c.height);"
            "window._vPainting=false;"
        )
        if ui_refs.get('map_container'):
            ui_refs['map_container'].classes(remove='cursor-packa cursor-kresleni cursor-krizek cursor-jip cursor-oblast cursor-ladeni cursor-vyber cursor-kruh cursor-guma')
            ui_refs['map_container'].classes(add=f'cursor-{target}')
        if ui_refs.get('nastroje_panel'):
            ui_refs['nastroje_panel'].refresh()
        return True

    _VELETRH_KBD_HANDLERS[user_id] = _veletrh_kbd_handler

    # Live broadcast: zaregistruj update_svg pro tohoto uživatele.
    # save_data() pak po mutaci sdíleného state aktualizuje SVG všem připojeným.
    _VELETRH_UPDATE_CALLBACKS[user_id] = update_svg