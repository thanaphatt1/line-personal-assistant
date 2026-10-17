"""Reusable role runner with injected authorized transport; no HTTP or secrets.

The default budget is two calls. Three is an explicit development-only option.
This runner is separate from, and does not alter, the production app factory.
"""
from copy import deepcopy
from decimal import Decimal
from threading import Lock
from time import monotonic
import json
from benchmark_roles import validate_config

class RoleRunner:
    def __init__(self,config,transport):
        self.config=validate_config(config);self.transport=transport;self.records=[];self.lock=Lock();self.begin_turn('unset')
    def begin_turn(self,turn,*,development_critic=False):
        self.turn=turn;self.limit=3 if development_critic else 2;self.used=0
    def request(self,role,body):
        from benchmark_roles import request_for_role
        body=request_for_role(self.config,role,body)
        if body.get('store') is not False:raise ValueError('benchmark_store')
        price=self.config['prices_usd_per_million'][body['model']]
        bound=len(json.dumps(body,ensure_ascii=False).encode())+4096
        reserve=(Decimal(bound)*Decimal(price['cache_write'])+Decimal(body['max_output_tokens'])*Decimal(price['output']))/1000000
        with self.lock:
            if self.used>=self.limit:raise ValueError('benchmark_turn_budget')
            self.used+=1;record={'role':role,'model':body['model'],'turn':self.turn,'status':'reserved','maximum_estimate_usd':str(reserve)};self.records.append(record)
        started=monotonic()
        try:
            response=self.transport(role,body)
            if response.get('model')!=body['model']:raise ValueError('benchmark_model_mismatch')
            usage=response.get('usage',{});a=usage.get('input_tokens');b=usage.get('output_tokens');detail=usage.get('input_tokens_details') or {};cached=detail.get('cached_tokens',0);written=detail.get('cache_write_tokens',0)
            if any(type(x) is not int or x<0 for x in [a,b,cached,written]) or cached+written>a:raise ValueError('benchmark_usage_unknown')
            cost=(Decimal(a-cached-written)*Decimal(price['input'])+Decimal(cached)*Decimal(price['cached_input'])+Decimal(written)*Decimal(price['cache_write'])+Decimal(b)*Decimal(price['output']))/1000000
            record.update(status='received',input_tokens=a,output_tokens=b,cached_tokens=cached,cache_write_tokens=written,estimated_usage_usd=str(cost))
            return response
        except Exception:
            record['status']='failed_or_usage_unknown';raise
        finally:record['seconds']=monotonic()-started
