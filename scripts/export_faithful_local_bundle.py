"""Export recorded research evidence; read-only SQLite transactions, no service imports."""
import csv, gzip, hashlib, json, re, sqlite3, sys, time
from pathlib import Path
LIVE=Path(r'D:\Kalshi\btc15-signal')
OUT=Path(__file__).resolve().parents[1]/'reports/live_evidence_20261007'
OUT.mkdir(parents=True,exist_ok=True)
sys.path.insert(0,str(LIVE/'src'))
from btc15_signal.config import Settings
settings=Settings(_env_file=LIVE/'.env').model_dump(mode='json')
sensitive=re.compile(r'key|secret|token|password|chat_id|user_id|label|database|path',re.I)
safe={k:v for k,v in settings.items() if not sensitive.search(k)}
(OUT/'settings.sanitized.json').write_text(json.dumps(safe,indent=2),encoding='utf-8')
secrets=[]
for k,v in settings.items():
 if re.search(r'api_key|secret|token|password',k,re.I) and isinstance(v,str) and len(v)>=12:secrets.append(v)
def check(s):
 if any(v in s for v in secrets) or re.search(r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----',s):
  raise RuntimeError('Credential detected: export stopped')
manifest={'created_ms':int(time.time()*1000),'snapshot_semantics':'one read transaction per database; different databases are not globally atomic','files':{},'databases':{}}
supplement='--supplement' in sys.argv
if supplement:
 manifest=json.loads((OUT/'manifest.json').read_text())
tables='strategy_alerts reversion_signals observations predictions allsignal_trades trade_proposals executions shadow_decisions decision_records decision_details settlements fills daily_ledger realised_events capital_days recovery_deficit recovery_adds recovery_add_budget recovery_cycle_wins fund_reservations intelligence_decisions candidate_evaluations input_gaps learning_runs policy_activations policy_withdrawals learning_state settings'.split()
def export(path,label,requested):
 if not path.exists():return
 con=sqlite3.connect('file:'+path.as_posix()+'?mode=ro',uri=True);con.execute('PRAGMA query_only=ON');con.execute('BEGIN')
 available={r[0]:r[1] for r in con.execute("select name,sql from sqlite_master where type='table'")}
 dest=OUT/label;dest.mkdir(exist_ok=True)
 info={'started_ms':int(time.time()*1000),'tables':{}}
 schema={}
 for table in requested:
  if table not in available:continue
  schema[table]=available[table]
  cur=con.execute('SELECT * FROM "'+table+'"');cols=[x[0] for x in cur.description]
  count=0
  with gzip.open(dest/(table+'.csv.gz'),'wt',newline='',encoding='utf-8') as f:
   w=csv.writer(f);w.writerow(cols)
   for row in cur:
    values=[r'\N' if v is None else v.hex() if isinstance(v,bytes) else v for v in row]
    check(str(values));w.writerow(values);count+=1
  info['tables'][table]=count
 con.rollback();con.close();info['finished_ms']=int(time.time()*1000)
 (dest/'schema.json').write_text(json.dumps(schema,indent=2),encoding='utf-8')
 manifest['databases'][label]=info
 print(label,info['tables'].get('observations',0),'observations',flush=True)
for asset in ([] if supplement else ['btc','gold','eth','sol','silver','xrp','near','bnb']):
 export(LIVE/(asset+'15.db'),asset,tables)
if not supplement:
 export(LIVE/'runtime/daily_profit.db','daily_profit',['profit_days'])
 export(LIVE/'runtime/hourly.db','hourly',['hourly_chains','hourly_strikes','hourly_settlements'])
export(LIVE/'runtime/settlement_reference.db','settlement_reference', ['reference_observations','settlement_reconciliation','feed_gaps','brti_features'])
for p in ([] if supplement else sorted((LIVE/'data').glob('market_data*.db'))):
 export(p,'cache_'+p.stem,['markets'])
p=LIVE/'runtime/mirror.jsonl'
if p.exists():
 with p.open(encoding='utf-8') as f,gzip.open(OUT/'mirror.jsonl.gz','wt',encoding='utf-8') as out:
  for line in f:
   try:json.loads(line)
   except ValueError:continue
   check(line);out.write(line)
digest=hashlib.sha256()
for p in sorted((LIVE/'src/btc15_signal').rglob('*.py')):
 digest.update(p.relative_to(LIVE/'src/btc15_signal').as_posix().encode());digest.update(p.read_bytes())
manifest['disk_source_fingerprint']=digest.hexdigest()[:12]
manifest['running_source_fingerprint']='100c3efebc28'
manifest['source_commit']='ec876943f20cdd1888e1580d42a8b8f7baf28d0e'
manifest['env_modified_ms']=int((LIVE/'.env').stat().st_mtime*1000)
manifest['process_started_ms']=1791225028119
for p in OUT.rglob('*'):
 if p.is_file() and p.name!='manifest.json':manifest['files'][p.relative_to(OUT).as_posix()]={'bytes':p.stat().st_size,'sha256':hashlib.sha256(p.read_bytes()).hexdigest()}
(OUT/'manifest.json').write_text(json.dumps(manifest,indent=2),encoding='utf-8')
print('Total compressed MB',round(sum(x['bytes'] for x in manifest['files'].values())/1e6,2))
