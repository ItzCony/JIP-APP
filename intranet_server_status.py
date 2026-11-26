"""Server Status — živý monitor serveru ve stylu Grafany (jen hlavní administrátor).

Měření běží na pozadí každých 10 s (bg_mereni, spouští web_main.py):
  • RAM: posledních 60 min po 10 s (deque) — grafy „1 hodina" a 5min průměry,
  • MySQL `server_status_historie`: minutové průměry pro 24 h / 7 / 30 dní,
    starší než 30 dní se mažou (≈ 43 tis. řádků),
  • upozornění (disk, RAM, CPU, load, soubory procesu, MySQL) s hysterezí —
    jen na stránce modulu a jako odznak na dlaždici.
Monitor nic nikam neposílá (žádné e-maily). Data jdou do prohlížeče jen přes
WebSocket stránky hlavního admina — žádný veřejný HTTP endpoint
(/metrics apod.) nevzniká.

Produkce běží na Linuxu. Na Windows (vývoj) chybí iowait, load, file
deskriptory a vytížení disku — místo nich je „—".
"""
import asyncio
import collections
import datetime
import os
import platform
import socket
import sys
import time

import psutil
from nicegui import Client, app, ui

import intranet_data
import intranet_jobs
import intranet_session

try:
    import resource          # jen POSIX — limit otevřených souborů procesu (ulimit -n)
except ImportError:          # Windows (vývoj)
    resource = None

TAB = 'server_status'          # klíč záložky = URL /server_status
INTERVAL_S = 10                         # perioda měření
_RAM_VZORKU = 3600 // INTERVAL_S        # historie v RAM: 60 min
_VZORKU_5M = 300 // INTERVAL_S          # okno 5min průměrů (budíky CPU / iowait, pravidlo CPU)
_MYSQL_KAZDYCH = 6                      # stav MySQL jednou za minutu
_VELIKOST_DB_KAZDYCH = 60               # velikost DB (information_schema) jednou za 10 min
_DB_DNY = 30                            # jak dlouho držet minutovou historii v MySQL
_LINUX = sys.platform.startswith('linux')
_HOST = socket.gethostname()

# Časové řady: položky RAM historie i sloupce tabulky server_status_historie.
_SLOUPCE = ('cpu', 'cpu_user', 'cpu_system', 'iowait', 'ram', 'ram_used_mb', 'ram_cache_mb',
            'swap', 'disk', 'load1', 'load5', 'load15', 'disk_r', 'disk_w', 'disk_util',
            'net_rx', 'net_tx', 'rss_mb', 'klienti', 'prihlaseni', 'db_vlakna')

# Rozsahy grafů: popisek, délka [s], krok agregace z DB [s] (None = RAM po 10 s).
_ROZSAHY = {
    '1h': ('1 hodina', 3600, None),
    '24h': ('24 hodin', 86400, 300),
    '7d': ('7 dní', 7 * 86400, 1800),
    '30d': ('30 dní', 30 * 86400, 7200),
}

_HISTORIE: collections.deque = collections.deque(maxlen=_RAM_VZORKU)
_STAV: dict = {}            # poslední kompletní měření (systém + aplikace)
_MYSQL: dict = {}           # poslední stav MySQL
_UDALOSTI: collections.deque = collections.deque(maxlen=100)   # (ts, 'chyba'|'ok'|'info', text)
_AKTIVNI: dict = {}         # klíč pravidla → {'od', 'popis', 'hodnota'}
_PREKROCENO_OD: dict = {}   # klíč pravidla → ts, od kdy je hodnota nad prahem
_PROC = psutil.Process()
_DETI: dict = {}            # pid → psutil.Process (cpu_percent potřebuje stálý objekt)
_CITACE: dict = {}          # předchozí čítače pro rychlosti (CPU, disk, síť, MySQL)
_DB_INIT = False
_POSLEDNI_UKLID = 0
_CACHE_DB: dict = {}        # rozsah → (ts načtení, body) — DB historie stačí po minutě
_CACHE_PROCESY: dict = {'t': 0.0, 'data': []}


def je_hlavni_admin(user_id) -> bool:
    return str(user_id) == str(intranet_data.SKRYTY_ADMIN_ID)


def pocet_upozorneni() -> int:
    """Počet aktivních upozornění — odznak na dlaždici v přehledu."""
    return len(_AKTIVNI)


# =========================================================
# MĚŘENÍ — systém (běží ve vlákně, psutil čte /proc)
# =========================================================
def _rychlost(klic: str, hodnoty: tuple, t: float):
    """Přírůstek čítačů od minulého měření za sekundu; None při prvním měření."""
    pred = _CITACE.get(klic)
    _CITACE[klic] = (t, hodnoty)
    if not pred or t <= pred[0] or len(pred[1]) != len(hodnoty):
        return None
    dt = t - pred[0]
    return tuple(max(0.0, (a - b) / dt) for a, b in zip(hodnoty, pred[1]))


def _cpu(ct) -> dict:
    """Vytížení CPU z rozdílu čítačů. Vlastní stav — nezávislé na jiném volání psutil.
    „CPU" = 100 − idle (iowait se počítá do vytížení, stejně jako v Grafaně)."""
    pred = _CITACE.get('cpu')
    _CITACE['cpu'] = ct
    if pred is None:
        return {}
    d = {f: max(0.0, getattr(ct, f) - getattr(pred, f)) for f in ct._fields}
    celkem = sum(d.values()) - d.get('guest', 0) - d.get('guest_nice', 0)   # guest už je v user/nice
    if celkem <= 0:
        return {}

    def pct(x):
        return round(100.0 * x / celkem, 2)
    return {
        'cpu': pct(celkem - d.get('idle', 0)),
        'cpu_user': pct(d.get('user', 0) + d.get('nice', 0)),
        'cpu_system': pct(d.get('system', 0) + d.get('irq', 0) + d.get('softirq', 0)
                          + d.get('interrupt', 0) + d.get('dpc', 0)),
        'iowait': pct(d['iowait']) if 'iowait' in d else None,
    }


_FS_PRESKOC = {'squashfs', 'tmpfs', 'devtmpfs', 'overlay', 'iso9660', 'ramfs', 'autofs'}
_DISK_PRESKOC = ('loop', 'ram', 'sr', 'zram', 'fd')


def _oddily() -> list:
    """Připojené oddíly s využitím (bez snap/tmpfs; bind-mount stejného zařízení jen jednou)."""
    vysl, zarizeni = [], set()
    for p in sorted(psutil.disk_partitions(all=False), key=lambda p: len(p.mountpoint)):
        if p.fstype in _FS_PRESKOC or p.device.startswith('/dev/loop'):
            continue
        if 'ro' in (p.opts or '').split(','):     # jen pro čtení — vždy „plný", nic se na něj nezapisuje
            continue
        if _LINUX and p.device in zarizeni:
            continue
        try:
            u = psutil.disk_usage(p.mountpoint)
        except OSError:
            continue
        if not u.total:
            continue
        zarizeni.add(p.device)
        vysl.append({'mount': p.mountpoint, 'fs': p.fstype, 'dev': p.device,
                     'total': u.total, 'free': u.free, 'pct': round(u.percent, 2)})
    return sorted(vysl, key=lambda d: d['mount'])


def _disk_io(t: float):
    """(čtení KiB/s, zápis KiB/s, vytížení nejvytíženějšího disku %)."""
    try:
        celkem = psutil.disk_io_counters(perdisk=False)
        per = psutil.disk_io_counters(perdisk=True) or {}
    except Exception:
        return None, None, None
    r = w = util = None
    if celkem:
        v = _rychlost('disk', (celkem.read_bytes, celkem.write_bytes), t)
        if v:
            r, w = round(v[0] / 1024, 1), round(v[1] / 1024, 1)
    busy = {n: c.busy_time for n, c in per.items()
            if hasattr(c, 'busy_time') and not n.startswith(_DISK_PRESKOC)}
    if busy:
        pred = _CITACE.get('busy')
        _CITACE['busy'] = (t, busy)
        if pred and t > pred[0]:
            dt_ms = (t - pred[0]) * 1000
            util = round(min(100.0, max(
                [(ms - pred[1].get(n, ms)) / dt_ms * 100 for n, ms in busy.items()] or [0])), 2)
    return r, w, util


def _fd_system():
    """(alokované file deskriptory, systémové maximum) z /proc/sys/fs/file-nr."""
    if not _LINUX:
        return None, None
    try:
        with open('/proc/sys/fs/file-nr') as f:
            alok, _volne, maximum = (int(x) for x in f.read().split()[:3])
        return alok, maximum
    except (OSError, ValueError):
        return None, None


def _teplota():
    """Nejvyšší teplota CPU (°C), pokud ji jádro poskytuje — na VM obvykle ne."""
    fn = getattr(psutil, 'sensors_temperatures', None)
    if fn is None:
        return None
    try:
        data = fn() or {}
    except Exception:
        return None
    skupiny = [data[k] for k in ('coretemp', 'k10temp', 'zenpower', 'cpu_thermal') if k in data] \
        or list(data.values())
    hodnoty = [s.current for sk in skupiny for s in sk if s.current and 0 < s.current < 130]
    return round(max(hodnoty), 1) if hodnoty else None


def _proces() -> dict:
    """Proces aplikace + jeho worker procesy (intranet_jobs, výpočet KPI)."""
    with _PROC.oneshot():
        rss = _PROC.memory_info().rss
        cpu = _PROC.cpu_percent(None)
        vlakna = _PROC.num_threads()
        fd = _PROC.num_fds() if hasattr(_PROC, 'num_fds') else None
    deti_rss, deti_cpu, zive = 0, 0.0, set()
    try:
        deti = _PROC.children(recursive=True)
    except psutil.Error:
        deti = []
    for c in deti:
        p = _DETI.setdefault(c.pid, c)
        try:
            deti_rss += p.memory_info().rss
            deti_cpu += p.cpu_percent(None)
            zive.add(c.pid)
        except psutil.Error:
            pass
    for pid in [p for p in _DETI if p not in zive]:
        _DETI.pop(pid, None)
    limit = None
    if resource is not None:
        try:
            limit = resource.getrlimit(resource.RLIMIT_NOFILE)[0]
            if limit == resource.RLIM_INFINITY:
                limit = None
        except (OSError, ValueError):
            pass
    jadra = psutil.cpu_count() or 1
    return {
        'rss_mb': round((rss + deti_rss) / 2**20, 1), 'rss_hlavni': rss, 'rss_deti': deti_rss,
        'procesu_deti': len(zive), 'cpu_proc': round((cpu + deti_cpu) / jadra, 1),
        'vlakna': vlakna, 'fd_proc': fd, 'fd_limit': limit,
        'fd_proc_pct': round(100 * fd / limit, 1) if fd is not None and limit else None,
    }


def _zmer_system(t: float) -> dict:
    m = _cpu(psutil.cpu_times())
    vm = psutil.virtual_memory()
    sw = psutil.swap_memory()
    pouzito = vm.total - vm.available
    cache = (getattr(vm, 'cached', 0) or 0) + (getattr(vm, 'buffers', 0) or 0)
    m.update(
        ram=round(vm.percent, 2), ram_total=vm.total, ram_available=vm.available,
        ram_used_mb=round(pouzito / 2**20, 1),
        ram_cache_mb=round(max(0, min(cache, vm.total - pouzito)) / 2**20, 1),
        swap=round(sw.percent, 2) if sw.total else None, swap_total=sw.total, swap_used=sw.used,
        jadra=psutil.cpu_count() or 1, jadra_fyzicka=psutil.cpu_count(logical=False),
        boot=psutil.boot_time(),
    )
    if hasattr(os, 'getloadavg'):
        m['load1'], m['load5'], m['load15'] = (round(x, 2) for x in os.getloadavg())
    oddily = _oddily()
    m['oddily'] = oddily
    if oddily:
        nejplnejsi = max(oddily, key=lambda d: d['pct'])
        koren = next((d for d in oddily if d['mount'] == '/'), oddily[0])
        m.update(disk=nejplnejsi['pct'], disk_max_mount=nejplnejsi['mount'],
                 disk_root=koren['pct'], disk_root_mount=koren['mount'])
    m['disk_r'], m['disk_w'], m['disk_util'] = _disk_io(t)
    try:
        nic = psutil.net_io_counters(pernic=True) or {}
    except Exception:
        nic = {}
    venku = [c for n, c in nic.items() if n != 'lo' and not n.lower().startswith('loopback')]
    v = _rychlost('net', (sum(c.bytes_recv for c in venku), sum(c.bytes_sent for c in venku)), t)
    m['net_rx'], m['net_tx'] = (round(v[0] / 1024, 1), round(v[1] / 1024, 1)) if v else (None, None)
    m['fd_sys'], m['fd_sys_max'] = _fd_system()
    m['teplota'] = _teplota()
    m.update(_proces())
    return m


# =========================================================
# MĚŘENÍ — aplikace (event loop: čte struktury session/NiceGUI)
# =========================================================
def _zmer_aplikace() -> dict:
    zive = [v for v in list(intranet_session.ZIVE_PRIPOJENI.values()) if v > 0]
    klienti = sum(1 for c in list(Client.instances.values()) if c.has_socket_connection)
    pool = intranet_data.DB_POOL
    velikost = volne = None
    if pool is not None:
        velikost = getattr(pool, 'pool_size', None)
        try:
            volne = pool._cnx_queue.qsize()     # neveřejné API konektoru — jen informativně
        except Exception:
            pass
    try:
        jobs = intranet_jobs.info()
    except Exception:
        jobs = {}
    return {'prihlaseni': len(zive), 'websockety': sum(zive), 'klienti': klienti, 'jobs': jobs,
            'db_pool_velikost': velikost, 'db_pool_volne': volne}


# =========================================================
# MĚŘENÍ — MySQL (vlákno, jedno spojení z poolu)
# =========================================================
_MYSQL_STATUS = ('Uptime', 'Threads_connected', 'Threads_running', 'Max_used_connections',
                 'Slow_queries', 'Questions', 'Aborted_connects', 'Aborted_clients',
                 'Innodb_buffer_pool_read_requests', 'Innodb_buffer_pool_reads')
_MYSQL_PROMENNE = ('max_connections', 'version', 'version_comment', 'slow_query_log',
                   'long_query_time', 'innodb_buffer_pool_size')


def _txt(v):
    return v.decode('utf-8', 'replace') if isinstance(v, (bytes, bytearray)) else v


def _int(v):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def _zavri(*objekty):
    for o in objekty:
        try:
            if o is not None:
                o.close()
        except Exception:
            pass


def _zmer_mysql(vcetne_velikosti: bool) -> dict:
    conn = intranet_data.get_db_connection()
    if not conn:
        return {'ok': False, 'chyba': 'Spojení s databází se nepodařilo navázat.'}
    cur = None
    try:
        cur = conn.cursor()
        t0 = time.perf_counter()        # až po získání spojení — první volání zakládá celý pool
        cur.execute('SELECT 1')
        cur.fetchall()
        odezva = round((time.perf_counter() - t0) * 1000, 1)
        cur.execute('SHOW GLOBAL STATUS WHERE Variable_name IN (%s)'
                    % ', '.join(['%s'] * len(_MYSQL_STATUS)), _MYSQL_STATUS)
        stav = {_txt(k): _txt(v) for k, v in cur.fetchall()}
        cur.execute('SHOW GLOBAL VARIABLES WHERE Variable_name IN (%s)'
                    % ', '.join(['%s'] * len(_MYSQL_PROMENNE)), _MYSQL_PROMENNE)
        prom = {_txt(k): _txt(v) for k, v in cur.fetchall()}
        velikost = None
        if vcetne_velikosti:
            cur.execute('SELECT COALESCE(SUM(data_length + index_length), 0) '
                        'FROM information_schema.tables WHERE table_schema = DATABASE()')
            velikost = _int(cur.fetchone()[0])
    except Exception as e:
        return {'ok': False, 'chyba': str(e)[:300]}
    finally:
        _zavri(cur, conn)
    t = time.time()
    otazky = _int(stav.get('Questions'))
    v = _rychlost('mysql_q', (otazky,), t) if otazky is not None else None
    req, reads = _int(stav.get('Innodb_buffer_pool_read_requests')), _int(stav.get('Innodb_buffer_pool_reads'))
    return {
        'ok': True, 'odezva_ms': odezva,
        'verze': ' '.join(x for x in (prom.get('version'), prom.get('version_comment')) if x),
        'uptime': _int(stav.get('Uptime')),
        'vlakna': _int(stav.get('Threads_connected')), 'bezici': _int(stav.get('Threads_running')),
        'max_pouzito': _int(stav.get('Max_used_connections')),
        'max_spojeni': _int(prom.get('max_connections')),
        'pomale': _int(stav.get('Slow_queries')),
        'slow_log': prom.get('slow_query_log'), 'long_query_time': prom.get('long_query_time'),
        'qps': round(v[0], 1) if v else None,
        'aborted_connects': _int(stav.get('Aborted_connects')),
        'aborted_clients': _int(stav.get('Aborted_clients')),
        'hit': round(100 * (1 - reads / req), 2) if req and reads is not None else None,
        'buffer_pool': _int(prom.get('innodb_buffer_pool_size')),
        'velikost': velikost,
    }


# =========================================================
# HISTORIE V DB — minutové průměry, 30 dní
# =========================================================
def inicializace_db() -> bool:
    """Tabulka historie (idempotentní; chybějící sloupce doplní)."""
    global _DB_INIT
    if _DB_INIT:
        return True
    conn = intranet_data.get_db_connection()
    if not conn:
        return False
    cur = conn.cursor()
    try:
        sloupce = ',\n'.join(f'{c} FLOAT NULL' for c in _SLOUPCE)
        cur.execute(f"""CREATE TABLE IF NOT EXISTS server_status_historie (
            ts INT UNSIGNED NOT NULL PRIMARY KEY COMMENT 'unix čas začátku minuty',
            {sloupce}
        ) ENGINE=InnoDB""")
        cur.execute('SHOW COLUMNS FROM server_status_historie')
        existuje = {_txt(r[0]) for r in cur.fetchall()}
        for c in _SLOUPCE:
            if c not in existuje:
                cur.execute(f'ALTER TABLE server_status_historie ADD COLUMN {c} FLOAT NULL')
        conn.commit()
        _DB_INIT = True
    except Exception as e:
        print(f'[server_status] inicializace DB: {e}')
    finally:
        _zavri(cur, conn)
    return _DB_INIT


def _uloz_minutu(ts: int, vzorky: list) -> None:
    global _POSLEDNI_UKLID
    prumery = []
    for c in _SLOUPCE:
        h = [v[c] for v in vzorky if v.get(c) is not None]
        prumery.append(round(sum(h) / len(h), 3) if h else None)
    if not inicializace_db():
        return
    conn = intranet_data.get_db_connection()
    if not conn:
        return
    cur = conn.cursor()
    try:
        sl = ('ts',) + _SLOUPCE
        cur.execute(
            f"INSERT INTO server_status_historie ({', '.join(sl)}) VALUES ({', '.join(['%s'] * len(sl))}) "
            f"ON DUPLICATE KEY UPDATE {', '.join(f'{c}=VALUES({c})' for c in _SLOUPCE)}",
            (ts, *prumery))
        if ts - _POSLEDNI_UKLID >= 3600:
            cur.execute('DELETE FROM server_status_historie WHERE ts < %s', (ts - _DB_DNY * 86400,))
            _POSLEDNI_UKLID = ts
        conn.commit()
    finally:
        _zavri(cur, conn)


def _historie_db(rozsah_s: int, krok_s: int) -> list:
    """Průměry po `krok_s` sekundách za posledních `rozsah_s` sekund."""
    if not inicializace_db():
        return []
    conn = intranet_data.get_db_connection()
    if not conn:
        return []
    cur = conn.cursor(dictionary=True)
    krok = int(krok_s)
    try:
        vyber = ', '.join(f'AVG({c}) AS {c}' for c in _SLOUPCE)
        cur.execute(f'SELECT ts DIV {krok} * {krok} AS t, {vyber} FROM server_status_historie '
                    f'WHERE ts >= %s GROUP BY t ORDER BY t', (int(time.time()) - int(rozsah_s),))
        return [{'t': float(r['t']), **{c: (round(float(r[c]), 3) if r[c] is not None else None) for c in _SLOUPCE}}
                for r in cur.fetchall()]
    finally:
        _zavri(cur, conn)


def _nacti_posledni_hodinu() -> None:
    """Po restartu předvyplní graf „1 hodina" minutovými průměry z DB."""
    try:
        body = _historie_db(3600, 60)
    except Exception as e:
        print(f'[server_status] načtení historie: {e}')
        return
    if body and not _HISTORIE:
        _HISTORIE.extend(body)


# =========================================================
# UPOZORNĚNÍ — prahy s hysterezí, jen na stránce (nic se neodesílá)
# =========================================================
def _udalost(uroven: str, text: str) -> None:
    _UDALOSTI.appendleft((time.time(), uroven, text))
    print(f'[server_status] {text}')


def _prumer(klic: str, n: int):
    h = [b[klic] for b in list(_HISTORIE)[-n:] if b.get(klic) is not None]
    return round(sum(h) / len(h), 2) if h else None


def _pravidla(m: dict) -> list:
    """(klíč, popis, hodnota, práh, uvolnění, trvání [s], formát hodnoty)."""
    jadra = m.get('jadra') or 1
    mysql = None if not _MYSQL else (0 if _MYSQL.get('ok') else 1)
    return [
        ('disk', f"Disk téměř plný ({m.get('disk_max_mount') or '?'})", m.get('disk'), 90, 85, 0, '{:.1f} %'),
        ('ram', 'Dochází operační paměť (RAM)', m.get('ram'), 90, 85, 300, '{:.1f} %'),
        ('cpu', 'Trvale vysoké vytížení CPU (průměr 5 min)', _prumer('cpu', _VZORKU_5M), 90, 75, 300, '{:.1f} %'),
        ('load', 'Přetížený server (load 15 min)', m.get('load15'), 2 * jadra, 1.5 * jadra, 300, '{:.2f}'),
        ('fd', 'Aplikace vyčerpává limit otevřených souborů', m.get('fd_proc_pct'), 80, 70, 0, '{:.0f} %'),
        ('mysql', 'Databáze MySQL neodpovídá', mysql, 1, 1, 120, None),
    ]


PRAVIDLA_POPIS = (
    'Disk: nejplnější oddíl ≥ 90 % (vyřešeno pod 85 %)',
    'RAM: ≥ 90 % po dobu 5 min (vyřešeno pod 85 %)',
    'CPU: 5min průměr ≥ 90 % po dobu 5 min (vyřešeno pod 75 %)',
    'Load 15 min ≥ 2× počet jader po dobu 5 min',
    'Otevřené soubory aplikace ≥ 80 % limitu (ulimit -n)',
    'MySQL neodpovídá déle než 2 min',
)


def _kontroluj_prahy(m: dict, t: float) -> None:
    for klic, popis, hodnota, prah, uvolneni, trvani, fmt in _pravidla(m):
        if hodnota is None:
            continue
        hodnota_txt = fmt.format(hodnota) if fmt else (_MYSQL.get('chyba') or 'nedostupná')
        a = _AKTIVNI.get(klic)
        if hodnota >= prah:
            od = _PREKROCENO_OD.setdefault(klic, t)
            if a is None and t - od >= trvani:
                _AKTIVNI[klic] = {'od': od, 'popis': popis, 'hodnota': hodnota_txt}
                _udalost('chyba', f'{popis}: {hodnota_txt}')
            elif a is not None:
                a.update(popis=popis, hodnota=hodnota_txt)
        elif a is None:
            _PREKROCENO_OD.pop(klic, None)        # pod prahem dřív, než uplynula doba trvání
        elif hodnota < uvolneni:
            _AKTIVNI.pop(klic, None)
            _PREKROCENO_OD.pop(klic, None)
            _udalost('ok', f"Vyřešeno: {a['popis']} (teď {hodnota_txt if fmt else 'odpovídá'})")


# =========================================================
# SMYČKA NA POZADÍ
# =========================================================
async def bg_mereni():
    """Měří každých INTERVAL_S s, ukládá minutové průměry do DB a hlídá prahy.
    Spouští web_main.py (app.on_startup); chyba v jednom průchodu smyčku nezastaví."""
    global _STAV, _MYSQL
    try:
        await asyncio.to_thread(_nacti_posledni_hodinu)
        await asyncio.to_thread(_zmer_system, time.time())    # první čítače pro rychlosti
    except Exception as e:
        print(f'[server_status] start měření: {e}')
    _udalost('info', f'Monitoring spuštěn (měření každých {INTERVAL_S} s).')
    cyklus, minuta, vzorky = 0, None, []
    while True:
        await asyncio.sleep(INTERVAL_S)
        try:
            if cyklus % _MYSQL_KAZDYCH == 0:
                novy = await asyncio.to_thread(_zmer_mysql, cyklus % _VELIKOST_DB_KAZDYCH == 0)
                if novy.get('ok') and novy.get('velikost') is None:
                    novy['velikost'] = _MYSQL.get('velikost')
                novy['t'] = time.time()
                _MYSQL = novy
            t = time.time()
            m = await asyncio.to_thread(_zmer_system, t)
            m.update(_zmer_aplikace())
            m['db_vlakna'] = _MYSQL.get('vlakna') if _MYSQL.get('ok') else None
            m['t'] = t
            _STAV = m
            bod = {'t': t, **{c: m.get(c) for c in _SLOUPCE}}
            _HISTORIE.append(bod)
            klic = int(t // 60)
            if minuta is not None and klic != minuta and vzorky:
                try:
                    await asyncio.to_thread(_uloz_minutu, minuta * 60, vzorky)
                except Exception as e:
                    print(f'[server_status] uložení minuty: {e}')
                vzorky = []
            minuta = klic
            vzorky.append(bod)
            _kontroluj_prahy(m, t)
        except Exception as e:
            print(f'[server_status] bg_mereni: {e}')
        cyklus += 1


# =========================================================
# UI — formátování a grafy
# =========================================================
def _bajty(n) -> str:
    if n is None:
        return '—'
    n = float(n)
    for jednotka in ('B', 'KiB', 'MiB', 'GiB', 'TiB'):
        if abs(n) < 1024 or jednotka == 'TiB':
            return f'{n:.0f} {jednotka}' if jednotka == 'B' else f'{n:.1f} {jednotka}'
        n /= 1024
    return '—'


def _doba(s) -> str:
    if s is None:
        return '—'
    s = int(s)
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    mi = s // 60
    if d:
        return f'{d} d {h} h'
    if h:
        return f'{h} h {mi} min'
    return f'{mi} min'


def _cislo(v, fmt='{:.1f}', pripona='') -> str:
    return '—' if v is None else fmt.format(v) + pripona


def _barva(pomer: float, prahy=(0.7, 0.9)) -> str:
    return '#22c55e' if pomer < prahy[0] else ('#f59e0b' if pomer < prahy[1] else '#ef4444')


def _budik_opt(nazev: str) -> dict:
    return {
        'animation': False,
        'title': {'text': nazev, 'left': 'center', 'top': 2,
                  'textStyle': {'fontSize': 13, 'fontWeight': 600, 'color': '#374151'}},
        'series': [{
            'type': 'gauge', 'min': 0, 'max': 100, 'startAngle': 210, 'endAngle': -30,
            'center': ['50%', '62%'], 'radius': '88%',
            'progress': {'show': True, 'width': 14, 'itemStyle': {'color': '#22c55e'}},
            'axisLine': {'lineStyle': {'width': 14, 'color': [[1, '#e5e7eb']]}},
            'pointer': {'show': False}, 'anchor': {'show': False},
            'axisTick': {'show': False}, 'splitLine': {'show': False}, 'axisLabel': {'show': False},
            'title': {'show': True, 'offsetCenter': [0, '48%'], 'fontSize': 11, 'color': '#6b7280'},
            'detail': {'offsetCenter': [0, '8%'], 'fontSize': 22, 'fontWeight': 700,
                       'formatter': '—', 'color': '#9ca3af'},
            'data': [{'value': 0, 'name': ''}],
        }],
    }


def _nastav_budik(budik, hodnota, maximum=100.0, text=None, popisek='', prahy=(0.7, 0.9)) -> None:
    s = budik.options['series'][0]
    if hodnota is None:
        s['data'] = [{'value': 0, 'name': popisek}]
        s['detail'].update(formatter=text or '—', color='#9ca3af')
    else:
        barva = _barva(hodnota / maximum if maximum else 0, prahy)
        s['max'] = maximum
        s['data'] = [{'value': min(hodnota, maximum), 'name': popisek}]
        s['progress']['itemStyle']['color'] = barva
        s['detail'].update(formatter=text or f'{hodnota:.1f} %', color=barva)
    budik.update()


# (klíč, nadpis, jednotka osy, [(řada, popisek, barva)], skládané)
_GRAFY = (
    ('load', 'Průměrná zátěž (load average)', '',
     [('load1', '1 min', '#22c55e'), ('load5', '5 min', '#eab308'), ('load15', '15 min', '#06b6d4')], False),
    ('cpu', 'CPU a vytížení disku', '%',
     [('cpu_system', 'Systém', '#22c55e'), ('cpu_user', 'Uživatel', '#eab308'),
      ('iowait', 'I/O wait', '#06b6d4'), ('disk_util', 'Vytížení disku', '#f97316')], False),
    ('pamet', 'Paměť serveru', 'MiB',
     [('ram_used_mb', 'Použito', '#eab308'), ('ram_cache_mb', 'Cache / buffery', '#06b6d4')], True),
    ('io', 'Síť a disk', 'KiB/s',
     [('net_rx', 'Síť – příjem', '#22c55e'), ('net_tx', 'Síť – odesílání', '#a855f7'),
      ('disk_r', 'Disk – čtení', '#06b6d4'), ('disk_w', 'Disk – zápis', '#f97316')], False),
    ('app', 'JIPka – klienti a databáze', '',
     [('klienti', 'Otevřená okna', '#3b82f6'), ('prihlaseni', 'Přihlášení uživatelé', '#22c55e'),
      ('db_vlakna', 'Spojení MySQL', '#f97316')], False),
    ('rss', 'Paměť aplikace (vč. worker procesů)', 'MiB', [('rss_mb', 'RSS', '#8b5cf6')], False),
)


def _graf_opt(nazev: str, jednotka: str, rady: list, skladany: bool) -> dict:
    return {
        'animation': False,
        'title': {'text': nazev, 'left': 'center', 'textStyle': {'fontSize': 14, 'fontWeight': 600}},
        'tooltip': {'trigger': 'axis',
                    ':valueFormatter': '(v) => (v == null ? "—" : Number(v).toLocaleString("cs-CZ", '
                                       '{maximumFractionDigits: 2}))'},
        'legend': {'bottom': 0, 'type': 'scroll'},
        'grid': {'left': 56, 'right': 18, 'top': 42, 'bottom': 52},
        'xAxis': {'type': 'time'},
        'yAxis': {'type': 'value', 'name': jednotka, 'min': 0},
        'series': [{'name': popisek, 'type': 'line', 'showSymbol': False, 'connectNulls': False,
                    'color': barva, 'lineStyle': {'width': 1.5}, 'areaStyle': {'opacity': 0.15},
                    **({'stack': 'celkem'} if skladany else {}), 'data': []}
                   for _klic, popisek, barva in rady],
    }


def _top_procesy(n: int = 10) -> list:
    """Procesy serveru podle paměti. Bez příkazové řádky — mohla by obsahovat hesla."""
    vlastni = {_PROC.pid} | set(_DETI.copy())      # _DETI mění vlákno měření
    vysl = []
    for p in psutil.process_iter(['pid', 'name', 'username', 'memory_info', 'cpu_percent']):
        i = p.info
        mi = i.get('memory_info')
        if not mi:
            continue
        nazev = i.get('name') or '?'
        if i['pid'] == _PROC.pid:
            nazev += ' (JIPka)'
        elif i['pid'] in vlastni:
            nazev += ' (JIPka worker)'
        vysl.append({'pid': i['pid'], 'nazev': nazev, 'uzivatel': i.get('username') or '—',
                     'rss': mi.rss, 'ram': _bajty(mi.rss), 'cpu': round(i.get('cpu_percent') or 0.0, 1)})
    vysl.sort(key=lambda d: d['rss'], reverse=True)
    return vysl[:n]


def _os_popis() -> str:
    try:
        if _LINUX:
            return platform.freedesktop_os_release().get('PRETTY_NAME') or f'Linux {platform.release()}'
    except OSError:
        pass
    return f'{platform.system()} {platform.release()}'


_OS = _os_popis()


# =========================================================
# UI — modul
# =========================================================
def _radek(popis: str, hodnota, barva: str = 'text-gray-800') -> None:
    with ui.row().classes('w-full justify-between items-center gap-2 py-0.5 border-b border-gray-100'):
        ui.label(popis).classes('text-sm text-gray-500')
        ui.label(str(hodnota)).classes(f'text-sm font-semibold {barva} text-right')


def _mrizka(min_px: int):
    return ui.element('div').classes('w-full').style(
        f'display:grid;grid-template-columns:repeat(auto-fill,minmax(min({min_px}px,100%),1fr));gap:12px')


async def vykresli(user_id, user_name=None, vsechna_prava=None):
    if not je_hlavni_admin(user_id):
        ui.label('Server Status je dostupný jen hlavnímu administrátorovi.').classes('text-gray-500')
        return
    stav = {'rozsah': '1h'}

    with ui.column().classes('w-full gap-4'):
        with ui.row().classes('w-full items-center justify-between flex-wrap gap-2'):
            with ui.column().classes('gap-0'):
                ui.label('🖥️ Server Status').classes('text-2xl font-bold text-gray-800')
                lbl_info = ui.label('Čekám na první měření…').classes('text-sm text-gray-500')

            async def _zmena_rozsahu(e):
                stav['rozsah'] = e.value
                await obnov(vynutit=True)
            ui.toggle({k: v[0] for k, v in _ROZSAHY.items()}, value='1h', on_change=_zmena_rozsahu) \
                .props('no-caps unelevated rounded toggle-color=primary')

        banner = ui.column().classes('w-full gap-2')

        # --- souhrnná čísla ---
        staty = {}
        with _mrizka(160):
            for klic, nazev in (('uptime', 'Běh serveru'), ('app', 'Běh aplikace'), ('jadra', 'Jádra CPU'),
                                ('ram', 'Celková RAM'), ('swap', 'Swap'), ('teplota', 'Teplota CPU')):
                with ui.card().classes('items-center justify-center p-3 shadow-sm rounded-xl gap-1') as karta:
                    ui.label(nazev).classes('text-xs uppercase tracking-wide text-gray-500')
                    staty[klic] = ui.label('—').classes('text-2xl font-bold text-emerald-600')
                if klic == 'teplota':
                    staty['teplota_karta'] = karta
                    karta.set_visibility(False)

        # --- budíky ---
        budiky = {}
        with _mrizka(190):
            for klic, nazev in (('cpu', 'CPU (5 min)'), ('iowait', 'I/O wait (5 min)'), ('ram', 'Paměť RAM'),
                                ('swap', 'Swap'), ('root', 'Kořenový oddíl'), ('disk', 'Nejplnější oddíl'),
                                ('fd', 'Otevřené soubory procesu')):
                with ui.card().classes('p-1 shadow-sm rounded-xl'):
                    budiky[klic] = ui.echart(_budik_opt(nazev)).classes('w-full').style('height:170px')

        # --- grafy ---
        grafy = {}
        with _mrizka(440):
            for klic, nazev, jednotka, rady, skladany in _GRAFY:
                with ui.card().classes('p-2 shadow-sm rounded-xl'):
                    grafy[klic] = ui.echart(_graf_opt(nazev, jednotka, rady, skladany)) \
                        .classes('w-full').style('height:300px')

        # --- oddíly a procesy ---
        with _mrizka(440):
            with ui.card().classes('p-3 shadow-sm rounded-xl gap-2'):
                ui.label('Volné místo na oddílech').classes('text-base font-semibold text-gray-800')
                tab_oddily = ui.table(columns=[
                    {'name': 'mount', 'label': 'Oddíl', 'field': 'mount', 'align': 'left'},
                    {'name': 'fs', 'label': 'FS', 'field': 'fs', 'align': 'left'},
                    {'name': 'dev', 'label': 'Zařízení', 'field': 'dev', 'align': 'left'},
                    {'name': 'velikost', 'label': 'Velikost', 'field': 'velikost', 'align': 'right'},
                    {'name': 'volno', 'label': 'Volno', 'field': 'volno', 'align': 'right'},
                    {'name': 'pct', 'label': 'Využito', 'field': 'pct', 'align': 'right'},
                ], rows=[], row_key='mount').props('flat dense').classes('w-full')
                tab_oddily.add_slot('body-cell-pct', r'''
                    <q-td :props="props">
                      <q-badge :color="props.value >= 90 ? 'red' : (props.value >= 80 ? 'orange' : 'green')"
                               :label="props.value.toFixed(1) + ' %'" />
                    </q-td>''')
            with ui.card().classes('p-3 shadow-sm rounded-xl gap-2'):
                ui.label('Procesy podle paměti').classes('text-base font-semibold text-gray-800')
                tab_procesy = ui.table(columns=[
                    {'name': 'pid', 'label': 'PID', 'field': 'pid', 'align': 'left'},
                    {'name': 'nazev', 'label': 'Proces', 'field': 'nazev', 'align': 'left'},
                    {'name': 'uzivatel', 'label': 'Uživatel', 'field': 'uzivatel', 'align': 'left'},
                    {'name': 'ram', 'label': 'RAM', 'field': 'ram', 'align': 'right'},
                    {'name': 'cpu', 'label': 'CPU % jádra', 'field': 'cpu', 'align': 'right'},
                ], rows=[], row_key='pid').props('flat dense').classes('w-full')

        # --- aplikace, MySQL, upozornění ---
        with _mrizka(360):
            karta_app = ui.card().classes('p-3 shadow-sm rounded-xl gap-1')
            karta_mysql = ui.card().classes('p-3 shadow-sm rounded-xl gap-1')
            with ui.card().classes('p-3 shadow-sm rounded-xl gap-2'):
                ui.label('🔔 Upozornění').classes('text-base font-semibold text-gray-800')
                for p in PRAVIDLA_POPIS:
                    ui.label(f'• {p}').classes('text-xs text-gray-500 leading-tight')
                udalosti = ui.column().classes('w-full gap-0 mt-1')

    def _karta_app(m: dict):
        karta_app.clear()
        j = m.get('jobs') or {}
        with karta_app:
            ui.label('🧩 Aplikace JIPka').classes('text-base font-semibold text-gray-800 mb-1')
            _radek('Přihlášení uživatelé', m.get('prihlaseni', '—'))
            _radek('Otevřená okna / WebSocket spojení', f"{m.get('klienti', '—')} / {m.get('websockety', '—')}")
            _radek('Paměť hlavního procesu', _bajty(m.get('rss_hlavni')))
            _radek(f"Paměť worker procesů ({m.get('procesu_deti', 0)})", _bajty(m.get('rss_deti')))
            _radek('CPU aplikace (% celého serveru)', _cislo(m.get('cpu_proc'), pripona=' %'))
            _radek('Vlákna', m.get('vlakna', '—'))
            fd, limit = m.get('fd_proc'), m.get('fd_limit')
            _radek('Otevřené soubory / limit', f"{_cislo(fd, '{:,}')} / {_cislo(limit, '{:,}')}".replace(',', ' '))
            _radek('Proces-pool: běžící úlohy / workerů',
                   f"{j.get('bezici_ulohy', '—')} / {j.get('max_workers', '—')}"
                   + ('' if j.get('pool_aktivni', True) else ' (neaktivní)'))
            _radek('Proces-pool: náhradní běh ve vlákně', j.get('fallbacku_na_vlakno', '—'),
                   'text-orange-600' if j.get('fallbacku_na_vlakno') else 'text-gray-800')
            _radek('DB pool: volná spojení / velikost',
                   f"{_cislo(m.get('db_pool_volne'), '{}')} / {_cislo(m.get('db_pool_velikost'), '{}')}")
            fd_sys, fd_max = m.get('fd_sys'), m.get('fd_sys_max')
            _radek('File deskriptory systému', _cislo(fd_sys, '{:,}').replace(',', ' ')
                   + (f' (max {fd_max:,})'.replace(',', ' ') if fd_max and fd_max < 10**12 else ''))

    def _karta_mysql():
        karta_mysql.clear()
        d = _MYSQL
        with karta_mysql:
            ui.label('🐬 Databáze MySQL').classes('text-base font-semibold text-gray-800 mb-1')
            if not d:
                ui.label('Čekám na první měření…').classes('text-sm text-gray-500')
                return
            if not d.get('ok'):
                ui.label(f"❌ Nedostupná: {d.get('chyba')}").classes('text-sm text-red-600')
                return
            _radek('Stav', '✅ odpovídá', 'text-emerald-600')
            _radek('Verze', d.get('verze') or '—')
            _radek('Běží', _doba(d.get('uptime')))
            _radek('Odezva dotazu',_cislo(d.get('odezva_ms'), pripona=' ms'))
            _radek('Spojení / max_connections', f"{d.get('vlakna', '—')} / {d.get('max_spojeni', '—')}")
            _radek('Nejvíc spojení najednou', d.get('max_pouzito', '—'))
            _radek('Právě běžící dotazy', d.get('bezici', '—'))
            _radek('Dotazy za sekundu', _cislo(d.get('qps')))
            pomale = d.get('pomale')
            _radek(f"Pomalé dotazy (slow log {d.get('slow_log') or '?'}, > {d.get('long_query_time') or '?'} s)",
                   pomale if pomale is not None else '—', 'text-orange-600' if pomale else 'text-gray-800')
            _radek('Odmítnutá / přerušená spojení',
                   f"{d.get('aborted_connects', '—')} / {d.get('aborted_clients', '—')}")
            _radek('Buffer pool: zásah z RAM / velikost',
                   f"{_cislo(d.get('hit'), '{:.2f}', ' %')} / {_bajty(d.get('buffer_pool'))}")
            _radek('Velikost databáze', _bajty(d.get('velikost')))

    def _banner_a_udalosti():
        banner.clear()
        with banner:
            if _AKTIVNI:
                for a in list(_AKTIVNI.values()):
                    od = datetime.datetime.fromtimestamp(a['od']).strftime('%d.%m. %H:%M')
                    with ui.row().classes('w-full items-center gap-2 px-4 py-2 rounded-xl '
                                          'bg-red-50 border border-red-200'):
                        ui.label('⚠️').classes('text-lg')
                        ui.label(f"{a['popis']}: {a['hodnota']}").classes('font-semibold text-red-700')
                        ui.label(f'od {od}').classes('text-sm text-red-500')
        udalosti.clear()
        with udalosti:
            ui.label('Poslední události').classes('text-sm font-semibold text-gray-700')
            if not _UDALOSTI:
                ui.label('Zatím žádné.').classes('text-xs text-gray-400')
            for ts, uroven, text in list(_UDALOSTI)[:12]:
                barva = {'chyba': 'text-red-600', 'ok': 'text-emerald-600'}.get(uroven, 'text-gray-500')
                cas = datetime.datetime.fromtimestamp(ts).strftime('%d.%m. %H:%M')
                ui.label(f'{cas}  {text}').classes(f'text-xs {barva} leading-tight')

    async def _body() -> list:
        nazev, rozsah_s, krok_s = _ROZSAHY[stav['rozsah']]
        if krok_s is None:
            return list(_HISTORIE)
        uloz = _CACHE_DB.get(stav['rozsah'])
        if uloz and time.time() - uloz[0] < 60:
            return uloz[1]
        try:
            body = await asyncio.to_thread(_historie_db, rozsah_s, krok_s)
        except Exception as e:
            print(f'[server_status] historie {nazev}: {e}')
            body = []
        _CACHE_DB[stav['rozsah']] = (time.time(), body)
        return body

    async def obnov(vynutit: bool = False):
        if not vynutit:
            try:
                if app.storage.user.get('intranet_tab') != TAB:
                    return                      # modul není vidět — nic neposílej do prohlížeče
            except Exception:
                return
        m = _STAV
        if not m:
            return
        lbl_info.text = (f"{_HOST} · {_OS} · Python {platform.python_version()} · "
                         f"měřeno {datetime.datetime.fromtimestamp(m['t']).strftime('%H:%M:%S')}")

        staty['uptime'].text = _doba(time.time() - m['boot'])
        staty['app'].text = _doba(time.time() - _PROC.create_time())
        staty['jadra'].text = f"{m['jadra']}" + (f" ({m['jadra_fyzicka']} fyz.)" if m.get('jadra_fyzicka') else '')
        staty['ram'].text = _bajty(m.get('ram_total'))
        staty['swap'].text = _bajty(m.get('swap_total')) if m.get('swap_total') else 'není'
        if m.get('teplota') is not None:
            staty['teplota'].text = f"{m['teplota']:.0f} °C"
            staty['teplota_karta'].set_visibility(True)

        _nastav_budik(budiky['cpu'], _prumer('cpu', _VZORKU_5M))
        _nastav_budik(budiky['iowait'], _prumer('iowait', _VZORKU_5M),
                      text=None if _LINUX else 'jen Linux', prahy=(0.1, 0.25))
        _nastav_budik(budiky['ram'], m.get('ram'), prahy=(0.8, 0.9),
                      popisek=f"{_bajty(m.get('ram_available'))} volno")
        _nastav_budik(budiky['swap'], m.get('swap'), text=None if m.get('swap_total') else 'bez swapu',
                      prahy=(0.5, 0.8), popisek=_bajty(m.get('swap_used')) if m.get('swap_total') else '')
        _nastav_budik(budiky['root'], m.get('disk_root'), prahy=(0.8, 0.9), popisek=m.get('disk_root_mount', ''))
        _nastav_budik(budiky['disk'], m.get('disk'), prahy=(0.8, 0.9), popisek=m.get('disk_max_mount', ''))
        fd, limit = m.get('fd_proc'), m.get('fd_limit')
        if fd is not None and limit:
            _nastav_budik(budiky['fd'], float(fd), maximum=float(limit), text=f'{fd:,}'.replace(',', ' '),
                          popisek=f'limit {limit:,}'.replace(',', ' '))
        else:
            _nastav_budik(budiky['fd'], None, text='jen Linux')

        body = await _body()
        for klic, _nazev, _jednotka, rady, _skladany in _GRAFY:
            g = grafy[klic]
            for serie, (rada, _p, _b) in zip(g.options['series'], rady):
                serie['data'] = [[int(b['t'] * 1000), b.get(rada)] for b in body]
            if klic == 'pamet' and m.get('ram_total'):
                g.options['yAxis']['max'] = round(m['ram_total'] / 2**20)
            if klic == 'load':
                g.options['series'][0]['markLine'] = {
                    'symbol': 'none', 'silent': True, 'lineStyle': {'color': '#ef4444', 'type': 'dashed'},
                    'label': {'formatter': 'jádra'}, 'data': [{'yAxis': m.get('jadra') or 1}]}
            g.update()

        tab_oddily.rows = [{**d, 'velikost': _bajty(d['total']), 'volno': _bajty(d['free'])}
                           for d in m.get('oddily') or []]
        if time.time() - _CACHE_PROCESY['t'] >= INTERVAL_S - 1:
            try:
                _CACHE_PROCESY['data'] = await asyncio.to_thread(_top_procesy)
            except Exception as e:
                print(f'[server_status] procesy: {e}')
            _CACHE_PROCESY['t'] = time.time()
        tab_procesy.rows = _CACHE_PROCESY['data']

        _karta_app(m)
        _karta_mysql()
        _banner_a_udalosti()

    ui.timer(INTERVAL_S, obnov)
    await obnov(vynutit=True)
