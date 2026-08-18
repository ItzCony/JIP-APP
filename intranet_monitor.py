from nicegui import ui, app
import psutil
import time
import datetime
import platform
import socket
import asyncio
from collections import deque
import intranet_jobs

# ==========================================
# --- SLEDOVÁNÍ AKTIVNÍCH PŘIHLÁŠENÍ ---
# ==========================================
_AKTIVNI_UZIVATELE: dict = {}   # email → {'jmeno': str, 'cas': datetime, 'ip': str, 'device': str}

def zaznamenej_prihlaseni(email: str, jmeno: str, ip: str = '', device: str = ''):
    """Zaregistruje přihlášení uživatele (volitelně s IP adresou a zařízením)."""
    _AKTIVNI_UZIVATELE[email] = {
        'jmeno': jmeno,
        'cas': datetime.datetime.now(),
        'ip': ip or '',
        'device': device or '',
    }

def odeber_prihlaseni(email: str):
    """Odebere uživatele ze seznamu přihlášených (při odhlášení/odpojení)."""
    _AKTIVNI_UZIVATELE.pop(email, None)

def ziskej_aktivni() -> list:
    """Vrátí seřazený seznam aktivních uživatelů s dobou trvání relace."""
    now = datetime.datetime.now()
    result = []
    for email, info in list(_AKTIVNI_UZIVATELE.items()):
        delta = now - info['cas']
        total_s = int(delta.total_seconds())
        if total_s < 60:
            trvani = f'{total_s} s'
        elif total_s < 3600:
            trvani = f'{total_s // 60} min'
        else:
            trvani = f'{total_s // 3600}h {(total_s % 3600) // 60}min'
        result.append({
            'jmeno':  info['jmeno'],
            'email':  email,
            'od':     info['cas'].strftime('%H:%M'),
            'trvani': trvani,
            'ip':     info.get('ip', ''),
            'device': info.get('device', ''),
        })
    return sorted(result, key=lambda x: x['od'])

# ==========================================
# --- HISTORIE METRIK (posledních 60 s) ---
# ==========================================
HISTORY_LEN = 60
pocet_jader = psutil.cpu_count() or 1

casova_osa          = deque(maxlen=HISTORY_LEN)
ram_historie        = deque(maxlen=HISTORY_LEN)
swap_historie       = deque(maxlen=HISTORY_LEN)
disk_historie       = deque(maxlen=HISTORY_LEN)
cpu_celk_historie   = deque(maxlen=HISTORY_LEN)
net_up_historie     = deque(maxlen=HISTORY_LEN)   # KB/s
net_down_historie   = deque(maxlen=HISTORY_LEN)   # KB/s
disk_read_historie  = deque(maxlen=HISTORY_LEN)   # KB/s
disk_write_historie = deque(maxlen=HISTORY_LEN)   # KB/s
cpu_historie        = [deque(maxlen=HISTORY_LEN) for _ in range(pocet_jader)]

# Předvyplnění, aby grafy naskakovaly zprava
for _ in range(HISTORY_LEN):
    casova_osa.append('')
    for _dq in (ram_historie, swap_historie, disk_historie, cpu_celk_historie,
                net_up_historie, net_down_historie, disk_read_historie, disk_write_historie):
        _dq.append(0)
    for _c in cpu_historie:
        _c.append(0)

# Poslední naměřené hodnoty (sdílené background smyčkou se všemi UI timery)
_posledni: dict = {
    'cpu': 0.0, 'cpu_jadra': [], 'freq': 0.0, 'load': (0.0, 0.0, 0.0),
    'ram_pct': 0.0, 'ram_pouzito': 0.0, 'ram_celkem': 0.0, 'ram_volno': 0.0,
    'swap_pct': 0.0, 'swap_pouzito': 0.0, 'swap_celkem': 0.0,
    'disk_pct': 0.0, 'disk_pouzito': 0.0, 'disk_volno': 0.0, 'disk_celkem': 0.0,
    'net_up': 0.0, 'net_down': 0.0, 'net_up_celkem': 0, 'net_down_celkem': 0,
    'disk_read': 0.0, 'disk_write': 0.0,
    'procesy': 0, 'app_cpu': 0.0, 'app_ram': 0.0, 'app_thr': 0,
}

# Pomalé/těžší struktury (aktualizují se řidčeji)
_top_procesy: list = []
_disk_oddily: list = []

# Statické informace o serveru (zjištěné jednou při importu)
_BOOT = psutil.boot_time()
try:
    _PROC_APP = psutil.Process()
except Exception:
    _PROC_APP = None

def _zjisti_cpu_model() -> str:
    model = platform.processor() or ''
    try:
        if not model and platform.system() == 'Linux':
            with open('/proc/cpuinfo', encoding='utf-8') as f:
                for radek in f:
                    if 'model name' in radek:
                        model = radek.split(':', 1)[1].strip()
                        break
    except Exception:
        pass
    return model or platform.machine() or 'neznámý'

_INFO = {
    'hostname':  socket.gethostname(),
    'os':        f'{platform.system()} {platform.release()}',
    'arch':      platform.machine(),
    'python':    platform.python_version(),
    'cpu_model': _zjisti_cpu_model(),
    'jader_fyz': psutil.cpu_count(logical=False) or pocet_jader,
    'jader_log': pocet_jader,
}

# Baseline pro výpočet propustnosti (delta mezi vzorky)
_prev = {
    'net':  psutil.net_io_counters(),
    'disk': psutil.disk_io_counters(),
    't':    time.time(),
}
_proc_cache: dict = {}   # pid → psutil.Process (kvůli cpu_percent mezi voláními)


def _sber_top_procesy(limit: int = 8) -> list:
    """Vrátí TOP procesy podle CPU. Běží v threadu (volá se 1×/3 s)."""
    global _proc_cache
    nove: dict = {}
    vystup = []
    for p in psutil.process_iter(['pid', 'name', 'memory_percent']):
        try:
            pid = p.info['pid']
            if pid == 0:        # System Idle Process (Win) / scheduler — nezajímavé
                continue
            proc = _proc_cache.get(pid) or p
            nove[pid] = proc
            cpu = proc.cpu_percent(None) / pocet_jader     # podíl na celém CPU
            vystup.append({
                'pid':  pid,
                'name': (p.info.get('name') or '?')[:30],
                'cpu':  cpu,
                'mem':  p.info.get('memory_percent') or 0.0,
            })
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        except Exception:
            continue
    _proc_cache = nove
    vystup.sort(key=lambda x: x['cpu'], reverse=True)
    return vystup[:limit]


def _sber_oddily() -> list:
    """Využití jednotlivých diskových oddílů. Běží v threadu (1×/3 s)."""
    out = []
    try:
        oddily = psutil.disk_partitions(all=False)
    except Exception:
        oddily = []
    videno = set()
    for part in oddily:
        mp = part.mountpoint
        if mp in videno or 'cdrom' in (part.opts or '') or not part.fstype:
            continue
        videno.add(mp)
        try:
            u = psutil.disk_usage(mp)
            out.append({
                'mount':   mp,
                'fs':      part.fstype,
                'pct':     u.percent,
                'pouzito': u.used,
                'celkem':  u.total,
                'volno':   u.free,
            })
        except Exception:
            continue
    return out


def aktualizuj_data_pozadi():
    """Lehký sběr metrik každou sekundu (bez blokujících operací)."""
    casova_osa.append(time.strftime('%H:%M:%S'))

    mem  = psutil.virtual_memory()
    swap = psutil.swap_memory()
    disk = psutil.disk_usage('/')
    cpu_jadra = psutil.cpu_percent(percpu=True)
    cpu_celkem = sum(cpu_jadra) / len(cpu_jadra) if cpu_jadra else 0.0

    now_t = time.time()
    dt = max(now_t - _prev['t'], 0.001)

    net = psutil.net_io_counters()
    up_bps   = max(net.bytes_sent - _prev['net'].bytes_sent, 0) / dt
    down_bps = max(net.bytes_recv - _prev['net'].bytes_recv, 0) / dt

    dio = psutil.disk_io_counters()
    if dio and _prev['disk']:
        rd_bps = max(dio.read_bytes  - _prev['disk'].read_bytes, 0) / dt
        wr_bps = max(dio.write_bytes - _prev['disk'].write_bytes, 0) / dt
    else:
        rd_bps = wr_bps = 0.0

    _prev['net'], _prev['disk'], _prev['t'] = net, dio, now_t

    # Historie pro grafy
    ram_historie.append(round(mem.percent, 1))
    swap_historie.append(round(swap.percent, 1))
    disk_historie.append(round(disk.percent, 1))
    cpu_celk_historie.append(round(cpu_celkem, 1))
    net_up_historie.append(round(up_bps / 1024, 1))
    net_down_historie.append(round(down_bps / 1024, 1))
    disk_read_historie.append(round(rd_bps / 1024, 1))
    disk_write_historie.append(round(wr_bps / 1024, 1))
    for i, proc in enumerate(cpu_jadra):
        if i < pocet_jader:
            cpu_historie[i].append(proc)

    # Doplňkové metriky (best-effort napříč platformami)
    try:
        f = psutil.cpu_freq()
        freq = f.current if f else 0.0
    except Exception:
        freq = 0.0
    try:
        load = psutil.getloadavg()
    except Exception:
        load = (0.0, 0.0, 0.0)
    try:
        app_cpu = _PROC_APP.cpu_percent(None) / pocet_jader if _PROC_APP else 0.0
        app_ram = _PROC_APP.memory_info().rss / (1024 ** 2) if _PROC_APP else 0.0
        app_thr = _PROC_APP.num_threads() if _PROC_APP else 0
    except Exception:
        app_cpu = app_ram = 0.0
        app_thr = 0

    _posledni.update({
        'cpu': cpu_celkem, 'cpu_jadra': list(cpu_jadra), 'freq': freq, 'load': load,
        'ram_pct': mem.percent, 'ram_pouzito': mem.used / 1024 ** 3,
        'ram_celkem': mem.total / 1024 ** 3, 'ram_volno': mem.available / 1024 ** 3,
        'swap_pct': swap.percent, 'swap_pouzito': swap.used / 1024 ** 3,
        'swap_celkem': swap.total / 1024 ** 3,
        'disk_pct': disk.percent, 'disk_pouzito': disk.used / 1024 ** 3,
        'disk_volno': disk.free / 1024 ** 3, 'disk_celkem': disk.total / 1024 ** 3,
        'net_up': up_bps, 'net_down': down_bps,
        'net_up_celkem': net.bytes_sent, 'net_down_celkem': net.bytes_recv,
        'disk_read': rd_bps, 'disk_write': wr_bps,
        'procesy': len(psutil.pids()),
        'app_cpu': app_cpu, 'app_ram': app_ram, 'app_thr': app_thr,
    })


async def bg_monitor():
    # Prvotní nahození (první volání cpu_percent vrací nulu)
    psutil.cpu_percent(percpu=True)
    if _PROC_APP:
        try:
            _PROC_APP.cpu_percent(None)
        except Exception:
            pass
    tik = 0
    while True:
        try:
            aktualizuj_data_pozadi()
            tik += 1
            if tik % 3 == 0:
                # Těžší sběr mimo event-loop, ať neblokuje ostatní klienty
                global _top_procesy, _disk_oddily
                _top_procesy = await asyncio.to_thread(_sber_top_procesy)
                _disk_oddily = await asyncio.to_thread(_sber_oddily)
        except Exception:
            pass
        await asyncio.sleep(1)

# Spustí nekonečnou smyčku sběru dat hned při startu serveru
app.on_startup(lambda: asyncio.create_task(bg_monitor()))


# ==========================================
# --- POMOCNÉ FORMÁTOVÁNÍ ---
# ==========================================
def _barva_pct(p: float) -> str:
    if p >= 90:
        return '#ef4444'   # červená
    if p >= 75:
        return '#f59e0b'   # oranžová
    return '#10b981'       # zelená

def _lidsky_tok(bps: float) -> str:
    if bps >= 1024 ** 2:
        return f'{bps / 1024 ** 2:.1f} MB/s'
    if bps >= 1024:
        return f'{bps / 1024:.0f} KB/s'
    return f'{bps:.0f} B/s'

def _lidska_velikost(b: float) -> str:
    for u in ('B', 'KB', 'MB', 'GB', 'TB'):
        if b < 1024:
            return f'{b:.1f} {u}'
        b /= 1024
    return f'{b:.1f} PB'

def _uptime_text() -> str:
    s = int(time.time() - _BOOT)
    d, h, m = s // 86400, (s % 86400) // 3600, (s % 3600) // 60
    if d:
        return f'{d}d {h}h {m}m'
    if h:
        return f'{h}h {m}m'
    return f'{m}m'


# ==========================================
# --- VYKRESLENÍ DASHBOARDU ---
# ==========================================
def vykresli_monitor(vsechna_prava):
    if "vse" not in vsechna_prava and "admin_server" not in vsechna_prava:
        ui.label('Přístup odepřen. Tuto sekci mohou vidět pouze administrátoři.') \
            .classes('text-red-500 text-xl font-bold')
        return

    _CARD = 'p-5 rounded-2xl bg-white border border-gray-100 shadow-sm'

    # ── Hlavička ──────────────────────────────────────────────────────────────
    with ui.row().classes('w-full items-center justify-between mb-6 flex-wrap gap-3'):
        with ui.row().classes('items-center gap-3'):
            ui.icon('dns', size='32px').classes('text-blue-600')
            with ui.column().classes('gap-0'):
                ui.label('Monitor serveru').classes('text-3xl font-black text-gray-800 leading-tight')
                ui.label(f"{_INFO['hostname']} · {_INFO['os']}").classes('text-sm text-gray-400')
        with ui.row().classes('items-center gap-2 bg-green-50 border border-green-200 rounded-full px-4 py-1.5'):
            ui.element('div').classes('rounded-full bg-green-500') \
                .style('width:9px;height:9px;animation:pulse 1.5s infinite')
            ui.label('ŽIVĚ · 1 s').classes('text-sm font-bold text-green-700')
    ui.add_head_html('<style>@keyframes pulse{0%{opacity:1}50%{opacity:.3}100%{opacity:1}}</style>')

    # ── Pomocné buildery karet ────────────────────────────────────────────────
    def _kpi_pct(titulek, ikona, ikona_barva):
        with ui.card().classes(_CARD + ' gap-3'):
            with ui.row().classes('w-full items-center justify-between flex-nowrap'):
                with ui.row().classes('items-center gap-2 min-w-0'):
                    ui.icon(ikona, size='sm').classes(ikona_barva + ' shrink-0')
                    ui.label(titulek).classes('text-xs font-bold uppercase tracking-wider text-gray-400 truncate')
                val = ui.label('0 %').classes('text-2xl font-black text-gray-800 shrink-0')
            track = ui.element('div').classes('w-full rounded-full bg-gray-100').style('height:8px;overflow:hidden')
            with track:
                fill = ui.element('div').classes('rounded-full') \
                    .style('height:8px;width:0%;background:#10b981;transition:width .4s ease,background .4s ease')
            detail = ui.label('').classes('text-xs text-gray-400 truncate')
        return {'val': val, 'fill': fill, 'detail': detail}

    def _kpi_val(titulek, ikona, ikona_barva):
        with ui.card().classes(_CARD + ' gap-2'):
            with ui.row().classes('w-full items-center gap-2 flex-nowrap'):
                ui.icon(ikona, size='sm').classes(ikona_barva + ' shrink-0')
                ui.label(titulek).classes('text-xs font-bold uppercase tracking-wider text-gray-400 truncate')
            val = ui.label('—').classes('text-2xl font-black text-gray-800')
            detail = ui.label('').classes('text-xs text-gray-400 truncate')
        return {'val': val, 'detail': detail}

    def _set_pct(ref, pct, detail):
        ref['val'].set_text(f'{pct:.1f} %')
        ref['fill'].style(f'width:{min(pct, 100):.1f}%;background:{_barva_pct(pct)}')
        ref['detail'].set_text(detail)

    # ── Řada 1: procentuální ukazatele ────────────────────────────────────────
    with ui.element('div').classes('w-full grid gap-4 grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 mb-4'):
        kpi_cpu  = _kpi_pct('Procesor (CPU)',     'memory',       'text-green-500')
        kpi_ram  = _kpi_pct('Operační paměť',     'developer_board', 'text-blue-500')
        kpi_swap = _kpi_pct('Odkládací (Swap)',   'swap_horiz',   'text-indigo-500')
        kpi_disk = _kpi_pct('Disk ( / )',         'storage',      'text-purple-500')

    # ── Řada 2: hodnotové ukazatele ───────────────────────────────────────────
    with ui.element('div').classes('w-full grid gap-4 grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 mb-6'):
        kpi_net   = _kpi_val('Síť (provoz)',     'swap_vert',     'text-teal-500')
        kpi_dio   = _kpi_val('Disk I/O',         'sync_alt',      'text-cyan-500')
        kpi_jobs  = _kpi_val('Těžké úlohy',      'bolt',          'text-amber-500')
        kpi_users = _kpi_val('Přihlášení',       'group',         'text-rose-500')

    # ── Systémový informační panel ────────────────────────────────────────────
    def _info_item(popisek, hodnota, ikona):
        with ui.row().classes('items-center gap-2 min-w-0'):
            ui.icon(ikona, size='20px').classes('text-gray-400 shrink-0')
            with ui.column().classes('gap-0 min-w-0'):
                ui.label(popisek).classes('text-[10px] font-bold uppercase tracking-wider text-gray-400')
                lbl = ui.label(hodnota).classes('text-sm font-bold text-gray-700 truncate')
        return lbl

    with ui.card().classes(_CARD + ' w-full mb-6'):
        with ui.element('div').classes('w-full grid gap-x-6 gap-y-4 grid-cols-2 md:grid-cols-3 xl:grid-cols-4'):
            _info_item('Procesor', f"{_INFO['cpu_model']}", 'developer_board')
            _info_item('Jádra', f"{_INFO['jader_fyz']} fyz. / {_INFO['jader_log']} log.", 'memory')
            lbl_freq   = _info_item('Frekvence CPU', '—', 'speed')
            lbl_uptime = _info_item('Doba běhu', _uptime_text(), 'schedule')
            lbl_load   = _info_item('Zátěž (1/5/15 min)', '—', 'show_chart')
            lbl_proc   = _info_item('Procesy', '—', 'account_tree')
            _info_item('Start serveru', datetime.datetime.fromtimestamp(_BOOT).strftime('%d.%m.%Y %H:%M'), 'power_settings_new')
            lbl_app    = _info_item('Aplikace (tento proces)', '—', 'terminal')
            _info_item('Architektura / Python', f"{_INFO['arch']} · Python {_INFO['python']}", 'code')

    # ── Graf: CPU po jádrech + průměr ─────────────────────────────────────────
    ui.label('Vytížení procesoru').classes('text-lg font-bold text-gray-700 mb-2')
    cpu_series = [{
        'name': f'Jádro {i}', 'type': 'line', 'data': list(cpu_historie[i]),
        'smooth': True, 'showSymbol': False, 'lineStyle': {'width': 1}, 'areaStyle': {'opacity': 0.05},
    } for i in range(pocet_jader)]
    cpu_series.append({
        'name': 'Průměr', 'type': 'line', 'data': list(cpu_celk_historie),
        'smooth': True, 'showSymbol': False, 'z': 10, 'lineStyle': {'width': 3, 'color': '#111827'},
    })
    graf_cpu = ui.echart({
        'tooltip': {'trigger': 'axis'},
        'legend': {'type': 'scroll', 'bottom': 0,
                   'data': [f'Jádro {i}' for i in range(pocet_jader)] + ['Průměr']},
        'grid': {'left': '3%', 'right': '4%', 'bottom': '14%', 'top': '8%', 'containLabel': True},
        'xAxis': {'type': 'category', 'boundaryGap': False, 'data': list(casova_osa)},
        'yAxis': {'type': 'value', 'max': 100, 'axisLabel': {'formatter': '{value} %'}},
        'series': cpu_series,
    }).classes('w-full h-72 bg-white p-4 shadow-sm rounded-2xl border border-gray-100 mb-6')

    # ── Grafy: RAM/Swap a Síť ─────────────────────────────────────────────────
    with ui.element('div').classes('w-full grid gap-6 grid-cols-1 xl:grid-cols-2 mb-6'):
        with ui.column().classes('gap-2'):
            ui.label('Paměť (RAM & Swap)').classes('text-lg font-bold text-gray-700')
            graf_mem = ui.echart({
                'tooltip': {'trigger': 'axis'},
                'legend': {'data': ['RAM', 'Swap'], 'bottom': 0},
                'grid': {'left': '3%', 'right': '4%', 'bottom': '14%', 'top': '8%', 'containLabel': True},
                'xAxis': {'type': 'category', 'boundaryGap': False, 'data': list(casova_osa)},
                'yAxis': {'type': 'value', 'max': 100, 'axisLabel': {'formatter': '{value} %'}},
                'series': [
                    {'name': 'RAM', 'type': 'line', 'data': list(ram_historie), 'smooth': True,
                     'showSymbol': False, 'itemStyle': {'color': '#3b82f6'}, 'areaStyle': {'opacity': 0.2}},
                    {'name': 'Swap', 'type': 'line', 'data': list(swap_historie), 'smooth': True,
                     'showSymbol': False, 'itemStyle': {'color': '#6366f1'}, 'areaStyle': {'opacity': 0.15}},
                ],
            }).classes('w-full h-72 bg-white p-4 shadow-sm rounded-2xl border border-gray-100')

        with ui.column().classes('gap-2'):
            ui.label('Síťový provoz').classes('text-lg font-bold text-gray-700')
            graf_net = ui.echart({
                'tooltip': {'trigger': 'axis'},
                'legend': {'data': ['Download', 'Upload'], 'bottom': 0},
                'grid': {'left': '3%', 'right': '4%', 'bottom': '14%', 'top': '8%', 'containLabel': True},
                'xAxis': {'type': 'category', 'boundaryGap': False, 'data': list(casova_osa)},
                'yAxis': {'type': 'value', 'axisLabel': {'formatter': '{value} KB/s'}},
                'series': [
                    {'name': 'Download', 'type': 'line', 'data': list(net_down_historie), 'smooth': True,
                     'showSymbol': False, 'itemStyle': {'color': '#10b981'}, 'areaStyle': {'opacity': 0.2}},
                    {'name': 'Upload', 'type': 'line', 'data': list(net_up_historie), 'smooth': True,
                     'showSymbol': False, 'itemStyle': {'color': '#f59e0b'}, 'areaStyle': {'opacity': 0.15}},
                ],
            }).classes('w-full h-72 bg-white p-4 shadow-sm rounded-2xl border border-gray-100')

    # ── Graf: Disk I/O + TOP procesy ──────────────────────────────────────────
    with ui.element('div').classes('w-full grid gap-6 grid-cols-1 xl:grid-cols-2 mb-6'):
        with ui.column().classes('gap-2'):
            ui.label('Diskové operace (I/O)').classes('text-lg font-bold text-gray-700')
            graf_dio = ui.echart({
                'tooltip': {'trigger': 'axis'},
                'legend': {'data': ['Čtení', 'Zápis'], 'bottom': 0},
                'grid': {'left': '3%', 'right': '4%', 'bottom': '14%', 'top': '8%', 'containLabel': True},
                'xAxis': {'type': 'category', 'boundaryGap': False, 'data': list(casova_osa)},
                'yAxis': {'type': 'value', 'axisLabel': {'formatter': '{value} KB/s'}},
                'series': [
                    {'name': 'Čtení', 'type': 'line', 'data': list(disk_read_historie), 'smooth': True,
                     'showSymbol': False, 'itemStyle': {'color': '#06b6d4'}, 'areaStyle': {'opacity': 0.2}},
                    {'name': 'Zápis', 'type': 'line', 'data': list(disk_write_historie), 'smooth': True,
                     'showSymbol': False, 'itemStyle': {'color': '#ef4444'}, 'areaStyle': {'opacity': 0.15}},
                ],
            }).classes('w-full h-72 bg-white p-4 shadow-sm rounded-2xl border border-gray-100')

        with ui.column().classes('gap-2'):
            ui.label('Nejnáročnější procesy (CPU)').classes('text-lg font-bold text-gray-700')
            with ui.card().classes(_CARD + ' w-full h-72 overflow-y-auto'):
                _proc_sloupce = [
                    {'name': 'pid',  'label': 'PID',     'field': 'pid',  'align': 'left'},
                    {'name': 'name', 'label': 'Proces',  'field': 'name', 'align': 'left'},
                    {'name': 'cpu',  'label': 'CPU %',   'field': 'cpu',  'align': 'right', 'sortable': True},
                    {'name': 'mem',  'label': 'RAM %',   'field': 'mem',  'align': 'right', 'sortable': True},
                ]

                @ui.refreshable
                def _tab_procesy():
                    if not _top_procesy:
                        ui.label('Sbírám data o procesech…').classes('text-sm text-gray-400 italic')
                        return
                    rows = [{'pid': p['pid'], 'name': p['name'],
                             'cpu': f"{p['cpu']:.1f}", 'mem': f"{p['mem']:.1f}"} for p in _top_procesy]
                    ui.table(columns=_proc_sloupce, rows=rows, row_key='pid') \
                        .props('flat dense').classes('w-full')
                _tab_procesy()

    # ── Diskové oddíly + přihlášení uživatelé ─────────────────────────────────
    with ui.element('div').classes('w-full grid gap-6 grid-cols-1 xl:grid-cols-2 mb-6'):
        with ui.column().classes('gap-2'):
            ui.label('Využití diskových oddílů').classes('text-lg font-bold text-gray-700')
            with ui.card().classes(_CARD + ' w-full gap-4'):
                @ui.refreshable
                def _tab_oddily():
                    if not _disk_oddily:
                        ui.label('Načítám oddíly…').classes('text-sm text-gray-400 italic')
                        return
                    for o in _disk_oddily:
                        with ui.column().classes('w-full gap-1'):
                            with ui.row().classes('w-full justify-between items-center flex-nowrap'):
                                ui.label(f"{o['mount']}  ·  {o['fs']}").classes('text-sm font-bold text-gray-700 truncate')
                                ui.label(f"{_lidska_velikost(o['pouzito'])} / {_lidska_velikost(o['celkem'])}  ·  {o['pct']:.0f} %") \
                                    .classes('text-xs text-gray-500 shrink-0')
                            track = ui.element('div').classes('w-full rounded-full bg-gray-100').style('height:8px;overflow:hidden')
                            with track:
                                ui.element('div').classes('rounded-full') \
                                    .style(f"height:8px;width:{min(o['pct'], 100)}%;background:{_barva_pct(o['pct'])}")
                _tab_oddily()

        with ui.column().classes('gap-2'):
            with ui.row().classes('w-full items-center justify-between'):
                ui.label('Přihlášení uživatelé').classes('text-lg font-bold text-gray-700')
                lbl_users_pocet = ui.label('0').classes('text-sm font-bold text-rose-600 bg-rose-50 rounded-full px-3 py-0.5')
            with ui.card().classes(_CARD + ' w-full max-h-72 overflow-y-auto gap-1'):
                @ui.refreshable
                def _tab_uzivatele():
                    akt = ziskej_aktivni()
                    if not akt:
                        ui.label('Nikdo není přihlášen.').classes('text-sm text-gray-400 italic')
                        return
                    for u in akt:
                        with ui.row().classes('w-full items-center gap-3 p-2 rounded-lg hover:bg-gray-50 flex-nowrap'):
                            ui.icon('account_circle', size='sm').classes('text-blue-400 shrink-0')
                            with ui.column().classes('gap-0 min-w-0 flex-1'):
                                ui.label(u['jmeno']).classes('text-sm font-bold text-gray-700 truncate')
                                _pod = u['email'] + (f"  ·  {u['ip']}" if u['ip'] else '')
                                ui.label(_pod).classes('text-xs text-gray-400 truncate')
                            with ui.column().classes('gap-0 items-end shrink-0'):
                                ui.label(u['trvani']).classes('text-sm font-bold text-gray-700')
                                ui.label(f"od {u['od']}").classes('text-xs text-gray-400')
                _tab_uzivatele()

    # ── Detail fronty těžkých úloh ────────────────────────────────────────────
    with ui.card().classes(_CARD + ' w-full mb-2'):
        with ui.row().classes('w-full items-center gap-3'):
            ui.icon('bolt', size='sm').classes('text-amber-500')
            ui.label('Fronta těžkých úloh (exporty / tisk / parsování uploadů)') \
                .classes('text-lg font-bold text-gray-700')
        jobs_detail = ui.label('').classes('text-sm text-gray-500 mt-1')

    # ── Aktualizace UI (čte jen cache, žádné psutil volání) ───────────────────
    _ui_tik = {'n': 0}

    def update_ui():
        p = _posledni
        _set_pct(kpi_cpu, p['cpu'],
                 f"{pocet_jader} jader" + (f" · {p['freq']:.0f} MHz" if p['freq'] else ''))
        _set_pct(kpi_ram, p['ram_pct'],
                 f"{p['ram_pouzito']:.1f} / {p['ram_celkem']:.1f} GB  ·  volno {p['ram_volno']:.1f} GB")
        _set_pct(kpi_swap, p['swap_pct'],
                 (f"{p['swap_pouzito']:.1f} / {p['swap_celkem']:.1f} GB" if p['swap_celkem'] > 0 else 'bez swapu'))
        _set_pct(kpi_disk, p['disk_pct'],
                 f"volno {p['disk_volno']:.1f} / {p['disk_celkem']:.1f} GB")

        kpi_net['val'].set_text(f"↓ {_lidsky_tok(p['net_down'])}")
        kpi_net['detail'].set_text(
            f"↑ {_lidsky_tok(p['net_up'])}  ·  celkem ↓{_lidska_velikost(p['net_down_celkem'])}")
        kpi_dio['val'].set_text(f"R {_lidsky_tok(p['disk_read'])}")
        kpi_dio['detail'].set_text(f"W {_lidsky_tok(p['disk_write'])}")

        ji = intranet_jobs.info()
        kpi_jobs['val'].set_text(f"{ji['bezici_ulohy']} / {ji['max_workers']}")
        kpi_jobs['detail'].set_text(
            ('proces-pool ✓' if ji['pool_aktivni'] else 'jen vlákna') + f"  ·  {ji['jader']} jader")
        jobs_detail.set_text(
            f"Běžící: {ji['bezici_ulohy']} / {ji['max_workers']}  ·  "
            f"{'proces-pool aktivní' if ji['pool_aktivni'] else 'pouze vlákna'}  ·  "
            f"{ji['jader']} jader  ·  fallbacků na vlákno: {ji['fallbacku_na_vlakno']}")

        _pocet_u = len(_AKTIVNI_UZIVATELE)
        kpi_users['val'].set_text(str(_pocet_u))
        kpi_users['detail'].set_text('aktivních relací')
        lbl_users_pocet.set_text(str(_pocet_u))

        lbl_freq.set_text(f"{p['freq']:.0f} MHz" if p['freq'] else '—')
        lbl_uptime.set_text(_uptime_text())
        la = p['load']
        lbl_load.set_text(f"{la[0]:.2f}  ·  {la[1]:.2f}  ·  {la[2]:.2f}")
        lbl_proc.set_text(str(p['procesy']))
        lbl_app.set_text(f"{p['app_ram']:.0f} MB RAM  ·  {p['app_thr']} vláken  ·  {p['app_cpu']:.1f} % CPU")

        osa = list(casova_osa)
        graf_cpu.options['xAxis']['data'] = osa
        for i in range(pocet_jader):
            graf_cpu.options['series'][i]['data'] = list(cpu_historie[i])
        graf_cpu.options['series'][pocet_jader]['data'] = list(cpu_celk_historie)
        graf_cpu.update()

        graf_mem.options['xAxis']['data'] = osa
        graf_mem.options['series'][0]['data'] = list(ram_historie)
        graf_mem.options['series'][1]['data'] = list(swap_historie)
        graf_mem.update()

        graf_net.options['xAxis']['data'] = osa
        graf_net.options['series'][0]['data'] = list(net_down_historie)
        graf_net.options['series'][1]['data'] = list(net_up_historie)
        graf_net.update()

        graf_dio.options['xAxis']['data'] = osa
        graf_dio.options['series'][0]['data'] = list(disk_read_historie)
        graf_dio.options['series'][1]['data'] = list(disk_write_historie)
        graf_dio.update()

        # Tabulky (procesy, oddíly, uživatelé) stačí přepsat 1×/3 s
        _ui_tik['n'] += 1
        if _ui_tik['n'] % 3 == 0:
            _tab_procesy.refresh()
            _tab_oddily.refresh()
            _tab_uzivatele.refresh()

    update_ui()
    ui.timer(1.0, update_ui)
