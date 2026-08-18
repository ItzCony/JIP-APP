# -*- coding: utf-8 -*-
"""
Čistý výpočetní engine modulu Cenopřípad — kontrola nastavení letákových cen.

Bez závislosti na DB/UI. Funkce pracují nad slovníky:
  - `master`  = {kód(str): {nc, nc2, nc3, nc4, nc5, dnc, dph, id, sortiment, op, nazev}}
  - `radek`   = vstupní řádek žádosti s pojmenovanými poli dle typu dlaždice
Vrací verdikt OK / CHYBA / VYJIMKA + dopočtené hodnoty (OP a marže v % smí v UI
vidět POUZE správce — viz intranet_cenopripad).

Reverzně získaný model z `Kontrola letáky_ceníky_ostatní-*.xlsb` (ověřeno self-testem):
  • OP produktu = VLOOKUP(Sortiment -> OP soubor, POSLEDNÍ sloupec listu OP).
    Z OP se počítají všechny marže.
  • Sazba DPH je v datech TEXT "      12%" -> nutno parsovat (Excel coercuje sám).
  • Join produktu = přesná shoda str(int(kód)); 'porovnání' navíc zfill(8)
    (kvůli TEXT(A,"00000000")). Kódy s koncovým '!' jsou speciální -> #N/A.
  • mimoleták CC/MO/VO mohou být nevyplněné ('x'/prázdné) -> nekontrolovat.
  • Výjimka: karta s názvem začínajícím '*', která hlásí chybu, je OK
    (platí pro webportal/sklad6/mimoletak; ne pro porovnani/paima).
"""
from __future__ import annotations
import io
import math

# ----------------------------------------------------------------------------
# Prahy (z zadání)
# ----------------------------------------------------------------------------
PRAH_POROVNANI = 0.03   # % marže netto < 3 % -> špatně (dle podm. formátování souboru: „Hodnota buňky < 0,03")
PRAH_WEBPORTAL = 0.10   # ŠPATNĚ jen když ANC i NC2 < 10 % ZÁROVEŇ (AND)
PRAH_SKLAD6    = 0.05   # ŠPATNĚ jen když ANC i NC2 < 5 % ZÁROVEŇ (AND) — dle podm. formátování souboru „Hodnota buňky < 0,05" (sloupce w+x)
PRAH_MIMOLETAK = 0.05   # vyplněná MARŽE CC/MO/VO < 5 % -> špatně (marže = workbook, přes Akční FC×(1−OP))

OK, CHYBA, VYJIMKA = "OK", "CHYBA", "VYJIMKA"

# Důvod řádku, jehož kód není v cenopripad_master (nová karta bez cen). Text se
# porovnává i v UI (obarvení kódu), proto konstanta na jednom místě.
DUVOD_NENALEZEN = "Produkt nenalezen (#N/A)"

# ----------------------------------------------------------------------------
# Parsování hodnot
# ----------------------------------------------------------------------------
def je_excel_chyba(v) -> bool:
    """pyxlsb vrací excelovské chyby jako řetězce '0x2a' (#N/A), '0xf' (#VALUE!)…"""
    return isinstance(v, str) and v.startswith("0x")


def parse_cislo(v):
    """Číslo z hodnoty. '12%' / '  12 %' -> 0.12. Nečíslo/None/chyba -> None."""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return None if (isinstance(v, float) and math.isnan(v)) else float(v)
    s = str(v).strip().replace("\xa0", "").replace(" ", "")
    if not s:
        return None
    try:
        if s.endswith("%"):
            return float(s[:-1].replace(",", ".")) / 100.0
        return float(s.replace(",", "."))
    except ValueError:
        return None


def parse_cena_kc(v):
    """Cena v KORUNÁCH. Na rozdíl od parse_cislo NEBERE procenta.
      • 12.5 / '12,5' / '1 234,50' -> 12.5 / 12.5 / 1234.5
      • '5%', 'dohodou', prázdno, None -> None
    Používá se pro sloupec „PC bez DPH" (vstupní kontrola i export do skladu)."""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return None if (isinstance(v, float) and math.isnan(v)) else float(v)
    s = str(v).replace("\xa0", "").replace(" ", "").strip()
    if not s or "%" in s:
        return None
    try:
        return float(s.replace(",", "."))
    except ValueError:
        return None


def parse_prirazka(v):
    """Přirážka/srážka -> ZLOMEK.
      • '5%'   -> 0,05  (explicitní procenta)
      • '10'   -> 0,10  (CELÉ číslo |n|≥1 = procenta, dělí se 100)
      • '0,05' -> 0,05  (DESETINNÉ |n|<1 = už zlomek, bere se přímo = 5 %)
      • srážka přes mínus: '-10' -> -0,10, '-0,05' -> -0,05
    Prázdné/nečíslo -> None."""
    if v is None:
        return None
    s = str(v).strip()
    if not s:
        return None
    if "%" in s:
        return parse_cislo(s)            # parse_cislo dělí procenta /100
    n = parse_cislo(s)
    if n is None:
        return None
    return n / 100.0 if abs(n) >= 1 else n   # celé = procenta; desetinné <1 = už zlomek


def normalizuj_kod(v):
    """Vstupní kód -> textový klíč jako Excel CONCATENATE: 40206072.0 -> '40206072'."""
    if v is None:
        return None
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    if isinstance(v, int):
        return str(v)
    s = str(v).strip()
    return s or None


def _je_prazdno(v) -> bool:
    """Nevyplněná buňka (mimoleták CC/MO/VO): None, '' nebo 'x'/'X'."""
    if v is None:
        return True
    return str(v).strip().lower() in ("", "x")


def _vyjimka_hvezdicka(nazev) -> bool:
    return isinstance(nazev, str) and nazev.strip().startswith("*")


def _min_nenulove(hodnoty):
    """Mimika MIN(IF(rozsah=0; MAX(rozsah); rozsah)) = nejmenší z nenulových.
    None se bere jako 0; jsou-li všechny 0, vrací 0 (jako Excel)."""
    nums = [(h if h is not None else 0.0) for h in hodnoty]
    if not nums:
        return 0.0
    mx = max(nums)
    return min((mx if h == 0 else h) for h in nums)


# ----------------------------------------------------------------------------
# Vyhodnocení jednotlivých typů
# ----------------------------------------------------------------------------
def _doplnit_kontrolu(out, radek):
    """SAMOSTATNÁ kontrola nastavení ceny — NEovlivňuje verdikt případu (jen info „pro nás").

    Přirážka/srážka se zadává DVĚMA způsoby (dle příručky):
      • s „%“  → PROCENTNÍ:  očekávané = výchozí × (1 + přirážka)   (např. „5 %“, „-3 %“)
      • bez „%“ → v KORUNÁCH: očekávané = výchozí + přirážka         (např. „10“, „-2,5“)
    Vzorec: očekávané ≈ PC bez DPH; povolená RELATIVNÍ odchylka 0,005 (= 0,5 %, ne Kč):
    |očekávané − PC| / očekávané ≤ 0,005 → 'OK', jinak 'Chyba'.
    PRÁZDNÁ přirážka se bere jako 0 (žádná přirážka → očekávané = výchozí cena).
    Nevyplněná výchozí cena / PC -> kontrola = None (nepočítá se)."""
    vychozi = parse_cislo(radek.get("vychozi_cena"))
    pc = parse_cislo(radek.get("pc_bez_dph"))
    raw = radek.get("prirazka")
    s = "" if raw is None else str(raw).strip()
    if vychozi is None or pc is None:
        out["kontrola_duvod"] = "Kontrola: nevyplněno (výchozí cena / PC)"
        return
    if not s:
        # Prázdná buňka = 0 (žádná přirážka/srážka) → očekávané = výchozí cena.
        ocekavane = vychozi
        popis = "bez přirážky (0)"
    elif "%" in s:
        prirazka = parse_cislo(s)                # zlomek (parse_cislo dělí % /100)
        if prirazka is None:
            out["kontrola_duvod"] = "Kontrola: neplatná hodnota přirážky"
            return
        ocekavane = vychozi * (1.0 + prirazka)
        popis = f"přirážka {prirazka*100:+.2f} %"
    else:
        kc = parse_cislo(s)                       # absolutní Kč (bez „%“)
        if kc is None:
            out["kontrola_duvod"] = "Kontrola: neplatná hodnota přirážky"
            return
        ocekavane = vychozi + kc
        popis = f"přirážka {kc:+.2f} Kč"
    if ocekavane == 0:
        odchylka = 0.0 if pc == 0 else 1.0                  # 0 vs nenula = 100 % mimo
    else:
        odchylka = abs(ocekavane - pc) / abs(ocekavane)     # RELATIVNÍ odchylka (zlomek)
    # tolerance 0,005 = 0,5 % (relativně, ne v Kč); +epsilon kvůli float-šumu na hranici.
    out["kontrola"] = "OK" if odchylka <= 0.005 + 1e-9 else "Chyba"
    out["kontrola_duvod"] = (f"očekávané {ocekavane:.2f} vs PC {pc:.2f} ({popis}; "
                             f"odchylka {odchylka*100:.2f} %, limit 0,5 %)")


def vyhodnot_porovnani(radek, master):
    """porovnani: VC=PC×(1−bonus); nejvýh.NC=min nenul.{NC,NC2,NC5(+NC3 když ID~'A')}−komp;
    netto=nejvýh.NC×(1−OP); % marže netto=(VC−netto)/VC (kladné) jinak /netto.
    Špatně když < 3 % (dle podm. formátování souboru „Hodnota buňky < 0,03"). (Bez výjimky hvězdičkou.)
    Navíc samostatná „Kontrola" (výchozí×(1+přirážka)≈PC) — mimo verdikt, viz _doplnit_kontrolu."""
    out = {"op": None, "vc": None, "netto": None, "nej_nc": None, "sleva_nej_nc": None,
           "marze_netto": None, "verdikt": CHYBA, "duvod": "",
           "kontrola": None, "kontrola_duvod": ""}
    _doplnit_kontrolu(out, radek)   # samostatně, nezávisle na masteru i verdiktu
    kod = normalizuj_kod(radek.get("kod_produktu"))
    m = master.get(kod.zfill(8)) if kod else None
    if m is None:
        out["duvod"] = DUVOD_NENALEZEN
        return out
    op = m["op"] if m["op"] is not None else 0.0  # prázdné OP = 0 (jako workbook)
    bez_dph = parse_cislo(radek.get("pc_bez_dph"))
    bonus = parse_cislo(radek.get("bonus")) or 0.0
    komp = parse_cislo(radek.get("kompenzace")) or 0.0
    if bez_dph is None:
        out["duvod"] = "Chybí PC bez DPH"
        return out
    vc = bez_dph - bez_dph * bonus
    if "A" in str(m["id"] or ""):
        zaklad = _min_nenulove([m["nc"], m["nc2"], m["nc5"], m["nc3"]])
    else:
        zaklad = _min_nenulove([m["nc"], m["nc2"], m["nc5"]])
    p = zaklad - komp
    netto = p - p * op
    out["op"], out["vc"], out["netto"], out["nej_nc"] = op, vc, netto, p
    # S „sleva z nej NC" = IF((VC−P)>0; (VC−P)/VC; (VC−P)/P) — informativní (neovlivňuje verdikt).
    rozdil_s = vc - p
    delitel_s = vc if rozdil_s > 0 else p
    out["sleva_nej_nc"] = (rozdil_s / delitel_s) if delitel_s else None
    rozdil = vc - netto
    delitel = vc if rozdil > 0 else netto
    if delitel == 0:
        out["duvod"] = "Dělení nulou"
        return out
    marze = rozdil / delitel
    out["marze_netto"] = marze
    # Dle podm. formátování v souboru („Hodnota buňky < 0,03") — ostře menší než 3 %.
    if marze < PRAH_POROVNANI:
        out["verdikt"], out["duvod"] = CHYBA, f"% marže netto {marze*100:.2f} % < 3 %"
    else:
        out["verdikt"], out["duvod"] = OK, f"% marže netto {marze*100:.2f} %"
    return out


def _vyhodnot_letak(radek, master, kod_key, prodejni_key, nakupni_key, nc2_key, prah):
    """Společné pro webportal i sklad6:
    marže ANC = (prodejní − nákupní×(1−OP))/prodejní;
    marže NC2 = totéž s NC2 (když NC2>0), jinak fallback = marže ANC.
    Špatně JEN když je POD prahem ANC I NC2 ZÁROVEŇ (AND — „ANC menší a plus NC2
    menší = chyba"; je-li aspoň jedna nad prahem, je OK). Práh: webportál 0,1; sklad6 0,05.
    Výjimka: název začíná '*'."""
    out = {"op": None, "marze_anc": None, "marze_nc2": None,
           "verdikt": CHYBA, "duvod": ""}
    hvezda = _vyjimka_hvezdicka(radek.get("nazev"))

    def chyb(d):
        out["duvod"] = d
        out["verdikt"] = VYJIMKA if hvezda else CHYBA
        return out

    kod = normalizuj_kod(radek.get(kod_key))
    m = master.get(kod) if kod else None
    if m is None:
        return chyb(DUVOD_NENALEZEN)
    op = m["op"] if m["op"] is not None else 0.0  # prázdné OP = 0 (jako workbook)
    prodejni = parse_cislo(radek.get(prodejni_key))
    nakupni = parse_cislo(radek.get(nakupni_key))
    nc2 = parse_cislo(radek.get(nc2_key))
    if prodejni is None or prodejni == 0 or nakupni is None:
        return chyb("Chybí cena nebo dělení nulou")
    marze_anc = (prodejni - (nakupni - nakupni * op)) / prodejni
    if nc2 is not None and nc2 > 0:
        marze_nc2 = (prodejni - (nc2 - nc2 * op)) / prodejni
    else:
        marze_nc2 = marze_anc
    out.update(op=op, marze_anc=marze_anc, marze_nc2=marze_nc2)
    prah_pct = prah * 100
    # ŠPATNĚ jen když je POD prahem ANC i NC2 ZÁROVEŇ (AND) — „ANC menší a plus NC2 menší = chyba".
    # Výjimka: karta, jejíž název začíná '*', se i při chybě bere jako správná (VYJIMKA → necount).
    if marze_anc < prah and marze_nc2 < prah:
        out["verdikt"] = VYJIMKA if hvezda else CHYBA
        out["duvod"] = (f"ANC {marze_anc*100:.2f} % i NC2 {marze_nc2*100:.2f} % < {prah_pct:.0f} %"
                        + (" — výjimka * (bráno jako OK)" if hvezda else ""))
    else:
        out["verdikt"] = OK
        out["duvod"] = f"ANC {marze_anc*100:.2f} %, NC2 {marze_nc2*100:.2f} % (≥ {prah_pct:.0f} %)"
    return out


def vyhodnot_webportal(radek, master):
    return _vyhodnot_letak(radek, master, "kod", "akcni_pc", "akcni_nc",
                           "aktualni_nc2", PRAH_WEBPORTAL)


def vyhodnot_sklad6(radek, master):
    return _vyhodnot_letak(radek, master, "kod", "pc_akce", "anc", "nc2", PRAH_SKLAD6)


def vyhodnot_mimoletak(radek, master):
    """mimoletak — MARŽE přesně dle kontrolního workbooku (sloupce marže CC/MO/VO):
       náklad = Akční FC × (1−OP)
       marže CC/MO = (cena_s_DPH/(1+DPH) − náklad) / (cena_s_DPH/(1+DPH))
       marže VO    = (VO_bez_DPH − náklad) / VO_bez_DPH
    DPH i OP se berou z MASTERU (DATA_POROVNANI). NE přirážka (ta dělí akční NCK)!
    Kontrolují se jen VYPLNĚNÉ CC/MO/VO; špatně když < 0,05. Výjimka: název '*'."""
    out = {"op": None, "dph": None, "marze_cc": None, "marze_mo": None,
           "marze_vo": None, "verdikt": CHYBA, "duvod": ""}
    hvezda = _vyjimka_hvezdicka(radek.get("nazev"))

    def chyb(d):
        out["duvod"] = d
        out["verdikt"] = VYJIMKA if hvezda else CHYBA
        return out

    # Kód může přijít s vodicími nulami (nové řádky, 8 míst) i bez (starší
    # uložené případy) → zkusí se obojí.
    kod = normalizuj_kod(radek.get("kod"))
    m = (master.get(kod) or master.get(kod.zfill(8))) if kod else None
    if m is None:
        return chyb(DUVOD_NENALEZEN)
    op, dph = m["op"], m["dph"]
    op = op if op is not None else 0.0  # prázdné OP = 0 (jako workbook)
    fc = parse_cislo(radek.get("akcni_fc"))
    if dph is None or fc is None:
        return chyb("Chybí DPH/Akční FC")
    out["op"], out["dph"] = op, dph
    naklad = fc - fc * op
    problemy, vyplnene = [], 0

    def _marze_dph(cena_s_dph):
        net = cena_s_dph / (1.0 + dph)
        return None if net == 0 else (net - naklad) / net

    for klic, popisek, bez_dph in (("cc_s_dph", "CC", False),
                                   ("mo_s_dph", "MO", False),
                                   ("vo_bez_dph", "VO", True)):
        raw = radek.get(klic)
        if _je_prazdno(raw):
            continue
        cislo = parse_cislo(raw)
        if cislo is None or cislo == 0:
            return chyb(f"Marže {popisek}: neplatná hodnota")
        marze = (cislo - naklad) / cislo if bez_dph else _marze_dph(cislo)
        out["marze_" + popisek.lower()] = marze
        vyplnene += 1
        if marze is not None and marze < PRAH_MIMOLETAK:
            problemy.append(f"{popisek} {marze*100:.2f} %")

    if vyplnene == 0:
        return chyb("Nevyplněna žádná marže (CC/MO/VO)")
    if problemy:
        # Výjimka: karta s názvem začínajícím '*' se i při chybě bere jako správná (VYJIMKA → necount).
        out["verdikt"] = VYJIMKA if hvezda else CHYBA
        out["duvod"] = ("Marže < 5 %: " + ", ".join(problemy)
                        + (" — výjimka * (bráno jako OK)" if hvezda else ""))
    else:
        out["verdikt"], out["duvod"] = OK, "Všechny vyplněné marže ≥ 5 %"
    return out


def vyhodnot_paima(radek, master):
    """paima-op: kontrola OP = IF(master_OP < vstup_OP; 'Chyba'; 'OK'). Chyba = špatně.
    Srovnáno s workbookem: prázdné vstupní OP = 0 (→ obvykle OK), nečíselný TEXT v OP
    = Chyba (Excel: „číslo < text" = PRAVDA). Produkt nenalezen (#N/A) = chyba."""
    out = {"master_op": None, "vstup_op": None, "verdikt": CHYBA, "duvod": ""}
    kod = normalizuj_kod(radek.get("kod"))
    m = master.get(kod) if kod else None
    if m is None:
        out["duvod"] = DUVOD_NENALEZEN
        return out
    master_op = m["op"] if m["op"] is not None else 0.0  # prázdné OP = 0 (jako workbook)
    raw_op = radek.get("op")
    vstup_op = parse_cislo(raw_op)
    if vstup_op is None:
        if raw_op is not None and str(raw_op).strip() != "":
            # Nečíselný text v OP: Excel „master_OP < text" = PRAVDA → IF(O<F)="Chyba".
            out["master_op"] = master_op
            out["verdikt"], out["duvod"] = CHYBA, "kontrola OP = Chyba (nečíselné OP)"
            return out
        vstup_op = 0.0  # prázdné nabídnuté OP = 0 (Excel bere prázdnou buňku v „O<F" jako 0)
    out["master_op"], out["vstup_op"] = master_op, vstup_op
    # Tolerance proti float-šumu: z importu přijde OP jako čisté číslo (0.119),
    # ale z paste jako text „11,90 %" → float('11.90')/100 = 0.11900000000000001
    # (o 1 ULP víc). Bez tolerance by jinak SHODNÉ OP přes paste spadlo na
    # „master < vstup" = Chyba. OP má reálně max ~4 des. místa (krok 0,01 %),
    # takže EPS 1e-9 nezamaskuje žádný skutečný rozdíl.
    EPS = 1e-9
    if vstup_op - master_op > EPS:
        out["verdikt"], out["duvod"] = CHYBA, "kontrola OP = Chyba (master OP < vstup OP)"
    else:
        out["verdikt"], out["duvod"] = OK, "kontrola OP = OK"
    return out


def vyhodnot_ncfaktura(radek, master):
    """NC faktura — dva podtypy formuláře (detekce v intranet_cenopripad dle hlavičky,
    uložen na řádku jako '_subtyp'):
      • 'nc': porovná „Cena pro nastavení" s DNC3 (= NC3 z data_porovnání,
              master['nc3']). Cena > DNC3 → CHYBA, Cena ≤ DNC3 → OK.
      • 'fc': porovná NC-IMPORT a FC-IMPORT přímo v dokumentu (bez masteru).
              NC > FC → CHYBA, NC ≤ FC → OK.
    CHYBA = nastavení NENÍ v pořádku (cena/NC příliš vysoká)."""
    EPS = 1e-9
    sub = radek.get("_subtyp")
    if sub == "fc":
        nc = parse_cislo(radek.get("nc_import"))
        fc = parse_cislo(radek.get("fc_import"))
        out = {"nc": nc, "fc": fc, "verdikt": CHYBA, "duvod": ""}
        if nc is None or fc is None:
            out["duvod"] = "Chybí NC nebo FC (nečíselná hodnota)"
            return out
        if nc - fc > EPS:
            out["verdikt"], out["duvod"] = CHYBA, f"NC > FC ({nc} > {fc})"
        else:
            out["verdikt"], out["duvod"] = OK, f"NC ≤ FC ({nc} ≤ {fc})"
        return out
    # podtyp 'nc'
    cena = parse_cislo(radek.get("cena_nastaveni"))
    kod = normalizuj_kod(radek.get("kod"))
    m = (master.get(kod) or master.get(kod.zfill(8))) if kod else None
    out = {"cena_nastaveni": cena, "dnc3": None, "verdikt": CHYBA, "duvod": ""}
    if m is None:
        out["duvod"] = DUVOD_NENALEZEN
        return out
    dnc3 = m.get("nc3")
    out["dnc3"] = dnc3
    if cena is None:
        out["duvod"] = "Chybí cena pro nastavení (nečíselná)"
        return out
    if dnc3 is None:
        out["duvod"] = "DNC3 (NC3) nenalezeno v datech"
        return out
    if cena - dnc3 > EPS:
        out["verdikt"], out["duvod"] = CHYBA, f"Cena > DNC3 ({cena} > {dnc3})"
    else:
        out["verdikt"], out["duvod"] = OK, f"Cena ≤ DNC3 ({cena} ≤ {dnc3})"
    return out


EVALUATORY = {
    "porovnani": vyhodnot_porovnani,
    "webportal": vyhodnot_webportal,
    "sklad6": vyhodnot_sklad6,
    "mimoletak": vyhodnot_mimoletak,
    "paima": vyhodnot_paima,
    "ncfaktura": vyhodnot_ncfaktura,
}


def vyhodnot_radek(typ, radek, master):
    return EVALUATORY[typ](radek, master)


def vyhodnot_pripad(typ, radky, master):
    """Vyhodnotí celý případ. Případ je OK, jen když žádný řádek není CHYBA.
    Každý řádek je odolný vůči výjimce — selhání jednoho řádku ho označí jako CHYBA,
    ale nezhodí celý případ."""
    vys = []
    for r in radky:
        try:
            vys.append(vyhodnot_radek(typ, r, master))
        except Exception as e:
            vys.append({"verdikt": CHYBA, "duvod": f"Interní chyba řádku: {e}", "op": None})
    chyby = sum(1 for v in vys if v["verdikt"] == CHYBA)
    return {"radky": vys, "pocet": len(vys), "chyby": chyby, "ok": chyby == 0}


# ----------------------------------------------------------------------------
# Načítání zdrojových dat (pyxlsb) — používá import i self-test.
# Přijímá cestu nebo bytes (io.BytesIO).
# ----------------------------------------------------------------------------
def _zdroj(zdroj):
    return io.BytesIO(zdroj) if isinstance(zdroj, (bytes, bytearray)) else zdroj


def nacti_op_mapu(zdroj, klic_nazev="sort", hodnota_nazev="zadej OP FC") -> dict:
    """OP soubor (obchodní podmínky) -> {sort(str): OP(float)}.

    Bere list pojmenovaný `OP` (dle názvu; fallback = první list sešitu). Klíč
    i hodnotu hledá podle NÁZVU hlavičky — porovnává se vůči sloupci `zadej OP FC`
    (zpětně se přijímá i starší `ostré OP FC`), klíč = `sort`. Když se hlavička
    nenajde: klíč = 1. sloupec, hodnota = poslední sloupec (zpětná kompatibilita)."""
    from pyxlsb import open_workbook

    def _h(v):  # normalizace hlavičky: lower + sjednocení mezer
        return " ".join(str(v or "").strip().lower().split())

    cil_k = _h(klic_nazev)
    cil_v = {_h(hodnota_nazev), _h("zadej OP FC"), _h("ostré OP FC")}  # nový i starý název
    mapa = {}
    with open_workbook(_zdroj(zdroj)) as wb:
        nazev = next((s for s in wb.sheets if _h(s) == "op"), wb.sheets[0])
        with wb.get_sheet(nazev) as sh:
            key_col = val_col = None
            for i, row in enumerate(sh.rows()):
                cells = {c.c: c.v for c in row}
                if i == 0:
                    hlav = {k: _h(v) for k, v in cells.items()}
                    key_col = next((k for k, v in hlav.items() if v == cil_k), 0)
                    val_col = next((k for k, v in hlav.items() if v in cil_v),
                                   max(cells) if cells else 1)
                    continue
                klic = normalizuj_kod(cells.get(key_col))
                if klic is None:
                    continue
                mapa[klic] = parse_cislo(cells.get(val_col))
    return mapa


def nacti_master(zdroj_data, op_mapa, list_nazev="DATA_POROVNANI") -> dict:
    """DATA_POROVNANI -> master dict vč. dopočteného OP přes sortiment.
    Při duplicitě kódu se drží PRVNÍ výskyt (jako Excel VLOOKUP). Není-li list
    `list_nazev` v sešitu, vezme se první list."""
    from pyxlsb import open_workbook
    master = {}
    with open_workbook(_zdroj(zdroj_data)) as wb:
        nazev = list_nazev if list_nazev in wb.sheets else wb.sheets[0]
        with wb.get_sheet(nazev) as sh:
            for i, row in enumerate(sh.rows()):
                if i == 0:
                    continue  # hlavička (OP se bere VÝHRADNĚ z OP souboru, ne ze sloupce AJ)
                d = {c.c: c.v for c in row}
                kod = d.get(0)
                if kod is None:
                    continue
                key = str(kod).strip()
                if key in master:
                    continue  # první výskyt vyhrává (VLOOKUP)
                sort = d.get(12)
                sort_s = normalizuj_kod(sort)  # '100'/100.0 -> '100' (shoda s OP mapou)
                master[key] = {
                    "nazev": d.get(2),
                    "sortiment_popis": d.get(13),   # N (Sortiment Popis)
                    "nc": parse_cislo(d.get(4)),    # E
                    "nc2": parse_cislo(d.get(5)),   # F
                    "nc3": parse_cislo(d.get(6)),   # G
                    "nc4": parse_cislo(d.get(7)),   # H
                    "nc5": parse_cislo(d.get(8)),   # I
                    "dnc": parse_cislo(d.get(9)),   # J
                    "id": d.get(14),                # O
                    "dph": parse_cislo(d.get(16)),  # Q (Sazba DPH = text)
                    "sortiment": sort_s,
                    # OP: VÝHRADNĚ z OP souboru (obchodní podmínky) přes sortiment;
                    # není-li sortiment v OP souboru -> None -> engine počítá s 0.
                    "op": op_mapa.get(sort_s) if sort_s is not None else None,
                }
    return master


# ----------------------------------------------------------------------------
# Self-test: ověří engine proti uloženým hodnotám v Excelu.
#   python cenopripad_vypocet.py [složka_se_soubory]
# ----------------------------------------------------------------------------
if __name__ == "__main__":
    import os
    import sys

    slozka = sys.argv[1] if len(sys.argv) > 1 else r"C:\Users\ondre\Desktop\Letákové ceny"
    f_op = os.path.join(slozka, "OP pro porovnání konkurence.xlsb")
    f_data = os.path.join(slozka, "Kontrola letáky_ceníky_ostatní-2026-06-17.xlsb")
    from pyxlsb import open_workbook

    print("» Načítám OP mapu a master…")
    op_mapa = nacti_op_mapu(f_op)
    master = nacti_master(f_data, op_mapa)
    print(f"  OP mapa: {len(op_mapa)} sortimentů, master: {len(master)} produktů")

    # 1) OP join vs. (starší) cached AJ — očekáváme ~93 % (zbytek = data-drift staré verze)
    shod = neshod = na = 0
    with open_workbook(f_data) as wb:
        with wb.get_sheet("DATA_POROVNANI") as sh:
            for i, row in enumerate(sh.rows()):
                if i == 0:
                    continue
                d = {c.c: c.v for c in row}
                if d.get(0) is None:
                    continue
                cached = d.get(35)
                my = op_mapa.get(normalizuj_kod(d.get(12)))
                if je_excel_chyba(cached):
                    na += 1
                elif my is not None and isinstance(cached, (int, float)) and abs(my - cached) < 1e-6:
                    shod += 1
                else:
                    neshod += 1
    print(f"» OP join vs. cached: shoda={shod}, neshoda(drift staré OP)={neshod}, cached #N/A={na}")

    # 2) mimoleták — engine PŘIRÁŽKA CC vs. cached sloupec „přirážka" (I = idx 8)
    print("» mimoleták: engine marže CC vs. cached (sloupec marže CC)…")
    testovano = ok_cc = chyba = 0
    with open_workbook(f_data) as wb:
        with wb.get_sheet("mimoleták") as sh:
            for i, row in enumerate(sh.rows()):
                if i == 0:
                    continue
                d = {c.c: c.v for c in row}
                if d.get(0) is None:
                    continue
                testovano += 1
                radek = {"kod": d.get(0), "nazev": d.get(1), "akcni_fc": d.get(5),
                         "cc_s_dph": d.get(7), "mo_s_dph": d.get(9), "vo_bez_dph": d.get(11)}
                r = vyhodnot_mimoletak(radek, master)
                cached = d.get(16)   # Q = marže CC
                if r["marze_cc"] is not None and isinstance(cached, (int, float)):
                    if abs(r["marze_cc"] - cached) < 1e-6:
                        ok_cc += 1
                    else:
                        chyba += 1
                        print(f"    NESHODA: engine={r['marze_cc']:.8f} cached={cached}")
    print(f"  testováno řádků={testovano}, marže CC shoda={ok_cc}, neshoda={chyba}")

    print("\nHotovo. Engine je připraven pro intranet_cenopripad.")
