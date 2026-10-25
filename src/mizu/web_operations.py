"""Request, replay, pagination, and response shaping for web tools."""

from __future__ import annotations

from .errors import Denied
from .fs import PREVIEW_BYTES, page, text_preview, DIGEST


def fetch(context, args: dict) -> dict:
    if "receipt_id" in args:
        if "url" in args:
            raise Denied("Supply receipt_id or url, not both")
        record = context.web.replay(args["receipt_id"], "fetch")
    else:
        if "url" not in args:
            raise Denied("Fetch requires url or receipt_id")
        record = context._bounded_web_operation(context.config, context.run_dir, "fetch", args["url"],
                                            context.tools_cancelled, context.role.name)
    text = record.get('text','')
    offset,limit = args.get('offset',0),args.get('limit',8192)
    end = min(len(text),offset+limit)
    return {**record,'text':text[offset:end],'offset':offset,
            'next_offset':end if end<len(text) else None,'truncated':end<len(text)}


def probe(context, args: dict) -> dict:
    if "receipt_id" in args:
        if any(key in args for key in ("url", "method", "headers", "body")):
            raise Denied("Supply receipt_id or a new probe request, not both")
        record = context.web.replay(args["receipt_id"], "probe")
    else:
        if "url" not in args or "method" not in args:
            raise Denied("Probe requires url and method, or receipt_id")
        payload = {"url": args["url"], "method": args["method"],
                   "headers": args.get("headers", []), "body": args.get("body", "")}
        record = context._bounded_web_operation(context.config, context.run_dir, "probe", payload,
                                            context.tools_cancelled, context.role.name)
    text = record.get('text','')
    offset,limit = args.get('offset',0),args.get('limit',8192)
    end = min(len(text),offset+limit)
    return {**record,'text':text[offset:end],'offset':offset,
            'next_offset':end if end<len(text) else None,'truncated':end<len(text)}


def search(context, args: dict) -> dict:
    if "result_id" in args:
        if "query" in args:
            raise Denied("Supply result_id or query, not both")
        record = context.web.replay(args["result_id"], "search")
    else:
        if "query" not in args:
            raise Denied("Search requires query or result_id")
        record = context._bounded_web_operation(context.config, context.run_dir, "search", args["query"],
                                            context.tools_cancelled, context.role.name)
        record = context.web.retain("search", record)
    evidence_id = record["result_id"]
    rows=[]
    for item in record.get('results',[]):
        row={}
        for key,bound in (('title',300),('url',4096),('summary',1500),('source',4096)):
            row[key],row[key+'_truncated']=text_preview(item.get(key,''),bound)
        receipt=item.get('source_receipt')
        if isinstance(receipt,str) and DIGEST.fullmatch(receipt):row['source_receipt']=receipt
        score=item.get('score')
        if type(score) in (int,float) and -2**63 <= score <= 2**63:row['score']=score
        rows.append(row)
    response=page(rows,args.get('offset',0),args.get('limit',1000),maximum=PREVIEW_BYTES*2)
    response['results']=response.pop('items')
    response['result_id'] = evidence_id
    response['evidence']='sources/search-'+evidence_id+'.json'
    for key in ('scope','trust','feeds_total','feeds_consulted'):
        if key in record:response[key]=record[key]
    response['source_truncated']=record.get('truncated',False)
    return response
