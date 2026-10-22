from pathlib import Path
import os
import re
import tempfile
import uuid
from datetime import date

import streamlit as st
import pandas as pd
import psycopg2
import psycopg2.extras
import pdfplumber

# =========================
# CONFIG
# =========================

# La stringa di connessione viene letta dalla variabile d'ambiente DATABASE_URL.
# Su Streamlit Cloud si configura in Settings > Secrets.
DATABASE_URL = os.environ.get("DATABASE_URL", "")

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
    "Gennaio": 1, "Febbraio": 2, "Marzo": 3, "Aprile": 4,
    "Maggio": 5, "Giugno": 6, "Luglio": 7, "Agosto": 8,
    "Settembre": 9, "Ottobre": 10, "Novembre": 11, "Dicembre": 12,
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
    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = False
    return conn


# =========================
# DB — SCHEMA
# =========================

SCHEMA_STATEMENTS = [
    """CREATE TABLE IF NOT EXISTS prestazioni (
        codice_prestazione VARCHAR(10) PRIMARY KEY,
        nome_prestazione   VARCHAR(255) NOT NULL,
        descrizione        TEXT,
        attiva             INTEGER DEFAULT 1
    )""",
    """CREATE TABLE IF NOT EXISTS clienti (
        id_cliente     SERIAL PRIMARY KEY,
        nome_cliente   VARCHAR(255) NOT NULL,
        partita_iva    VARCHAR(50),
        codice_fiscale VARCHAR(50),
        email          VARCHAR(255),
        telefono       VARCHAR(50),
        note           TEXT,
        created_at     TIMESTAMP DEFAULT NOW()
    )""",
    """CREATE TABLE IF NOT EXISTS sedi (
        id_sede      SERIAL PRIMARY KEY,
        id_cliente   INTEGER NOT NULL REFERENCES clienti(id_cliente),
        nome_sede    VARCHAR(255),
        indirizzo    VARCHAR(255),
        comune       VARCHAR(100),
        note_accesso TEXT,
        attiva       INTEGER DEFAULT 1,
        created_at   TIMESTAMP DEFAULT NOW()
    )""",
    """CREATE TABLE IF NOT EXISTS contratti (
        id_contratto          SERIAL PRIMARY KEY,
        id_cliente            INTEGER NOT NULL REFERENCES clienti(id_cliente),
        id_sede               INTEGER NOT NULL REFERENCES sedi(id_sede),
        numero_contratto      VARCHAR(100) UNIQUE,
        data_contratto        DATE,
        data_inizio           DATE,
        data_fine             DATE,
        durata_mesi           INTEGER,
        stato                 VARCHAR(20) DEFAULT 'bozza',
        modalita_fatturazione VARCHAR(100),
        modalita_pagamento    VARCHAR(100),
        pdf_path              VARCHAR(500),
        pdf_hash              VARCHAR(64) UNIQUE,
        note                  TEXT,
        created_at            TIMESTAMP DEFAULT NOW()
    )""",
    """CREATE TABLE IF NOT EXISTS contratto_prestazioni (
        id_contratto_prestazione SERIAL PRIMARY KEY,
        id_contratto             INTEGER NOT NULL REFERENCES contratti(id_contratto),
        codice_prestazione       VARCHAR(10) REFERENCES prestazioni(codice_prestazione),
        descrizione_voce         VARCHAR(255),
        frequenza                VARCHAR(100),
        quantita_annua           INTEGER,
        costo_unitario           DECIMAL(10,2),
        costo_totale             DECIMAL(10,2),
        tipo_costo               VARCHAR(20) DEFAULT 'ricorrente',
        note                     TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS tariffe_prestazioni (
        id_tariffa           SERIAL PRIMARY KEY,
        codice_prestazione   VARCHAR(10) NOT NULL REFERENCES prestazioni(codice_prestazione),
        voce_pdf             VARCHAR(255),
        descrizione_tariffa  VARCHAR(255) NOT NULL,
        descrizione_standard VARCHAR(255),
        frequenza            VARCHAR(100),
        quantita_annua       INTEGER DEFAULT 1,
        costo_unitario       DECIMAL(10,2),
        costo_totale         DECIMAL(10,2),
        tipo_costo           VARCHAR(20) DEFAULT 'ricorrente',
        attiva               INTEGER DEFAULT 1,
        note                 TEXT,
        UNIQUE(codice_prestazione, descrizione_tariffa)
    )""",
    """CREATE TABLE IF NOT EXISTS interventi_programmati (
        id_intervento_programmato SERIAL PRIMARY KEY,
        id_contratto              INTEGER REFERENCES contratti(id_contratto),
        id_sede                   INTEGER NOT NULL REFERENCES sedi(id_sede),
        codice_prestazione        VARCHAR(10) NOT NULL REFERENCES prestazioni(codice_prestazione),
        anno                      INTEGER NOT NULL,
        mese                      INTEGER NOT NULL,
        stato                     VARCHAR(20) DEFAULT 'programmato',
        id_tariffa                INTEGER REFERENCES tariffe_prestazioni(id_tariffa),
        costo_unitario            DECIMAL(10,2) DEFAULT 0,
        costo_totale              DECIMAL(10,2) DEFAULT 0,
        stato_pagamento           VARCHAR(20) DEFAULT 'non_pagato',
        stato_fatturazione        VARCHAR(20) DEFAULT 'da_fatturare',
        numero_fattura            VARCHAR(50),
        data_pagamento            DATE,
        note_fatturazione         TEXT,
        note                      TEXT,
        created_at                TIMESTAMP DEFAULT NOW()
    )""",
    """CREATE UNIQUE INDEX IF NOT EXISTS uq_intervento_programmato
        ON interventi_programmati(id_sede, anno, mese, codice_prestazione)""",
    """CREATE TABLE IF NOT EXISTS interventi_effettuati (
        id_intervento_effettuato  SERIAL PRIMARY KEY,
        id_intervento_programmato INTEGER NOT NULL REFERENCES interventi_programmati(id_intervento_programmato),
        data_intervento           DATE NOT NULL,
        tecnico                   VARCHAR(255),
        esito                     VARCHAR(20) DEFAULT 'eseguito',
        note                      TEXT,
        report_path               VARCHAR(500),
        created_at                TIMESTAMP DEFAULT NOW()
    )""",
    """CREATE TABLE IF NOT EXISTS allegati (
        id_allegato              SERIAL PRIMARY KEY,
        tipo_allegato            VARCHAR(20),
        id_contratto             INTEGER REFERENCES contratti(id_contratto),
        id_intervento_effettuato INTEGER REFERENCES interventi_effettuati(id_intervento_effettuato),
        nome_file                VARCHAR(255) NOT NULL,
        percorso_file            VARCHAR(500) NOT NULL,
        data_caricamento         TIMESTAMP DEFAULT NOW()
    )""",
    # Seed prestazioni
    """INSERT INTO prestazioni(codice_prestazione, nome_prestazione) VALUES
        ('DR','Derattizzazione'),('DB','Deblattizzazione'),
        ('DS','Disinfestazione'),('MTI','Monitoraggio topi e insetti')
       ON CONFLICT DO NOTHING""",
]


def ensure_schema():
    conn = get_connection()
    cur = conn.cursor()
    try:
        for stmt in SCHEMA_STATEMENTS:
            cur.execute(stmt)
        conn.commit()
    finally:
        cur.close()
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
    for parola in ["Disinfezione","Disinfestazione","Derattizzazione",
                   "Allontanamento Volatili","Trattamento Antitarlo","Restauro arredi"]:
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
    return righe[0].strip(), " ".join(righe[1:]).strip()


def estrai_sede_da_pdf(testo):
    blocco = estrai_blocco(testo, r"Sede di destinazione:", r"Oggetto:")
    return " ".join(rimuovi_parole_servizi(blocco).split())


def estrai_oggetto_da_pdf(testo):
    match = re.search(
        r"Oggetto:\s*(.+?)(?:PREMESSA|Premessa|INDICE|Sede legale:|Documento elaborato da:|$)",
        testo, re.IGNORECASE | re.DOTALL,
    )
    return " ".join(match.group(1).split()) if match else ""


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
    return {12:"mensile",6:"bimestrale",4:"trimestrale",2:"semestrale",1:"annuale"}.get(int(quantita),"altro")


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
        descrizione = re.sub(r"^(Servizio|importo|q\.tà|Imponibile)\s+","",descrizione,flags=re.IGNORECASE).strip()
        quantita = int(match.group(3))
        risultati.append({
            "codice_prestazione": classifica_prestazione(descrizione) or classifica_prestazione(testo) or "MTI",
            "descrizione_voce": descrizione,
            "frequenza": frequenza_da_quantita_e_descrizione(quantita, descrizione),
            "quantita_annua": quantita,
            "costo_unitario": normalizza_importo(match.group(2)),
            "costo_totale": normalizza_importo(match.group(4)),
            "tipo_costo": tipo_costo_da_descrizione(descrizione),
            "note": "",
        })
    return risultati


def estrai_dati_contratto(testo):
    cliente, indirizzo_cliente = estrai_cliente_da_pdf(testo)
    fatturazione, pagamento, durata_mesi = "", "", 12
    m = re.search(r"FATTURAZIONE:\s*(.+?)(?:;|\n|•)", testo, re.IGNORECASE)
    if m: fatturazione = m.group(1).strip()
    m = re.search(r"PAGAMENTO:\s*(.+?)(?:\.|\n|•)", testo, re.IGNORECASE)
    if m: pagamento = m.group(1).strip()
    m = re.search(r"validità\s+di\s+n\.\s*(\d+)\s+mesi", testo, re.IGNORECASE)
    if m: durata_mesi = int(m.group(1))
    return {
        "cliente": cliente, "indirizzo_cliente": indirizzo_cliente,
        "sede_destinazione": estrai_sede_da_pdf(testo),
        "oggetto": estrai_oggetto_da_pdf(testo),
        "codice_prestazione": classifica_prestazione(testo),
        "fatturazione": fatturazione, "pagamento": pagamento,
        "durata_mesi": durata_mesi, "voci_costo": estrai_voci_costo(testo),
    }


# =========================
# QUERY — LETTURA
# =========================

def carica_interventi(anno=None, mese=None, prestazione=None, cliente=None):
    conn = get_connection()
    query = """
        SELECT
            ip.id_intervento_programmato AS id,
            ip.id_contratto AS id_contratto,
            c.nome_cliente AS cliente,
            s.nome_sede AS sede,
            s.indirizzo AS indirizzo,
            ip.anno AS anno,
            ip.mese AS mese_numero,
            CASE ip.mese
                WHEN 1 THEN 'Gennaio'  WHEN 2 THEN 'Febbraio' WHEN 3 THEN 'Marzo'
                WHEN 4 THEN 'Aprile'   WHEN 5 THEN 'Maggio'   WHEN 6 THEN 'Giugno'
                WHEN 7 THEN 'Luglio'   WHEN 8 THEN 'Agosto'   WHEN 9 THEN 'Settembre'
                WHEN 10 THEN 'Ottobre' WHEN 11 THEN 'Novembre' WHEN 12 THEN 'Dicembre'
            END AS mese,
            p.nome_prestazione AS prestazione,
            ip.codice_prestazione,
            ip.stato,
            ip.costo_unitario, ip.costo_totale,
            COALESCE(ip.stato_fatturazione,'da_fatturare') AS stato_fatturazione,
            COALESCE(ip.stato_pagamento,'non_pagato') AS stato_pagamento,
            ip.numero_fattura, ip.data_pagamento,
            ip.note_fatturazione, ip.note
        FROM interventi_programmati ip
        JOIN sedi s ON ip.id_sede = s.id_sede
        JOIN clienti c ON s.id_cliente = c.id_cliente
        JOIN prestazioni p ON ip.codice_prestazione = p.codice_prestazione
        WHERE 1=1
    """
    params = []
    if anno is not None:
        query += " AND ip.anno = %s"; params.append(anno)
    if mese is not None:
        query += " AND ip.mese = %s"; params.append(mese)
    if prestazione is not None:
        query += " AND ip.codice_prestazione = %s"; params.append(prestazione)
    if cliente is not None:
        query += " AND c.nome_cliente = %s"; params.append(cliente)
    query += " ORDER BY ip.anno, ip.mese, c.nome_cliente, s.nome_sede, p.nome_prestazione"
    try:
        return pd.read_sql(query, conn, params=params)
    finally:
        conn.close()


def carica_clienti():
    conn = get_connection()
    try:
        return pd.read_sql("SELECT id_cliente, nome_cliente FROM clienti ORDER BY nome_cliente", conn)
    finally:
        conn.close()


def carica_sedi_cliente(id_cliente):
    conn = get_connection()
    try:
        return pd.read_sql(
            "SELECT id_sede, nome_sede, indirizzo FROM sedi WHERE id_cliente = %s ORDER BY nome_sede, indirizzo",
            conn, params=[id_cliente])
    finally:
        conn.close()


def carica_tariffe_prestazioni():
    conn = get_connection()
    try:
        return pd.read_sql(
            """SELECT id_tariffa, codice_prestazione, descrizione_tariffa,
                      costo_unitario, costo_totale
               FROM tariffe_prestazioni WHERE attiva = 1
               ORDER BY codice_prestazione, descrizione_tariffa""", conn)
    finally:
        conn.close()


def contratto_esiste(numero_contratto):
    if not numero_contratto:
        return False
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute("SELECT id_contratto FROM contratti WHERE numero_contratto = %s LIMIT 1", (numero_contratto,))
        return cur.fetchone() is not None
    finally:
        cur.close(); conn.close()


# =========================
# OPERAZIONI DB — SCRITTURA
# =========================

def get_or_create_cliente_cur(cur, nome_cliente):
    cur.execute("SELECT id_cliente FROM clienti WHERE nome_cliente = %s", (nome_cliente,))
    row = cur.fetchone()
    if row: return row[0]
    cur.execute("INSERT INTO clienti (nome_cliente) VALUES (%s) RETURNING id_cliente", (nome_cliente,))
    return cur.fetchone()[0]


def get_or_create_sede_cur(cur, id_cliente, nome_sede, indirizzo):
    cur.execute(
        """SELECT id_sede FROM sedi
           WHERE id_cliente = %s
             AND COALESCE(nome_sede,'') = COALESCE(%s,'')
             AND COALESCE(indirizzo,'') = COALESCE(%s,'')""",
        (id_cliente, nome_sede, indirizzo))
    row = cur.fetchone()
    if row: return row[0]
    cur.execute(
        "INSERT INTO sedi (id_cliente, nome_sede, indirizzo) VALUES (%s,%s,%s) RETURNING id_sede",
        (id_cliente, nome_sede, indirizzo))
    return cur.fetchone()[0]


def get_or_create_tariffa_cur(cur, codice_prestazione, descrizione_tariffa, costo_unitario, costo_totale):
    cur.execute(
        "SELECT id_tariffa FROM tariffe_prestazioni WHERE codice_prestazione=%s AND descrizione_tariffa=%s LIMIT 1",
        (codice_prestazione, descrizione_tariffa))
    row = cur.fetchone()
    if row:
        cur.execute(
            "UPDATE tariffe_prestazioni SET costo_unitario=%s, costo_totale=%s, attiva=1 WHERE id_tariffa=%s",
            (costo_unitario, costo_totale, row[0]))
        return row[0]
    cur.execute(
        """INSERT INTO tariffe_prestazioni (codice_prestazione, descrizione_tariffa, costo_unitario, costo_totale, attiva)
           VALUES (%s,%s,%s,%s,1) RETURNING id_tariffa""",
        (codice_prestazione, descrizione_tariffa, costo_unitario, costo_totale))
    return cur.fetchone()[0]


def registra_intervento(id_intervento_programmato, data_intervento, tecnico, esito, note):
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute(
            "SELECT stato FROM interventi_programmati WHERE id_intervento_programmato = %s FOR UPDATE",
            (id_intervento_programmato,))
        row = cur.fetchone()
        if row is None: raise Exception("Intervento programmato non trovato.")
        if row[0] == "eseguito": raise Exception("Intervento già registrato come eseguito.")
        cur.execute(
            """INSERT INTO interventi_effettuati
               (id_intervento_programmato, data_intervento, tecnico, esito, note)
               VALUES (%s,%s,%s,%s,%s)""",
            (id_intervento_programmato, data_intervento, tecnico, esito, note))
        nuovo_stato = {"eseguito":"eseguito","parziale":"programmato","non_eseguito":"annullato"}.get(esito,"programmato")
        cur.execute(
            "UPDATE interventi_programmati SET stato=%s WHERE id_intervento_programmato=%s",
            (nuovo_stato, id_intervento_programmato))
        conn.commit()
    except Exception:
        conn.rollback(); raise
    finally:
        cur.close(); conn.close()


def elimina_intervento_programmato(id_intervento_programmato):
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute(
            "SELECT id_intervento_programmato FROM interventi_programmati WHERE id_intervento_programmato=%s",
            (id_intervento_programmato,))
        if cur.fetchone() is None:
            raise Exception(f"Intervento {id_intervento_programmato} non trovato.")
        cur.execute("DELETE FROM interventi_effettuati WHERE id_intervento_programmato=%s", (id_intervento_programmato,))
        cur.execute("DELETE FROM interventi_programmati WHERE id_intervento_programmato=%s", (id_intervento_programmato,))
        conn.commit()
    except Exception:
        conn.rollback(); raise
    finally:
        cur.close(); conn.close()


def crea_intervento_manual(nome_cliente, nome_sede, indirizzo_sede, codice_prestazione,
                           anno, mese, costo_unitario, costo_totale, descrizione_tariffa, salva_tariffa, note):
    conn = get_connection()
    cur = conn.cursor()
    try:
        id_cliente = get_or_create_cliente_cur(cur, nome_cliente.strip())
        id_sede = get_or_create_sede_cur(cur, id_cliente, nome_sede.strip(), indirizzo_sede.strip())
        id_tariffa = None
        if salva_tariffa:
            id_tariffa = get_or_create_tariffa_cur(cur, codice_prestazione, descrizione_tariffa.strip(),
                                                   float(costo_unitario), float(costo_totale))
        cur.execute(
            """INSERT INTO interventi_programmati
               (id_contratto, id_tariffa, id_sede, codice_prestazione, anno, mese,
                costo_unitario, costo_totale, stato, note)
               VALUES (NULL,%s,%s,%s,%s,%s,%s,%s,'programmato',%s)
               ON CONFLICT DO NOTHING RETURNING id_intervento_programmato""",
            (id_tariffa, id_sede, codice_prestazione, anno, mese,
             float(costo_unitario), float(costo_totale), note))
        row = cur.fetchone()
        if row is None:
            raise Exception(f"Esiste già un intervento {codice_prestazione} per questa sede nel mese selezionato.")
        lastrowid = row[0]
        conn.commit()
        return lastrowid
    except Exception:
        conn.rollback(); raise
    finally:
        cur.close(); conn.close()


def salva_pdf_con_nome_unico(pdf_file):
    """Su cloud i PDF vengono salvati in /tmp — non persistono ma il path viene registrato."""
    if pdf_file is None:
        return None
    nome_originale = Path(pdf_file.name).name.replace(" ", "_")
    nome_sicuro = re.sub(r"[^A-Za-z0-9_.-]", "_", nome_originale)
    nome_finale = f"{uuid.uuid4().hex[:8]}_{nome_sicuro}"
    pdf_path = Path("/tmp") / nome_finale
    with open(pdf_path, "wb") as f:
        f.write(pdf_file.getbuffer())
    return str(pdf_path)


def genera_interventi_da_contratto_cur(cur, id_contratto, id_sede, codice_prestazione,
                                       data_inizio, frequenza, quantita_annua, costo_unitario, costo_totale):
    mesi_step = {"mensile":1,"bimestrale":2,"trimestrale":3,"semestrale":6,"annuale":12}
    if frequenza not in mesi_step: return 0
    step = mesi_step[frequenza]
    mese_corrente, anno_corrente = data_inizio.month, data_inizio.year
    interventi_creati = 0
    for _ in range(int(quantita_annua or 1)):
        cur.execute(
            """SELECT id_intervento_programmato FROM interventi_programmati
               WHERE id_sede=%s AND codice_prestazione=%s AND anno=%s AND mese=%s LIMIT 1""",
            (id_sede, codice_prestazione, anno_corrente, mese_corrente))
        row = cur.fetchone()
        if row:
            cur.execute(
                "UPDATE interventi_programmati SET id_contratto=%s, costo_unitario=%s, costo_totale=%s WHERE id_intervento_programmato=%s",
                (id_contratto, costo_unitario, costo_totale, row[0]))
        else:
            cur.execute(
                """INSERT INTO interventi_programmati
                   (id_contratto, id_sede, codice_prestazione, anno, mese, costo_unitario, costo_totale, stato)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,'programmato')""",
                (id_contratto, id_sede, codice_prestazione, anno_corrente, mese_corrente, costo_unitario, costo_totale))
            interventi_creati += 1
        mese_corrente += step
        while mese_corrente > 12:
            mese_corrente -= 12; anno_corrente += 1
    return interventi_creati


def genera_intervento_una_tantum_cur(cur, id_contratto, id_sede, codice_prestazione,
                                     data_inizio, costo_unitario, costo_totale):
    cur.execute(
        """SELECT id_intervento_programmato FROM interventi_programmati
           WHERE id_sede=%s AND codice_prestazione=%s AND anno=%s AND mese=%s LIMIT 1""",
        (id_sede, codice_prestazione, data_inizio.year, data_inizio.month))
    row = cur.fetchone()
    if row:
        cur.execute(
            "UPDATE interventi_programmati SET id_contratto=%s, costo_unitario=%s, costo_totale=%s WHERE id_intervento_programmato=%s",
            (id_contratto, costo_unitario, costo_totale, row[0]))
        return 0
    cur.execute(
        """INSERT INTO interventi_programmati
           (id_contratto, id_sede, codice_prestazione, anno, mese, costo_unitario, costo_totale, stato)
           VALUES (%s,%s,%s,%s,%s,%s,%s,'programmato')""",
        (id_contratto, id_sede, codice_prestazione, data_inizio.year, data_inizio.month, costo_unitario, costo_totale))
    return 1


def salva_contratto_completo(nome_cliente, sede_destinazione, indirizzo_sede, nome_documento,
                             data_contratto, data_inizio, data_fine, durata_mesi, stato,
                             modalita_fatturazione, modalita_pagamento, pdf_file, note_contratto, df_voci_editato):
    if contratto_esiste(nome_documento):
        raise Exception(f"Contratto già presente: {nome_documento}")
    pdf_path = salva_pdf_con_nome_unico(pdf_file)
    conn = get_connection()
    cur = conn.cursor()
    try:
        id_cliente = get_or_create_cliente_cur(cur, nome_cliente.strip())
        id_sede = get_or_create_sede_cur(cur, id_cliente, sede_destinazione.strip(), indirizzo_sede.strip())
        cur.execute(
            """INSERT INTO contratti
               (id_cliente, id_sede, numero_contratto, data_contratto, data_inizio, data_fine,
                durata_mesi, stato, modalita_fatturazione, modalita_pagamento, pdf_path, note)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id_contratto""",
            (id_cliente, id_sede, nome_documento, data_contratto, data_inizio, data_fine,
             durata_mesi, stato, modalita_fatturazione, modalita_pagamento, pdf_path, note_contratto))
        id_contratto = cur.fetchone()[0]
        righe_salvate = interventi_generati = 0
        for _, riga in df_voci_editato.iterrows():
            descrizione = str(riga.get("descrizione_voce","")).strip()
            if not descrizione: continue
            codice_prestazione = str(riga.get("codice_prestazione","MTI")).strip()
            if codice_prestazione not in PRESTAZIONI_CODICI: codice_prestazione = "MTI"
            frequenza = str(riga.get("frequenza","")).strip()
            quantita_annua = int(riga.get("quantita_annua",1) or 1)
            costo_unitario = float(riga.get("costo_unitario",0.0) or 0.0)
            costo_totale = float(riga.get("costo_totale",0.0) or 0.0)
            tipo_costo = str(riga.get("tipo_costo","ricorrente")).strip()
            note_prest = str(riga.get("note","")).strip()
            cur.execute(
                """INSERT INTO contratto_prestazioni
                   (id_contratto, codice_prestazione, descrizione_voce, frequenza,
                    quantita_annua, costo_unitario, costo_totale, tipo_costo, note)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (id_contratto, codice_prestazione, descrizione, frequenza,
                 quantita_annua, costo_unitario, costo_totale, tipo_costo, note_prest))
            righe_salvate += 1
            if tipo_costo == "ricorrente":
                interventi_generati += genera_interventi_da_contratto_cur(
                    cur, id_contratto, id_sede, codice_prestazione,
                    data_inizio, frequenza, quantita_annua, costo_unitario, costo_totale)
            elif tipo_costo == "una_tantum":
                interventi_generati += genera_intervento_una_tantum_cur(
                    cur, id_contratto, id_sede, codice_prestazione,
                    data_inizio, costo_unitario, costo_totale)
        conn.commit()
        return {"id_contratto": id_contratto, "righe_salvate": righe_salvate, "interventi_generati": interventi_generati}
    except Exception:
        conn.rollback(); raise
    finally:
        cur.close(); conn.close()


def aggiorna_costi_intervento(id_intervento_programmato, costo_unitario, frequenza, applica_simili_zero=False):
    conn = get_connection()
    cur = conn.cursor()
    try:
        costo_unitario = float(costo_unitario)
        frequenza = int(frequenza)
        if frequenza < 1: raise Exception("La frequenza deve essere almeno 1.")
        costo_totale = costo_unitario * frequenza
        cur.execute(
            "SELECT id_sede, codice_prestazione, anno FROM interventi_programmati WHERE id_intervento_programmato=%s",
            (id_intervento_programmato,))
        row = cur.fetchone()
        if row is None: raise Exception("Intervento non trovato.")
        id_sede, codice_prestazione, anno = row
        cur.execute(
            "UPDATE interventi_programmati SET costo_unitario=%s, costo_totale=%s WHERE id_intervento_programmato=%s",
            (costo_unitario, costo_totale, id_intervento_programmato))
        if applica_simili_zero:
            cur.execute(
                """UPDATE interventi_programmati SET costo_unitario=%s, costo_totale=%s
                   WHERE id_sede=%s AND codice_prestazione=%s AND anno=%s
                     AND COALESCE(costo_unitario,0)=0 AND COALESCE(costo_totale,0)=0""",
                (costo_unitario, costo_totale, id_sede, codice_prestazione, anno))
        conn.commit()
        return costo_totale
    except Exception:
        conn.rollback(); raise
    finally:
        cur.close(); conn.close()


def aggiorna_fatturazione_intervento(id_intervento_programmato, stato_fatturazione, stato_pagamento,
                                     numero_fattura, data_pagamento, note_fatturazione):
    conn = get_connection()
    cur = conn.cursor()
    try:
        if data_pagamento is None or (isinstance(data_pagamento, float) and pd.isna(data_pagamento)):
            data_pagamento = None
        cur.execute(
            """UPDATE interventi_programmati
               SET stato_fatturazione=%s, stato_pagamento=%s, numero_fattura=%s,
                   data_pagamento=%s, note_fatturazione=%s
               WHERE id_intervento_programmato=%s""",
            (stato_fatturazione, stato_pagamento,
             str(numero_fattura or "").strip() or None,
             data_pagamento,
             str(note_fatturazione or "").strip() or None,
             id_intervento_programmato))
        conn.commit()
    except Exception:
        conn.rollback(); raise
    finally:
        cur.close(); conn.close()


def normalizza_nome_per_match(testo):
    testo = str(testo or "").lower().strip()
    testo = re.sub(r"\b(srl|s\.r\.l\.|spa|s\.p\.a\.|snc|sas|societa|soc|cooperativa|coop)\b"," ",testo)
    testo = re.sub(r"[^a-z0-9]+"," ",testo)
    return " ".join(testo.split())


def trova_clienti_simili(nome_cliente, clienti_df, limite=8):
    if not nome_cliente or clienti_df.empty: return []
    query = normalizza_nome_per_match(nome_cliente)
    if not query: return []
    risultati = []
    for nome in clienti_df["nome_cliente"].dropna().astype(str).unique():
        nome_norm = normalizza_nome_per_match(nome)
        if query in nome_norm or nome_norm in query:
            risultati.append(nome)
    return sorted(risultati)[:limite]


# =========================
# UI
# =========================

st.set_page_config(page_title="Gestionale CDN", page_icon="🌿", layout="wide", initial_sidebar_state="expanded")

st.markdown("""
<style>
.main .block-container{padding-top:1.4rem;padding-bottom:2rem;max-width:1500px}
h1,h2,h3{letter-spacing:-0.02em}
.cdn-hero{padding:1.25rem 1.4rem;border-radius:18px;background:linear-gradient(135deg,#eef8f2 0%,#f7fbff 100%);border:1px solid #dfeee5;margin-bottom:1rem}
.cdn-hero-title{font-size:1.55rem;font-weight:750;margin-bottom:.25rem;color:#1f3b2d}
.cdn-hero-subtitle{color:#52635a;font-size:.98rem}
.cdn-card{padding:1rem 1.1rem;border-radius:16px;border:1px solid #e6e8eb;background:#ffffff;box-shadow:0 2px 10px rgba(20,36,28,.04);margin-bottom:.75rem}
.cdn-muted{color:#68746d;font-size:.92rem}
.cdn-pill{display:inline-block;padding:.28rem .62rem;border-radius:999px;background:#eef5f1;border:1px solid #dce9e1;color:#234331;font-size:.82rem;font-weight:650;margin-right:.35rem;margin-bottom:.35rem}
div[data-testid="stMetric"]{background:#ffffff;border:1px solid #e6e8eb;padding:.8rem 1rem;border-radius:14px;box-shadow:0 1px 8px rgba(20,36,28,.035)}
section[data-testid="stSidebar"]{background:#f7faf8}
.stButton>button{border-radius:10px;font-weight:650}
</style>""", unsafe_allow_html=True)

st.markdown("""
<div class="cdn-hero">
    <div class="cdn-hero-title">Gestionale CDN</div>
    <div class="cdn-hero-subtitle">Consultazione interventi, caricamento prestazioni e aggiornamento operativo guidato.</div>
</div>""", unsafe_allow_html=True)

if not DATABASE_URL:
    st.error("DATABASE_URL non configurata. Vai su Settings > Secrets in Streamlit Cloud e aggiungi la stringa di connessione.")
    st.stop()

try:
    ensure_schema()
except Exception as e:
    st.error(f"Errore connessione database: {e}")
    st.stop()

pagina = st.sidebar.radio("Menu", [
    "Dashboard consultazione",
    "Caricamento prestazione manuale",
    "Caricamento prestazione da contratto",
])


# =========================
# DASHBOARD CONSULTAZIONE
# =========================

if pagina == "Dashboard consultazione":
    st.subheader("Dashboard consultazione")
    st.caption("Consulta gli interventi e aggiorna in modo guidato stato operativo, fatturazione e pagamento.")

    clienti_df_filtri = carica_clienti()
    clienti_opzioni = ["Tutti"] + (sorted(clienti_df_filtri["nome_cliente"].dropna().astype(str).unique().tolist()) if not clienti_df_filtri.empty else [])

    with st.container(border=True):
        st.markdown("#### Filtri ricerca")
        col1, col2, col3, col4 = st.columns([1,1,1.2,1.8])
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
        df_vis = df.drop(columns=["mese_numero","codice_prestazione"], errors="ignore").reset_index(drop=True)
        with st.container(border=True):
            st.markdown("#### Tabella consultazione")
            st.dataframe(df_vis, use_container_width=True, height=390, hide_index=True,
                column_config={
                    "id": None, "id_contratto": None,
                    "costo_unitario": st.column_config.NumberColumn("Costo unitario", format="€ %.2f"),
                    "costo_totale":   st.column_config.NumberColumn("Costo totale",   format="€ %.2f"),
                    "data_pagamento": st.column_config.DateColumn("Data pagamento", format="DD/MM/YYYY"),
                })

        st.markdown("---")
        st.subheader("Gestione intervento selezionato")

        with st.container(border=True):
            st.markdown("#### Cerca intervento")
            clienti_gestione = sorted(df_vis["cliente"].dropna().astype(str).unique().tolist())
            col_s1, col_s2 = st.columns([1.4,1])
            with col_s1:
                cliente_gestione = st.selectbox("Cliente", options=clienti_gestione, index=0, key="gestione_cliente")
            df_cli = df_vis[df_vis["cliente"].astype(str)==str(cliente_gestione)].copy()
            prest_gestione = sorted(df_cli["prestazione"].dropna().astype(str).unique().tolist())
            with col_s2:
                prest_gestione_sel = st.selectbox("Prestazione", options=prest_gestione, index=0, key="gestione_prestazione")
            df_filt = df_cli[df_cli["prestazione"].astype(str)==str(prest_gestione_sel)].copy()

            opzioni, mappa = [], {}
            for _, r in df_filt.iterrows():
                sede_l = str(r.get("sede") or "Sede non specificata")
                ind_l  = str(r.get("indirizzo") or "").strip()
                label  = f"{sede_l}{' - '+ind_l if ind_l else ''} | {r['mese']} {r['anno']} | Stato: {r['stato']} | ID {r['id']}"
                opzioni.append(label); mappa[label] = r
            intervento_label = st.selectbox("Sede / intervento", options=opzioni, key="select_intervento")

        riga = mappa[intervento_label]
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

        tab_op, tab_fat, tab_imp, tab_del = st.tabs(["1. Stato operativo","2. Fattura / pagamento","3. Importi","4. Elimina"])

        with tab_op:
            with st.container(border=True):
                st.markdown("#### Stato operativo")
                c1,c2,c3 = st.columns([1,1,1.4])
                with c1: data_int = st.date_input("Data intervento", format="DD/MM/YYYY", key=f"data_{id_intervento}")
                with c2: esito = st.selectbox("Esito", ["eseguito","parziale","non_eseguito"], key=f"esito_{id_intervento}")
                with c3: tecnico = st.text_input("Tecnico", key=f"tecnico_{id_intervento}")
                note_op = st.text_area("Note intervento", key=f"note_op_{id_intervento}")
                if st.button("Aggiorna stato operativo", key=f"btn_op_{id_intervento}", type="primary"):
                    try:
                        registra_intervento(id_intervento, data_int, tecnico, esito, note_op)
                        st.success(f"Stato aggiornato a '{esito}'.")
                        st.rerun()
                    except Exception as e:
                        st.error(str(e))

        with tab_fat:
            with st.container(border=True):
                st.markdown("#### Fattura / pagamento")
                sf_corr = str(riga.get("stato_fatturazione") or "da_fatturare")
                sp_corr = str(riga.get("stato_pagamento")    or "non_pagato")
                c1,c2 = st.columns(2)
                with c1:
                    opts_f = ["da_fatturare","fatturato","non_fatturare"]
                    sf = st.selectbox("Stato fatturazione", opts_f,
                        index=opts_f.index(sf_corr) if sf_corr in opts_f else 0, key=f"sf_{id_intervento}")
                    nf = st.text_input("Numero fattura",
                        value="" if pd.isna(riga.get("numero_fattura")) else str(riga.get("numero_fattura") or ""),
                        disabled=sf!="fatturato", key=f"nf_{id_intervento}")
                with c2:
                    opts_p = ["non_pagato","pagato","parziale","non_dovuto"]
                    sp = st.selectbox("Stato pagamento", opts_p,
                        index=opts_p.index(sp_corr) if sp_corr in opts_p else 0, key=f"sp_{id_intervento}")
                    dp = None
                    if sp in ["pagato","parziale"]:
                        dp_corr = riga.get("data_pagamento")
                        if pd.isna(dp_corr) or dp_corr is None: dp_corr = date.today()
                        dp = st.date_input("Data pagamento", value=dp_corr, format="DD/MM/YYYY", key=f"dp_{id_intervento}")
                    else:
                        st.info("Data pagamento vuota finché non pagato.")
                note_f = st.text_area("Note fatturazione",
                    value="" if pd.isna(riga.get("note_fatturazione")) else str(riga.get("note_fatturazione") or ""),
                    key=f"note_f_{id_intervento}")
                if st.button("Salva fattura / pagamento", key=f"btn_fat_{id_intervento}", type="primary"):
                    try:
                        if sf=="fatturato" and not str(nf or "").strip():
                            st.error("Inserire il numero fattura.")
                        elif sp in ["pagato","parziale"] and dp is None:
                            st.error("Inserire la data pagamento.")
                        else:
                            aggiorna_fatturazione_intervento(id_intervento, sf, sp, nf,
                                dp if sp in ["pagato","parziale"] else None, note_f)
                            st.success("Dati salvati.")
                            st.rerun()
                    except Exception as e:
                        st.error(str(e))

        with tab_imp:
            with st.container(border=True):
                st.markdown("#### Importi")
                c1,c2,c3,c4 = st.columns([1,1,1,1.4])
                with c1: cu = st.number_input("Importo unitario", min_value=0.0,
                    value=float(riga.get("costo_unitario",0) or 0), step=10.0, format="%.2f", key=f"cu_{id_intervento}")
                with c2: freq = st.number_input("Frequenza", min_value=1, value=1, step=1, key=f"freq_{id_intervento}")
                with c3: st.number_input("Importo totale", value=float(cu)*int(freq), disabled=True, format="%.2f", key=f"tot_{id_intervento}")
                with c4: simili = st.checkbox("Applica anche alle prestazioni simili senza importo", key=f"simili_{id_intervento}")
                if st.button("Aggiorna importi", key=f"btn_imp_{id_intervento}"):
                    try:
                        tot = aggiorna_costi_intervento(id_intervento, cu, freq, simili)
                        st.success(f"Importi aggiornati. Totale: € {tot:.2f}")
                        st.rerun()
                    except Exception as e:
                        st.error(str(e))

        with tab_del:
            with st.container(border=True):
                st.markdown("#### Elimina riga")
                st.warning("Elimina definitivamente questo intervento. L'operazione non è reversibile.")
                conf = st.checkbox("Confermo eliminazione", key=f"conf_{id_intervento}")
                if st.button("Elimina intervento selezionato", type="secondary", key=f"btn_del_{id_intervento}"):
                    if not conf:
                        st.error("Spuntare la casella di conferma.")
                    else:
                        try:
                            elimina_intervento_programmato(id_intervento)
                            st.success(f"Intervento {id_intervento} eliminato.")
                            st.rerun()
                        except Exception as e:
                            st.error(str(e))


# =========================
# CARICAMENTO MANUALE
# =========================

if pagina == "Caricamento prestazione manuale":
    st.subheader("Caricamento prestazione manuale")
    st.caption("Per privati, interventi occasionali o prestazioni non collegate a un contratto PDF.")

    tariffe = carica_tariffe_prestazioni()
    usa_tariffa = False
    tariffa_selezionata = None
    if not tariffe.empty:
        usa_tariffa = st.checkbox("Usa tariffa standard esistente", value=False)
        if usa_tariffa:
            tariffe["label"] = (tariffe["codice_prestazione"].astype(str) + " - "
                + tariffe["descrizione_tariffa"].astype(str) + " - € " + tariffe["costo_totale"].astype(str))
            tl = st.selectbox("Tariffa", options=tariffe["label"].tolist())
            tm = tariffe[tariffe["label"]==tl]
            if tm.empty: st.warning("Tariffa non trovata."); st.stop()
            tariffa_selezionata = tm.iloc[0]

    clienti_df = carica_clienti()
    col1, col2 = st.columns(2)

    with col1:
        lista_c = sorted(clienti_df["nome_cliente"].dropna().astype(str).unique().tolist()) if not clienti_df.empty else []
        nome_cliente = st.selectbox("Cliente", options=lista_c, index=None,
            placeholder="Scrivi o seleziona cliente...", accept_new_options=True, key="man_cliente") or ""

        id_cliente_esistente = None
        if nome_cliente and not clienti_df.empty:
            cm = clienti_df[clienti_df["nome_cliente"].astype(str).str.lower()==nome_cliente.lower()]
            if not cm.empty:
                id_cliente_esistente = int(cm.iloc[0]["id_cliente"])
            else:
                simili = trova_clienti_simili(nome_cliente, clienti_df)
                if simili:
                    st.warning("Possibili clienti già presenti: " + ", ".join(simili))

        if id_cliente_esistente is not None:
            st.success(f"Cliente esistente: {nome_cliente}")
            sedi_df = carica_sedi_cliente(id_cliente_esistente)
            if not sedi_df.empty:
                sedi_df["label"] = (sedi_df["nome_sede"].fillna("") + " - " + sedi_df["indirizzo"].fillna("")).str.strip(" -")
                lista_s = ["-- Nuova sede --"] + sedi_df["label"].tolist()
                ss = st.selectbox("Sede", options=lista_s, key="man_sede")
                if ss != "-- Nuova sede --":
                    sr = sedi_df[sedi_df["label"]==ss]
                    if sr.empty: st.warning("Sede non trovata."); st.stop()
                    nome_sede = str(sr.iloc[0]["nome_sede"] or "")
                    indirizzo_sede = str(sr.iloc[0]["indirizzo"] or "")
                    st.text_input("Indirizzo", value=indirizzo_sede, disabled=True)
                else:
                    nome_sede = st.text_input("Nuova sede", key="man_nuova_sede")
                    indirizzo_sede = st.text_input("Indirizzo", key="man_nuovo_ind")
            else:
                nome_sede = st.text_input("Sede", key="man_sede_ns")
                indirizzo_sede = st.text_input("Indirizzo", key="man_ind_ns")
        else:
            nome_sede = st.text_input("Sede", key="man_sede_nc")
            indirizzo_sede = st.text_input("Indirizzo", key="man_ind_nc")

    with col2:
        anno = st.number_input("Anno", min_value=2025, max_value=2035, value=2026, step=1)
        mese_l = st.selectbox("Mese", options=[m for m in MESI_LABEL if m!="Tutti"])
        mese = MESI_LABEL[mese_l]

        if usa_tariffa and tariffa_selezionata is not None:
            cp = tariffa_selezionata["codice_prestazione"]
            dt = tariffa_selezionata["descrizione_tariffa"]
            cu = float(tariffa_selezionata["costo_unitario"] or 0)
            ct_tar = float(tariffa_selezionata["costo_totale"] or 0)
            freq_def = max(1, int(round(ct_tar/cu))) if cu>0 and ct_tar>0 else 1
            st.text_input("Prestazione", value=cp, disabled=True)
            st.text_input("Descrizione tariffa", value=dt, disabled=True)
            cu = st.number_input("Importo unitario", min_value=0.0, value=cu, step=10.0, format="%.2f", key="man_cu_t")
            freq = st.number_input("Frequenza", min_value=1, value=freq_def, step=1, key="man_freq_t")
            ct = float(cu)*int(freq)
            st.number_input("Importo totale", value=ct, disabled=True, format="%.2f", key="man_tot_t")
            codice_prestazione, descrizione_tariffa, costo_unitario, costo_totale, salva_tariffa = cp, dt, cu, ct, False
        else:
            pl = st.selectbox("Prestazione", options=[p for p in PRESTAZIONI_LABEL if p!="Tutte"])
            codice_prestazione = PRESTAZIONI_LABEL[pl]
            descrizione_tariffa = st.text_input("Descrizione tariffa", value=pl)
            costo_unitario = st.number_input("Importo unitario", min_value=0.0, value=0.0, step=10.0, format="%.2f", key="man_cu_n")
            freq = st.number_input("Frequenza", min_value=1, value=1, step=1, key="man_freq_n")
            costo_totale = float(costo_unitario)*int(freq)
            st.number_input("Importo totale", value=costo_totale, disabled=True, format="%.2f", key="man_tot_n")
            salva_tariffa = st.checkbox("Salva come tariffa standard riutilizzabile", value=True)

    note = st.text_area("Note")
    if st.button("Salva prestazione manuale"):
        if not str(nome_cliente).strip(): st.error("Inserire il cliente.")
        elif not str(nome_sede).strip(): st.error("Inserire la sede.")
        elif not str(descrizione_tariffa).strip(): st.error("Inserire la descrizione.")
        else:
            try:
                id_int = crea_intervento_manual(nome_cliente, nome_sede, indirizzo_sede,
                    codice_prestazione, int(anno), int(mese), float(costo_unitario),
                    float(costo_totale), descrizione_tariffa, salva_tariffa, note)
                st.success(f"Prestazione inserita. ID intervento: {id_int}.")
                st.rerun()
            except Exception as e:
                st.error(str(e))


# =========================
# CARICAMENTO DA CONTRATTO
# =========================

if pagina == "Caricamento prestazione da contratto":
    st.subheader("Caricamento prestazione da contratto")

    pdf_file = st.file_uploader("Carica PDF contratto", type=["pdf"])
    dati_pdf = {"cliente":"","indirizzo_cliente":"","sede_destinazione":"","oggetto":"",
                "fatturazione":"","pagamento":"","durata_mesi":12,"voci_costo":[]}
    nome_documento = pdf_file.name if pdf_file else ""

    if pdf_file is not None:
        try:
            testo_pdf = estrai_testo_pdf(pdf_file)
            dati_pdf  = estrai_dati_contratto(testo_pdf)
            if contratto_esiste(nome_documento):
                st.warning(f"Contratto già presente: {nome_documento}")
            else:
                st.success("PDF letto correttamente. Verifica i dati prima di salvare.")
            with st.expander("Dati estratti dal PDF"):
                st.json(dati_pdf)
        except Exception as e:
            st.error(f"Errore lettura PDF: {e}")

    st.markdown("---")
    st.subheader("Dati cliente e sede")
    c1,c2 = st.columns(2)
    with c1:
        nome_cliente      = st.text_input("Cliente",           value=dati_pdf.get("cliente",""))
        indirizzo_cliente = st.text_input("Indirizzo cliente",  value=dati_pdf.get("indirizzo_cliente",""))
    with c2:
        sede_destinazione = st.text_input("Sede di destinazione", value=dati_pdf.get("sede_destinazione",""))
        indirizzo_sede    = st.text_input("Indirizzo sede",        value=dati_pdf.get("sede_destinazione",""))

    st.markdown("---")
    st.subheader("Dati contratto")
    durata_default = int(dati_pdf.get("durata_mesi",12) or 12)
    c1,c2 = st.columns(2)
    with c1:
        data_contratto = st.date_input("Data contratto", format="DD/MM/YYYY", key="dc")
        data_inizio    = st.date_input("Data inizio",    format="DD/MM/YYYY", key="di")
    with c2:
        durata_mesi = st.number_input("Durata mesi", min_value=1, max_value=120, value=durata_default, key="dm")
        data_fine   = st.date_input("Data fine", value=aggiungi_mesi(data_inizio, durata_mesi), format="DD/MM/YYYY", key="df")
        stato       = st.selectbox("Stato contratto", ["bozza","attivo","scaduto","annullato"], index=1, key="sc")
    modalita_fatturazione = st.text_input("Modalità fatturazione", value=dati_pdf.get("fatturazione",""))
    modalita_pagamento    = st.text_input("Modalità pagamento",    value=dati_pdf.get("pagamento","Bonifico Bancario"))
    note_contratto        = st.text_area("Note contratto",         value=dati_pdf.get("oggetto",""))

    st.markdown("---")
    st.subheader("Voci costo / prestazioni")
    voci_default = dati_pdf.get("voci_costo",[]) or [
        {"codice_prestazione": dati_pdf.get("codice_prestazione","MTI") or "MTI",
         "descrizione_voce":"","frequenza":"mensile","quantita_annua":12,
         "costo_unitario":0.0,"costo_totale":0.0,"tipo_costo":"ricorrente","note":""}]
    df_voci_editato = st.data_editor(pd.DataFrame(voci_default), num_rows="dynamic", use_container_width=True,
        column_config={
            "codice_prestazione": st.column_config.SelectboxColumn("Prestazione", options=PRESTAZIONI_CODICI, required=True),
            "frequenza": st.column_config.SelectboxColumn("Frequenza",
                options=["mensile","bimestrale","trimestrale","semestrale","annuale","una tantum","altro"], required=True),
            "tipo_costo":     st.column_config.SelectboxColumn("Tipo costo", options=["ricorrente","una_tantum","opzionale"], required=True),
            "quantita_annua": st.column_config.NumberColumn("Quantità", min_value=1, step=1),
            "costo_unitario": st.column_config.NumberColumn("Costo unitario", min_value=0, step=10, format="%.2f"),
            "costo_totale":   st.column_config.NumberColumn("Costo totale",   min_value=0, step=10, format="%.2f"),
        })

    if st.button("Salva contratto"):
        if pdf_file is None: st.error("Caricare un PDF.")
        elif not nome_cliente.strip(): st.error("Inserire il cliente.")
        elif not sede_destinazione.strip(): st.error("Inserire la sede di destinazione.")
        elif contratto_esiste(nome_documento): st.error(f"Contratto già presente: {nome_documento}")
        else:
            try:
                res = salva_contratto_completo(nome_cliente, sede_destinazione, indirizzo_sede,
                    nome_documento, data_contratto, data_inizio, data_fine, durata_mesi, stato,
                    modalita_fatturazione, modalita_pagamento, pdf_file, note_contratto, df_voci_editato)
                st.success(f"Contratto salvato. ID: {res['id_contratto']}. "
                           f"Voci: {res['righe_salvate']}. Interventi: {res['interventi_generati']}.")
            except Exception as e:
                st.error(str(e))
