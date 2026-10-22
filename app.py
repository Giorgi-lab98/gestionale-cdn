from pathlib import Path
import os
import re
import sqlite3
import tempfile
import uuid
from datetime import date

import streamlit as st
import pandas as pd
import pdfplumber


# =========================
# CONFIG
# =========================

# Il database è un file nella stessa cartella dell'app.
# Non serve MySQL, non serve nessun servizio esterno.
DB_PATH = Path(__file__).parent / "gestionale_cdn.sqlite"

UPLOAD_DIR = Path(__file__).parent / "allegati" / "contratti"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

ANNI_LABEL = {
    "Tutti": None,
    "2025": 2025,
    "2026": 2026,
    "2027": 2027,
    "2028": 2028,
    "2029": 2029,
}

MESI_LABEL = {
    "Tutti": None,
    "Gennaio": 1,
    "Febbraio": 2,
    "Marzo": 3,
    "Aprile": 4,
    "Maggio": 5,
    "Giugno": 6,
    "Luglio": 7,
    "Agosto": 8,
    "Settembre": 9,
    "Ottobre": 10,
    "Novembre": 11,
    "Dicembre": 12,
}

PRESTAZIONI_LABEL = {
    "Tutte": None,
    "Derattizzazione": "DR",
    "Deblattizzazione": "DB",
    "Disinfestazione": "DS",
    "Monitoraggio topi e insetti": "MTI",
}

PRESTAZIONI_CODICI = ["DR", "DB", "DS", "MTI"]


# =========================
# DB — CONNESSIONE
# =========================

def get_connection():
    """
    Restituisce una connessione SQLite.
    detect_types permette la conversione automatica di DATE -> date Python.
    isolation_level=None = autocommit disabilitato (si gestisce manualmente).
    """
    conn = sqlite3.connect(
        DB_PATH,
        detect_types=sqlite3.PARSE_DECLTYPES | sqlite3.PARSE_COLNAMES,
        isolation_level=None,   # gestione manuale delle transazioni
        check_same_thread=False,
    )
    conn.row_factory = sqlite3.Row
    # Abilita le foreign key (SQLite le ignora per default)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")  # migliore gestione concorrenza
    return conn


# =========================
# DB — SCHEMA COMPLETO
# =========================

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS prestazioni (
    codice_prestazione TEXT PRIMARY KEY,
    nome_prestazione   TEXT NOT NULL,
    descrizione        TEXT,
    attiva             INTEGER DEFAULT 1
);

CREATE TABLE IF NOT EXISTS clienti (
    id_cliente     INTEGER PRIMARY KEY AUTOINCREMENT,
    nome_cliente   TEXT NOT NULL,
    partita_iva    TEXT,
    codice_fiscale TEXT,
    email          TEXT,
    telefono       TEXT,
    note           TEXT,
    created_at     TEXT DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS sedi (
    id_sede       INTEGER PRIMARY KEY AUTOINCREMENT,
    id_cliente    INTEGER NOT NULL REFERENCES clienti(id_cliente),
    nome_sede     TEXT,
    indirizzo     TEXT,
    comune        TEXT,
    note_accesso  TEXT,
    attiva        INTEGER DEFAULT 1,
    created_at    TEXT DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS contratti (
    id_contratto         INTEGER PRIMARY KEY AUTOINCREMENT,
    id_cliente           INTEGER NOT NULL REFERENCES clienti(id_cliente),
    id_sede              INTEGER NOT NULL REFERENCES sedi(id_sede),
    numero_contratto     TEXT UNIQUE,
    data_contratto       TEXT,
    data_inizio          TEXT,
    data_fine            TEXT,
    durata_mesi          INTEGER,
    stato                TEXT DEFAULT 'bozza'
                             CHECK(stato IN ('bozza','attivo','scaduto','annullato')),
    modalita_fatturazione TEXT,
    modalita_pagamento    TEXT,
    pdf_path             TEXT,
    pdf_hash             TEXT UNIQUE,
    note                 TEXT,
    created_at           TEXT DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS contratto_prestazioni (
    id_contratto_prestazione INTEGER PRIMARY KEY AUTOINCREMENT,
    id_contratto             INTEGER NOT NULL REFERENCES contratti(id_contratto),
    codice_prestazione       TEXT REFERENCES prestazioni(codice_prestazione),
    descrizione_voce         TEXT,
    frequenza                TEXT,
    quantita_annua           INTEGER,
    costo_unitario           REAL,
    costo_totale             REAL,
    tipo_costo               TEXT DEFAULT 'ricorrente'
                                 CHECK(tipo_costo IN ('ricorrente','una_tantum','opzionale')),
    note                     TEXT
);

CREATE TABLE IF NOT EXISTS interventi_programmati (
    id_intervento_programmato INTEGER PRIMARY KEY AUTOINCREMENT,
    id_contratto              INTEGER REFERENCES contratti(id_contratto),
    id_sede                   INTEGER NOT NULL REFERENCES sedi(id_sede),
    codice_prestazione        TEXT NOT NULL REFERENCES prestazioni(codice_prestazione),
    anno                      INTEGER NOT NULL,
    mese                      INTEGER NOT NULL,
    stato                     TEXT DEFAULT 'programmato'
                                  CHECK(stato IN ('programmato','eseguito','annullato')),
    id_tariffa                INTEGER REFERENCES tariffe_prestazioni(id_tariffa),
    costo_unitario            REAL DEFAULT 0,
    costo_totale              REAL DEFAULT 0,
    stato_pagamento           TEXT DEFAULT 'non_pagato'
                                  CHECK(stato_pagamento IN ('non_pagato','pagato','parziale','non_dovuto')),
    stato_fatturazione        TEXT DEFAULT 'da_fatturare'
                                  CHECK(stato_fatturazione IN ('da_fatturare','fatturato','non_fatturare')),
    numero_fattura            TEXT,
    data_pagamento            TEXT,
    note_fatturazione         TEXT,
    note                      TEXT,
    created_at                TEXT DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS interventi_effettuati (
    id_intervento_effettuato  INTEGER PRIMARY KEY AUTOINCREMENT,
    id_intervento_programmato INTEGER NOT NULL REFERENCES interventi_programmati(id_intervento_programmato),
    data_intervento           TEXT NOT NULL,
    tecnico                   TEXT,
    esito                     TEXT DEFAULT 'eseguito'
                                  CHECK(esito IN ('eseguito','non_eseguito','parziale')),
    note                      TEXT,
    report_path               TEXT,
    created_at                TEXT DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS allegati (
    id_allegato              INTEGER PRIMARY KEY AUTOINCREMENT,
    tipo_allegato            TEXT CHECK(tipo_allegato IN ('contratto','report','foto','altro')),
    id_contratto             INTEGER REFERENCES contratti(id_contratto),
    id_intervento_effettuato INTEGER REFERENCES interventi_effettuati(id_intervento_effettuato),
    nome_file                TEXT NOT NULL,
    percorso_file            TEXT NOT NULL,
    data_caricamento         TEXT DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS tariffe_prestazioni (
    id_tariffa           INTEGER PRIMARY KEY AUTOINCREMENT,
    codice_prestazione   TEXT NOT NULL REFERENCES prestazioni(codice_prestazione),
    voce_pdf             TEXT,
    descrizione_tariffa  TEXT NOT NULL,
    descrizione_standard TEXT,
    frequenza            TEXT,
    quantita_annua       INTEGER DEFAULT 1,
    costo_unitario       REAL,
    costo_totale         REAL,
    tipo_costo           TEXT DEFAULT 'ricorrente'
                             CHECK(tipo_costo IN ('ricorrente','una_tantum','opzionale')),
    attiva               INTEGER DEFAULT 1,
    note                 TEXT,
    UNIQUE(codice_prestazione, descrizione_tariffa)
);

-- Indice per evitare duplicati negli interventi programmati
CREATE UNIQUE INDEX IF NOT EXISTS uq_intervento_programmato
    ON interventi_programmati(id_sede, anno, mese, codice_prestazione);

-- Seed prestazioni standard (ignora se già presenti)
INSERT OR IGNORE INTO prestazioni(codice_prestazione, nome_prestazione) VALUES
    ('DR',  'Derattizzazione'),
    ('DB',  'Deblattizzazione'),
    ('DS',  'Disinfestazione'),
    ('MTI', 'Monitoraggio topi e insetti');
"""


def ensure_schema():
    """Crea tutte le tabelle e gli indici se non esistono già."""
    conn = get_connection()
    try:
        conn.executescript(SCHEMA_SQL)
    finally:
        conn.close()


# =========================
# DATE UTILITY
# =========================

def aggiungi_mesi(data_base, mesi):
    mese = data_base.month - 1 + int(mesi)
    anno = data_base.year + mese // 12
    mese = mese % 12 + 1
    giorni_mese = [
        31,
        29 if anno % 4 == 0 and (anno % 100 != 0 or anno % 400 == 0) else 28,
        31, 30, 31, 30, 31, 31, 30, 31, 30, 31,
    ]
    giorno = min(data_base.day, giorni_mese[mese - 1])
    return date(anno, mese, giorno)


# =========================
# PDF UTILITY
# =========================

def normalizza_importo(valore):
    if valore is None:
        return 0.0
    valore = str(valore).replace(".", "").replace(",", ".")
    valore = re.sub(r"[^\d.]", "", valore)
    try:
        return float(valore) if valore else 0.0
    except ValueError:
        return 0.0


def pulisci_riga(riga):
    return " ".join(str(riga).replace("\n", " ").split()).strip()


def estrai_testo_pdf(pdf_file):
    testo_completo = ""
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
            tmp.write(pdf_file.getbuffer())
            tmp_path = tmp.name
        with pdfplumber.open(tmp_path) as pdf:
            for pagina in pdf.pages:
                testo = pagina.extract_text()
                if testo:
                    testo_completo += testo + "\n"
        return testo_completo
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.remove(tmp_path)


def estrai_blocco(testo, start, end):
    pattern = rf"{start}\s*(.+?)(?:{end})"
    match = re.search(pattern, testo, re.IGNORECASE | re.DOTALL)
    if not match:
        return ""
    return " ".join(match.group(1).split())


def rimuovi_parole_servizi(blocco):
    parole_da_escludere = [
        "Disinfezione", "Disinfestazione", "Derattizzazione",
        "Allontanamento Volatili", "Trattamento Antitarlo", "Restauro arredi",
    ]
    for parola in parole_da_escludere:
        blocco = blocco.replace(parola, "")
    return " ".join(blocco.split())


def estrai_cliente_da_pdf(testo):
    blocco = estrai_blocco(testo, r"Spett\.Le", r"Sede di destinazione:")
    blocco = rimuovi_parole_servizi(blocco)
    righe = []
    for pezzo in re.split(r"(?=\d{5}\s)|(?=Piazza)|(?=Via)|(?=Viale)|(?=Corso)", blocco):
        pezzo = pezzo.strip()
        if pezzo:
            righe.append(pezzo)
    if not righe:
        return "", ""
    cliente = righe[0].strip()
    indirizzo_cliente = " ".join(righe[1:]).strip()
    return cliente, indirizzo_cliente


def estrai_sede_da_pdf(testo):
    blocco = estrai_blocco(testo, r"Sede di destinazione:", r"Oggetto:")
    blocco = rimuovi_parole_servizi(blocco)
    return " ".join(blocco.split())


def estrai_oggetto_da_pdf(testo):
    match = re.search(
        r"Oggetto:\s*(.+?)(?:PREMESSA|Premessa|INDICE|Sede legale:|Documento elaborato da:|$)",
        testo, re.IGNORECASE | re.DOTALL,
    )
    if not match:
        return ""
    return " ".join(match.group(1).split())


def classifica_prestazione(testo):
    t = str(testo).lower()
    if "monitoraggio" in t and ("roditori" in t or "insetti" in t or "topi" in t):
        return "MTI"
    if "deblattizzazione" in t or "blatte" in t:
        return "DB"
    if "derattizzazione" in t or "roditori" in t or "topi" in t:
        return "DR"
    if "disinfestazione" in t:
        return "DS"
    return ""


def tipo_costo_da_descrizione(descrizione):
    d = str(descrizione).lower()
    if "installazione" in d or "una tantum" in d:
        return "una_tantum"
    if "su richiesta" in d or "opzionale" in d or "lampada" in d:
        return "opzionale"
    return "ricorrente"


def frequenza_da_quantita_e_descrizione(quantita, descrizione):
    d = str(descrizione).lower()
    if "una tantum" in d or "installazione" in d:
        return "una tantum"
    mapping = {12: "mensile", 6: "bimestrale", 4: "trimestrale", 2: "semestrale", 1: "annuale"}
    return mapping.get(int(quantita), "altro")


def estrai_voci_costo(testo):
    testo_norm = " ".join(testo.split())
    segmento = testo_norm
    if "Servizio importo q.tà Imponibile" in testo_norm:
        segmento = testo_norm.split("Servizio importo q.tà Imponibile", 1)[1]
    if "Augurandoci" in segmento:
        segmento = segmento.split("Augurandoci", 1)[0]
    pattern = re.compile(r"(.+?)\s+€\s*([\d.,]+)\s+(\d+)\s+€\s*([\d.,]+)", re.IGNORECASE)
    risultati = []
    for match in pattern.finditer(segmento):
        descrizione = pulisci_riga(match.group(1))
        descrizione = re.sub(
            r"^(Servizio|importo|q\.tà|Imponibile)\s+", "", descrizione, flags=re.IGNORECASE
        ).strip()
        costo_unitario = normalizza_importo(match.group(2))
        quantita = int(match.group(3))
        costo_totale = normalizza_importo(match.group(4))
        codice = classifica_prestazione(descrizione) or classifica_prestazione(testo) or "MTI"
        tipo_costo = tipo_costo_da_descrizione(descrizione)
        frequenza = frequenza_da_quantita_e_descrizione(quantita, descrizione)
        risultati.append({
            "codice_prestazione": codice,
            "descrizione_voce": descrizione,
            "frequenza": frequenza,
            "quantita_annua": quantita,
            "costo_unitario": costo_unitario,
            "costo_totale": costo_totale,
            "tipo_costo": tipo_costo,
            "note": "",
        })
    return risultati


def estrai_dati_contratto(testo):
    cliente, indirizzo_cliente = estrai_cliente_da_pdf(testo)
    sede = estrai_sede_da_pdf(testo)
    oggetto = estrai_oggetto_da_pdf(testo)
    fatturazione = ""
    pagamento = ""
    durata_mesi = 12
    match_fatturazione = re.search(r"FATTURAZIONE:\s*(.+?)(?:;|\n|•)", testo, re.IGNORECASE)
    if match_fatturazione:
        fatturazione = match_fatturazione.group(1).strip()
    match_pagamento = re.search(r"PAGAMENTO:\s*(.+?)(?:\.|\n|•)", testo, re.IGNORECASE)
    if match_pagamento:
        pagamento = match_pagamento.group(1).strip()
    match_durata = re.search(r"validità\s+di\s+n\.\s*(\d+)\s+mesi", testo, re.IGNORECASE)
    if match_durata:
        durata_mesi = int(match_durata.group(1))
    return {
        "cliente": cliente,
        "indirizzo_cliente": indirizzo_cliente,
        "sede_destinazione": sede,
        "oggetto": oggetto,
        "codice_prestazione": classifica_prestazione(testo),
        "fatturazione": fatturazione,
        "pagamento": pagamento,
        "durata_mesi": durata_mesi,
        "voci_costo": estrai_voci_costo(testo),
    }


# =========================
# QUERY — LETTURA
# =========================

def carica_interventi(anno=None, mese=None, prestazione=None, cliente=None):
    conn = get_connection()
    # SQLite non ha CASE WHEN sui mesi come MySQL, usiamo una subquery con stringa
    query = """
        SELECT
            ip.id_intervento_programmato AS id,
            ip.id_contratto              AS id_contratto,
            c.nome_cliente               AS cliente,
            s.nome_sede                  AS sede,
            s.indirizzo                  AS indirizzo,
            ip.anno                      AS anno,
            ip.mese                      AS mese_numero,
            CASE ip.mese
                WHEN 1  THEN 'Gennaio'   WHEN 2  THEN 'Febbraio'
                WHEN 3  THEN 'Marzo'     WHEN 4  THEN 'Aprile'
                WHEN 5  THEN 'Maggio'    WHEN 6  THEN 'Giugno'
                WHEN 7  THEN 'Luglio'    WHEN 8  THEN 'Agosto'
                WHEN 9  THEN 'Settembre' WHEN 10 THEN 'Ottobre'
                WHEN 11 THEN 'Novembre'  WHEN 12 THEN 'Dicembre'
            END                          AS mese,
            p.nome_prestazione           AS prestazione,
            ip.codice_prestazione        AS codice_prestazione,
            ip.stato                     AS stato,
            ip.costo_unitario            AS costo_unitario,
            ip.costo_totale              AS costo_totale,
            COALESCE(ip.stato_fatturazione, 'da_fatturare') AS stato_fatturazione,
            COALESCE(ip.stato_pagamento,   'non_pagato')   AS stato_pagamento,
            ip.numero_fattura            AS numero_fattura,
            ip.data_pagamento            AS data_pagamento,
            ip.note_fatturazione         AS note_fatturazione,
            ip.note                      AS note
        FROM interventi_programmati ip
        JOIN sedi     s ON ip.id_sede              = s.id_sede
        JOIN clienti  c ON s.id_cliente            = c.id_cliente
        JOIN prestazioni p ON ip.codice_prestazione = p.codice_prestazione
        WHERE 1=1
    """
    params = []
    if anno is not None:
        query += " AND ip.anno = ?"
        params.append(anno)
    if mese is not None:
        query += " AND ip.mese = ?"
        params.append(mese)
    if prestazione is not None:
        query += " AND ip.codice_prestazione = ?"
        params.append(prestazione)
    if cliente is not None:
        query += " AND c.nome_cliente = ?"
        params.append(cliente)
    query += " ORDER BY ip.anno, ip.mese, c.nome_cliente, s.nome_sede, p.nome_prestazione"
    try:
        return pd.read_sql(query, conn, params=params)
    finally:
        conn.close()


def carica_clienti():
    conn = get_connection()
    try:
        return pd.read_sql(
            "SELECT id_cliente, nome_cliente FROM clienti ORDER BY nome_cliente", conn
        )
    finally:
        conn.close()


def carica_sedi_cliente(id_cliente):
    conn = get_connection()
    try:
        return pd.read_sql(
            "SELECT id_sede, nome_sede, indirizzo FROM sedi WHERE id_cliente = ? ORDER BY nome_sede, indirizzo",
            conn, params=[id_cliente],
        )
    finally:
        conn.close()


def carica_tariffe_prestazioni():
    conn = get_connection()
    try:
        return pd.read_sql(
            """SELECT id_tariffa, codice_prestazione, descrizione_tariffa,
                      costo_unitario, costo_totale
               FROM tariffe_prestazioni WHERE attiva = 1
               ORDER BY codice_prestazione, descrizione_tariffa""",
            conn,
        )
    finally:
        conn.close()


def contratto_esiste(numero_contratto):
    if not numero_contratto:
        return False
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT id_contratto FROM contratti WHERE numero_contratto = ? LIMIT 1",
            (numero_contratto,),
        ).fetchone()
        return row is not None
    finally:
        conn.close()


# =========================
# OPERAZIONI DB — SCRITTURA
# =========================

def get_or_create_cliente_cur(conn, nome_cliente):
    row = conn.execute(
        "SELECT id_cliente FROM clienti WHERE nome_cliente = ?", (nome_cliente,)
    ).fetchone()
    if row:
        return row[0]
    cur = conn.execute("INSERT INTO clienti (nome_cliente) VALUES (?)", (nome_cliente,))
    return cur.lastrowid


def get_or_create_sede_cur(conn, id_cliente, nome_sede, indirizzo):
    row = conn.execute(
        """SELECT id_sede FROM sedi
           WHERE id_cliente = ?
             AND COALESCE(nome_sede, '') = COALESCE(?, '')
             AND COALESCE(indirizzo,  '') = COALESCE(?, '')""",
        (id_cliente, nome_sede, indirizzo),
    ).fetchone()
    if row:
        return row[0]
    cur = conn.execute(
        "INSERT INTO sedi (id_cliente, nome_sede, indirizzo) VALUES (?, ?, ?)",
        (id_cliente, nome_sede, indirizzo),
    )
    return cur.lastrowid


def get_or_create_tariffa_cur(conn, codice_prestazione, descrizione_tariffa, costo_unitario, costo_totale):
    row = conn.execute(
        """SELECT id_tariffa FROM tariffe_prestazioni
           WHERE codice_prestazione = ? AND descrizione_tariffa = ? LIMIT 1""",
        (codice_prestazione, descrizione_tariffa),
    ).fetchone()
    if row:
        conn.execute(
            "UPDATE tariffe_prestazioni SET costo_unitario = ?, costo_totale = ?, attiva = 1 WHERE id_tariffa = ?",
            (costo_unitario, costo_totale, row[0]),
        )
        return row[0]
    cur = conn.execute(
        """INSERT INTO tariffe_prestazioni
           (codice_prestazione, descrizione_tariffa, costo_unitario, costo_totale, attiva)
           VALUES (?, ?, ?, ?, 1)""",
        (codice_prestazione, descrizione_tariffa, costo_unitario, costo_totale),
    )
    return cur.lastrowid


def registra_intervento(id_intervento_programmato, data_intervento, tecnico, esito, note):
    conn = get_connection()
    try:
        conn.execute("BEGIN")
        row = conn.execute(
            "SELECT stato FROM interventi_programmati WHERE id_intervento_programmato = ?",
            (id_intervento_programmato,),
        ).fetchone()
        if row is None:
            raise Exception("Intervento programmato non trovato.")
        if row[0] == "eseguito":
            raise Exception("Intervento già registrato come eseguito.")
        conn.execute(
            """INSERT INTO interventi_effettuati
               (id_intervento_programmato, data_intervento, tecnico, esito, note)
               VALUES (?, ?, ?, ?, ?)""",
            (id_intervento_programmato, str(data_intervento), tecnico, esito, note),
        )
        nuovo_stato = {"eseguito": "eseguito", "parziale": "programmato", "non_eseguito": "annullato"}.get(
            esito, "programmato"
        )
        conn.execute(
            "UPDATE interventi_programmati SET stato = ? WHERE id_intervento_programmato = ?",
            (nuovo_stato, id_intervento_programmato),
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def elimina_intervento_programmato(id_intervento_programmato):
    conn = get_connection()
    try:
        conn.execute("BEGIN")
        row = conn.execute(
            "SELECT id_intervento_programmato FROM interventi_programmati WHERE id_intervento_programmato = ?",
            (id_intervento_programmato,),
        ).fetchone()
        if row is None:
            raise Exception(f"Intervento {id_intervento_programmato} non trovato.")
        conn.execute(
            "DELETE FROM interventi_effettuati WHERE id_intervento_programmato = ?",
            (id_intervento_programmato,),
        )
        conn.execute(
            "DELETE FROM interventi_programmati WHERE id_intervento_programmato = ?",
            (id_intervento_programmato,),
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def crea_intervento_manual(
    nome_cliente, nome_sede, indirizzo_sede, codice_prestazione,
    anno, mese, costo_unitario, costo_totale, descrizione_tariffa, salva_tariffa, note,
):
    conn = get_connection()
    try:
        conn.execute("BEGIN")
        id_cliente = get_or_create_cliente_cur(conn, nome_cliente.strip())
        id_sede = get_or_create_sede_cur(conn, id_cliente, nome_sede.strip(), indirizzo_sede.strip())
        id_tariffa = None
        if salva_tariffa:
            id_tariffa = get_or_create_tariffa_cur(
                conn, codice_prestazione, descrizione_tariffa.strip(),
                float(costo_unitario), float(costo_totale),
            )
        cur = conn.execute(
            """INSERT OR IGNORE INTO interventi_programmati
               (id_contratto, id_tariffa, id_sede, codice_prestazione,
                anno, mese, costo_unitario, costo_totale, stato, note)
               VALUES (NULL, ?, ?, ?, ?, ?, ?, ?, 'programmato', ?)""",
            (id_tariffa, id_sede, codice_prestazione, anno, mese,
             float(costo_unitario), float(costo_totale), note),
        )
        if cur.rowcount == 0:
            raise Exception(
                f"Esiste già un intervento {codice_prestazione} per questa sede nel mese selezionato."
            )
        lastrowid = cur.lastrowid
        conn.execute("COMMIT")
        return lastrowid
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def salva_pdf_con_nome_unico(pdf_file):
    if pdf_file is None:
        return None
    nome_originale = Path(pdf_file.name).name.replace(" ", "_")
    nome_sicuro = re.sub(r"[^A-Za-z0-9_.-]", "_", nome_originale)
    nome_finale = f"{uuid.uuid4().hex[:8]}_{nome_sicuro}"
    pdf_path = UPLOAD_DIR / nome_finale
    with open(pdf_path, "wb") as f:
        f.write(pdf_file.getbuffer())
    return str(pdf_path)


def genera_interventi_da_contratto_cur(
    conn, id_contratto, id_sede, codice_prestazione,
    data_inizio, frequenza, quantita_annua, costo_unitario, costo_totale,
):
    mesi_step = {"mensile": 1, "bimestrale": 2, "trimestrale": 3, "semestrale": 6, "annuale": 12}
    if frequenza not in mesi_step:
        return 0
    step = mesi_step[frequenza]
    mese_corrente = data_inizio.month
    anno_corrente = data_inizio.year
    numero_interventi = int(quantita_annua or 1)
    interventi_creati = 0
    for _ in range(numero_interventi):
        row = conn.execute(
            """SELECT id_intervento_programmato FROM interventi_programmati
               WHERE id_sede = ? AND codice_prestazione = ? AND anno = ? AND mese = ? LIMIT 1""",
            (id_sede, codice_prestazione, anno_corrente, mese_corrente),
        ).fetchone()
        if row:
            conn.execute(
                """UPDATE interventi_programmati
                   SET id_contratto = ?, costo_unitario = ?, costo_totale = ?
                   WHERE id_intervento_programmato = ?""",
                (id_contratto, costo_unitario, costo_totale, row[0]),
            )
        else:
            conn.execute(
                """INSERT INTO interventi_programmati
                   (id_contratto, id_sede, codice_prestazione, anno, mese,
                    costo_unitario, costo_totale, stato)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 'programmato')""",
                (id_contratto, id_sede, codice_prestazione, anno_corrente,
                 mese_corrente, costo_unitario, costo_totale),
            )
            interventi_creati += 1
        mese_corrente += step
        while mese_corrente > 12:
            mese_corrente -= 12
            anno_corrente += 1
    return interventi_creati


def genera_intervento_una_tantum_cur(
    conn, id_contratto, id_sede, codice_prestazione,
    data_inizio, costo_unitario, costo_totale,
):
    anno = data_inizio.year
    mese = data_inizio.month
    row = conn.execute(
        """SELECT id_intervento_programmato FROM interventi_programmati
           WHERE id_sede = ? AND codice_prestazione = ? AND anno = ? AND mese = ? LIMIT 1""",
        (id_sede, codice_prestazione, anno, mese),
    ).fetchone()
    if row:
        conn.execute(
            """UPDATE interventi_programmati
               SET id_contratto = ?, costo_unitario = ?, costo_totale = ?
               WHERE id_intervento_programmato = ?""",
            (id_contratto, costo_unitario, costo_totale, row[0]),
        )
        return 0
    conn.execute(
        """INSERT INTO interventi_programmati
           (id_contratto, id_sede, codice_prestazione, anno, mese,
            costo_unitario, costo_totale, stato)
           VALUES (?, ?, ?, ?, ?, ?, ?, 'programmato')""",
        (id_contratto, id_sede, codice_prestazione, anno, mese, costo_unitario, costo_totale),
    )
    return 1


def salva_contratto_completo(
    nome_cliente, sede_destinazione, indirizzo_sede, nome_documento,
    data_contratto, data_inizio, data_fine, durata_mesi, stato,
    modalita_fatturazione, modalita_pagamento, pdf_file, note_contratto, df_voci_editato,
):
    if contratto_esiste(nome_documento):
        raise Exception(f"Contratto già presente: {nome_documento}")
    pdf_path = salva_pdf_con_nome_unico(pdf_file)
    conn = get_connection()
    try:
        conn.execute("BEGIN")
        id_cliente = get_or_create_cliente_cur(conn, nome_cliente.strip())
        id_sede = get_or_create_sede_cur(conn, id_cliente, sede_destinazione.strip(), indirizzo_sede.strip())
        cur = conn.execute(
            """INSERT INTO contratti
               (id_cliente, id_sede, numero_contratto, data_contratto, data_inizio, data_fine,
                durata_mesi, stato, modalita_fatturazione, modalita_pagamento, pdf_path, note)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (id_cliente, id_sede, nome_documento, str(data_contratto), str(data_inizio),
             str(data_fine), durata_mesi, stato, modalita_fatturazione,
             modalita_pagamento, pdf_path, note_contratto),
        )
        id_contratto = cur.lastrowid
        righe_salvate = 0
        interventi_generati = 0
        for _, riga in df_voci_editato.iterrows():
            descrizione = str(riga.get("descrizione_voce", "")).strip()
            if not descrizione:
                continue
            codice_prestazione = str(riga.get("codice_prestazione", "MTI")).strip()
            if codice_prestazione not in PRESTAZIONI_CODICI:
                codice_prestazione = "MTI"
            frequenza      = str(riga.get("frequenza", "")).strip()
            quantita_annua = int(riga.get("quantita_annua", 1) or 1)
            costo_unitario = float(riga.get("costo_unitario", 0.0) or 0.0)
            costo_totale   = float(riga.get("costo_totale", 0.0) or 0.0)
            tipo_costo     = str(riga.get("tipo_costo", "ricorrente")).strip()
            note_prest     = str(riga.get("note", "")).strip()
            conn.execute(
                """INSERT INTO contratto_prestazioni
                   (id_contratto, codice_prestazione, descrizione_voce, frequenza,
                    quantita_annua, costo_unitario, costo_totale, tipo_costo, note)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (id_contratto, codice_prestazione, descrizione, frequenza,
                 quantita_annua, costo_unitario, costo_totale, tipo_costo, note_prest),
            )
            righe_salvate += 1
            if tipo_costo == "ricorrente":
                interventi_generati += genera_interventi_da_contratto_cur(
                    conn, id_contratto, id_sede, codice_prestazione,
                    data_inizio, frequenza, quantita_annua, costo_unitario, costo_totale,
                )
            elif tipo_costo == "una_tantum":
                interventi_generati += genera_intervento_una_tantum_cur(
                    conn, id_contratto, id_sede, codice_prestazione,
                    data_inizio, costo_unitario, costo_totale,
                )
        conn.execute("COMMIT")
        return {"id_contratto": id_contratto, "righe_salvate": righe_salvate, "interventi_generati": interventi_generati}
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def aggiorna_costi_intervento(id_intervento_programmato, costo_unitario, frequenza, applica_simili_zero=False):
    conn = get_connection()
    try:
        conn.execute("BEGIN")
        costo_unitario = float(costo_unitario)
        frequenza = int(frequenza)
        if frequenza < 1:
            raise Exception("La frequenza deve essere almeno 1.")
        costo_totale = costo_unitario * frequenza
        row = conn.execute(
            "SELECT id_sede, codice_prestazione, anno FROM interventi_programmati WHERE id_intervento_programmato = ?",
            (id_intervento_programmato,),
        ).fetchone()
        if row is None:
            raise Exception("Intervento non trovato.")
        id_sede, codice_prestazione, anno = row[0], row[1], row[2]
        conn.execute(
            "UPDATE interventi_programmati SET costo_unitario = ?, costo_totale = ? WHERE id_intervento_programmato = ?",
            (costo_unitario, costo_totale, id_intervento_programmato),
        )
        if applica_simili_zero:
            conn.execute(
                """UPDATE interventi_programmati
                   SET costo_unitario = ?, costo_totale = ?
                   WHERE id_sede = ? AND codice_prestazione = ? AND anno = ?
                     AND COALESCE(costo_unitario, 0) = 0 AND COALESCE(costo_totale, 0) = 0""",
                (costo_unitario, costo_totale, id_sede, codice_prestazione, anno),
            )
        conn.execute("COMMIT")
        return costo_totale
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def aggiorna_fatturazione_intervento(
    id_intervento_programmato, stato_fatturazione, stato_pagamento,
    numero_fattura, data_pagamento, note_fatturazione,
):
    conn = get_connection()
    try:
        conn.execute("BEGIN")
        if data_pagamento is None or (isinstance(data_pagamento, float) and pd.isna(data_pagamento)):
            data_pagamento = None
        conn.execute(
            """UPDATE interventi_programmati
               SET stato_fatturazione = ?, stato_pagamento = ?, numero_fattura = ?,
                   data_pagamento = ?, note_fatturazione = ?
               WHERE id_intervento_programmato = ?""",
            (
                stato_fatturazione,
                stato_pagamento,
                str(numero_fattura or "").strip() or None,
                str(data_pagamento) if data_pagamento else None,
                str(note_fatturazione or "").strip() or None,
                id_intervento_programmato,
            ),
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def normalizza_nome_per_match(testo):
    testo = str(testo or "").lower().strip()
    testo = re.sub(r"\b(srl|s\.r\.l\.|spa|s\.p\.a\.|snc|sas|societa|soc|cooperativa|coop)\b", " ", testo)
    testo = re.sub(r"[^a-z0-9]+", " ", testo)
    return " ".join(testo.split())


def trova_clienti_simili(nome_cliente, clienti_df, limite=8):
    if not nome_cliente or clienti_df.empty:
        return []
    query = normalizza_nome_per_match(nome_cliente)
    if not query:
        return []
    risultati = []
    for nome in clienti_df["nome_cliente"].dropna().astype(str).unique():
        nome_norm = normalizza_nome_per_match(nome)
        if query in nome_norm or nome_norm in query:
            risultati.append(nome)
    return sorted(risultati)[:limite]


# =========================
# UI
# =========================

st.set_page_config(
    page_title="Gestionale CDN",
    page_icon="🌿",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.markdown("""
<style>
.main .block-container { padding-top:1.4rem; padding-bottom:2rem; max-width:1500px; }
h1,h2,h3 { letter-spacing:-0.02em; }
.cdn-hero { padding:1.25rem 1.4rem; border-radius:18px;
    background:linear-gradient(135deg,#eef8f2 0%,#f7fbff 100%);
    border:1px solid #dfeee5; margin-bottom:1rem; }
.cdn-hero-title { font-size:1.55rem; font-weight:750; margin-bottom:.25rem; color:#1f3b2d; }
.cdn-hero-subtitle { color:#52635a; font-size:.98rem; }
.cdn-card { padding:1rem 1.1rem; border-radius:16px; border:1px solid #e6e8eb;
    background:#ffffff; box-shadow:0 2px 10px rgba(20,36,28,.04); margin-bottom:.75rem; }
.cdn-muted { color:#68746d; font-size:.92rem; }
.cdn-pill { display:inline-block; padding:.28rem .62rem; border-radius:999px;
    background:#eef5f1; border:1px solid #dce9e1; color:#234331;
    font-size:.82rem; font-weight:650; margin-right:.35rem; margin-bottom:.35rem; }
div[data-testid="stMetric"] { background:#ffffff; border:1px solid #e6e8eb;
    padding:.8rem 1rem; border-radius:14px; box-shadow:0 1px 8px rgba(20,36,28,.035); }
section[data-testid="stSidebar"] { background:#f7faf8; }
.stButton>button { border-radius:10px; font-weight:650; }
</style>
""", unsafe_allow_html=True)

st.markdown("""
<div class="cdn-hero">
    <div class="cdn-hero-title">Gestionale CDN</div>
    <div class="cdn-hero-subtitle">Consultazione interventi, caricamento prestazioni e aggiornamento operativo guidato.</div>
</div>
""", unsafe_allow_html=True)

try:
    ensure_schema()
except Exception as e:
    st.error(f"Errore inizializzazione database: {e}")
    st.stop()

pagina = st.sidebar.radio(
    "Menu",
    ["Dashboard consultazione", "Caricamento prestazione manuale", "Caricamento prestazione da contratto"],
)


# =========================
# DASHBOARD CONSULTAZIONE
# =========================

if pagina == "Dashboard consultazione":
    st.subheader("Dashboard consultazione")
    st.caption("Consulta gli interventi e aggiorna in modo guidato stato operativo, fatturazione e pagamento.")

    clienti_df_filtri = carica_clienti()
    clienti_opzioni = ["Tutti"]
    if not clienti_df_filtri.empty:
        clienti_opzioni += sorted(clienti_df_filtri["nome_cliente"].dropna().astype(str).unique().tolist())

    with st.container(border=True):
        st.markdown("#### Filtri ricerca")
        col1, col2, col3, col4 = st.columns([1, 1, 1.2, 1.8])
        with col1:
            anno_label = st.selectbox("Anno", options=list(ANNI_LABEL.keys()))
            anno = ANNI_LABEL[anno_label]
        with col2:
            mese_label = st.selectbox("Mese", options=list(MESI_LABEL.keys()))
            mese = MESI_LABEL[mese_label]
        with col3:
            prestazione_label = st.selectbox("Prestazione", options=list(PRESTAZIONI_LABEL.keys()))
            prestazione = PRESTAZIONI_LABEL[prestazione_label]
        with col4:
            cliente_label = st.selectbox("Cliente", options=clienti_opzioni, index=0, key="filtro_cliente_dashboard")
            cliente = None if cliente_label == "Tutti" else cliente_label

    df = carica_interventi(anno=anno, mese=mese, prestazione=prestazione, cliente=cliente)
    st.write(f"Interventi trovati: **{len(df)}**")

    if df.empty:
        st.info("Nessun intervento trovato con i filtri selezionati.")
    else:
        df_visualizzazione = df.drop(columns=["mese_numero", "codice_prestazione"], errors="ignore").reset_index(drop=True)

        with st.container(border=True):
            st.markdown("#### Tabella consultazione")
            st.dataframe(
                df_visualizzazione, use_container_width=True, height=390, hide_index=True,
                column_config={
                    "id": None, "id_contratto": None,
                    "costo_unitario": st.column_config.NumberColumn("Costo unitario", format="€ %.2f"),
                    "costo_totale":   st.column_config.NumberColumn("Costo totale",   format="€ %.2f"),
                    "data_pagamento": st.column_config.DateColumn("Data pagamento", format="DD/MM/YYYY"),
                },
            )

        st.markdown("---")
        st.subheader("Gestione intervento selezionato")
        st.caption("Cerca prima il cliente, poi la prestazione: il sistema mostra solo sedi e interventi coerenti con i filtri.")

        with st.container(border=True):
            st.markdown("#### Cerca intervento")
            clienti_gestione = sorted(df_visualizzazione["cliente"].dropna().astype(str).unique().tolist())
            col_sel1, col_sel2 = st.columns([1.4, 1])
            with col_sel1:
                cliente_gestione = st.selectbox("Cliente", options=clienti_gestione, index=0, key="gestione_cliente_intervento")
            df_cliente = df_visualizzazione[df_visualizzazione["cliente"].astype(str) == str(cliente_gestione)].copy()
            prestazioni_gestione = sorted(df_cliente["prestazione"].dropna().astype(str).unique().tolist())
            with col_sel2:
                prestazione_gestione = st.selectbox("Prestazione", options=prestazioni_gestione, index=0, key="gestione_prestazione_intervento")
            df_interventi_filtrati = df_cliente[df_cliente["prestazione"].astype(str) == str(prestazione_gestione)].copy()

            opzioni_intervento = []
            mappa_interventi = {}
            for _, riga_opzione in df_interventi_filtrati.iterrows():
                sede_label = str(riga_opzione.get("sede") or "Sede non specificata")
                indirizzo_label = str(riga_opzione.get("indirizzo") or "").strip()
                indirizzo_testo = f" - {indirizzo_label}" if indirizzo_label else ""
                label = (f"{sede_label}{indirizzo_testo} | {riga_opzione['mese']} {riga_opzione['anno']} | "
                         f"Stato: {riga_opzione['stato']} | ID {riga_opzione['id']}")
                opzioni_intervento.append(label)
                mappa_interventi[label] = riga_opzione

            intervento_label = st.selectbox("Sede / intervento corretto", options=opzioni_intervento, key="select_intervento_azioni_dashboard")

        riga = mappa_interventi[intervento_label]
        id_intervento = int(riga["id"])

        st.markdown(f"""
        <div class="cdn-card">
            <div style="font-weight:750;font-size:1.05rem;margin-bottom:.35rem;">{riga['cliente']}</div>
            <div class="cdn-muted">{riga['sede']} · {riga['prestazione']} · {riga['mese']} {riga['anno']}</div>
            <div style="margin-top:.65rem;">
                <span class="cdn-pill">Stato: {riga['stato']}</span>
                <span class="cdn-pill">Fattura: {riga.get('stato_fatturazione','da_fatturare')}</span>
                <span class="cdn-pill">Pagamento: {riga.get('stato_pagamento','non_pagato')}</span>
            </div>
        </div>""", unsafe_allow_html=True)

        tab_operativo, tab_fattura, tab_importi, tab_elimina = st.tabs([
            "1. Stato operativo", "2. Fattura / pagamento", "3. Importi", "4. Elimina",
        ])

        with tab_operativo:
            with st.container(border=True):
                st.markdown("#### Stato operativo")
                st.caption("Usa questa sezione quando il tecnico ha eseguito, eseguito parzialmente o non eseguito l'intervento.")
                col_op1, col_op2, col_op3 = st.columns([1, 1, 1.4])
                with col_op1:
                    data_intervento = st.date_input("Data intervento", format="DD/MM/YYYY", key=f"data_intervento_{id_intervento}")
                with col_op2:
                    esito = st.selectbox("Esito", options=["eseguito", "parziale", "non_eseguito"], key=f"esito_{id_intervento}")
                with col_op3:
                    tecnico = st.text_input("Tecnico", value="", key=f"tecnico_{id_intervento}")
                note = st.text_area("Note intervento", key=f"note_operativa_{id_intervento}")
                if st.button("Aggiorna stato operativo", key=f"btn_stato_operativo_{id_intervento}", type="primary"):
                    try:
                        registra_intervento(id_intervento, data_intervento, tecnico, esito, note)
                        st.success(f"Stato intervento ID {id_intervento} aggiornato a '{esito}'.")
                        st.rerun()
                    except Exception as e:
                        st.error(str(e))

        with tab_fattura:
            with st.container(border=True):
                st.markdown("#### Fattura / pagamento")
                st.caption("Flusso guidato: prima fattura, poi stato pagamento.")
                stato_fatturazione_corrente = str(riga.get("stato_fatturazione") or "da_fatturare")
                stato_pagamento_corrente    = str(riga.get("stato_pagamento")    or "non_pagato")
                col_f1, col_f2 = st.columns(2)
                with col_f1:
                    opts_fat = ["da_fatturare", "fatturato", "non_fatturare"]
                    stato_fatturazione = st.selectbox("Stato fatturazione", options=opts_fat,
                        index=opts_fat.index(stato_fatturazione_corrente) if stato_fatturazione_corrente in opts_fat else 0,
                        key=f"stato_fatturazione_{id_intervento}")
                    numero_fattura = st.text_input("Numero fattura",
                        value="" if pd.isna(riga.get("numero_fattura")) else str(riga.get("numero_fattura") or ""),
                        disabled=stato_fatturazione != "fatturato",
                        key=f"numero_fattura_{id_intervento}")
                with col_f2:
                    opts_pag = ["non_pagato", "pagato", "parziale", "non_dovuto"]
                    stato_pagamento = st.selectbox("Stato pagamento", options=opts_pag,
                        index=opts_pag.index(stato_pagamento_corrente) if stato_pagamento_corrente in opts_pag else 0,
                        key=f"stato_pagamento_{id_intervento}")
                    data_pagamento = None
                    if stato_pagamento in ["pagato", "parziale"]:
                        dp_corrente = riga.get("data_pagamento")
                        if pd.isna(dp_corrente) or dp_corrente is None:
                            dp_corrente = date.today()
                        data_pagamento = st.date_input("Data pagamento", value=dp_corrente,
                            format="DD/MM/YYYY", key=f"data_pagamento_{id_intervento}")
                    else:
                        st.info("La data pagamento sarà vuota finché il pagamento non risulta pagato o parziale.")
                note_fatturazione = st.text_area("Note fatturazione / pagamento",
                    value="" if pd.isna(riga.get("note_fatturazione")) else str(riga.get("note_fatturazione") or ""),
                    key=f"note_fatturazione_{id_intervento}")
                if st.button("Salva fattura / pagamento", key=f"btn_fatturazione_{id_intervento}", type="primary"):
                    try:
                        if stato_fatturazione == "fatturato" and not str(numero_fattura or "").strip():
                            st.error("Inserire il numero fattura se lo stato è 'fatturato'.")
                        elif stato_pagamento in ["pagato", "parziale"] and data_pagamento is None:
                            st.error("Inserire la data pagamento se il pagamento è pagato o parziale.")
                        else:
                            aggiorna_fatturazione_intervento(
                                id_intervento, stato_fatturazione, stato_pagamento,
                                numero_fattura,
                                data_pagamento if stato_pagamento in ["pagato", "parziale"] else None,
                                note_fatturazione,
                            )
                            st.success("Dati fattura / pagamento aggiornati correttamente.")
                            st.rerun()
                    except Exception as e:
                        st.error(str(e))

        with tab_importi:
            with st.container(border=True):
                st.markdown("#### Dati economici prestazione")
                st.caption("Aggiorna l'importo della prestazione selezionata. L'opzione 'simili' agisce solo sulle righe senza importo.")
                col_c1, col_c2, col_c3, col_c4 = st.columns([1, 1, 1, 1.4])
                with col_c1:
                    nuovo_costo_unitario = st.number_input("Importo unitario", min_value=0.0,
                        value=float(riga.get("costo_unitario", 0) or 0), step=10.0, format="%.2f",
                        key=f"nuovo_costo_unitario_{id_intervento}")
                with col_c2:
                    frequenza_economica = st.number_input("Frequenza", min_value=1, value=1, step=1,
                        key=f"frequenza_economica_{id_intervento}")
                with col_c3:
                    importo_totale_calcolato = float(nuovo_costo_unitario) * int(frequenza_economica)
                    st.number_input("Importo totale", min_value=0.0, value=importo_totale_calcolato,
                        step=10.0, format="%.2f", disabled=True, key=f"importo_totale_calcolato_{id_intervento}")
                with col_c4:
                    applica_simili_zero = st.checkbox("Applica anche alle prestazioni simili senza importo",
                        value=False, key=f"applica_simili_zero_{id_intervento}")
                if st.button("Aggiorna importi", key=f"btn_aggiorna_importi_{id_intervento}"):
                    try:
                        totale = aggiorna_costi_intervento(id_intervento, nuovo_costo_unitario,
                            frequenza_economica, applica_simili_zero)
                        st.success(f"Importi aggiornati. Totale: € {totale:.2f}")
                        st.rerun()
                    except Exception as e:
                        st.error(str(e))

        with tab_elimina:
            with st.container(border=True):
                st.markdown("#### Elimina riga")
                st.warning("Elimina questo intervento programmato dal database. Se già eseguito, verrà eliminata anche la registrazione.")
                conferma = st.checkbox("Confermo eliminazione dell'intervento selezionato",
                    key=f"conferma_eliminazione_intervento_{id_intervento}")
                if st.button("Elimina intervento selezionato", type="secondary", key=f"btn_elimina_{id_intervento}"):
                    if not conferma:
                        st.error("Spuntare la casella di conferma prima di procedere.")
                    else:
                        try:
                            elimina_intervento_programmato(id_intervento)
                            st.success(f"Intervento ID {id_intervento} eliminato correttamente.")
                            st.rerun()
                        except Exception as e:
                            st.error(f"Errore eliminazione: {e}")


# =========================
# CARICAMENTO PRESTAZIONE MANUALE
# =========================

if pagina == "Caricamento prestazione manuale":
    st.subheader("Caricamento prestazione manuale")
    st.caption("Da usare per privati, interventi occasionali o prestazioni non collegate a un contratto PDF.")

    tariffe = carica_tariffe_prestazioni()
    usa_tariffa = False
    tariffa_selezionata = None

    if not tariffe.empty:
        usa_tariffa = st.checkbox("Usa tariffa standard esistente", value=False)
        if usa_tariffa:
            tariffe = tariffe.copy()
            tariffe["label"] = (tariffe["codice_prestazione"].astype(str) + " - "
                + tariffe["descrizione_tariffa"].astype(str) + " - € "
                + tariffe["costo_totale"].astype(str))
            tariffa_label = st.selectbox("Tariffa", options=tariffe["label"].tolist())
            tariffa_match = tariffe[tariffe["label"] == tariffa_label]
            if tariffa_match.empty:
                st.warning("Tariffa non più trovata. Seleziona di nuovo.")
                st.stop()
            tariffa_selezionata = tariffa_match.iloc[0]

    clienti_df = carica_clienti()
    col1, col2 = st.columns(2)

    with col1:
        lista_clienti = sorted(clienti_df["nome_cliente"].dropna().astype(str).unique().tolist()) if not clienti_df.empty else []
        nome_cliente = st.selectbox("Cliente", options=lista_clienti, index=None,
            placeholder="Scrivi o seleziona cliente...", accept_new_options=True,
            key="manuale_cliente_autocomplete")
        nome_cliente = nome_cliente or ""

        id_cliente_esistente = None
        if nome_cliente and not clienti_df.empty:
            cliente_match = clienti_df[clienti_df["nome_cliente"].astype(str).str.lower() == str(nome_cliente).lower()]
            if not cliente_match.empty:
                id_cliente_esistente = int(cliente_match.iloc[0]["id_cliente"])
            else:
                clienti_simili = trova_clienti_simili(nome_cliente, clienti_df)
                if clienti_simili:
                    st.warning("Possibili clienti già presenti: selezionane uno dalla tendina per evitare duplicati.")
                    st.write(", ".join(clienti_simili))

        if id_cliente_esistente is not None:
            st.success(f"Cliente esistente: {nome_cliente}")
            sedi_df = carica_sedi_cliente(id_cliente_esistente)
            if not sedi_df.empty:
                sedi_df = sedi_df.copy()
                sedi_df["label"] = (sedi_df["nome_sede"].fillna("").astype(str) + " - "
                    + sedi_df["indirizzo"].fillna("").astype(str)).str.strip(" -")
                lista_sedi = ["-- Nuova sede --"] + sedi_df["label"].tolist()
                sede_scelta = st.selectbox("Sede / luogo intervento", options=lista_sedi, key="manuale_sede_autocomplete")
                if sede_scelta != "-- Nuova sede --":
                    sede_match = sedi_df[sedi_df["label"] == sede_scelta]
                    if sede_match.empty:
                        st.warning("Sede non trovata. Seleziona di nuovo.")
                        st.stop()
                    sede_row = sede_match.iloc[0]
                    nome_sede    = str(sede_row["nome_sede"]  or "")
                    indirizzo_sede = str(sede_row["indirizzo"] or "")
                    st.text_input("Indirizzo", value=indirizzo_sede, disabled=True)
                else:
                    nome_sede      = st.text_input("Nuova sede / luogo intervento", key="manuale_nuova_sede_ce")
                    indirizzo_sede = st.text_input("Indirizzo", key="manuale_nuovo_indirizzo_ce")
            else:
                nome_sede      = st.text_input("Sede / luogo intervento", key="manuale_sede_ce_senza_sedi")
                indirizzo_sede = st.text_input("Indirizzo", key="manuale_indirizzo_ce_senza_sedi")
        else:
            nome_sede      = st.text_input("Sede / luogo intervento", key="manuale_sede_nuovo_cliente")
            indirizzo_sede = st.text_input("Indirizzo", key="manuale_indirizzo_nuovo_cliente")

    with col2:
        anno      = st.number_input("Anno", min_value=2025, max_value=2035, value=2026, step=1)
        mese_label = st.selectbox("Mese", options=[m for m in MESI_LABEL.keys() if m != "Tutti"])
        mese      = MESI_LABEL[mese_label]

        if usa_tariffa and tariffa_selezionata is not None:
            codice_prestazione = tariffa_selezionata["codice_prestazione"]
            descrizione_tariffa = tariffa_selezionata["descrizione_tariffa"]
            costo_unitario = float(tariffa_selezionata["costo_unitario"] or 0)
            costo_totale_tariffa = float(tariffa_selezionata["costo_totale"] or 0)
            frequenza_default = max(1, int(round(costo_totale_tariffa / costo_unitario))) if costo_unitario > 0 and costo_totale_tariffa > 0 else 1
            st.text_input("Prestazione", value=codice_prestazione, disabled=True)
            st.text_input("Descrizione tariffa", value=descrizione_tariffa, disabled=True)
            costo_unitario = st.number_input("Importo unitario", min_value=0.0, value=costo_unitario,
                step=10.0, format="%.2f", key="manuale_importo_unitario_tariffa")
            frequenza = st.number_input("Frequenza", min_value=1, value=frequenza_default, step=1, key="manuale_frequenza_tariffa")
            costo_totale = float(costo_unitario) * int(frequenza)
            st.number_input("Importo totale calcolato", min_value=0.0, value=costo_totale,
                step=10.0, format="%.2f", disabled=True, key="manuale_totale_tariffa")
            salva_tariffa = False
        else:
            prestazione_label = st.selectbox("Prestazione", options=[p for p in PRESTAZIONI_LABEL.keys() if p != "Tutte"])
            codice_prestazione = PRESTAZIONI_LABEL[prestazione_label]
            descrizione_tariffa = st.text_input("Descrizione tariffa / prestazione", value=prestazione_label)
            costo_unitario = st.number_input("Importo unitario", min_value=0.0, value=0.0,
                step=10.0, format="%.2f", key="manuale_importo_unitario_nuovo")
            frequenza = st.number_input("Frequenza", min_value=1, value=1, step=1, key="manuale_frequenza_nuova")
            costo_totale = float(costo_unitario) * int(frequenza)
            st.number_input("Importo totale calcolato", min_value=0.0, value=costo_totale,
                step=10.0, format="%.2f", disabled=True, key="manuale_totale_nuovo")
            salva_tariffa = st.checkbox("Salva/aggiorna come tariffa standard riutilizzabile", value=True)

    note = st.text_area("Note")

    if st.button("Salva prestazione manuale"):
        if not str(nome_cliente).strip():
            st.error("Inserire il cliente.")
        elif not str(nome_sede).strip():
            st.error("Inserire la sede / luogo intervento.")
        elif not str(descrizione_tariffa).strip():
            st.error("Inserire la descrizione tariffa / prestazione.")
        else:
            try:
                id_intervento = crea_intervento_manual(
                    nome_cliente=nome_cliente, nome_sede=nome_sede, indirizzo_sede=indirizzo_sede,
                    codice_prestazione=codice_prestazione, anno=int(anno), mese=int(mese),
                    costo_unitario=float(costo_unitario), costo_totale=float(costo_totale),
                    descrizione_tariffa=descrizione_tariffa, salva_tariffa=salva_tariffa, note=note,
                )
                st.success(f"Prestazione manuale inserita correttamente. ID intervento: {id_intervento}.")
                st.rerun()
            except Exception as e:
                st.error(str(e))


# =========================
# CARICAMENTO PRESTAZIONE DA CONTRATTO
# =========================

if pagina == "Caricamento prestazione da contratto":
    st.subheader("Caricamento prestazione da contratto")

    pdf_file = st.file_uploader("Carica PDF contratto", type=["pdf"])
    dati_pdf = {"cliente": "", "indirizzo_cliente": "", "sede_destinazione": "", "oggetto": "",
                "fatturazione": "", "pagamento": "", "durata_mesi": 12, "voci_costo": []}
    nome_documento = pdf_file.name if pdf_file else ""

    if pdf_file is not None:
        try:
            testo_pdf  = estrai_testo_pdf(pdf_file)
            dati_pdf   = estrai_dati_contratto(testo_pdf)
            if contratto_esiste(nome_documento):
                st.warning(f"Questo contratto è già presente: {nome_documento}")
            else:
                st.success("PDF letto correttamente. Controlla e correggi i dati prima del salvataggio.")
            with st.expander("Dati estratti dal PDF"):
                st.json(dati_pdf)
        except Exception as e:
            st.error(f"Errore lettura PDF: {e}")

    st.markdown("---")
    st.subheader("Dati cliente e sede")
    col_c1, col_c2 = st.columns(2)
    with col_c1:
        nome_cliente      = st.text_input("Cliente",          value=dati_pdf.get("cliente", ""))
        indirizzo_cliente = st.text_input("Indirizzo cliente", value=dati_pdf.get("indirizzo_cliente", ""))
    with col_c2:
        sede_destinazione = st.text_input("Sede di destinazione", value=dati_pdf.get("sede_destinazione", ""))
        indirizzo_sede    = st.text_input("Indirizzo sede",        value=dati_pdf.get("sede_destinazione", ""))

    st.markdown("---")
    st.subheader("Dati contratto")
    durata_default = int(dati_pdf.get("durata_mesi", 12) or 12)
    col_k1, col_k2 = st.columns(2)
    with col_k1:
        data_contratto = st.date_input("Data contratto", format="DD/MM/YYYY", key="data_contratto")
        data_inizio    = st.date_input("Data inizio",    format="DD/MM/YYYY", key="data_inizio_contratto")
    with col_k2:
        durata_mesi         = st.number_input("Durata mesi", min_value=1, max_value=120, value=durata_default, key="durata_mesi_contratto")
        data_fine_suggerita = aggiungi_mesi(data_inizio, durata_mesi)
        data_fine  = st.date_input("Data fine", value=data_fine_suggerita, format="DD/MM/YYYY", key="data_fine_contratto")
        stato      = st.selectbox("Stato contratto", options=["bozza","attivo","scaduto","annullato"], index=1, key="stato_contratto")

    modalita_fatturazione = st.text_input("Modalità fatturazione", value=dati_pdf.get("fatturazione", ""))
    modalita_pagamento    = st.text_input("Modalità pagamento",    value=dati_pdf.get("pagamento", "Bonifico Bancario"))
    note_contratto        = st.text_area("Note contratto",         value=dati_pdf.get("oggetto", ""))

    st.markdown("---")
    st.subheader("Voci costo / prestazioni del contratto")
    voci_default = dati_pdf.get("voci_costo", [])
    if not voci_default:
        voci_default = [{"codice_prestazione": dati_pdf.get("codice_prestazione", "MTI") or "MTI",
                         "descrizione_voce": "", "frequenza": "mensile", "quantita_annua": 12,
                         "costo_unitario": 0.0, "costo_totale": 0.0, "tipo_costo": "ricorrente", "note": ""}]
    df_voci = pd.DataFrame(voci_default)
    df_voci_editato = st.data_editor(
        df_voci, num_rows="dynamic", use_container_width=True,
        column_config={
            "codice_prestazione": st.column_config.SelectboxColumn("Prestazione", options=PRESTAZIONI_CODICI, required=True),
            "frequenza": st.column_config.SelectboxColumn("Frequenza",
                options=["mensile","bimestrale","trimestrale","semestrale","annuale","una tantum","altro"], required=True),
            "tipo_costo":     st.column_config.SelectboxColumn("Tipo costo", options=["ricorrente","una_tantum","opzionale"], required=True),
            "quantita_annua": st.column_config.NumberColumn("Quantità",     min_value=1, step=1),
            "costo_unitario": st.column_config.NumberColumn("Costo unitario", min_value=0, step=10, format="%.2f"),
            "costo_totale":   st.column_config.NumberColumn("Costo totale",   min_value=0, step=10, format="%.2f"),
        },
    )

    if st.button("Salva contratto"):
        if pdf_file is None:
            st.error("Caricare un PDF contratto.")
        elif not nome_cliente.strip():
            st.error("Inserire il cliente.")
        elif not sede_destinazione.strip():
            st.error("Inserire la sede di destinazione.")
        elif contratto_esiste(nome_documento):
            st.error(f"Contratto già presente: {nome_documento}")
        else:
            try:
                risultato = salva_contratto_completo(
                    nome_cliente=nome_cliente, sede_destinazione=sede_destinazione,
                    indirizzo_sede=indirizzo_sede, nome_documento=nome_documento,
                    data_contratto=data_contratto, data_inizio=data_inizio,
                    data_fine=data_fine, durata_mesi=durata_mesi, stato=stato,
                    modalita_fatturazione=modalita_fatturazione,
                    modalita_pagamento=modalita_pagamento,
                    pdf_file=pdf_file, note_contratto=note_contratto,
                    df_voci_editato=df_voci_editato,
                )
                st.success(
                    f"Contratto salvato. ID: {risultato['id_contratto']}. "
                    f"Voci salvate: {risultato['righe_salvate']}. "
                    f"Interventi generati: {risultato['interventi_generati']}."
                )
            except Exception as e:
                st.error(str(e))
