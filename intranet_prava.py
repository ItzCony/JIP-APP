# ==========================================
# --- ZÁKLADNÍ STATICKÁ PRÁVA (STRUKTUROVANÁ) ---
# ==========================================
ZAKLADNI_PRAVA = {
    'vse': {'kategorie': 'Administrace portálu', 'nazev': 'Plný přístup (SuperAdmin)', 'popis': 'Má neomezená práva na všechno. Ignoruje ostatní omezení.', 'ikona': 'local_police'},
    'uzivatele': {'kategorie': 'Administrace portálu', 'nazev': 'Správa uživatelů', 'popis': 'Může zakládat uživatele, měnit hesla, práva a oddělení.', 'ikona': 'manage_accounts'},
    'mysql': {'kategorie': 'Administrace portálu', 'nazev': 'Nastavení portálu', 'popis': 'Může měnit globální nastavení, zapínat moduly a e-maily.', 'ikona': 'dns'},
    'dlazdice': {'kategorie': 'Administrace portálu', 'nazev': 'Úprava dlaždic', 'popis': 'Může přejmenovávat dlaždice na hlavní nástěnce.', 'ikona': 'dashboard_customize'},
    'admin_logy': {'kategorie': 'Administrace portálu', 'nazev': 'Audit Log', 'popis': 'Vidí historii aktivity a kliknutí všech uživatelů.', 'ikona': 'history'},
    'admin_server': {'kategorie': 'Administrace portálu', 'nazev': 'Monitor serveru', 'popis': 'Vidí vytížení hardwaru serveru.', 'ikona': 'memory'},

    'dlazdice_dochazka_zaklad': {'kategorie': 'Docházka: Obecné', 'nazev': 'Přístup do Docházky', 'popis': 'Uživatel vidí dlaždici Docházka a může modul otevřít.', 'ikona': 'event'},
    'dochazka_zadosti': {'kategorie': 'Docházka: Osobní', 'nazev': 'Zadávat žádosti o volno', 'popis': 'Umožňuje uživateli vytvořit si vlastní žádost v kalendáři.', 'ikona': 'edit_calendar'},
    'dochazka_email': {'kategorie': 'Docházka: Osobní', 'nazev': 'Vypnout maily o schválení', 'popis': 'Může si v profilu vypnout e-mailová upozornění.', 'ikona': 'mark_email_read'},

    'dochazka_mazani': {'kategorie': 'Docházka: Zvýšená práva', 'nazev': 'Smazat jakoukoliv žádost', 'popis': 'Může nevratně odstranit libovolnou žádost v celém systému!', 'ikona': 'delete_forever'},
    'dochazka_schvalovat_sebe': {'kategorie': 'Docházka: Zvýšená práva', 'nazev': 'Schválit sám sebe', 'popis': 'Je-li schvalovatel, může si potvrdit vlastní volno bez asistence nadřízeného.', 'ikona': 'how_to_reg'},
    'dochazka_admin': {'kategorie': 'Docházka: Zvýšená práva', 'nazev': 'Super-Schvalovatel', 'popis': 'Může schválit volno KOMUKOLIV v celé firmě, i napříč odděleními.', 'ikona': 'verified'},

    'slozky_vse': {'kategorie': 'Složky a Evidence', 'nazev': 'Vidí všechny složky oddělení', 'popis': 'Má přístup do záznamů všech oddělení.', 'ikona': 'folder_shared'},
    'kalendar_vse': {'kategorie': 'Složky a Evidence', 'nazev': 'Vidí všechny v kalendáři', 'popis': 'V globálním kalendáři vidí všechny lidi z celé firmy.', 'ikona': 'calendar_month'},

    'tisk_vse': {'kategorie': 'Práva k Exportům', 'nazev': 'Tisk: Všechny typy volna', 'popis': 'Umožňuje exportovat všechny druhy absencí do Excelu.', 'ikona': 'print'},
    'tisk_odd_vse': {'kategorie': 'Práva k Exportům', 'nazev': 'Tisk: Všechna oddělení', 'popis': 'Umožňuje exportovat docházku do Excelu za jakékoliv oddělení.', 'ikona': 'print'},
    'ikos_export': {'kategorie': 'Práva k Exportům', 'nazev': 'IKOS Export', 'popis': 'Může stáhnout IKOS export všech typů volna (os.č. / datum od–do / čas od–do / druh volna) za celou firmu.', 'ikona': 'output'},

    'porovnani_vse': {'kategorie': 'Porovnání dovolené', 'nazev': 'Porovnání: Všechna oddělení', 'popis': 'Může otevřít porovnání CSV zůstatků dovolené pro celou firmu.', 'ikona': 'compare'},

    'kviz': {'kategorie': 'Modul Kvíz', 'nazev': 'Přístup do Kvízu', 'popis': 'Může otevřít modul Zkouškový Kvíz.', 'ikona': 'school'},
    'vystup_osobni': {'kategorie': 'Modul Kvíz', 'nazev': 'Kvíz: Osobní výsledky', 'popis': 'Vidí v exportech jen vlastní absolvované testy.', 'ikona': 'assessment'},
    'vystup_vse': {'kategorie': 'Modul Kvíz', 'nazev': 'Kvíz: Všechny výsledky', 'popis': 'Může exportovat výsledky testů za celou firmu.', 'ikona': 'insights'},

    'veletrh_pristup':    {'kategorie': 'Modul Veletrh', 'nazev': 'Základní přístup (Čtení)',  'popis': 'Vidí mapu, ceník a seznam stánků. Nemůže je upravovat.',                           'ikona': 'visibility'},
    'veletrh_komentator': {'kategorie': 'Modul Veletrh', 'nazev': 'Komentátor zákazníků',      'popis': 'Může přidávat zákazníky do evidence veletrhu. Nemůže upravovat ani mazat.',     'ikona': 'person_add'},
    'veletrh_uzivatel':   {'kategorie': 'Modul Veletrh', 'nazev': 'Rezervace stánků',          'popis': 'Může vyplňovat údaje stánků, měnit velikost a připojení.',                       'ikona': 'edit_note'},
    'veletrh_admin':      {'kategorie': 'Modul Veletrh', 'nazev': 'Plná správa Veletrhu',      'popis': 'Může mazat cizí stánky, podepisovat smlouvy a stahovat Wordy.',                  'ikona': 'gavel'},

    # ── Modul Aprovia — 3 role dle dokumentu ──────────────────────────────
    'nakup_uzivatel':  {'kategorie': 'Modul Aprovia', 'nazev': 'Uživatel', 'popis': 'Může vytvářet objednávky, vidět své koncepty a objednávky a nahrávat fakturu ke svému případu.', 'ikona': 'shopping_cart'},
    'nakup_schvalit':  {'kategorie': 'Modul Aprovia', 'nazev': 'Schvalovatel objednávek', 'popis': 'Zobrazí se mu fronta ke schválení a může schvalovat/zamítat přiřazené objednávky.', 'ikona': 'fact_check'},
    'faktury_seznam_schvalit': {'kategorie': 'Modul Aprovia', 'nazev': 'Schvalovatel faktur', 'popis': 'Schvaluje objednávky s přiloženou fakturou a zadává schválené faktury do účetnictví.', 'ikona': 'receipt_long'},
    'ucetni_pristup': {'kategorie': 'Účetní pohledy', 'nazev': 'Exporty (Účtárna)', 'popis': 'Speciální přístupy k detailním exportům pro mzdy.', 'ikona': 'account_balance'},
    'import_dovolene': {'kategorie': 'Účetní pohledy', 'nazev': 'Import dovolené', 'popis': 'Může nahrávat Excel s reálnými zůstatky dovolené, které se párují na osobní číslo zaměstnance.', 'ikona': 'upload_file'},

    'znacky_uzivatel': {'kategorie': 'Modul Značky JIP', 'nazev': 'Uživatel hlasování', 'popis': 'Může hlasovat, psát poznámky a zobrazovat případy, ke kterým byl přizván.', 'ikona': 'how_to_vote'},
    'znacky_spravce': {'kategorie': 'Modul Značky JIP', 'nazev': 'Správce hlasování', 'popis': 'Zadává případy, vybírá hlasující a dědí práva uživatele.', 'ikona': 'manage_accounts'},

    'znacky_provoz_uzivatel': {'kategorie': 'Modul Značky Provoz', 'nazev': 'Uživatel hlasování', 'popis': 'Může hlasovat, psát poznámky a zobrazovat vše v modulu Značky Provoz.', 'ikona': 'how_to_vote'},
    'znacky_provoz_spravce':  {'kategorie': 'Modul Značky Provoz', 'nazev': 'Správce hlasování', 'popis': 'Zadává případy ke schválení a dědí práva uživatele v modulu Značky Provoz.', 'ikona': 'manage_accounts'},

    'prodej_akt_ctenar':       {'kategorie': 'Modul Prodejní aktivity', 'nazev': 'Čtenář', 'popis': 'Může otevřít modul a číst všechny záznamy prodejních aktivit. Nemůže nic editovat.', 'ikona': 'visibility'},
    'prodej_akt_zadavatel':    {'kategorie': 'Modul Prodejní aktivity', 'nazev': 'Zadavatel', 'popis': 'Zakládá nové aktivity a zapisuje pole nákupčího: dodavatel, termíny, kompenzace, kódy zboží…', 'ikona': 'edit_note'},
    'prodej_akt_ucetni':       {'kategorie': 'Modul Prodejní aktivity', 'nazev': 'Komentátor účetní', 'popis': 'Zapisuje pouze 3 účetní sloupce: částka bez DPH, číslo dokladu, proúčtoval provozovnám.', 'ikona': 'receipt_long'},
    'prodej_akt_ao':           {'kategorie': 'Modul Prodejní aktivity', 'nazev': 'Komentátor AO', 'popis': 'Zapisuje výhradně sloupec „Stav AO".', 'ikona': 'rate_review'},
    'prodej_akt_schvalovatel': {'kategorie': 'Modul Prodejní aktivity', 'nazev': 'Schvalovatel', 'popis': 'Plná správa: edituje vše včetně stavu aktivity, jako jediný maže záznamy.', 'ikona': 'verified'},

    'narozeniny_zobrazeni': {'kategorie': 'Modul Narozeniny', 'nazev': 'Zobrazení přehledu', 'popis': 'Může otevřít modul Narozeniny a prohlížet přehled nadcházejících narozenin.', 'ikona': 'cake'},
    'narozeniny_sprava': {'kategorie': 'Modul Narozeniny', 'nazev': 'Správa databáze', 'popis': 'Může nahrávat a mazat CSV databázi narozenin. Zahrnuje přístup k přehledu narozenin.', 'ikona': 'manage_accounts'},

    'smeny_zobrazit': {'kategorie': 'Modul Plánování směn', 'nazev': 'Zobrazení vlastních směn', 'popis': 'Může otevřít modul Plánování směn a vidět své přiřazené směny.', 'ikona': 'calendar_month'},
    'smeny_vedouci':  {'kategorie': 'Modul Plánování směn', 'nazev': 'Vedoucí plánování', 'popis': 'Může vytvářet, upravovat a mazat směny v odděleních, jejichž je členem. Vidí všechny zaměstnance daného oddělení a může jim přiřazovat směny.', 'ikona': 'edit_calendar'},
    'smeny_admin': {'kategorie': 'Modul Plánování směn', 'nazev': 'Správce plánování', 'popis': 'Může spravovat skupiny, vytvářet a upravovat směny ve všech skupinách.', 'ikona': 'manage_accounts'},

    'nastenkove_schvalovani': {'kategorie': 'Modul Komunikační portál', 'nazev': 'Schvalovatel místností', 'popis': 'Může schvalovat, zamítat a vracet žádosti o vytvoření nových místností na Komunikačním portálu JIP.', 'ikona': 'approval'},

    'planogram_admin':   {'kategorie': 'Modul Plánogram tabáku', 'nazev': 'Správce plánogramu', 'popis': 'Plná správa plánogramu: vytváření, úpravy a mazání políček, slučování buněk, správa poboček, rozesílání aktualizací. Zahrnuje i právo nahlížení a komentování.', 'ikona': 'grid_on'},
    'planogram_pristup': {'kategorie': 'Modul Plánogram tabáku', 'nazev': 'Nahlížení a komentáře', 'popis': 'Může otevřít plánogram tabákových výrobků, prohlížet rozmístění a přidávat komentáře. Nemůže nic upravovat.', 'ikona': 'visibility'},

    'ochutnavky_admin':   {'kategorie': 'Modul Ochutnávky MO a CC', 'nazev': 'Zápis – Nákup Office', 'popis': 'Pro nákupčí: zadávání, úprava a mazání akcí, výběr provozoven a rozesílání oznámení o aktualizaci. Může také chatovat a přidávat k akcím poznámky a přílohy. Zahrnuje i právo nahlížení.', 'ikona': 'restaurant'},
    'ochutnavky_pristup': {'kategorie': 'Modul Ochutnávky MO a CC', 'nazev': 'Čtenář (supervizoři)', 'popis': 'Může otevřít přehled ochutnávek, filtrovat podle provozoven a období, chatovat a přidávat k akcím poznámky a přílohy (foto, PDF), exportovat seznam do PDF. Nemůže zadávat, upravovat ani mazat akce.', 'ikona': 'visibility'},

    'ukolovnik_admin':      {'kategorie': 'Modul Porady a úkoly', 'nazev': 'Administrátor',              'popis': 'Plný přístup do celého modulu — vidí všechny úkoly, porady a projekty bez ohledu na přiřazení. Zahrnuje všechna práva níže.', 'ikona': 'gavel'},
    'ukolovnik_porady':     {'kategorie': 'Modul Porady a úkoly', 'nazev': 'Sekce: Porady a zápisy',     'popis': 'Vidí záložku Porady a zápisy a může procházet porady, ke kterým je přiřazen.', 'ikona': 'meeting_room'},
    'ukolovnik_porady_zadat':   {'kategorie': 'Modul Porady a úkoly', 'nazev': 'Porady: Zakládat porady',          'popis': 'Může vytvářet nové porady. Vyžaduje také právo „Sekce: Porady a zápisy".', 'ikona': 'add_circle'},
    'ukolovnik_porady_vsichni': {'kategorie': 'Modul Porady a úkoly', 'nazev': 'Porady: Vidět všechny uživatele', 'popis': 'V polích Moderátor, Zapisovatel a Účastníci vidí všechny uživatele. Bez tohoto práva se zobrazují pouze vedoucí oddělení.', 'ikona': 'groups'},
    'ukolovnik_ukoly':               {'kategorie': 'Modul Porady a úkoly', 'nazev': 'Sekce: Úkoly',                       'popis': 'Vidí záložku Zadávání úkolů a přehled úkolů a může spravovat přidělené úkoly.', 'ikona': 'task_alt'},
    'ukolovnik_ukoly_zadat_sobe':    {'kategorie': 'Modul Porady a úkoly', 'nazev': 'Úkoly: Zadávat úkoly jen sobě',     'popis': 'Může vytvořit nový úkol — přiřadit jej může pouze sám sobě. Výběr oddělení se nezobrazí.', 'ikona': 'person'},
    'ukolovnik_ukoly_zadat_oddeleni':{'kategorie': 'Modul Porady a úkoly', 'nazev': 'Úkoly: Zadávat úkoly v oddělení',  'popis': 'Může vytvořit nový úkol a přiřadit jej komukoliv ve svém oddělení. Výběr oddělení se nezobrazí.', 'ikona': 'group'},
    'ukolovnik_ukoly_oddeleni_vse':  {'kategorie': 'Modul Porady a úkoly', 'nazev': 'Úkoly: Vidět všechny úkoly v oddělení', 'popis': 'V přehledu a kalendáři vidí úkoly všech členů svého oddělení (jen ke čtení). Měnit stav a přehazovat může i nadále jen své úkoly.', 'ikona': 'visibility'},
    'ukolovnik_kalendar':   {'kategorie': 'Modul Porady a úkoly', 'nazev': 'Sekce: Kalendář',            'popis': 'Vidí záložku Kalendář s přehledem úkolů a porad v čase.', 'ikona': 'calendar_month'},
    'ukolovnik_kapacita':   {'kategorie': 'Modul Porady a úkoly', 'nazev': 'Sekce: Kapacita oddělení',  'popis': 'Vidí záložku Kapacita oddělení s přehledem vytížení a Gantt diagramem.', 'ikona': 'groups'},
    'ukolovnik_projekty':   {'kategorie': 'Modul Porady a úkoly', 'nazev': 'Sekce: Projekty',            'popis': 'Vidí záložku Projekty a může pracovat na přiřazených projektech.', 'ikona': 'rocket_launch'},
    'ukolovnik_statistika': {'kategorie': 'Modul Porady a úkoly', 'nazev': 'Sekce: Statistika',          'popis': 'Vidí záložku Statistika s výkonnostními přehledy.', 'ikona': 'bar_chart'},

    'vysledky_ao':     {'kategorie': 'Modul Výsledky poboček', 'nazev': 'Komentátor AO',      'popis': 'Plný přístup ke všem pobočkám: může číst a zadávat data do všech záložek včetně Podrobných nákladů.', 'ikona': 'gavel'},
    'vysledky_ucetni': {'kategorie': 'Modul Výsledky poboček', 'nazev': 'Účetní hlavní',      'popis': 'Vidí všechny záložky všech poboček (čtení) a může zapisovat a měnit data v záložce Podrobné náklady.', 'ikona': 'receipt_long'},
    'vysledky_ucetni_bezna': {'kategorie': 'Modul Výsledky poboček', 'nazev': 'Účetní běžná',  'popis': 'Vidí dlaždice poboček a stavový panel; v detailu pobočky pouze záložku Podrobné náklady (vč. Porovnání) – jen pro čtení. Nevidí Obraty/Zisk ani Tabulku nákladů.', 'ikona': 'visibility'},
    'vysledky_majitel': {'kategorie': 'Modul Výsledky poboček', 'nazev': 'Majitel – přehled',  'popis': 'Vidí dlaždici „Přehled poboček" se srovnávací tabulkou všech poboček (rok i měsíc přepínatelně) a má přístup do všech poboček – pouze pro čtení.', 'ikona': 'leaderboard'},

    'vysledky_pobocka_pardubice':       {'kategorie': 'Modul Výsledky poboček', 'nazev': 'Čtenář: Pardubice',        'popis': 'VO vedoucí – vidí pouze dlaždici a data pobočky Pardubice. Pouze pro čtení.',        'ikona': 'store'},
    'vysledky_pobocka_praha':           {'kategorie': 'Modul Výsledky poboček', 'nazev': 'Čtenář: Praha',            'popis': 'VO vedoucí – vidí pouze dlaždici a data pobočky Praha. Pouze pro čtení.',            'ikona': 'store'},
    'vysledky_pobocka_jilemnice':       {'kategorie': 'Modul Výsledky poboček', 'nazev': 'Čtenář: Jilemnice',        'popis': 'VO vedoucí – vidí pouze dlaždici a data pobočky Jilemnice. Pouze pro čtení.',        'ikona': 'store'},
    'vysledky_pobocka_most':            {'kategorie': 'Modul Výsledky poboček', 'nazev': 'Čtenář: Most',             'popis': 'VO vedoucí – vidí pouze dlaždici a data pobočky Most. Pouze pro čtení.',             'ikona': 'store'},
    'vysledky_pobocka_liberec':         {'kategorie': 'Modul Výsledky poboček', 'nazev': 'Čtenář: Liberec',          'popis': 'VO vedoucí – vidí pouze dlaždici a data pobočky Liberec. Pouze pro čtení.',          'ikona': 'store'},
    'vysledky_pobocka_hodonin':         {'kategorie': 'Modul Výsledky poboček', 'nazev': 'Čtenář: Hodonín',          'popis': 'VO vedoucí – vidí pouze dlaždici a data pobočky Hodonín. Pouze pro čtení.',          'ikona': 'store'},
    'vysledky_pobocka_winelife':        {'kategorie': 'Modul Výsledky poboček', 'nazev': 'Čtenář: Winelife',         'popis': 'VO vedoucí – vidí pouze dlaždici a data pobočky Winelife. Pouze pro čtení.',         'ikona': 'store'},
    'vysledky_pobocka_zlin':            {'kategorie': 'Modul Výsledky poboček', 'nazev': 'Čtenář: Zlín',             'popis': 'VO vedoucí – vidí pouze dlaždici a data pobočky Zlín. Pouze pro čtení.',             'ikona': 'store'},
    'vysledky_pobocka_ostrava':         {'kategorie': 'Modul Výsledky poboček', 'nazev': 'Čtenář: Ostrava',          'popis': 'VO vedoucí – vidí pouze dlaždici a data pobočky Ostrava. Pouze pro čtení.',          'ikona': 'store'},
    'vysledky_pobocka_olomouc':         {'kategorie': 'Modul Výsledky poboček', 'nazev': 'Čtenář: Olomouc',          'popis': 'VO vedoucí – vidí pouze dlaždici a data pobočky Olomouc. Pouze pro čtení.',          'ikona': 'store'},
    'vysledky_pobocka_ceske_budejovice':{'kategorie': 'Modul Výsledky poboček', 'nazev': 'Čtenář: České Budějovice', 'popis': 'VO vedoucí – vidí pouze dlaždici a data pobočky České Budějovice. Pouze pro čtení.', 'ikona': 'store'},
    'vysledky_pobocka_plzen':           {'kategorie': 'Modul Výsledky poboček', 'nazev': 'Čtenář: Plzeň',            'popis': 'VO vedoucí – vidí pouze dlaždici a data pobočky Plzeň. Pouze pro čtení.',            'ikona': 'store'},
    'vysledky_pobocka_horsovsky_tyn':   {'kategorie': 'Modul Výsledky poboček', 'nazev': 'Čtenář: Horšovský Týn',   'popis': 'VO vedoucí – vidí pouze dlaždici a data pobočky Horšovský Týn. Pouze pro čtení.',   'ikona': 'store'},
    'vysledky_pobocka_nova_role':       {'kategorie': 'Modul Výsledky poboček', 'nazev': 'Čtenář: Nová Role',        'popis': 'VO vedoucí – vidí pouze dlaždici a data pobočky Nová Role. Pouze pro čtení.',        'ikona': 'store'},
    'vysledky_pobocka_bourarna':        {'kategorie': 'Modul Výsledky poboček', 'nazev': 'Čtenář: Bourárna',         'popis': 'VO vedoucí – vidí pouze dlaždici a data pobočky Bourárna (015). Pouze pro čtení.',   'ikona': 'store'},
    'vysledky_pobocka_kamiony':         {'kategorie': 'Modul Výsledky poboček', 'nazev': 'Čtenář: Kamiony',          'popis': 'VO vedoucí – vidí pouze dlaždici a data pobočky Kamiony (090). Pouze pro čtení.',    'ikona': 'store'},
    'vysledky_pobocka_prefakturace':    {'kategorie': 'Modul Výsledky poboček', 'nazev': 'Čtenář: Přefakturace',     'popis': 'VO vedoucí – vidí pouze dlaždici a data Přefakturace. Pouze pro čtení.',             'ikona': 'store'},
    'vysledky_pobocka_ostatni_provozy': {'kategorie': 'Modul Výsledky poboček', 'nazev': 'Čtenář: Ostatní provozy',  'popis': 'VO vedoucí – vidí pouze dlaždici a data Ostatních provozů (Praha-Becher, Lípa, restaurace, gastro, Supervizor, Údržba…). Pouze pro čtení.', 'ikona': 'store'},

    'sankce_analytik': {'kategorie': 'Modul Sankce', 'nazev': 'Analytik (import)', 'popis': 'Importuje list DATA do obou sestav (Zamítnuté dodávky i Sankce k vystavení) a nastavuje/zamyká výchozí filtr období pro všechny.', 'ikona': 'upload_file'},
    'sankce_ucetni':   {'kategorie': 'Modul Sankce', 'nazev': 'Účtárna (Sankce k vystavení)', 'popis': 'Vidí sestavu „Sankce k vystavení", mění stav řádků (nevyfakturováno / rozpracováno / vyfakturováno) a píše poznámky. Každá změna se zaznamenává (očičko).', 'ikona': 'receipt_long'},
    'sankce_nakup':    {'kategorie': 'Modul Sankce', 'nazev': 'Nákup (Zamítnuté dodávky)', 'popis': 'Vidí sestavu „Zamítnuté dodávky dodavatelem" a může u řádků psát poznámky. Každá změna se zaznamenává (očičko).', 'ikona': 'shopping_cart'},
    'sankce_ctenar':   {'kategorie': 'Modul Sankce', 'nazev': 'Čtenář (obě sestavy)', 'popis': 'Vidí obě sestavy (Zamítnuté dodávky i Sankce k vystavení) pouze pro čtení – může filtrovat, řadit, exportovat a psát do diskuze (bublina 💬). Nemůže editovat buňky, importovat ani mazat data.', 'ikona': 'visibility'},

    'spolvecer_schvalovatel': {'kategorie': 'Modul Společenský večer', 'nazev': 'Schvalovatel', 'popis': 'Vidí všechny pobočky, komunikuje v chatu a schvaluje náklady (mění stav na Schváleno).', 'ikona': 'verified'},
    'spolvecer_ctenar':       {'kategorie': 'Modul Společenský večer', 'nazev': 'Čtenář', 'popis': 'Vidí všechny pobočky pouze pro čtení a může psát do chatu. Nemůže editovat tabulku ani schvalovat.', 'ikona': 'visibility'},

    'spolvecer_organizator_pardubice': {'kategorie': 'Modul Společenský večer', 'nazev': 'Organizátor: 010 Pardubice', 'popis': 'Vedoucí/ASM VO Pardubice – vyplňuje a edituje tabulku své pobočky a odesílá náklady ke schválení.', 'ikona': 'edit_note'},
    'spolvecer_organizator_praha':     {'kategorie': 'Modul Společenský večer', 'nazev': 'Organizátor: 011 Praha', 'popis': 'Vedoucí/ASM VO Praha – vyplňuje a edituje tabulku své pobočky a odesílá náklady ke schválení.', 'ikona': 'edit_note'},
    'spolvecer_organizator_jilemnice': {'kategorie': 'Modul Společenský večer', 'nazev': 'Organizátor: 012 Jilemnice', 'popis': 'Vedoucí/ASM VO Jilemnice – vyplňuje a edituje tabulku své pobočky a odesílá náklady ke schválení.', 'ikona': 'edit_note'},
    'spolvecer_organizator_liberec':   {'kategorie': 'Modul Společenský večer', 'nazev': 'Organizátor: 014 Liberec', 'popis': 'Vedoucí/ASM VO Liberec – vyplňuje a edituje tabulku své pobočky a odesílá náklady ke schválení.', 'ikona': 'edit_note'},
    'spolvecer_organizator_zlin':      {'kategorie': 'Modul Společenský večer', 'nazev': 'Organizátor: 020 Zlín', 'popis': 'Vedoucí/ASM VO Zlín – vyplňuje a edituje tabulku své pobočky a odesílá náklady ke schválení.', 'ikona': 'edit_note'},
    'spolvecer_organizator_ostrava':   {'kategorie': 'Modul Společenský večer', 'nazev': 'Organizátor: 026 Ostrava', 'popis': 'Vedoucí/ASM VO Ostrava – vyplňuje a edituje tabulku své pobočky a odesílá náklady ke schválení.', 'ikona': 'edit_note'},
    'spolvecer_organizator_olomouc':   {'kategorie': 'Modul Společenský večer', 'nazev': 'Organizátor: 032 Olomouc', 'popis': 'Vedoucí/ASM VO Olomouc – vyplňuje a edituje tabulku své pobočky a odesílá náklady ke schválení.', 'ikona': 'edit_note'},
    'spolvecer_organizator_plzen':     {'kategorie': 'Modul Společenský večer', 'nazev': 'Organizátor: 034 Plzeň', 'popis': 'Vedoucí/ASM VO Plzeň – vyplňuje a edituje tabulku své pobočky a odesílá náklady ke schválení.', 'ikona': 'edit_note'},
    'spolvecer_organizator_nova_role': {'kategorie': 'Modul Společenský večer', 'nazev': 'Organizátor: 037 Nová Role', 'popis': 'Vedoucí/ASM VO Nová Role – vyplňuje a edituje tabulku své pobočky a odesílá náklady ke schválení.', 'ikona': 'edit_note'},

    'vizitky_zadatel':    {'kategorie': 'Modul Vizitky a podpisy', 'nazev': 'Žadatel (automaticky všem)', 'popis': 'Žadatelem je automaticky KAŽDÝ přihlášený uživatel – může vytvářet žádosti o vizitky a e-mailové podpisy, vidí jen své a může u vyřízených žádat o změnu. Toto právo proto není nutné přiřazovat.', 'ikona': 'badge'},
    'vizitky_realizator': {'kategorie': 'Modul Vizitky a podpisy', 'nazev': 'Realizátor (grafik)', 'popis': 'Vidí všechny žádosti, realizuje je (vkládá podklady ke stažení) a spravuje číselníky oddělení, poboček a adres. Dědí i práva žadatele.', 'ikona': 'design_services'},

    'cenopripad_zadatel_nakup':  {'kategorie': 'Modul Cenopřípad', 'nazev': 'Žadatel nákup',  'popis': 'Podává žádosti o kontrolu letákových cen (dlaždice leták/sklad6/mimoleták/paima) a vidí jen své tikety a jejich stavy. Nevidí OP ani marže v %.', 'ikona': 'sell'},
    'cenopripad_zadatel_obchod': {'kategorie': 'Modul Cenopřípad', 'nazev': 'Žadatel obchod', 'popis': 'Podává žádosti o kontrolu individuálních cen (dlaždice porovnání) a vidí jen své tikety a jejich stavy. Nevidí OP ani marže v %.', 'ikona': 'sell'},
    'cenopripad_office_nakup':   {'kategorie': 'Modul Cenopřípad', 'nazev': 'Office nákup',   'popis': 'Uzavírá a dotahuje schválené nákupní případy do nastavení a může je stornovat s uvedením důvodu. Rozhoduje se pouze podle verdiktu aplikace — NEvidí OP ani marže v %.', 'ikona': 'task_alt'},
    'cenopripad_office_obchod':  {'kategorie': 'Modul Cenopřípad', 'nazev': 'Office obchod',  'popis': 'Uzavírá a dotahuje schválené obchodní případy (porovnání) do nastavení a může je stornovat s uvedením důvodu. Rozhoduje se pouze podle verdiktu — NEvidí OP ani marže v %.', 'ikona': 'task_alt'},
    'cenopripad_zadatel_letaky': {'kategorie': 'Modul Cenopřípad', 'nazev': 'Žadatel – Letáková kontrola', 'popis': 'LETÁKOVÁ KONTROLA — Nahrává kontrolní sestavy letáků (dlaždice „Kontrolní data letáků“, kanály VO/MO/SP), vidí jen své nahrané sestavy a jejich stav a komunikuje v komentářích.', 'ikona': 'sell'},
    'cenopripad_office_letaky':  {'kategorie': 'Modul Cenopřípad', 'nazev': 'Office – Letáková kontrola', 'popis': 'LETÁKOVÁ KONTROLA — Vidí všechny nahrané kontrolní sestavy letáků, komunikuje v komentářích a označuje sestavy jako „Zpracováno“.', 'ikona': 'task_alt'},
    'cenopripad_ctenar_letaky':  {'kategorie': 'Modul Cenopřípad', 'nazev': 'Čtenář – Letáková kontrola', 'popis': 'LETÁKOVÁ KONTROLA — Pouze pro čtení: prohlíží všechny nahrané kontrolní sestavy letáků a smí stahovat exporty. NEMŮŽE nahrávat sestavy, psát komentáře ani měnit stav.', 'ikona': 'visibility'},
    'cenopripad_zadatel_letaky_final': {'kategorie': 'Modul Cenopřípad', 'nazev': 'Žadatel – Letáková kontrola (Finální)', 'popis': 'LETÁKOVÁ KONTROLA — FINÁLNÍ SESTAVY. Kopie práva „Žadatel – Letáková kontrola“, ale pro finální sestavy: nahrává je, vidí jen své a komunikuje v komentářích. Bez tohoto práva uživatel finální sestavy vůbec nevidí (má jen kontrolní).', 'ikona': 'sell'},
    'cenopripad_office_letaky_final':  {'kategorie': 'Modul Cenopřípad', 'nazev': 'Office – Letáková kontrola (Finální)', 'popis': 'LETÁKOVÁ KONTROLA — FINÁLNÍ SESTAVY. Kopie práva „Office – Letáková kontrola“ pro finální sestavy: vidí všechny, komentuje, označuje „Zpracováno“ a maže. Bez tohoto práva uživatel finální sestavy vůbec nevidí.', 'ikona': 'task_alt'},
    'cenopripad_ctenar_letaky_final':  {'kategorie': 'Modul Cenopřípad', 'nazev': 'Čtenář – Letáková kontrola (Finální)', 'popis': 'LETÁKOVÁ KONTROLA — FINÁLNÍ SESTAVY. Kopie práva „Čtenář – Letáková kontrola“ pro finální sestavy: pouze čtení a stahování exportů. NEMŮŽE nahrávat, komentovat ani měnit stav.', 'ikona': 'visibility'},
    'cenopripad_spravce_nakup':  {'kategorie': 'Modul Cenopřípad', 'nazev': 'Správce nákup',  'popis': 'Plná správa nákupních (letákových) dlaždic — vidí vše včetně OP a marží v %, schvaluje druhou kontrolu. Vidí jen dlaždice leták/sklad6/mimoleták/paima.', 'ikona': 'admin_panel_settings'},
    'cenopripad_spravce':        {'kategorie': 'Modul Cenopřípad', 'nazev': 'Správce (vše)',  'popis': 'Plná správa celého modulu vč. importu dat — vidí vše včetně OP a marží v % ve všech dlaždicích. OP a procenta vidí pouze správci (tato role + Správce nákup u letákových dlaždic), NIKDY žadatel ani office.', 'ikona': 'local_police'},
    'cenopripad_vkladatel':      {'kategorie': 'Modul Cenopřípad', 'nazev': 'Vkladatel dat', 'popis': 'Smí pouze nahrávat data (dlaždice „Import dat“ — OP + DATA_POROVNANI). Nevidí žádné případy, OP ani marže v %.', 'ikona': 'upload'},
    'cenopripad_zobrazeni_oddeleni': {'kategorie': 'Modul Cenopřípad', 'nazev': 'Zobrazení oddělení', 'popis': 'Přiřaďte k ODDĚLENÍ: členové tohoto oddělení uvidí případy (tikety) ostatních členů téhož oddělení navzájem, ne jen své. Nepřidává práva žadatele/správce ani zobrazení OP a marží v %.', 'ikona': 'groups'},
    'cenopripad_spravce_bez_emailu': {'kategorie': 'Modul Cenopřípad', 'nazev': 'Správce (Vše) - Bez emailu', 'popis': 'Stejná práva jako Správce (vše) — plná správa celého modulu vč. importu dat, vidí vše včetně OP a marží v %. Rozdíl: NECHODÍ mu žádné e-mailové notifikace z modulu Cenopřípad.', 'ikona': 'local_police'},

    'asm_zadatel':            {'kategorie': 'Modul Formuláře ASM', 'nazev': 'Žadatel',            'popis': 'ASM / vedoucí poboček. Zakládá a vyplňuje formulář „Změna zákazníků na OZ/ASM“, vidí jen své případy a jejich stavy.', 'ikona': 'edit_note'},
    'asm_office':             {'kategorie': 'Modul Formuláře ASM', 'nazev': 'Office obchod',       'popis': 'Vidí celou frontu, případy zpracovává, vrací k opravě nebo postupuje správci, uzavírá.', 'ikona': 'task_alt'},
    'asm_spravce':            {'kategorie': 'Modul Formuláře ASM', 'nazev': 'Správce obchod',      'popis': 'Finální arbitr při sporech. Vidí celou frontu, postoupené případy schvaluje nebo zamítá. Má i přístup k dlaždici „Data“.', 'ikona': 'local_police'},
    'asm_spravce_bez_emailu': {'kategorie': 'Modul Formuláře ASM', 'nazev': 'Správce obchod - Bez emailu', 'popis': 'Stejná práva jako Správce obchod, ale NECHODÍ mu e-mailové notifikace z modulu Formuláře ASM.', 'ikona': 'local_police'},
    'asm_vkladatel':          {'kategorie': 'Modul Formuláře ASM', 'nazev': 'Vkladatel dat',       'popis': 'AO. Smí pouze nahrávat číselníky v dlaždici „Data“ (Dealer + Kontaktní údaje VO). Nevidí žádné případy.', 'ikona': 'upload'},

    # Lupou na obchod — přístup ASM a vedoucích se NEUDĚLUJE právem. Odvozuje se:
    # ASM se páruje s uživatelem podle příjmení, vedoucí vidí ASM svého oddělení
    # (právo hlavni_vedouci_{oddělení}). Zde jsou jen role nad rámec toho.
    'lupa_extra':     {'kategorie': 'Modul Lupou na obchod', 'nazev': 'Extra čtenář',  'popis': 'Vidí data všech ASM a smí vkládat komentáře k jejich poznámkám. Nemůže nahrávat data.', 'ikona': 'visibility'},
    'lupa_vkladatel': {'kategorie': 'Modul Lupou na obchod', 'nazev': 'Vkladatel dat', 'popis': 'Smí nahrávat soubory GIST prodejů (dávkově). Nevidí data ASM, ke kterým nemá přístup jinou cestou.', 'ikona': 'upload'},
    'lupa_admin':     {'kategorie': 'Modul Lupou na obchod', 'nazev': 'Správce',       'popis': 'Plná práva k modulu: všechna ASM, import, komentáře, audit výjezdů.', 'ikona': 'local_police'},

}

# Práva kategorie „Administrace portálu" — nikdy se nepřidělují přes UI ani
# se neukládají komukoliv jinému než skrytému hlavnímu adminovi (iduser=1).
# Katalog si je ponechává kvůli popiskům a kontrolám v kódu; z nabídky
# (ziskej_kompletni_seznam_prav) i z DB (intranet_data._vycisti_admin_prava)
# jsou odstraněna.
ADMIN_ONLY_PRAVA = {k for k, v in ZAKLADNI_PRAVA.items()
                    if v.get('kategorie') == 'Administrace portálu'}

def ziskej_kompletni_seznam_prav(oddeleni, typy_volna):
    prava = {k: v for k, v in ZAKLADNI_PRAVA.items() if k not in ADMIN_ONLY_PRAVA}

    for odd in oddeleni.keys():
        prava[f'slozka_{odd.lower()}'] = {'kategorie': 'Složky a Evidence', 'nazev': f'Složka: {odd}', 'popis': f'Přístup do záznamů oddělení {odd}.', 'ikona': 'folder'}
        prava[f'kalendar_{odd.lower()}'] = {'kategorie': 'Složky a Evidence', 'nazev': f'Kalendář: {odd}', 'popis': f'Vidí v kalendáři lidi z oddělení {odd}.', 'ikona': 'event_note'}
        prava[f'tisk_odd_{odd.lower()}'] = {'kategorie': 'Práva k Exportům', 'nazev': f'Export oddělení: {odd}', 'popis': f'Může stáhnout do Excelu docházku {odd}.', 'ikona': 'print'}
        prava[f'hlavni_vedouci_{odd.lower()}'] = {'kategorie': 'Manažer oddělení', 'nazev': f'Hlavní vedoucí: {odd}', 'popis': f'Oficiální šéf (oranžová hvězda) oddělení {odd}. Umožňuje mu schvalovat.', 'ikona': 'star'}
        prava[f'porovnani_odd_{odd.lower()}'] = {'kategorie': 'Porovnání dovolené', 'nazev': f'Porovnání: {odd}', 'popis': f'Může porovnávat CSV zůstatky dovolené zaměstnanců oddělení {odd}.', 'ikona': 'compare'}
        # Prefix „cp_schval_odd_" je krátký záměrně (privileges.name = VARCHAR(45)),
        # aby se vešel i delší název oddělení. Stejný řetězec se kontroluje v
        # intranet_cenopripad._schval_odd_slugs a v bráně modulu v intranet.py.
        prava[f'cp_schval_odd_{odd.lower()}'] = {'kategorie': 'Modul Cenopřípad', 'nazev': f'Schvalovatel – oddělení: {odd}', 'popis': f'Smí schvalovat/zamítat žádosti (cenopřípady) všech uživatelů z oddělení {odd}. Nepřidává práva žadatele ani zobrazení OP a marží v %.', 'ikona': 'how_to_reg'}

    for t_id, t_nazev in typy_volna.items():
        prava[f'tisk_{t_id}'] = {'kategorie': 'Práva k Exportům', 'nazev': f'Tisk: {t_nazev}', 'popis': f'Umožňuje exportovat absence typu {t_nazev}.', 'ikona': 'print'}

    return prava