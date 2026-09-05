import json, re
import locparse, salary, seniority

def prepare(raw, pref, table):
    locparse.LOC_TABLE = table
    out = []
    for j in raw:
        loc = locparse.parse(j.get('location') or '', j.get('jd') or '', j.get('title') or '')
        eligible, reason = locparse.eligibility(pref, j.get('location') or '', j.get('jd') or '', j.get('title') or '') if pref else (None, '')
        out.append(dict(k=f"{j['ats']}/{j['slug']}#{j['id']}", t=j.get('title') or '', c=j.get('company') or '', l=j.get('location') or '', u=j.get('url') or '', s=j.get('first_seen') or j.get('seen'), p=j.get('pub'), jd=j.get('jd') or '', g=j['leaf'], g3=j['leaf'], v=j['v'], sim=j['sim'], rm=loc['remote'], co=loc['countries'], rg=loc['regions'], ci=loc['cities'], coe=loc.get('country_est'), sn=seniority.extract(j.get('title') or ''), el=eligible, elr=reason, sal=salary.extract(j.get('jd') or ''), e=None, co_=None))
    clauses = [x.strip() for x in re.split(r'\bor\b|;|\||/', pref, flags=re.I) if x.strip()]
    return json.dumps({'jobs':out, 'remoteOnly':bool(clauses) and all(locparse.parse(c)['remote']=='remote' for c in clauses)})
