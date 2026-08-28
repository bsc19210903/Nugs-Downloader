#!/usr/bin/env python3
from __future__ import annotations

import argparse, hashlib, html, json, re, sys, time, urllib.parse
from collections import deque
from pathlib import Path
from typing import Any
import requests
from bs4 import BeautifulSoup

UA='GlobalFisheriesPriorityDiscovery/2026.08 (+https://github.com/)'
URL_RE=re.compile(r'https?://[^\s\"\'<>\\]+')
ITEM_RE=re.compile(r'(?<![0-9a-fA-F])[0-9a-fA-F]{32}(?![0-9a-fA-F])')
SERVICE_RE=re.compile(r'/(?:MapServer|FeatureServer)(?:/\d+)?(?:$|[/?#])',re.I)
VECTOR_RE=re.compile(r'(?:MapServer|FeatureServer|geoserver|ows\?|wfs\?|\.geojson(?:$|\?)|\.json(?:$|\?)|\.zip(?:$|\?)|\.shp(?:$|\?)|\.gpkg(?:$|\?)|\.kml(?:$|\?)|\.kmz(?:$|\?))',re.I)

TARGETS=[
 ('japan_api_list','https://portal.msil.go.jp/apis'),
 ('japan_api_items','https://portal.msil.go.jp/msil-api-list'),
 ('japan_home','https://portal.msil.go.jp/'),
 ('korea_msp_plan','https://www.msp.go.kr/manageplan/plan.do'),
 ('korea_msp_map','https://www.msp.go.kr/map.do'),
 ('korea_meis','https://meis.go.kr/portal/main.do'),
 ('malaysia_mydofgis','https://mydofgis.mysa.gov.my/'),
 ('malaysia_hydro_item','https://geohub.hydro.gov.my/portal/home/item.html?id=7a66e1af30ff4682a4392e2777aba47f'),
 ('malaysia_dof','https://www.dof.gov.my/en/'),
 ('hongkong_csdi','https://portal.csdi.gov.hk/csdi-webpage/dataset/afcd_rcd_1635130855075_76661'),
 ('thailand_dof','https://gisportal.fisheries.go.th/portal/apps/webappviewer/index.html?id=d2308e424dbf4b9ca6fe4758e395c31d'),
 ('gfcm_fra','https://experience.arcgis.com/experience/232ba2e7562a49888e457022ad0edc8a'),
 ('gfcm_closures','https://experience.arcgis.com/experience/c03b310793974b93969f8dace5d8859c'),
 ('india_pfz','https://incois.gov.in/MarineFisheries/PfzAdvisory'),
 ('indonesia_kkp','https://sidako.kkp.go.id/'),
 ('philippines_bfar','https://www.bfar.da.gov.ph/'),
 ('taiwan_fisheries','https://www.fa.gov.tw/'),
 ('canada_dfo_atlas','https://www.dfo-mpo.gc.ca/oceans/planning-planification/atlas/index-eng.html'),
 ('noaa_fisheries_gis','https://www.fisheries.noaa.gov/resources/maps-and-geographic-information-systems-data'),
]

PORTALS=[
 ('arcgis','https://www.arcgis.com'),
 ('thailand','https://gisportal.fisheries.go.th/portal'),
 ('malaysia_hydro','https://geohub.hydro.gov.my/portal'),
]
SEED_ITEMS={
 'thailand':['d2308e424dbf4b9ca6fe4758e395c31d'],
 'malaysia_hydro':['7a66e1af30ff4682a4392e2777aba47f'],
 'arcgis':['232ba2e7562a49888e457022ad0edc8a','c03b310793974b93969f8dace5d8859c'],
}


def session():
 s=requests.Session(); s.headers.update({'User-Agent':UA,'Accept':'*/*'})
 retry=requests.adapters.Retry(total=3,connect=3,read=3,backoff_factor=1,status_forcelist=(429,500,502,503,504),allowed_methods=frozenset(['GET','POST']))
 s.mount('https://',requests.adapters.HTTPAdapter(max_retries=retry)); s.mount('http://',requests.adapters.HTTPAdapter(max_retries=retry)); return s

def clean_url(u):
 return html.unescape(u.replace('\\/','/')).rstrip('.,;:)\"\'<>]')

def scan_text(text, base):
 urls=[]; items=[]
 for m in URL_RE.finditer(html.unescape(text).replace('\\/','/')):
  try: urls.append(clean_url(urllib.parse.urljoin(base,m.group(0))))
  except: pass
 items.extend(ITEM_RE.findall(text))
 return urls,items

def fetch(s,u,timeout=90):
 r=s.get(u,timeout=timeout,allow_redirects=True); r.raise_for_status(); return r

def save(out,name,content,suffix):
 p=out/f'{name}{suffix}'; p.write_bytes(content); return str(p.name)

def crawl_page(s,name,url,out):
 rec={'name':name,'requested_url':url,'status':'pending','final_url':'','http_status':None,'content_type':'','bytes':0,'sha256':'','saved_files':[],'urls':[],'vector_candidates':[],'item_ids':[],'errors':[]}
 try:
  r=fetch(s,url); body=r.content
  rec.update(status='ok',final_url=r.url,http_status=r.status_code,content_type=r.headers.get('content-type',''),bytes=len(body),sha256=hashlib.sha256(body).hexdigest())
  rec['saved_files'].append(save(out,name,body,'.html'))
  text=body.decode(r.encoding or 'utf-8','replace')
  soup=BeautifulSoup(text,'html.parser')
  candidates=[]
  for tag,attr in [('script','src'),('link','href'),('a','href'),('form','action'),('iframe','src')]:
   for el in soup.find_all(tag):
    v=el.get(attr)
    if v: candidates.append(urllib.parse.urljoin(r.url,v))
  for el in soup.find_all(True):
   for attr,val in el.attrs.items():
    if attr.lower().startswith('on') or attr.lower() in ('data-url','data-href','value'):
     vals=val if isinstance(val,list) else [val]
     for x in vals:
      u,i=scan_text(str(x),r.url); candidates.extend(u); rec['item_ids'].extend(i)
  u,i=scan_text(text,r.url); candidates.extend(u); rec['item_ids'].extend(i)
  # Fetch same-origin scripts plus obvious app config JSON.
  basehost=urllib.parse.urlparse(r.url).netloc
  script_urls=[]
  for c in candidates:
   p=urllib.parse.urlparse(c)
   if p.netloc==basehost and (p.path.lower().endswith(('.js','.json')) or 'config' in p.path.lower() or 'manifest' in p.path.lower()):
    script_urls.append(c)
  for j,su in enumerate(dict.fromkeys(script_urls[:60])):
   try:
    rr=fetch(s,su,60)
    if len(rr.content)>12_000_000: continue
    txt=rr.content.decode(rr.encoding or 'utf-8','replace')
    rec['saved_files'].append(save(out,f'{name}_asset_{j:02d}',rr.content,Path(urllib.parse.urlparse(su).path).suffix or '.txt'))
    uu,ii=scan_text(txt,su); candidates.extend(uu); rec['item_ids'].extend(ii)
   except Exception as exc: rec['errors'].append(f'asset {su}: {type(exc).__name__}: {exc}')
  rec['urls']=sorted(set(clean_url(x) for x in candidates if x.startswith('http')))[:10000]
  rec['vector_candidates']=[x for x in rec['urls'] if VECTOR_RE.search(x)]
  rec['item_ids']=sorted(set(rec['item_ids']))
 except Exception as exc:
  rec['status']='failed'; rec['errors'].append(f'{type(exc).__name__}: {exc}')
 return rec

def arcgis_recurse(s,label,portal,seeds,out):
 base=portal.rstrip('/'); q=deque((x,0) for x in seeds); seen=set(); records=[]; services=set()
 while q and len(seen)<500:
  item,depth=q.popleft()
  if item in seen or depth>6: continue
  seen.add(item); rec={'item_id':item,'depth':depth,'meta':{},'data_saved':'','services':[],'nested_items':[],'errors':[]}
  try:
   mr=fetch(s,f'{base}/sharing/rest/content/items/{item}?f=json'); meta=mr.json(); rec['meta']=meta
   if meta.get('url'): services.add(clean_url(meta['url'])); rec['services'].append(clean_url(meta['url']))
   dr=fetch(s,f'{base}/sharing/rest/content/items/{item}/data?f=json'); raw=dr.content; rec['data_saved']=save(out,f'arcgis_{label}_{item}',raw,'.json')
   try: data=dr.json()
   except: data={}
   txt=json.dumps(data,ensure_ascii=False)
   urls,ids=scan_text(txt,base); ids=set(ids)
   for u in urls:
    if SERVICE_RE.search(u) or VECTOR_RE.search(u): services.add(u); rec['services'].append(u)
   rec['nested_items']=sorted(ids)
   for x in ids:
    if x not in seen: q.append((x,depth+1))
  except Exception as exc: rec['errors'].append(f'{type(exc).__name__}: {exc}')
  records.append(rec)
 return {'label':label,'portal':base,'items_seen':len(seen),'records':records,'services':sorted(services)}

def japan_probe(s,out):
 keys=['0e83ad5d93214e04abf37c970c32b641','10784fa6ea604de687b2052e55e03879','61b85294618247a6bf652a979c5a5bbc']
 slugs=['submarine-cable-line','submarine-cable-area','submarine-pipeline-line','submarine-pipeline','offshore-structure','common-fishery-right','demarcated-fishery-right','fixed-net-fishery-right','fishery-right-common','fishery-right-demarcated','fishery-right-fixed-net','dumping-area','wreck','submarine-obstruction']
 results=[]
 for slug in slugs:
  rec={'slug':slug,'attempts':[]}
  for ver in ('v3','v2'):
   for layer in range(0,6):
    u=f'https://api.msil.go.jp/{slug}/{ver}/MapServer/{layer}/query'
    ok=False
    for key in keys:
     for placement in ('header','query'):
      params={'f':'geojson','where':'1=1','returnGeometry':'true','resultRecordCount':'1'}
      headers={}
      if placement=='header': headers['Ocp-Apim-Subscription-Key']=key
      else: params['subscription-key']=key
      try:
       r=s.get(u,params=params,headers=headers,timeout=30)
       ct=r.headers.get('content-type',''); sample=r.text[:500]
       att={'url':r.url,'status':r.status_code,'content_type':ct,'placement':placement,'sample':sample}
       rec['attempts'].append(att)
       if r.status_code==200 and ('FeatureCollection' in sample or 'features' in sample):
        ok=True; rec['working']={'base':f'https://api.msil.go.jp/{slug}/{ver}/MapServer/{layer}','placement':placement,'key':key}; break
      except Exception as exc: rec['attempts'].append({'url':u,'error':f'{type(exc).__name__}: {exc}'})
     if ok: break
    if ok: break
   if ok: break
  results.append(rec)
 (out/'japan_probe.json').write_text(json.dumps(results,ensure_ascii=False,indent=2),encoding='utf-8')
 return results

def main():
 ap=argparse.ArgumentParser(); ap.add_argument('--out',type=Path,default=Path('discovery')); a=ap.parse_args(); a.out.mkdir(parents=True,exist_ok=True)
 s=session(); pages=[]
 for name,url in TARGETS:
  print('PAGE',name,url,flush=True); pages.append(crawl_page(s,name,url,a.out))
 arcs=[]
 for label,portal in PORTALS:
  print('ARCGIS',label,portal,flush=True); arcs.append(arcgis_recurse(s,label,portal,SEED_ITEMS.get(label,[]),a.out))
 jp=japan_probe(s,a.out)
 summary={'generated_at':time.time(),'pages':pages,'arcgis':arcs,'japan':jp}
 (a.out/'priority_discovery.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf-8')
 # compact report
 lines=[]
 for r in pages:
  lines.append(f"[{r['status']}] {r['name']}: vectors={len(r['vector_candidates'])} items={len(r['item_ids'])} errors={len(r['errors'])}")
  lines.extend('  '+x for x in r['vector_candidates'][:30])
 for ar in arcs:
  lines.append(f"[arcgis] {ar['label']}: items={ar['items_seen']} services={len(ar['services'])}")
  lines.extend('  '+x for x in ar['services'][:80])
 for r in jp:
  if r.get('working'): lines.append(f"[japan] {r['slug']}: {r['working']}")
 (a.out/'priority_discovery.txt').write_text('\n'.join(lines)+'\n',encoding='utf-8')
 print('\n'.join(lines),flush=True)
 return 0
if __name__=='__main__': raise SystemExit(main())
