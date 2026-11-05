import io
import re
import threading
import time
from datetime import datetime, timezone
from urllib.parse import quote

import fitz
import pandas as pd
import requests
import streamlit as st
import tldextract

st.set_page_config(page_title='Cornuto Scadenza Domini', page_icon='🌐', layout='wide', initial_sidebar_state='collapsed')
st.markdown('''<style>.block-container{padding-top:1rem;max-width:1100px}div[data-testid="stMetric"]{background:#f2f6fc;border-radius:12px;padding:10px}div.stButton>button,div.stDownloadButton>button{min-height:46px;border-radius:10px}@media(max-width:650px){.block-container{padding-left:.8rem;padding-right:.8rem}}</style>''', unsafe_allow_html=True)
st.title('🌐 Cornuto Scadenza Domini')
st.caption('Analisi completa di PDF con migliaia di domini · versione iPhone')

EXCLUDED = set('''facebook.com instagram.com google.com gmail.com youtube.com youtu.be tiktok.com twitter.com x.com linkedin.com whatsapp.com wa.me telegram.org t.me pinterest.com amazon.com amazon.it microsoft.com apple.com outlook.com hotmail.com yahoo.com icloud.com netflix.com wikipedia.org wordpress.com github.com dropbox.com google.it facebook.it instagram.it youtube.it tiktok.it googleusercontent.com googlesyndication.com gstatic.com cloudflare.com adobe.com paypal.com spotify.com bing.com live.com office.com office365.com microsoftonline.com'''.split())
DOMAIN_RE = re.compile(r'(?<![\w@])(?:[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?\.)+[a-zA-Z]{2,63}(?![\w-])')
extractor = tldextract.TLDExtract(suffix_list_urls=())


def pdf_domains(data):
    found = set()
    with fitz.open(stream=data, filetype='pdf') as doc:
        for page in doc:
            content = page.get_text('text', sort=True)
            content = re.sub(r'(?<=\.)\s*\n\s*(?=[a-zA-Z])', '', content)
            for candidate in DOMAIN_RE.findall(content):
                ext = extractor(candidate.lower())
                if ext.domain and ext.suffix:
                    root = f'{ext.domain}.{ext.suffix}'
                    if root not in EXCLUDED:
                        found.add(root)
    return found


def rdap_check(domain):
    result = {'Dominio': domain, 'Scadenza': '', 'Giorni': None, 'Stato': '', 'Registrar': '', 'Esito': ''}
    url = 'https://rdap.org/domain/' + quote(domain)
    for attempt in range(3):
        try:
            response = requests.get(url, timeout=(8, 20), headers={'Accept': 'application/rdap+json', 'User-Agent': 'CornutoScadenzaDomini/2.0'}, allow_redirects=True)
            if response.status_code == 404:
                result['Esito'] = 'Non trovato in RDAP (non significa necessariamente libero)'
                return result
            if response.status_code in (429, 500, 502, 503, 504):
                if attempt < 2:
                    retry_after = response.headers.get('Retry-After', '')
                    try:
                        wait = min(30, max(2, float(retry_after)))
                    except ValueError:
                        wait = 3 * (attempt + 1)
                    time.sleep(wait)
                    continue
                result['Esito'] = f'Errore HTTP {response.status_code} / limite RDAP'
                return result
            response.raise_for_status()
            data = response.json()
            result['Stato'] = ', '.join(str(x) for x in data.get('status', []))
            for entity in data.get('entities', []):
                if 'registrar' not in entity.get('roles', []):
                    continue
                vcard = entity.get('vcardArray', [])
                if isinstance(vcard, list) and len(vcard) > 1:
                    for item in vcard[1]:
                        if isinstance(item, list) and len(item) > 3 and item[0] in ('fn', 'org'):
                            result['Registrar'] = str(item[3]); break
                if not result['Registrar']:
                    result['Registrar'] = str(entity.get('handle', ''))
                break
            for event in data.get('events', []):
                if str(event.get('eventAction', '')).lower() in ('expiration', 'expiry') and event.get('eventDate'):
                    date = datetime.fromisoformat(event['eventDate'].replace('Z', '+00:00'))
                    if date.tzinfo is None:
                        date = date.replace(tzinfo=timezone.utc)
                    result['Scadenza'] = date.strftime('%d/%m/%Y')
                    result['Giorni'] = (date.date() - datetime.now(timezone.utc).date()).days
                    break
            result['Esito'] = 'OK' if result['Scadenza'] else 'Data di scadenza non pubblicata'
            return result
        except (requests.RequestException, ValueError, TypeError, KeyError):
            if attempt < 2:
                time.sleep(2 * (attempt + 1))
            else:
                result['Esito'] = 'Errore RDAP / timeout (riprovare)'
    return result


def run_checks(job):
    # Una sola richiesta alla volta, con intervallo configurabile tra gli avvii.
    # Questo riduce il carico, ma NON garantisce che i server RDAP non applichino limiti.
    for domain in job['domains']:
        start = time.monotonic()
        try:
            row = rdap_check(domain)
        except Exception:
            row = {'Dominio': domain, 'Scadenza': '', 'Giorni': None, 'Stato': '', 'Registrar': '', 'Esito': 'Errore inatteso'}
        with job['lock']:
            job['results'].append(row)
            job['done'] += 1
        # Il tempo di attesa si applica anche in caso di errore.
        time.sleep(max(0, job['delay'] - (time.monotonic() - start)))
    with job['lock']:
        job['finished'] = True


files = st.file_uploader('📄 Carica uno o più PDF', type=['pdf'], accept_multiple_files=True)
col1, col2 = st.columns(2)
with col1:
    days = st.selectbox('Scadenza entro', [7, 15, 30, 60, 90, 180, 365], index=2, format_func=lambda x: f'{x} giorni')
with col2:
    include_pending = st.checkbox('Mostra Pending / Redemption', True)
include_expired = st.checkbox('Mostra anche i domini già scaduti', True)
delay = st.select_slider('⏱️ Intervallo minimo tra richieste RDAP', options=[1, 2, 3, 5, 10], value=3, format_func=lambda n: f'{n} secondi')
st.caption('Consigliato: 3–5 secondi. Per 3.000 domini, 3 secondi richiedono almeno 2 ore e 30 minuti, oltre ai tempi di risposta e ai retry.')

if files:
    all_domains = set()
    for file in files:
        try:
            all_domains.update(pdf_domains(file.getvalue()))
        except Exception:
            st.error(f'Non riesco a leggere {file.name}')
    domains = tuple(sorted(all_domains))
    st.success(f'📋 {len(domains):,} domini principali unici estratti. Nessun limite ai primi 10/250.'.replace(',', '.'))
    st.download_button('⬇️ Scarica elenco domini estratti', '\n'.join(domains), 'domini_estratti.txt', mime='text/plain', use_container_width=True)
    current = st.session_state.get('job')
    if not domains:
        st.warning('Nessun dominio trovato. I PDF scansionati senza testo richiedono OCR.')
    elif st.button('🚀 Controlla TUTTI i domini', type='primary', use_container_width=True, disabled=bool(current and not current['finished'])):
        job = {'domains': domains, 'results': [], 'done': 0, 'finished': False, 'lock': threading.Lock(), 'delay': delay}
        st.session_state['job'] = job
        threading.Thread(target=run_checks, args=(job,), daemon=True).start()
        st.rerun()

@st.fragment(run_every='3s')
def show_results():
    job = st.session_state.get('job')
    if not job:
        return
    with job['lock']:
        rows = list(job['results'])
        done = job['done']
        finished = job['finished']
        total = len(job['domains'])
    st.subheader('📊 Avanzamento')
    st.progress(done / total if total else 0, text=f'{done:,} / {total:,} domini controllati'.replace(',', '.'))
    if not finished:
        st.info(f'Analisi in corso · intervallo minimo {job["delay"]} s. Su iPhone tieni Safari aperto: Streamlit gratuito può interrompere i lavori lunghi. Scarica periodicamente i risultati parziali.')
    else:
        st.success('Analisi completata: tutti i domini sono stati tentati. Controlla gli eventuali errori RDAP.')
    if not rows:
        return
    df = pd.DataFrame(rows)
    df['Giorni'] = pd.to_numeric(df['Giorni'], errors='coerce')
    pending = df['Stato'].str.contains('pendingdelete|redemptionperiod', case=False, na=False)
    due = df['Giorni'].between(0, days, inclusive='both')
    expired = df['Giorni'].lt(0) if include_expired else pd.Series(False, index=df.index)
    selected = df.loc[due | expired | (pending if include_pending else False)].copy()
    selected = selected.sort_values(['Giorni', 'Dominio'], na_position='last')
    a, b, c = st.columns(3)
    a.metric('Controllati', done)
    b.metric('Prioritari', len(selected))
    c.metric('Pending', int(pending.sum()))
    st.subheader('⏰ Domini prioritari')
    st.caption('La scadenza non è la data di rilascio. I domini già scaduti possono essere ancora registrati.')
    st.dataframe(selected, use_container_width=True, hide_index=True)
    st.download_button('⬇️ Scarica prioritari (CSV)', selected.to_csv(index=False).encode('utf-8-sig'), 'cornuto_prioritari.csv', 'text/csv', use_container_width=True)
    all_sorted = df.sort_values(['Giorni', 'Dominio'], na_position='last')
    st.download_button('⬇️ Scarica TUTTI i risultati (CSV)', all_sorted.to_csv(index=False).encode('utf-8-sig'), 'cornuto_tutti.csv', 'text/csv', use_container_width=True)
    with st.expander('Mostra tutti i domini, compresi errori e scadenze lontane'):
        st.dataframe(all_sorted, use_container_width=True, hide_index=True)

show_results()
st.divider()
st.caption('⚠️ I server RDAP possono limitare le richieste. Un errore RDAP non equivale a dominio libero. Su Streamlit gratuito le analisi lunghe possono interrompersi se la sessione si disconnette.')
