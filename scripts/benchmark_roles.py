"""Small reusable role benchmark helpers; no credential discovery or HTTP.

A caller supplies its explicitly authorized model transport. Model IDs and prices
come from a reviewed config; this module never selects alternative models.
"""
from copy import deepcopy
from math import ceil,sqrt
import hashlib,json
from pathlib import Path

ROLES=('query_planner','response_style','critic')

def fingerprint(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()

def validate_config(config):
    if set(config['roles'])!=set(ROLES):raise ValueError('benchmark_roles')
    allowed=config.get('authorized_models',[])
    if not allowed or any(not isinstance(v,str) or not v for v in allowed):raise ValueError('explicit_model_authorization_required')
    if any(model not in allowed for model in config['roles'].values()):raise ValueError('unapproved_model')
    for model in set(config['roles'].values()):
        if model not in config['prices_usd_per_million']:raise ValueError('explicit_model_prices_required')
    return deepcopy(config)


def request_for_role(config,role,body):
    validate_config(config)
    if role not in ROLES:raise ValueError('unknown_role')
    result=deepcopy(body);result['model']=config['roles'][role]
    return result


def percentile(values,p):
    if not values:return None
    if not 0<=p<=1:raise ValueError('percentile')
    values=sorted(values);return values[max(0,ceil(len(values)*p)-1)]


def rate(success,n):
    if not n:return {'n':0,'success':0,'rate':None,'wilson95':None}
    z=1.959963984540054;p=success/n;scale=1+z*z/n
    mid=(p+z*z/(2*n))/scale
    radius=z*sqrt(p*(1-p)/n+z*z/(4*n*n))/scale
    return {'n':n,'success':success,'rate':p,'wilson95':[max(0,mid-radius),min(1,mid+radius)],
            'uncertainty_note':'Descriptive per-attempt interval; repetitions of the same scenario are correlated.'}


def critic_metrics(rows):
    # Expected pass is the frozen human-authored label, not a model judge.
    bad=[r for r in rows if not r['expected_pass']];good=[r for r in rows if r['expected_pass']]
    tp=sum(r.get('verdict_pass') is False for r in bad);fp=sum(r.get('verdict_pass') is False for r in good)
    fn=sum(r.get('verdict_pass') is True for r in bad)
    return {'valid_output':rate(sum(type(r.get('verdict_pass')) is bool for r in rows),len(rows)),
            'detection_recall':rate(tp,len(bad)),'rejection_precision':rate(tp,tp+fp),
            'false_accept':rate(fn,len(bad)),'false_reject':rate(fp,len(good)),
            'safe_gate_valid_admission':rate(sum(r.get('gate_admitted') is True for r in good),len(good)),
            'latency_seconds':{'p50':percentile([r['gate_seconds'] for r in rows],.5),'p95':percentile([r['gate_seconds'] for r in rows],.95)},
            'by_severity':{level:{'n':len(group),'false_accepts':sum(r.get('verdict_pass') is True and not r['expected_pass'] for r in group)}
                           for level in sorted({r['severity'] for r in rows})
                           for group in [[r for r in rows if r['severity']==level]]}}
