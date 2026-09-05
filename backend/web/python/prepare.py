import json, re
import locparse, salary, seniority

def prepare(raw, pref, table):
    locparse.LOC_TABLE = table
    out = []
    for j in raw:
        loc = locparse.parse(j.get('location') or '', j.get('jd') or '', j.get('title') or '')
        eligible, reason = locparse.eligibility(pref, j.get('location') or '', j.get('jd') or '', j.get('title') or '') if pref else (None, '')
        out.append(dict(rm=loc['remote'], co=loc['countries'], rg=loc['regions'], ci=loc['cities'], coe=loc.get('country_est'), sn=seniority.extract(j.get('title') or ''), el=eligible, elr=reason, sal=salary.extract(j.get('jd') or '')))
    clauses = [x.strip() for x in re.split(r'\bor\b|;|\||/', pref, flags=re.I) if x.strip()]
    return json.dumps({'jobs':out, 'remoteOnly':bool(clauses) and all(locparse.parse(c)['remote']=='remote' for c in clauses)})
