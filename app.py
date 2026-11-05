import io
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from urllib.parse import quote

import fitz
import pandas as pd
import requests
import streamlit as st
import tldextract

st.set_page_config(page_title='Cornuto Scadenza Domini', page_icon='🌐', layout='wide', initial_sidebar_state='collapsed')
st.markdown('''<style>
.block-container{padding-top:1.1rem;max-width:1050px}h1{letter-spacing:-.05em}div[data-testid="stMetric"]{background:#f2f6fc;border-radius:16px;padding:13px}div.stButton>button,div.stDownloadButton>button{border-radius:12px;min-height:45px} @media(max-width:650px){.block-container{padding-left:1rem;padding-right:1rem}h1{font-size:2rem!important}}
</style>''', unsafe_allow_html=True)
st.title('🌐 Cornuto Scadenza Domini')
st.caption('Trova domini in scadenza nei tuoi PDF · Ottimizzato per iPhone')

EXCLUDED = set('''facebook.com instagram.com google.com gmail.com youtube.com youtu.be tiktok.com twitter.com x.com linkedin.com whatsapp.com wa.me telegram.org t.me pinterest.com amazon.com amazon.it microsoft.com apple.com outlook.com hotmail.com yahoo.com icloud.com netflix.com wikipedia.org wordpress.com github.com dropbox.com google.it facebook.it instagram.it youtube.it tiktok.it googleusercontent.com googlesyndication.com gstatic.com cloudflare.com adobe.com paypal.com spotify.com bing.com live.com office.com office365.com microsoftonline.com'''.split())
DOMAIN_RE = re.compile(r'(?<![\w@])(?:[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?\.)+[a-zA-Z]{2,63}(?![\w-])')
extractor = tldextract.TLDExtract(suffix_list_urls=())


def pdf_domains(file_bytes):
    found = set()
    with fitz.open(stream=file_bytes, filetype='pdf') as doc:
        for page in doc:
            text = page.get_text('text', sort=True)
            # PDF a volte spezzano gli URL con newline.
            text = re.sub(r'(?<=\.)\s*\n\s*(?=[a-zA-Z])', '', text)
            for candidate in DOMAIN_RE.findall(text):
                ext = extractor(candidate.lower())
                if ext.domain and ext.suffix:
                    root = f'{ext.domain}.{ext.suffix}'
                    if root not in EXCLUDED:
                        found.add(root)
    return sorted(found)


def rdap_check(domain):
    result = {'Dominio': domain, 'Scadenza': '', 'Giorni': None, 'Stato': '', 'Registrar': '', 'Esito': ''}
    try:
        # rdap.org reindirizza al server RDAP competente per il TLD.
        resp = requests.get('https://rdap.org/domain/' + quote(domain), timeout=18,
                            headers={'Accept':'application/rdap+json','User-Agent':'DomainHunter/1.0'}, allow_redirects=True)
        if resp.status_code == 404:
            result['Esito'] = 'Non trovato nel RDAP (verificare disponibilità)'
            return result
        resp.raise_for_status()
        data = resp.json()
        states = data.get('status') or []
        result['Stato'] = ', '.join(str(s) for s in states)
        for ent in data.get('entities', []):
            if 'registrar' in ent.get('roles', []):
                vcard = ent.get('vcardArray', [None, []])
                names = [v[3] for v in vcard[1] if isinstance(v,list) and len(v)>3 and v[0] in ('fn','org')] if isinstance(vcard,list) and len(vcard)>1 else []
                result['Registrar'] = str(names[0]) if names else str(ent.get('handle',''))
                break
        events = data.get('events') or []
        expiry = next((e.get('eventDate') for e in events if str(e.get('eventAction','')).lower() in ('expiration','expiry') and e.get('eventDate')), None)
        if expiry:
            dt = datetime.fromisoformat(expiry.replace('Z','+00:00'))
            if dt.tzinfo is None: dt = dt.replace(tzinfo=timezone.utc)
            result['Scadenza'] = dt.strftime('%d/%m/%Y')
            result['Giorni'] = (dt.date() - datetime.now(timezone.utc).date()).days
            result['Esito'] = 'OK'
        else:
            result['Esito'] = 'Data di scadenza non pubblicata'
    except (requests.RequestException, ValueError, KeyError, TypeError) as exc:
        result['Esito'] = 'Errore RDAP / limite richieste'
    return result

@st.cache_data(ttl=3600, show_spinner=False)
def batch_check(domains):
    results = []
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = {pool.submit(rdap_check, d): d for d in domains}
        for future in as_completed(futures):
            results.append(future.result())
    return results

uploaded = st.file_uploader('📄 Carica uno o più PDF', type=['pdf'], accept_multiple_files=True)
col1,col2 = st.columns(2)
with col1:
    days = st.selectbox('Scadenza entro', [7,15,30,60,90,180,365], index=2, format_func=lambda n:f'{n} giorni')
with col2:
    include_pending = st.checkbox('Includi Pending / Redemption', value=True)
show_expired = st.checkbox('Includi anche i domini già scaduti', value=True)
max_domains = st.number_input('Numero massimo di domini da controllare per analisi', min_value=10, max_value=3000, value=250, step=50,
                              help='Per grandi elenchi i server RDAP potrebbero applicare limiti. Esegui analisi a gruppi.')

if uploaded:
    domains = set()
    for f in uploaded:
        try:
            domains.update(pdf_domains(f.getvalue()))
        except Exception:
            st.warning(f'Impossibile leggere {f.name}')
    domains = sorted(domains)
    st.info(f'🔎 {len(domains)} domini principali unici estratti (dopo le esclusioni).')
    if not domains:
        st.warning('Nessun dominio trovato. Se il PDF contiene solo scansioni, occorre una versione con testo selezionabile.')
    else:
        st.download_button('⬇️ Scarica elenco estratto', '\n'.join(domains), 'domini_estratti.txt', mime='text/plain')
        if len(domains)>max_domains:
            st.warning(f'Analizzerò solo i primi {max_domains} domini in ordine alfabetico. Aumenta il limite se necessario.')
        if st.button('🚀 Analizza scadenze e stati', type='primary', use_container_width=True):
            with st.spinner('Controllo RDAP in corso: con molti domini potrebbe richiedere diversi minuti…'):
                st.session_state['results'] = batch_check(tuple(domains[:max_domains]))
                st.session_state['signature'] = tuple(domains[:max_domains])

if st.session_state.get('results'):
    df = pd.DataFrame(st.session_state['results'])
    df['Giorni'] = pd.to_numeric(df['Giorni'], errors='coerce')
    pending = df['Stato'].str.contains('pendingdelete|redemptionperiod', case=False, na=False)
    in_range = df['Giorni'].between(0,days, inclusive='both')
    expired = df['Giorni'].lt(0) if show_expired else pd.Series(False,index=df.index)
    chosen = in_range | expired | (pending if include_pending else False)
    filtered = df.loc[chosen].copy()
    filtered['_sort'] = filtered['Giorni'].fillna(999999)
    filtered = filtered.sort_values(['_sort','Dominio']).drop(columns='_sort')
    a,b,c = st.columns(3)
    a.metric('Controllati',len(df))
    b.metric('Da monitorare',len(filtered))
    c.metric('Pending / Redemption',int(pending.sum()))
    st.subheader('📅 Domini prioritari')
    st.caption('Ordinati per data di scadenza. Giorni negativi = scadenza già trascorsa; non significa dominio libero.')
    st.dataframe(filtered, use_container_width=True, hide_index=True, column_config={'Giorni':st.column_config.NumberColumn('Giorni',format='%d')})
    st.download_button('⬇️ Scarica risultati filtrati (CSV)',filtered.to_csv(index=False).encode('utf-8-sig'), 'domain_hunter_prioritari.csv', 'text/csv',use_container_width=True)
    with st.expander('Visualizza tutti i domini controllati, inclusi errori e scadenze lontane'):
        st.dataframe(df.sort_values('Giorni',na_position='last'),use_container_width=True,hide_index=True)
        st.download_button('⬇️ Scarica tutti i risultati (CSV)',df.to_csv(index=False).encode('utf-8-sig'),'domain_hunter_tutti.csv','text/csv')

st.divider()
st.caption('ℹ️ La scadenza non equivale alla data di rilascio. RDAP può essere incompleto o limitato; verifica sempre le informazioni prima di fare un backorder. Nessun file PDF viene salvato permanentemente dall’app.')
